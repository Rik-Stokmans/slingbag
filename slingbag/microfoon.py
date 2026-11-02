"""Microfoon: neemt één spreekbeurt op en herkent zelf wanneer de gebruiker klaar is met praten.

De opname blijft in het geheugen (nooit op de microSD-kaart) en gaat alleen als WAV naar de
spraakherkenning. Tijdens het spreken van de slingbag staat de microfoon uit, zodat hij
zichzelf niet hoort (half-duplex, nodig zonder echo-onderdrukking op de Pi 3B).

Eindedetectie op basis van energie: eerst wordt het achtergrondgeluid gemeten, daarna telt
geluid ruim daarboven als spraak. Na een instelbare stilte (mensen met dementie pauzeren vaak
langer midden in een zin) is de beurt klaar.
"""

from __future__ import annotations

import io
import logging
import math
import platform
import re
import shutil
import subprocess
import time
import wave
from array import array
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)

RATE = 16000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2


@dataclass
class Turn:
    """Resultaat van één keer luisteren."""
    status: str            # "spraak", "stilte", "afgebroken" of "fout"
    wav: bytes = b""
    seconds: float = 0.0
    reason: str = ""       # bij afgebroken: de reden die ``control`` gaf


def level_db(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    if not samples:
        return -100.0
    energy = sum(s * s for s in samples) / len(samples)
    return 10 * math.log10(energy / (32768.0 ** 2) + 1e-12)


def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buf.getvalue()


class Endpointer:
    """Bepaalt per frame van 30 ms of de spreekbeurt begonnen of afgelopen is."""

    def __init__(self, pause_s: float = 1.6, no_speech_s: float = 10.0, max_s: float = 45.0,
                 min_margin_db: float = 12.0, floor_db: float = -50.0):
        self.pause_frames = int(pause_s * 1000 / FRAME_MS)
        self.no_speech_frames = int(no_speech_s * 1000 / FRAME_MS)
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.calibration_frames = 8  # eerste 240 ms: achtergrondgeluid meten
        self.min_margin_db = min_margin_db
        self.floor_db = floor_db
        self.noise_db: float | None = None
        self.frames = 0
        self.loud_run = 0
        self.quiet_run = 0
        self.speech_frames = 0
        self.started = False
        self._calibration: list[float] = []

    @property
    def threshold(self) -> float:
        noise = self.noise_db if self.noise_db is not None else -60.0
        return max(noise + self.min_margin_db, self.floor_db)

    def feed(self, db: float) -> str | None:
        """Geeft ``"klaar"``, ``"stilte"``, ``"te_lang"`` of None (doorgaan)."""
        self.frames += 1
        if self.noise_db is None:
            self._calibration.append(db)
            if len(self._calibration) >= self.calibration_frames:
                self.noise_db = sorted(self._calibration)[len(self._calibration) // 2]
            return None
        loud = db > self.threshold
        if loud:
            self.loud_run += 1
            self.quiet_run = 0
            if self.started:
                self.speech_frames += 1
            elif self.loud_run >= 4:  # 120 ms aaneengesloten geluid: spraak
                self.started = True
                self.speech_frames = self.loud_run
        else:
            self.loud_run = 0
            self.quiet_run += 1
            if not self.started:
                # Achtergrondgeluid langzaam bijhouden zolang er niet gesproken wordt.
                self.noise_db = 0.95 * self.noise_db + 0.05 * db
        if self.started and self.quiet_run >= self.pause_frames:
            return "klaar"
        if not self.started and self.frames >= self.no_speech_frames:
            return "stilte"
        if self.frames >= self.max_frames:
            return "te_lang"
        return None


def detect_device() -> str | None:
    """Zoekt de eerste geluidskaart met een opname-ingang (op de Pi 3B is dat de USB-microfoon)."""
    if shutil.which("arecord") is None:
        return None
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"^card \d+: (\S+) \[.*?\], device (\d+):", out, re.MULTILINE)
    return f"plughw:CARD={match.group(1)},DEV={match.group(2)}" if match else None


class Microphone:
    def __init__(self, device: str = "auto"):
        self.device = device
        self.is_mac = platform.system() == "Darwin"
        self._proc: subprocess.Popen | None = None
        self._cache: tuple[float, str | None] = (0.0, None)

    def resolved_device(self) -> str | None:
        if self.device == "geen":
            return None
        if self.is_mac:
            return ":0" if shutil.which("ffmpeg") else None
        if self.device != "auto":
            return self.device
        # De app vraagt elke anderhalve seconde de status op: niet steeds opnieuw zoeken.
        checked, device = self._cache
        if time.monotonic() - checked > 30 or device is None and time.monotonic() - checked > 5:
            device = detect_device()
            self._cache = (time.monotonic(), device)
        return device

    def available(self) -> bool:
        return self.resolved_device() is not None

    def _command(self, device: str) -> list[str]:
        if self.is_mac:
            return ["ffmpeg", "-loglevel", "quiet", "-f", "avfoundation", "-i", device,
                    "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        return ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]

    def frames(self) -> Iterable[bytes]:
        device = self.resolved_device()
        if device is None:
            raise OSError("Geen microfoon gevonden.")
        proc = subprocess.Popen(self._command(device), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._proc = proc
        try:
            while True:
                frame = proc.stdout.read(FRAME_BYTES)
                if not frame or len(frame) < FRAME_BYTES:
                    if proc.poll() is not None:
                        raise OSError("De microfoon stopte onverwacht.")
                    continue
                yield frame
        finally:
            self._proc = None
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def listen(self, endpointer: Endpointer, control: Callable[[], str | None],
               on_speech_start: Callable[[], None] | None = None,
               frames: Iterable[bytes] | None = None) -> Turn:
        """Neemt op tot de beurt klaar is. ``control`` kan per frame 'klaar' of een afbreekreden geven."""
        pre_roll: list[bytes] = []
        recorded: list[bytes] = []
        try:
            for frame in frames if frames is not None else self.frames():
                command = control()
                if command == "klaar":
                    if endpointer.started:
                        break
                    return Turn("stilte")
                if command:
                    return Turn("afgebroken", reason=command)
                was_started = endpointer.started
                result = endpointer.feed(level_db(frame))
                if endpointer.started:
                    if not was_started:
                        recorded.extend(pre_roll)  # het begin van het eerste woord niet missen
                        if on_speech_start:
                            on_speech_start()
                    recorded.append(frame)
                else:
                    pre_roll = (pre_roll + [frame])[-10:]
                if result == "stilte":
                    return Turn("stilte")
                if result in ("klaar", "te_lang"):
                    break
        except OSError as exc:
            log.warning("Microfoon: %s", exc)
            return Turn("fout", reason=str(exc))
        if not endpointer.started or endpointer.speech_frames < 8:  # korter dan 240 ms: geen zin
            return Turn("stilte")
        trailing = endpointer.quiet_run - 10  # stilte aan het eind inkorten tot 300 ms
        if trailing > 0:
            recorded = recorded[:-trailing]
        pcm = b"".join(recorded)
        return Turn("spraak", to_wav(pcm), len(pcm) / (RATE * 2))
