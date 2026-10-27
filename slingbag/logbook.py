"""Interactielogboek: legt per sessie alleen de gebeurtenissen vast die nodig zijn voor de proef.

Geen geluidsopnames, locatie of namen; alleen tijdstip, sessiecode, gebeurtenis,
toestand, activiteit, stapnummer en een korte toelichting.
"""

from __future__ import annotations

import csv
import io
import threading
from datetime import datetime
from pathlib import Path

FIELDS = ["tijdstip", "sessie", "gebeurtenis", "toestand", "activiteit", "stap", "detail"]


class Logbook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, when: datetime, session: str, event: str, state: str,
              activity: str = "", step: int | str = "", detail: str = "") -> None:
        row = [when.isoformat(timespec="seconds"), session, event, state, activity, step, detail]
        with self._lock:
            new = not self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new:
                    writer.writerow(FIELDS)
                writer.writerow(row)

    def read_csv(self) -> str:
        with self._lock:
            if not self.path.exists():
                return ",".join(FIELDS) + "\n"
            return self.path.read_text(encoding="utf-8")

    def tail(self, count: int = 50) -> list[dict]:
        rows = list(csv.DictReader(io.StringIO(self.read_csv())))
        return rows[-count:][::-1]

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
