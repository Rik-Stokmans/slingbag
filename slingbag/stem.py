"""Stem van de hulpknop: zet antwoorden zin voor zin om naar spraak en speelt ze direct af.

Twee bronnen:
* online: een natuurlijke stem via OpenRouter (verwerkt zonder opslag, zie openrouter.py);
* lokaal: Piper op de Pi zelf (helemaal offline, wat robotischer).
Valt de online stem weg, dan schakelt de slingbag automatisch over op Piper.

Om snel te reageren wordt de eerste zin al uitgesproken terwijl het model nog verder schrijft,
en wordt de volgende zin omgezet terwijl de vorige speelt. Gesproken antwoorden staan alleen
tijdelijk in het werkgeheugen van de Pi (/dev/shm) en worden na het gesprek gewist.

Ook de herkenbare geluidjes worden hier gemaakt: verbonden (ping), gehoord, nadenken en afsluiten.
"""

from __future__ import annotations

import hashlib
import logging
import math
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from .openrouter import AIUnavailable, pcm_to_wav
from .speech import SpeechUnavailable

log = logging.getLogger(__name__)

SOUND_RATE = 22050
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


# --- geluidjes ---------------------------------------------------------------------------

def _tone(freq: float, length: float, volume: float, decay: float = 0.0, attack: float = 0.01,
          harmonics: tuple = ((1, 1.0),)) -> list[float]:
    n = int(SOUND_RATE * length)
    out = []
    for i in range(n):
        t = i / SOUND_RATE
        env = min(1.0, t / attack) if attack else 1.0
        env *= math.exp(-decay * t) if decay else math.sin(math.pi * i / n)
        out.append(volume * env * sum(a * math.sin(2 * math.pi * freq * h * t) for h, a in harmonics))
    return out


def _write(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SOUND_RATE)
        handle.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))


def _mix(*parts: tuple[float, list[float]]) -> list[float]:
    """Legt tonen over elkaar met een starttijd in seconden."""
    length = max(int(start * SOUND_RATE) + len(s) for start, s in parts)
    out = [0.0] * length
    for start, samples in parts:
        offset = int(start * SOUND_RATE)
        for i, value in enumerate(samples):
            out[offset + i] += value
    return out


BELL = ((1, 1.0), (2, 0.35), (3, 0.12))


def write_sounds(directory: Path) -> dict[str, Path]:
    """Maakt de geluidjes één keer aan; geen externe geluidsbestanden nodig."""
    sounds = {
        # Helder, opwaarts 'ping-ping': je bent verbonden.
        "verbonden": lambda: _mix((0.0, _tone(784, 0.5, 0.30, decay=7, harmonics=BELL)),
                                  (0.13, _tone(1175, 0.7, 0.30, decay=6, harmonics=BELL))),
        # Kort, zacht en laag: ik heb je gehoord.
        "gehoord": lambda: _tone(523, 0.18, 0.22, decay=14, harmonics=BELL),
        # Zacht, rustig 'hmm' van twee tonen: ik denk na.
        "nadenken": lambda: _mix((0.0, _tone(440, 0.35, 0.10)), (0.3, _tone(494, 0.45, 0.10))),
        # Neerwaarts: het gesprek is afgesloten.
        "afsluiten": lambda: _mix((0.0, _tone(1047, 0.5, 0.25, decay=7, harmonics=BELL)),
                                  (0.15, _tone(698, 0.8, 0.25, decay=5, harmonics=BELL))),
    }
    paths = {}
    for name, make in sounds.items():
        path = Path(directory) / f"{name}.wav"
        if not path.exists():
            _write(path, make())
        paths[name] = path
    return paths


# --- tekst opknippen voor spraak ------------------------------------------------------------

def clean_for_speech(text: str) -> str:
    text = _EMOJI.sub("", text)
    text = re.sub(r"[*_#`>|]+", "", text)
    text = re.sub(r"^\s*([-•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


class SentenceSplitter:
    """Knipt gestreamde tekst op in zinnen, zodat de eerste zin direct kan worden uitgesproken."""

    _END = re.compile(r"(.+?[.!?…:;])(\s+|$)|(.+?)\n+", re.DOTALL)

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.buffer = ""

    def feed(self, text: str) -> None:
        self.buffer += text
        while True:
            match = self._END.match(self.buffer)
            # Wacht op de spatie na het leesteken: "om 10.30 uur" mag niet worden opgeknipt.
            if not match or (match.group(1) and not match.group(2)):
                return
            sentence = match.group(1) or match.group(3)
            self.buffer = self.buffer[match.end():]
            sentence = clean_for_speech(sentence)
            if sentence:
                self.emit(sentence)

    def flush(self) -> None:
        rest = clean_for_speech(self.buffer)
        self.buffer = ""
        if rest:
            self.emit(rest)


# --- stem ---------------------------------------------------------------------------------------

def _temp_dir() -> Path:
    # Op de Pi staat /dev/shm in het werkgeheugen: gesproken antwoorden komen niet op de microSD-kaart.
    base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix="slingbag-hulp-", dir=base))


