"""Taal van de slingbag: Nederlands (standaard) of Engels.

De taal bepaalt wat de slingbag uitspreekt: de vaste zinnen, de stem van Piper, de activiteiten
(als die een Engelse versie hebben) en het hulpgesprek. De begeleidersapp zelf blijft Nederlands.
"""

from __future__ import annotations

TALEN = ("nl", "en")
ENGELSE_STEM = "en_US-lessac-medium"


def taal(config: dict) -> str:
    value = config.get("taal")
    return value if value in TALEN else "nl"


def is_engels(config: dict) -> bool:
    return taal(config) == "en"


def stem(config: dict) -> str:
    """De Piper-stem die past bij de gekozen taal."""
    if is_engels(config):
        return config.get("stem_en") or ENGELSE_STEM
    return config["stem"]


def _filled(item) -> bool:
    return isinstance(item, dict) and bool((item.get("tekst") or "").strip() or item.get("opname"))


def activiteit(act: dict | None, lang: str) -> dict | None:
    """Geeft de activiteit in de gekozen taal; ontbrekende Engelse teksten vallen terug op het Nederlands."""
    if act is None or lang != "en" or not isinstance(act.get("en"), dict):
        return act
    en = act["en"]
    out = dict(act)
    if isinstance(en.get("naam"), str) and en["naam"].strip():
        out["naam"] = en["naam"].strip()
    if _filled(en.get("uitnodiging")):
        out["uitnodiging"] = en["uitnodiging"]
    # Zonder Engelse afsluiting de Engelse standaardzin, niet de Nederlandse afsluiting.
    out["afsluiting"] = en["afsluiting"] if _filled(en.get("afsluiting")) else None
    steps = en.get("stappen") if isinstance(en.get("stappen"), list) else []
    out["stappen"] = [steps[i] if i < len(steps) and _filled(steps[i]) else step
                      for i, step in enumerate(act["stappen"])]
    return out
