"""Engelse modus: vaste zinnen, stem, activiteiten en hulpknop in de gekozen taal."""

import copy
from datetime import datetime

from slingbag import prompts, taal
from slingbag.config import DEFAULT_CONFIG, validate
from slingbag.interaction import session_items
from slingbag.texts import render


def config_en():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["taal"] = "en"
    config["teksten"] = {"stop": "Eigen Nederlandse zin."}
    config["activiteiten"][0]["en"] = {
        "naam": "making coffee",
        "uitnodiging": {"tekst": "It's coffee time. Do you want to make coffee?", "opname": None},
        "stappen": [{"tekst": "Take your favourite cup.", "opname": None}],
        "afsluiting": {"tekst": "", "opname": None},
    }
    return config


def test_standaard_nederlands():
    config = copy.deepcopy(DEFAULT_CONFIG)
    assert taal.taal(config) == "nl"
    assert taal.stem(config) == config["stem"]
    act = config["activiteiten"][0]
    assert taal.activiteit(act, "nl") is act


def test_engelse_vaste_zinnen_negeren_nederlandse_eigen_teksten():
    config = config_en()
    assert render(config["teksten"], "stop", "en") == "I will stop now. See you later."
    assert render(config["teksten"], "stop", "nl") == "Eigen Nederlandse zin."
    assert "15 minutes" in render({}, "uitstel", "en", minuten=15)


def test_engelse_activiteit_valt_per_stap_terug():
    config = config_en()
    act = taal.activiteit(config["activiteiten"][0], "en")
    assert act["naam"] == "making coffee"
    assert act["stappen"][0]["tekst"] == "Take your favourite cup."
    assert act["stappen"][1]["tekst"] == DEFAULT_CONFIG["activiteiten"][0]["stappen"][1]["tekst"]
    assert act["afsluiting"] is None  # dan de Engelse standaardzin
    assert taal.activiteit(act, "en") == act  # nog een keer toepassen verandert niets


def test_sessie_gebruikt_engelse_teksten_en_stem():
    config = config_en()
    items = session_items(config, config["activiteiten"][0])
    texts = [i if isinstance(i, str) else i["tekst"] for i in items]
    assert "Well done!" in texts
    assert "Are you done with making coffee? Green is yes. Blue is not yet, then I will help you further." in texts
    assert taal.stem(config) == "en_US-lessac-medium"


def test_validatie():
    config = config_en()
    assert validate(config) == []
    config["taal"] = "fr"
    assert validate(config)
    config["taal"] = "en"
    config["activiteiten"][0]["en"] = "fout"
    assert validate(config)


def test_hulpknop_in_het_engels():
    config = config_en()
    moment = datetime(2026, 11, 26, 10, 0)
    hello = prompts.greeting(config["hulpknop"], moment, "en")
    assert hello.startswith("Good morning") and "How can I help you?" in hello
    text = prompts.system_prompt(config, [], [], moment, [])
    assert "LANGUAGE: ENGLISH" in text
    assert "id koffie: making coffee" in text
    assert prompts.NOODPLAN_EN in text