class Voice:
    def __init__(self, client, speech_library, audio, settings: Callable[[], dict],
                 local_voice: Callable[[], str], cache_dir: Path):
        self.client = client
        self.speech = speech_library
        self.audio = audio
        self.settings = settings
        self.local_voice = local_voice
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sounds = write_sounds(self.cache_dir)
        self.tmp = _temp_dir()
        self.online_failed = False  # na een fout de rest van het gesprek lokaal

    def reset(self) -> None:
        self.online_failed = False
        for path in self.tmp.glob("*.wav"):
            path.unlink(missing_ok=True)

    def mode(self) -> str:
        if self.settings().get("stembron") == "online" and self.client.available() and not self.online_failed:
            return "online"
        return "lokaal"

    def synthesize(self, text: str, cache: bool = False) -> Path:
        """Geeft een WAV-bestand. Vaste zinnen (``cache``) worden bewaard voor direct afspelen."""
        mode = self.mode()
        settings = self.settings()
        key = f"{mode}|{settings.get('online_stem')}|{settings.get('spreeksnelheid')}|{self.local_voice()}|{text}"
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".wav"
        target = (self.cache_dir / "zinnen" if cache else self.tmp) / name
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "online":
            try:
                began = time.monotonic()
                pcm = self.client.speech(text)
                log.info("Online stem: %.2f s voor %d tekens", time.monotonic() - began, len(text))
                if len(pcm) < 1000:
                    raise AIUnavailable("Lege spraak ontvangen.")
                part = target.with_suffix(".part")
                pcm_to_wav(pcm, part)
                part.replace(target)
                return target
            except AIUnavailable as exc:
                log.warning("Online stem niet beschikbaar, val terug op Piper: %s", exc)
                self.online_failed = True
        # Lokaal: Piper. Vaste zinnen via de bestaande spraakcache, losse zinnen in het werkgeheugen.
        if cache:
            return self.speech.resolve(text, self.local_voice())
        self.speech.engine.synthesize(text, self.local_voice(), target)
        return target

    def play(self, paths: list[Path], interrupted: Callable[[], bool] | None = None) -> bool:
        """Speelt af en wacht tot het klaar is. Geeft False als het werd onderbroken."""
        if not paths:
            return True
        done = threading.Event()
        token = object()
        self.audio.play(paths, token, lambda _t: done.set())
        idle = 0.0
        while not done.wait(0.05):
            if interrupted and interrupted():
                self.audio.stop()
                return False
            # Afspelen gestopt zonder klaar-melding (audio.stop() van buitenaf): niet blijven wachten.
            idle = 0.0 if self.audio.playing else idle + 0.05
            if idle > 2.0:
                return False
        return True

    def say(self, text: str, cache: bool = False, interrupted=None, sound: str | None = None) -> list[Path]:
        paths = [self.sounds[sound]] if sound else []
        try:
            paths.append(self.synthesize(text, cache=cache))
        except (SpeechUnavailable, OSError) as exc:
            log.error("Kan niet spreken: %s", exc)
        self.play(paths, interrupted)
        return paths[1:] if sound else paths

    def sound(self, name: str, interrupted=None) -> None:
        self.play([self.sounds[name]], interrupted)

    def utterance(self, interrupted: Callable[[], bool], thinking_after_s: float = 1.8) -> "Utterance":
        return Utterance(self, interrupted, thinking_after_s)

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class Utterance:
    """Eén antwoord: zinnen komen binnen, worden omgezet en in volgorde afgespeeld.

    Zinnen die tegelijk klaarstaan worden samen omgezet: minder verzoeken en een natuurlijkere zinsmelodie.
    """

    def __init__(self, voice: Voice, interrupted: Callable[[], bool], thinking_after_s: float):
        self.voice = voice
        self.interrupted = interrupted
        self.thinking_after_s = thinking_after_s
        self.texts: queue.Queue = queue.Queue()
        self.audio: queue.Queue = queue.Queue()
        self.paths: list[Path] = []
        self.spoken: list[str] = []
        self.started_speaking = threading.Event()
        self._synth = threading.Thread(target=self._synth_loop, name="stem-omzetten", daemon=True)
        self._player = threading.Thread(target=self._play_loop, name="stem-afspelen", daemon=True)
        self._synth.start()
        self._player.start()

    def add(self, sentence: str) -> None:
        self.texts.put(sentence)

    def finish(self) -> None:
        self.texts.put(None)

    def _synth_loop(self) -> None:
        while True:
            item = self.texts.get()
            if item is None:
                break
            batch = [item]
            # Neem alles mee wat al klaarstaat, maar houd de eerste zin kort voor een snelle start.
            while self.paths and not self.texts.empty():
                nxt = self.texts.get()
                if nxt is None:
                    self.texts.put(None)
                    break
                batch.append(nxt)
            if self.interrupted():
                continue
            text = " ".join(batch)
            try:
                path = self.voice.synthesize(text)
            except (SpeechUnavailable, OSError) as exc:
                log.error("Zin kon niet worden omgezet: %s", exc)
                continue
            self.paths.append(path)
            self.audio.put((path, text))
        self.audio.put(None)

    def _play_loop(self) -> None:
        waited = 0.0
        thinking_played = False
        while True:
            try:
                item = self.audio.get(timeout=0.1)
            except queue.Empty:
                waited += 0.1
                if (not thinking_played and not self.started_speaking.is_set()
                        and waited >= self.thinking_after_s and not self.interrupted()):
                    thinking_played = True
                    self.voice.sound("nadenken", self.interrupted)
                continue
            if item is None:
                return
            if self.interrupted():
                continue
            path, text = item
            self.started_speaking.set()
            if self.voice.play([path], self.interrupted):
                self.spoken.append(text)

    def wait(self, timeout: float = 120) -> None:
        self._synth.join(timeout)
        self._player.join(timeout)
