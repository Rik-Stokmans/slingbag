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
    # Hulpknop: in rust start de blauwe knop een gesprek met een AI-model (zie assistent.py).
    "hulpknop": {
        "actief": True,
        "model": "anthropic/claude-sonnet-5.5",
        "transcriptiemodel": "mistralai/voxtral-mini-transcribe-3-0",
        "stemmodel": "google/gemini-3.8-flash-lite-tts",
        "stembron": "online",
        "online_stem": "Achird",
        "spreeksnelheid": 0.9,
        "denkniveau": "low",
        "alleen_zdr": True,
        "geheugen_bijwerken": True,
        "gesprekken_bewaren": False,
        "bewaartermijn_dagen": 7,
        "spreekpauze_s": 1.6,
        "stilte_s": 10,
        "gesprek_max_min": 20,
        "gebruiker": {
            "naam": "",
            "aanspreekvorm": "je",
            "over": "",
            "noodplan": "Bel 112 bij direct gevaar, zoals een val waarbij je niet kunt opstaan, "
                        "pijn op de borst of brand.",
        },
        "begeleider": {
            "naam": "",
            "relatie": "",
            "bereikbaar": True,
            "tijden": [],
            "pushmelding": {"actief": False, "server": "https://ntfy.sh", "onderwerp": ""},
        },
    },
}

ONLINE_STEMMEN = ["Achird", "Sulafat", "Kore", "Aoede", "Leda", "Charon", "Orus", "Puck"]

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MODEL = re.compile(r"^[a-z0-9~][a-z0-9._~:/-]{2,99}$")
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
    _validate_help(config.get("hulpknop"), errors)
    return errors


def _validate_help(help_cfg, errors: list[str]) -> None:
    if not isinstance(help_cfg, dict):
        errors.append("Instellingen van de hulpknop ontbreken.")
        return
    for key in ("actief", "alleen_zdr", "geheugen_bijwerken", "gesprekken_bewaren"):
        if not isinstance(help_cfg.get(key), bool):
            errors.append(f"Hulpknop: '{key}' moet aan of uit zijn.")
    for key in ("model", "transcriptiemodel", "stemmodel"):
        if not isinstance(help_cfg.get(key), str) or not _MODEL.match(help_cfg[key]):
            errors.append(f"Hulpknop: '{key}' is geen geldige modelnaam.")
    if help_cfg.get("denkniveau") not in ("minimal", "low", "medium", "high"):
        errors.append("Hulpknop: kies een denkniveau.")
    if help_cfg.get("stembron") not in ("online", "lokaal"):
        errors.append("Hulpknop: kies een online of lokale stem.")
    if not isinstance(help_cfg.get("online_stem"), str) or not help_cfg["online_stem"].isalpha():
        errors.append("Hulpknop: kies een online stem.")
    numbers = {"spreeksnelheid": (0.6, 1.3), "spreekpauze_s": (0.6, 5), "stilte_s": (4, 60),
               "gesprek_max_min": (2, 60), "bewaartermijn_dagen": (1, 90)}
    for key, (low, high) in numbers.items():
        value = help_cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            errors.append(f"Hulpknop: '{key}' moet tussen {low} en {high} liggen.")
    user = help_cfg.get("gebruiker")
    if not isinstance(user, dict):
        errors.append("Hulpknop: gegevens van de gebruiker ontbreken.")
    else:
        for key, limit in (("naam", 60), ("over", 3000), ("noodplan", 500)):
            if not isinstance(user.get(key), str) or len(user[key]) > limit:
                errors.append(f"Hulpknop: '{key}' moet tekst zijn van hooguit {limit} tekens.")
        if user.get("aanspreekvorm") not in ("je", "u"):
            errors.append("Hulpknop: aanspreekvorm moet 'je' of 'u' zijn.")
    caretaker = help_cfg.get("begeleider")
    if not isinstance(caretaker, dict):
        errors.append("Hulpknop: gegevens van de begeleider ontbreken.")
        return
    for key in ("naam", "relatie"):
        if not isinstance(caretaker.get(key), str) or len(caretaker[key]) > 60:
            errors.append(f"Begeleider: '{key}' moet tekst zijn van hooguit 60 tekens.")
    if not isinstance(caretaker.get("bereikbaar"), bool):
        errors.append("Begeleider: 'bereikbaar' moet aan of uit zijn.")
    windows = caretaker.get("tijden")
    if not isinstance(windows, list):
        errors.append("Begeleider: bereikbare tijden moeten een lijst zijn.")
        windows = []
    for index, window in enumerate(windows, start=1):
        where = f"Begeleider, tijdvak {index}"
        if not isinstance(window, dict):
            errors.append(f"{where} is ongeldig.")
            continue
        for key in ("van", "tot"):
            if not isinstance(window.get(key), str) or not _TIME.match(window[key]):
                errors.append(f"{where}: '{key}' moet de vorm UU:MM hebben.")
        if window.get("van") == window.get("tot"):
            errors.append(f"{where}: begin en einde zijn gelijk.")
        dagen = window.get("dagen")
        if not isinstance(dagen, list) or not dagen or any(d not in range(1, 8) for d in dagen):
            errors.append(f"{where}: kies minstens één dag.")
    push = caretaker.get("pushmelding")
    if not isinstance(push, dict) or not isinstance(push.get("actief"), bool):
        errors.append("Begeleider: pushmelding is ongeldig.")
    elif push["actief"]:
        if not isinstance(push.get("server"), str) or not push["server"].startswith("https://"):
            errors.append("Pushmelding: de server moet met https:// beginnen.")
        if not isinstance(push.get("onderwerp"), str) or not _TOPIC.match(push["onderwerp"]):
            errors.append("Pushmelding: het onderwerp mag alleen letters, cijfers, - en _ bevatten.")


def _merge_defaults(target: dict, defaults: dict) -> None:
    """Vult ontbrekende (geneste) instellingen aan zonder bestaande waarden te veranderen."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict) and key != "teksten":
            _merge_defaults(target[key], value)


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
        _merge_defaults(config, DEFAULT_CONFIG)
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
