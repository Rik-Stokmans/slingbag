import copy
import json
from datetime import datetime

import pytest

from slingbag.config import DEFAULT_CONFIG, ConfigError, ConfigStore, validate
from slingbag.scheduler import Planner


def test_default_config_is_valid():
    assert validate(DEFAULT_CONFIG) == []


def test_invalid_config_is_rejected_and_file_kept(tmp_path):  # TR-07
    store = ConfigStore(tmp_path / "config.json")
    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["activiteiten"][0]["stappen"] = []
    bad["planning"][0]["tijd"] = "25:00"
    with pytest.raises(ConfigError) as exc:
        store.save(bad)
    assert len(exc.value.errors) == 2
    assert json.loads((tmp_path / "config.json").read_text())["activiteiten"][0]["stappen"]


def test_planner_fires_once_per_day():  # FR-02, TR-06
    planning = [{"id": "p1", "activiteit": "koffie", "tijd": "10:30", "dagen": [2], "actief": True}]
    planner = Planner()
    tuesday = datetime(2026, 10, 27, 10, 30, 5)
    assert len(planner.due(planning, tuesday)) == 1
    assert planner.due(planning, tuesday.replace(second=40)) == []
    assert planner.due(planning, datetime(2026, 10, 28, 10, 30)) == []  # woensdag niet gekozen


def test_inactive_planning_is_ignored():
    planning = [{"id": "p1", "activiteit": "koffie", "tijd": "10:30", "dagen": [2], "actief": False}]
    assert Planner().due(planning, datetime(2026, 10, 27, 10, 30)) == []


def test_next_moment():
    planning = [{"id": "p1", "activiteit": "koffie", "tijd": "09:00", "dagen": [3], "actief": True}]
    moment, entry = Planner.next_moment(planning, datetime(2026, 10, 27, 10, 0))
    assert moment == datetime(2026, 10, 28, 9, 0)
