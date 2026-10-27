"""Planner: bepaalt wanneer een ingestelde uitnodiging aan de beurt is (FR-02, TR-06)."""

from __future__ import annotations

from datetime import datetime, timedelta


class Planner:
    def __init__(self):
        self._fired: set[str] = set()

    def due(self, planning: list[dict], now: datetime) -> list[dict]:
        """Geeft de planningsmomenten die nu (deze minuut) aan de beurt zijn, elk één keer per dag."""
        hhmm = now.strftime("%H:%M")
        day = now.isoweekday()
        today = now.date().isoformat()
        # Oude markeringen opruimen zodat de set niet blijft groeien.
        self._fired = {key for key in self._fired if key.startswith(today)}
        result = []
        for entry in planning:
            if not entry.get("actief") or entry.get("tijd") != hhmm or day not in entry.get("dagen", []):
                continue
            key = f"{today}/{entry.get('id')}/{hhmm}"
            if key not in self._fired:
                self._fired.add(key)
                result.append(entry)
        return result

    @staticmethod
    def next_moment(planning: list[dict], now: datetime) -> tuple[datetime, dict] | None:
        """Het eerstvolgende actieve moment binnen een week, voor weergave in de app."""
        best = None
        for entry in planning:
            if not entry.get("actief"):
                continue
            hour, minute = map(int, entry["tijd"].split(":"))
            for offset in range(8):
                day = now + timedelta(days=offset)
                moment = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if moment > now and moment.isoweekday() in entry.get("dagen", []):
                    if best is None or moment < best[0]:
                        best = (moment, entry)
                    break
        return best
