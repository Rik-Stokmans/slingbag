"""Spraakbibliotheek: zet tekst om naar spraak (Piper) en beheert eigen opnames (FR-01, FR-07).

Alle audio wordt als WAV-bestand op de microSD-kaart bewaard. Spraak wordt vooraf
gegenereerd en hergebruikt, zodat de Pi 3B tijdens een sessie direct kan afspelen.
"""

from __future__ import annotations

import hashlib
import logging
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
from pathlib import Path

log = logging.getLogger(__name__)


class SpeechUnavailable(RuntimeError):
    """Er is geen bruikbaar audiobestand voor een tekst of opname."""


class PiperEngine:
    """Roept Piper aan als los programma: het uitvoerbare bestand op de Pi, de Python-module op de Mac."""

    def __init__(self, voices_dir: Path, binary: Path | None = None):
        self.voices_dir = Path(voices_dir)
        self.binary = Path(binary) if binary else None

    def command(self) -> list[str] | None:
        if self.binary and self.binary.exists():
            return [str(self.binary)]
        try:
            import piper  # noqa: F401  (alleen aanwezig in de ontwikkelomgeving)
            return [sys.executable, "-m", "piper"]
        except ImportError:
            return None

    def available(self) -> bool:
        return self.command() is not None and bool(self.voices())

    def voices(self) -> list[str]:
        return sorted(p.name[: -len(".onnx")] for p in self.voices_dir.glob("*.onnx"))

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        cmd = self.command()
        model = self.voices_dir / f"{voice}.onnx"
        if cmd is None or not model.exists():
            raise SpeechUnavailable(f"Spraakengine of stem '{voice}' is niet geïnstalleerd.")
        tmp = out_path.with_suffix(".part")
        try:
            result = subprocess.run(
                [*cmd, "--model", str(model), "--output_file", str(tmp)],
                input=text.replace("\n", " ").encode("utf-8"),
                capture_output=True,
                timeout=180,
            )
        except OSError as exc:
            raise SpeechUnavailable(f"Piper kon niet worden gestart ({exc.strerror}).") from exc
        if result.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise SpeechUnavailable(f"Piper gaf een fout: {result.stderr.decode(errors='replace')[-300:]}")
        tmp.replace(out_path)


class SpeechLibrary:
    def __init__(self, data_dir: Path, engine: PiperEngine):
        self.engine = engine
        self.cache_dir = Path(data_dir) / "cache" / "spraak"
        self.recordings_dir = Path(data_dir) / "opnames"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.chime_path = Path(data_dir) / "cache" / "aandacht.wav"
        if not self.chime_path.exists():
            write_chime(self.chime_path)
        self._synth_lock = threading.Lock()
        self._progress = {"bezig": False, "klaar": 0, "totaal": 0, "fout": None}
        self._progress_lock = threading.Lock()
        self._generation = 0

    # --- opzoeken en genereren -------------------------------------------------

    def cache_path(self, text: str, voice: str) -> Path:
        digest = hashlib.sha1(f"{voice}\n{text.strip()}".encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.wav"

    def recording_path(self, recording_id: str) -> Path:
        return self.recordings_dir / f"{recording_id}.wav"

    def resolve(self, item: dict | str, voice: str, generate: bool = True) -> Path:
        """Geeft het WAV-bestand voor een tekst of opname; een eigen opname gaat voor."""
        if isinstance(item, str):
            item = {"tekst": item, "opname": None}
        if item.get("opname"):
            path = self.recording_path(item["opname"])
            if path.exists():
                return path
            if not (item.get("tekst") or "").strip():
                raise SpeechUnavailable("Een opname ontbreekt op de slingbag.")
        text = (item.get("tekst") or "").strip()
        if not text:
            raise SpeechUnavailable("Er is geen tekst of opname ingesteld.")
        path = self.cache_path(text, voice)
        if path.exists():
            return path
        if not generate:
            raise SpeechUnavailable(f"Spraak voor '{text[:40]}' is nog niet voorbereid.")
        with self._synth_lock:
            if not path.exists():
                self.engine.synthesize(text, voice, path)
        return path

    def is_ready(self, item: dict | str, voice: str) -> bool:
        try:
            self.resolve(item, voice, generate=False)
            return True
        except SpeechUnavailable:
            return False

    def prepare_async(self, items: list, voice: str) -> None:
        """Genereert ontbrekende spraak op de achtergrond na het opslaan van instellingen."""
        todo = [i for i in items if not self.is_ready(i, voice)]
        with self._progress_lock:
            # Een nieuwe ronde vervangt een lopende ronde; die stopt bij het volgende item.
            self._generation += 1
            generation = self._generation
            self._progress = {"bezig": bool(todo), "klaar": 0, "totaal": len(todo), "fout": None}
        if not todo:
            return

        def work():
            for item in todo:
                with self._progress_lock:
                    if generation != self._generation:
                        return
                try:
                    self.resolve(item, voice)
                except (SpeechUnavailable, subprocess.TimeoutExpired, OSError) as exc:
                    log.warning("Voorbereiden mislukt: %s", exc)
                    with self._progress_lock:
                        if generation == self._generation:
                            self._progress["fout"] = str(exc)
                with self._progress_lock:
                    if generation == self._generation:
                        self._progress["klaar"] += 1
            with self._progress_lock:
                if generation == self._generation:
                    self._progress["bezig"] = False

        threading.Thread(target=work, name="spraak-voorbereiden", daemon=True).start()

    def progress(self) -> dict:
        with self._progress_lock:
            return dict(self._progress)

    def cleanup(self, keep: set[Path]) -> int:
        """Verwijdert gegenereerde spraak die bij geen enkele tekst meer hoort."""
        removed = 0
        for path in self.cache_dir.glob("*.wav"):
            if path not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # --- eigen opnames ----------------------------------------------------------

    def import_recording(self, source: Path) -> dict:
        """Zet een opname van de telefoon (m4a, webm, mp3, wav …) om naar mono WAV."""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SpeechUnavailable("ffmpeg is niet geïnstalleerd; opnames kunnen niet worden omgezet.")
        recording_id = uuid.uuid4().hex[:12]
        target = self.recording_path(recording_id)
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
             "-ac", "1", "-ar", "22050", "-sample_fmt", "s16", str(target)],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0 or not target.exists():
            target.unlink(missing_ok=True)
            raise SpeechUnavailable("Dit audiobestand kon niet worden omgezet.")
        return {"id": recording_id, "duur_s": round(wav_duration(target), 1)}

    def delete_recording(self, recording_id: str) -> None:
        self.recording_path(recording_id).unlink(missing_ok=True)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def write_chime(path: Path, rate: int = 22050) -> None:
    """Maakt een zacht tweetonig aandachtsgeluid, zodat er geen extern geluidsbestand nodig is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for freq, length in ((660.0, 0.35), (880.0, 0.55)):
        n = int(rate * length)
        for i in range(n):
            envelope = math.sin(math.pi * i / n)  # zacht in- en uitfaden
            sample = 0.35 * envelope * math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(sample * 32767))
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".wav") as tmp:
        with wave.open(tmp, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
    Path(tmp.name).replace(path)
