"""Instellingenopslag: leest, controleert en bewaart ``config.json`` (TR-02, TR-07)."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from .texts import DEFAULT_TEXTS

DEFAULT_CONFIG = {
    "versie": 1,
    "volume": 70,
    "stem": "nl_NL-pim-medium",
    "knopuitleg": True,
    "aandachtsgeluid": True,
    "tijden": {
        "antwoord_wachttijd_s": 60,
        "uitstel_min": 15,
        "pauze_max_min": 30,
        "sessie_max_min": 90,
    },
    "teksten": {},
    "activiteiten": [
        {
            "id": "koffie",
            "naam": "koffiezetten",
            "uitnodiging": {"tekst": "Het is koffietijd. Wil je koffiezetten?", "opname": None},
            "stappen": [
                {"tekst": "Pak je favoriete kopje.", "opname": None},
                {"tekst": "Zet het kopje onder het koffiezetapparaat.", "opname": None},
                {"tekst": "Druk op de knop van het apparaat.", "opname": None},
                {"tekst": "Neem het kopje mee naar de tafel.", "opname": None},
            ],
            "afsluiting": {"tekst": "Goed gedaan. Geniet van je koffie!", "opname": None},
        }
    ],
    "planning": [
        {"id": "p1", "activiteit": "koffie", "tijd": "10:30", "dagen": [1, 2, 3, 4, 5, 6, 7], "actief": False}
    ],
}

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_LIMITS = {
    "antwoord_wachttijd_s": (10, 600),
    "uitstel_min": (1, 240),
    "pauze_max_min": (1, 240),
    "sessie_max_min": (5, 480),
}


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _check_item(item, where: str, errors: list[str], required: bool = True) -> None:
    if not isinstance(item, dict):
        errors.append(f"{where}: ontbreekt.")
        return
    tekst = item.get("tekst")
    opname = item.get("opname")
    if not isinstance(tekst, str) or (required and not tekst.strip() and not opname):
        errors.append(f"{where}: vul een tekst in of voeg een opname toe.")
    if opname is not None and not (isinstance(opname, str) and _ID.match(opname)):
        errors.append(f"{where}: ongeldige verwijzing naar opname.")


def validate(config: dict) -> list[str]:
    """Geeft een lijst met begrijpelijke fouten; leeg betekent geldig."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["De configuratie moet een object zijn."]
    volume = config.get("volume")
    if not isinstance(volume, int) or not 0 <= volume <= 100:
        errors.append("Volume moet een geheel getal van 0 tot en met 100 zijn.")
    if not isinstance(config.get("stem"), str) or not config["stem"]:
        errors.append("Kies een stem.")
    for key in ("knopuitleg", "aandachtsgeluid"):
        if not isinstance(config.get(key), bool):
            errors.append(f"'{key}' moet aan of uit zijn.")
    tijden = config.get("tijden")
    if not isinstance(tijden, dict):
        errors.append("Tijden ontbreken.")
    else:
        for key, (low, high) in _LIMITS.items():
            value = tijden.get(key)
            if not isinstance(value, int) or not low <= value <= high:
                errors.append(f"Tijd '{key}' moet tussen {low} en {high} liggen.")
    teksten = config.get("teksten", {})
    if not isinstance(teksten, dict) or any(
        k not in DEFAULT_TEXTS or not isinstance(v, str) for k, v in teksten.items()
    ):
        errors.append("Eigen teksten bevatten een onbekende sleutel of geen tekst.")

    activities = config.get("activiteiten")
    ids = set()
    if not isinstance(activities, list) or not activities:
        errors.append("Voeg minstens één activiteit toe.")
        activities = []
    for index, act in enumerate(activities, start=1):
        if not isinstance(act, dict):
            errors.append(f"Activiteit {index} is ongeldig.")
            continue
        name = act.get("naam") or f"activiteit {index}"
        if not isinstance(act.get("id"), str) or not _ID.match(act["id"]):
            errors.append(f"{name}: ongeldige id (kleine letters, cijfers, - of _).")
        elif act["id"] in ids:
            errors.append(f"{name}: id '{act['id']}' komt dubbel voor.")
        ids.add(act.get("id"))
        if not isinstance(act.get("naam"), str) or not act["naam"].strip():
            errors.append(f"Activiteit {index}: naam ontbreekt.")
        _check_item(act.get("uitnodiging"), f"{name}, uitnodiging", errors)
        steps = act.get("stappen")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{name}: voeg minstens één stap toe.")
        else:
            for n, step in enumerate(steps, start=1):
                _check_item(step, f"{name}, stap {n}", errors)
        if act.get("afsluiting") is not None:
            _check_item(act["afsluiting"], f"{name}, afsluiting", errors, required=False)

    planning = config.get("planning")
    if not isinstance(planning, list):
        errors.append("Planning moet een lijst zijn.")
        planning = []
    for index, entry in enumerate(planning, start=1):
        where = f"Planningsmoment {index}"
        if not isinstance(entry, dict):
            errors.append(f"{where} is ongeldig.")
            continue
        if entry.get("activiteit") not in ids:
            errors.append(f"{where}: kies een bestaande activiteit.")
        if not isinstance(entry.get("tijd"), str) or not _TIME.match(entry["tijd"]):
            errors.append(f"{where}: tijd moet de vorm UU:MM hebben.")
        dagen = entry.get("dagen")
        if not isinstance(dagen, list) or not dagen or any(d not in range(1, 8) for d in dagen):
            errors.append(f"{where}: kies minstens één dag.")
        if not isinstance(entry.get("actief"), bool):
            errors.append(f"{where}: 'actief' moet aan of uit zijn.")
    return errors


class ConfigStore:
    """Thread-veilige toegang tot de configuratie op de microSD-kaart."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            self._write(DEFAULT_CONFIG)
            return copy.deepcopy(DEFAULT_CONFIG)
        with self.path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        # Nieuwe instellingen uit een latere softwareversie krijgen hun standaardwaarde.
        for key, value in DEFAULT_CONFIG.items():
            config.setdefault(key, copy.deepcopy(value))
        for key, value in DEFAULT_CONFIG["tijden"].items():
            config["tijden"].setdefault(key, value)
        return config

    def _write(self, config: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomair vervangen: bij stroomuitval blijft de vorige versie intact.
        os.replace(tmp, self.path)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._config)

    def save(self, config: dict) -> None:
        errors = validate(config)
        if errors:
            raise ConfigError(errors)
        with self._lock:
            self._write(config)
            self._config = copy.deepcopy(config)

    def activity(self, activity_id: str) -> dict | None:
        with self._lock:
            for act in self._config["activiteiten"]:
                if act.get("id") == activity_id:
                    return copy.deepcopy(act)
        return None
