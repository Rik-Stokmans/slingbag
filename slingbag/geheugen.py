"""Lokale opslag van de hulpknop: herinneringen, hulpvragen en (optioneel) gesprekken.

Alles staat als JSON op de microSD-kaart van de Pi in ``<data>/hulp/``; niets wordt elders
bewaard. De begeleider kan alles inzien, aanpassen en wissen via de app.

* Herinneringen: korte feiten over de gebruiker die het gesprek persoonlijker en veiliger maken.
  De begeleider vult ze aan; na elk gesprek stelt het model nieuwe of gewijzigde feiten voor.
* Hulpvragen: momenten waarop de AI een mens nodig vond. Blijven zichtbaar tot de begeleider ze
  afhandelt (maximaal 100).
* Gesprekken: de volledige tekst, alleen als de begeleider dat aanzet, en automatisch gewist na
  de ingestelde bewaartermijn. Geluid wordt nooit bewaard.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

SOORTEN = ("persoon", "voorkeur", "routine", "gezondheid", "afspraak", "overig")
MAX_HERINNERINGEN = 150
MAX_TEKST = 300
MAX_HULPVRAGEN = 100


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read(path: Path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _now(clock) -> str:
    return clock.now().isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, data_dir: Path, clock):
        self.dir = Path(data_dir) / "hulp"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.Lock()
        self.memories_path = self.dir / "herinneringen.json"
        self.requests_path = self.dir / "hulpvragen.json"
        self.conversations_dir = self.dir / "gesprekken"

    # --- herinneringen ---------------------------------------------------------

    def memories(self) -> list[dict]:
        with self._lock:
            return _read(self.memories_path, [])

    def _clean(self, text: str, kind: str) -> tuple[str, str]:
        text = " ".join(str(text or "").split())[:MAX_TEKST]
        if not text:
            raise ValueError("Een herinnering heeft tekst nodig.")
        return text, kind if kind in SOORTEN else "overig"

    def add_memory(self, text: str, kind: str = "overig", source: str = "begeleider") -> dict:
        text, kind = self._clean(text, kind)
        with self._lock:
            items = _read(self.memories_path, [])
            item = {"id": secrets.token_hex(4), "tekst": text, "soort": kind, "bron": source,
                    "gemaakt": _now(self.clock), "bijgewerkt": _now(self.clock)}
            items.append(item)
            if len(items) > MAX_HERINNERINGEN:
                # Eerst de oudste herinneringen uit gesprekken laten vallen, nooit die van de begeleider.
                from_talks = [i for i in items if i["bron"] == "gesprek"]
                drop = from_talks[0] if from_talks else items[0]
                items.remove(drop)
            _atomic_write(self.memories_path, items)
            return item

    def update_memory(self, memory_id: str, text: str, kind: str | None = None) -> dict | None:
        with self._lock:
            items = _read(self.memories_path, [])
            for item in items:
                if item["id"] == memory_id:
                    item["tekst"], item["soort"] = self._clean(text, kind or item["soort"])
                    item["bijgewerkt"] = _now(self.clock)
                    _atomic_write(self.memories_path, items)
                    return item
        return None

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            items = _read(self.memories_path, [])
            kept = [i for i in items if i["id"] != memory_id]
            _atomic_write(self.memories_path, kept)
            return len(kept) != len(items)

    def clear_memories(self) -> None:
        with self._lock:
            _atomic_write(self.memories_path, [])

    def apply_changes(self, changes: dict) -> dict:
        """Verwerkt het voorstel van het model na een gesprek. Herinneringen van de begeleider blijven staan."""
        counts = {"toegevoegd": 0, "bijgewerkt": 0, "verwijderd": 0}
        if not isinstance(changes, dict):
            return counts
        protected = {m["id"] for m in self.memories() if m["bron"] == "begeleider"}
        for item in changes.get("verwijderen") or []:
            memory_id = item.get("id") if isinstance(item, dict) else item
            if isinstance(memory_id, str) and memory_id not in protected and self.delete_memory(memory_id):
                counts["verwijderd"] += 1
        for item in changes.get("bijwerken") or []:
            if (isinstance(item, dict) and item.get("id") not in protected
                    and isinstance(item.get("tekst"), str) and item["tekst"].strip()
                    and self.update_memory(item["id"], item["tekst"], item.get("soort"))):
                counts["bijgewerkt"] += 1
        for item in changes.get("toevoegen") or []:
            if isinstance(item, dict) and isinstance(item.get("tekst"), str) and item["tekst"].strip():
                self.add_memory(item["tekst"], item.get("soort", "overig"), source="gesprek")
                counts["toegevoegd"] += 1
        return counts

    # --- hulpvragen ------------------------------------------------------------

    def help_requests(self) -> list[dict]:
        with self._lock:
            return _read(self.requests_path, [])

    def add_help_request(self, reason: str, urgent: bool, available: bool) -> dict:
        with self._lock:
            items = _read(self.requests_path, [])
            item = {"id": secrets.token_hex(4), "tijdstip": _now(self.clock),
                    "reden": " ".join(str(reason or "").split())[:MAX_TEKST] or "Hulp gevraagd",
                    "spoed": bool(urgent), "begeleider_bereikbaar": bool(available),
                    "status": "open", "reactie": None, "reactie_tijd": None}
            items.append(item)
            _atomic_write(self.requests_path, items[-MAX_HULPVRAGEN:])
            return item

    def respond(self, request_id: str, status: str, message: str | None) -> dict | None:
        if status not in ("gezien", "afgehandeld"):
            raise ValueError("Onbekende status.")
        with self._lock:
            items = _read(self.requests_path, [])
            for item in items:
                if item["id"] == request_id:
                    item["status"] = status
                    if message:
                        item["reactie"] = " ".join(message.split())[:MAX_TEKST]
                        item["reactie_tijd"] = _now(self.clock)
                    _atomic_write(self.requests_path, items)
                    return item
        return None

    def open_requests(self) -> list[dict]:
        return [r for r in self.help_requests() if r["status"] != "afgehandeld"]

    # --- gesprekken (optioneel) ----------------------------------------------------

    def save_conversation(self, started: datetime, turns: list[dict], retention_days: int) -> str:
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        conversation_id = f"{started:%Y%m%d-%H%M%S}"
        _atomic_write(self.conversations_dir / f"{conversation_id}.json",
                      {"id": conversation_id, "begin": started.isoformat(timespec="seconds"),
                       "einde": _now(self.clock), "beurten": turns})
        self.purge_conversations(retention_days)
        return conversation_id

    def purge_conversations(self, retention_days: int) -> int:
        if not self.conversations_dir.exists():
            return 0
        limit = self.clock.now() - timedelta(days=retention_days)
        removed = 0
        for path in self.conversations_dir.glob("*.json"):
            try:
                started = datetime.strptime(path.stem, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if started < limit:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def conversations(self) -> list[dict]:
        if not self.conversations_dir.exists():
            return []
        result = []
        for path in sorted(self.conversations_dir.glob("*.json"), reverse=True):
            data = _read(path, {})
            result.append({"id": data.get("id", path.stem), "begin": data.get("begin"),
                           "einde": data.get("einde"), "beurten": len(data.get("beurten", []))})
        return result

    def conversation(self, conversation_id: str) -> dict | None:
        path = self.conversations_dir / f"{conversation_id}.json"
        if not path.exists() or path.parent != self.conversations_dir:
            return None
        return _read(path, None)

    def clear_conversations(self) -> None:
        if self.conversations_dir.exists():
            for path in self.conversations_dir.glob("*.json"):
                path.unlink(missing_ok=True)
