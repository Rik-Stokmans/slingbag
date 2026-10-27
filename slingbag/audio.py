"""Audiospeler: speelt WAV-bestanden af via de 3,5mm-uitgang en kan altijd worden onderbroken (FR-06, TR-03)."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class AudioPlayer:
    """Speelt een reeks bestanden achter elkaar af in een eigen thread.

    Na natuurlijk afronden roept de speler ``on_done(token)`` aan, zodat de
    interactielogica pas daarna de antwoordwachttijd laat ingaan.
    """

    def __init__(self, device: str = "plughw:CARD=Headphones,DEV=0",
                 mixer_card: str = "Headphones", mixer_control: str = "PCM"):
        self.device = device
        self.mixer_card = mixer_card
        self.mixer_control = mixer_control
        self.is_mac = platform.system() == "Darwin"
        self.volume = 70
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._generation = 0

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        if self.is_mac or shutil.which("amixer") is None:
            return  # op de Mac regelt afplay het volume per bestand
        result = subprocess.run(
            ["amixer", "-q", "-M", "-c", self.mixer_card, "sset", self.mixer_control, f"{self.volume}%"],
            capture_output=True,
        )
        if result.returncode != 0:
            log.warning("Volume instellen mislukt: %s", result.stderr.decode(errors="replace").strip())

    def _command(self, path: Path) -> list[str]:
        if self.is_mac:
            return ["afplay", "-v", f"{self.volume / 100:.2f}", str(path)]
        return ["aplay", "-q", "-D", self.device, str(path)]

    def play(self, paths: list[Path], token: object = None,
             on_done: Callable[[object], None] | None = None) -> None:
        """Stopt wat er speelt en begint direct met de nieuwe reeks."""
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation

        def run():
            for path in paths:
                with self._lock:
                    if generation != self._generation:
                        return  # onderbroken door stop() of een nieuwe play()
                    try:
                        self._proc = subprocess.Popen(self._command(path),
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    except OSError as exc:
                        log.error("Afspelen niet mogelijk: %s", exc)
                        self._proc = None
                        break
                    proc = self._proc
                _, err = proc.communicate()
                if proc.returncode not in (0, -15) and err:
                    log.warning("Afspeelfout bij %s: %s", path.name, err.decode(errors="replace").strip())
            with self._lock:
                if generation != self._generation:
                    return
                self._proc = None
            if on_done:
                on_done(token)

        threading.Thread(target=run, name="audio", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
