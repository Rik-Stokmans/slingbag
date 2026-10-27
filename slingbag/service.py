"""Slingbag-service: verbindt alle componenten en verwerkt gebeurtenissen één voor één.

Knoppen, audio, planner en app plaatsen hun gebeurtenissen in één wachtrij. Een
enkele thread voert ze uit op de interactiecontroller, zodat twee gelijktijdige
gebeurtenissen nooit tegelijk de toestand veranderen.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from . import __version__
from .audio import AudioPlayer
from .config import ConfigStore
from .hardware import create_panel
from .interaction import InteractionController, SystemClock, session_items
from .logbook import Logbook
from .scheduler import Planner
from .speech import PiperEngine, SpeechLibrary

log = logging.getLogger(__name__)
TICK_S = 0.25


class SlingbagService:
    def __init__(self, data_dir: Path, voices_dir: Path, piper_binary: Path | None = None,
                 simulate: bool = False, audio_device: str = "plughw:CARD=Headphones,DEV=0",
                 clock=None):
        self.data_dir = Path(data_dir)
        self.clock = clock or SystemClock()
        self.events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

        self.config_store = ConfigStore(self.data_dir / "config.json")
        self.logbook = Logbook(self.data_dir / "logboek.csv")
        self.speech = SpeechLibrary(self.data_dir, PiperEngine(voices_dir, piper_binary))
        self.audio = AudioPlayer(device=audio_device)
        self.panel = create_panel(on_press=self.press, on_shutdown=self.shutdown, simulate=simulate)
        self.planner = Planner()
        self.controller = InteractionController(
            self.config_store, self.speech, self.audio, self.panel, self.logbook,
            clock=self.clock,
            notify_audio_done=lambda token: self.post(lambda: self.controller.audio_finished(token)),
        )
        self._thread = threading.Thread(target=self._run, name="gebeurtenislus", daemon=True)

    # --- levenscyclus ------------------------------------------------------------

    def start(self) -> None:
        self.audio.set_volume(self.config_store.get()["volume"])
        self.prepare_speech()
        self.logbook.write(self.clock.now(), "", "opgestart", "rust", detail=f"versie {__version__}")
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        self.audio.stop()
        self.panel.close()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self.events.get(timeout=TICK_S)
            except queue.Empty:
                job = None
            with self._lock:
                if job is not None:
                    job()
                self.controller.tick()
                for entry in self.planner.due(self.config_store.get()["planning"], self.clock.now()):
                    self.controller.invite(entry["activiteit"], "planning")

    # --- gebeurtenissen ----------------------------------------------------------

    def post(self, job) -> None:
        self.events.put(job)

    def call(self, fn, timeout: float = 60):
        """Voert ``fn`` uit in de gebeurtenislus en wacht op het resultaat (voor de web-API)."""
        future: Future = Future()

        def job():
            try:
                future.set_result(fn())
            except Exception as exc:  # doorgeven aan de aanroeper
                future.set_exception(exc)

        self.post(job)
        return future.result(timeout=timeout)

    def press(self, button: str) -> None:
        self.post(lambda: self.controller.press(button))

    def prepare_speech(self) -> None:
        """Genereert vooraf alle spraak en ruimt verouderde spraakbestanden op."""
        config = self.config_store.get()
        voice = config["stem"]
        items = []
        for activity in config["activiteiten"]:
            items.extend(session_items(config, activity))
        keep = set()
        for item in items:
            text = item if isinstance(item, str) else (item.get("tekst") or "")
            if text.strip():
                keep.add(self.speech.cache_path(text, voice))
        self.speech.cleanup(keep)
        self.speech.prepare_async(items, voice)

    def config_saved(self) -> None:
        self.audio.set_volume(self.config_store.get()["volume"])
        self.prepare_speech()
        self.post(self.controller.clear_error)

    def shutdown(self) -> None:
        self.logbook.write(self.clock.now(), "", "afsluiten", self.controller.state)
        self.audio.stop()
        self.panel.set_light("uit")
        if self.panel.simulated:
            log.info("Afsluiten gevraagd (simulatie: genegeerd)")
            return
        subprocess.Popen(["sudo", "-n", "/usr/bin/systemctl", "poweroff"])

    def set_clock(self, epoch_s: float) -> bool:
        """Zet de systeemklok gelijk met de telefoon als die meer dan een minuut afwijkt (TR-06).

        De Pi 3B heeft geen klokbatterij; zonder internet kan de tijd na het opstarten afwijken.
        """
        if abs(time.time() - epoch_s) < 60:
            return False
        if self.panel.simulated:
            log.info("Klok gelijkzetten gevraagd (simulatie: genegeerd)")
            return False
        result = subprocess.run(["sudo", "-n", "/usr/bin/date", "-s", f"@{int(epoch_s)}"], capture_output=True)
        if result.returncode == 0:
            self.logbook.write(self.clock.now(), "", "klok_gelijkgezet", self.controller.state)
        return result.returncode == 0

    # --- status ----------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            snapshot = self.controller.snapshot()
        config = self.config_store.get()
        now = self.clock.now()
        upcoming = Planner.next_moment(config["planning"], now)
        snapshot.update({
            "versie": __version__,
            "tijd": now.isoformat(timespec="seconds"),
            "volgende": {"tijd": upcoming[0].isoformat(timespec="minutes"),
                         "activiteit": upcoming[1]["activiteit"]} if upcoming else None,
            "voorbereiding": self.speech.progress(),
            "spraakengine": self.speech.engine.available(),
            "simulatie": self.panel.simulated,
            "waarschuwingen": self._warnings(),
        })
        return snapshot

    def _warnings(self) -> list[str]:
        warnings = []
        if not self.speech.engine.available():
            warnings.append("De spraakengine of stem ontbreekt; alleen eigen opnames werken.")
        if shutil.which("vcgencmd"):
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                     text=True, timeout=2).stdout
                flags = int(out.strip().split("=")[1], 16)
                if flags & 0x1:
                    warnings.append("Te lage voedingsspanning: gebruik een voeding of powerbank van minstens 2,5 A.")
                elif flags & 0x10000:
                    warnings.append("Sinds het opstarten is de voedingsspanning te laag geweest.")
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
        return warnings
