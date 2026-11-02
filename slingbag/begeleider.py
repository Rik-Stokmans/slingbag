"""Begeleider: bepaalt of de begeleider nu bereikbaar is en waarschuwt hem of haar bij een hulpvraag.

Een hulpvraag verschijnt altijd in de begeleidersapp (lokaal, op de Pi). Daarnaast kan de
begeleider een pushmelding via ntfy aanzetten, handig als hij of zij niet thuis is. Die melding
bevat bewust geen inhoud, alleen dat er hulp gevraagd is; de reden staat alleen op de Pi.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DAGNAMEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _minutes(hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return hour * 60 + minute


def in_window(window: dict, moment: datetime) -> bool:
    """Een venster als 22:00–07:00 loopt door over middernacht en hoort bij de begindag."""
    start, end = _minutes(window["van"]), _minutes(window["tot"])
    now = moment.hour * 60 + moment.minute
    day = moment.isoweekday()
    previous_day = 7 if day == 1 else day - 1
    if start < end:
        return day in window["dagen"] and start <= now < end
    return (day in window["dagen"] and now >= start) or (previous_day in window["dagen"] and now < end)


def is_available(settings: dict, moment: datetime) -> bool:
    caretaker = settings["begeleider"]
    if not caretaker.get("bereikbaar"):
        return False
    windows = caretaker.get("tijden") or []
    return not windows or any(in_window(w, moment) for w in windows)


def next_available(settings: dict, moment: datetime) -> datetime | None:
    """Het eerstvolgende moment (binnen een week, per kwartier) waarop de begeleider bereikbaar is."""
    if not settings["begeleider"].get("bereikbaar"):
        return None
    probe = moment.replace(second=0, microsecond=0)
    probe += timedelta(minutes=15 - probe.minute % 15)
    for _ in range(7 * 24 * 4):
        if is_available(settings, probe):
            return probe
        probe += timedelta(minutes=15)
    return None


def describe_moment(moment: datetime, now: datetime) -> str:
    """Leesbaar voor een gesproken antwoord, bijvoorbeeld 'morgen om 09:00'."""
    days = (moment.date() - now.date()).days
    when = "vandaag" if days == 0 else "morgen" if days == 1 else f"op {DAGNAMEN[moment.weekday()]}"
    return f"{when} om {moment:%H:%M}"


def caretaker_name(settings: dict) -> str:
    return (settings["begeleider"].get("naam") or "").strip() or "je begeleider"


class Notifier:
    def __init__(self, settings):
        self.settings = settings  # geeft de actuele hulpknop-instellingen

    def push_enabled(self) -> bool:
        push = self.settings()["begeleider"].get("pushmelding") or {}
        return bool(push.get("actief") and push.get("onderwerp"))

    def send(self, urgent: bool, wait: bool = False) -> bool:
        """Verstuurt een pushmelding zonder inhoud. Geeft False als push uit staat."""
        if not self.push_enabled():
            return False
        push = self.settings()["begeleider"]["pushmelding"]
        url = f"{push['server'].rstrip('/')}/{push['onderwerp']}"
        body = ("Dringend: de slingbag vraagt om hulp. Open de app of bel even."
                if urgent else "De slingbag vraagt om hulp. Open de app voor de vraag.")
        request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers={
            "Title": "Slingbag hulpknop", "Priority": "5" if urgent else "4", "Tags": "sos" if urgent else "bell",
        })
        result = {"ok": False}

        def run():
            try:
                with urllib.request.urlopen(request, timeout=8):
                    result["ok"] = True
            except (urllib.error.URLError, OSError) as exc:
                log.warning("Pushmelding mislukt: %s", exc)

        thread = threading.Thread(target=run, name="pushmelding", daemon=True)
        thread.start()
        if wait:
            thread.join(10)
            return result["ok"]
        return True
