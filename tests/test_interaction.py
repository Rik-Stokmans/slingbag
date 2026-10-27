"""Tests per functionele requirement voor de toestandsmachine, zonder hardware."""

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from slingbag.config import ConfigStore
from slingbag.interaction import (AFRONDEN, BEGELEIDING, KEUZE, PAUZE, RUST, UITNODIGING,
                                  ZELFSTANDIG, InteractionController)
from slingbag.logbook import Logbook
from slingbag.speech import SpeechUnavailable


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.start = datetime(2026, 10, 27, 10, 0)

    def now(self):
        return self.start + timedelta(seconds=self.t - 1000.0)

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeSpeech:
    chime_path = Path("aandacht.wav")

    def __init__(self):
        self.missing = set()

    def resolve(self, item, voice, generate=True):
        text = item if isinstance(item, str) else (item.get("tekst") or item.get("opname"))
        if text in self.missing:
            raise SpeechUnavailable(f"ontbreekt: {text}")
        return Path(text)


class FakeAudio:
    def __init__(self):
        self.played = []
        self.stopped = 0
        self.last_token = None

    def play(self, paths, token=None, on_done=None):
        self.played.append([str(p) for p in paths])
        self.last_token = token

    def stop(self):
        self.stopped += 1


class FakePanel:
    simulated = True
    light = "uit"

    def set_light(self, pattern):
        self.light = pattern


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    audio = FakeAudio()
    speech = FakeSpeech()
    store = ConfigStore(tmp_path / "config.json")
    logbook = Logbook(tmp_path / "logboek.csv")
    controller = InteractionController(store, speech, audio, FakePanel(), logbook, clock=clock)

    class Env:
        pass

    e = Env()
    e.clock, e.audio, e.speech, e.store, e.logbook, e.c = clock, audio, speech, store, logbook, controller

    def press(button):
        clock.advance(1)  # buiten de vergrendeling tegen dubbel drukken
        controller.press(button)

    def finish_audio():
        controller.audio_finished(audio.last_token)

    def events():
        with (tmp_path / "logboek.csv").open() as handle:
            return [(r["gebeurtenis"], r["detail"]) for r in csv.DictReader(handle)]

    e.press, e.finish_audio, e.events = press, finish_audio, events
    return e


def last_spoken(env):
    return env.audio.played[-1]


def test_starts_in_rest(env):  # FR-09
    assert env.c.state == RUST
    assert env.c.panel.light == "uit"


def test_invitation_plays_chime_text_and_button_explanation(env):  # FR-01, FR-02
    ok, _ = env.c.invite("koffie", "planning")
    assert ok and env.c.state == UITNODIGING
    spoken = last_spoken(env)
    assert spoken[0] == "aandacht.wav"
    assert "Wil je koffiezetten?" in spoken[1]
    assert "groen" in spoken[2]


def test_no_second_invitation_during_session(env):  # FR-02
    env.c.invite("koffie", "planning")
    ok, _ = env.c.invite("koffie", "planning")
    assert not ok
    assert ("uitnodiging_overgeslagen", "bron: planning; sessie actief") in env.events()


def test_no_answer_starts_no_activity(env):  # FR-03
    env.c.invite("koffie", "planning")
    env.finish_audio()
    env.clock.advance(59)
    env.c.tick()
    assert env.c.state == UITNODIGING  # wachttijd gaat pas in na de spraak
    env.clock.advance(2)
    env.c.tick()
    assert env.c.state == RUST
    assert ("geen_antwoord", "") in env.events()


def test_postpone_gives_exactly_one_new_invitation(env):  # FR-03, TR-06
    env.c.invite("koffie", "planning")
    env.press("geel")
    assert env.c.state == RUST and env.c.postponed
    env.clock.advance(15 * 60)
    env.c.tick()
    assert env.c.state == UITNODIGING and env.c.source == "uitstel"
    env.press("geel")  # tweede keer later: geen derde uitnodiging
    assert env.c.state == RUST and env.c.postponed is None


def test_refuse(env):  # FR-03
    env.c.invite("koffie", "planning")
    env.press("rood")
    assert env.c.state == RUST
    assert ("keuze", "nee") in env.events()


def test_guided_steps_one_at_a_time_and_user_confirms_finish(env):  # FR-04
    env.c.invite("koffie", "planning")
    env.press("groen")
    assert env.c.state == KEUZE
    env.press("blauw")
    assert env.c.state == BEGELEIDING and env.c.step == 0
    assert "Pak je favoriete kopje." in last_spoken(env)
    for expected in (1, 2, 3):
        env.press("groen")
        assert env.c.step == expected
    env.press("groen")  # na de laatste stap vraagt de slingbag of de gebruiker klaar is
    assert env.c.state == AFRONDEN
    env.press("groen")
    assert env.c.state == RUST
    assert ("afgerond", "") in env.events()


def test_independent_then_ask_help(env):  # FR-04
    env.c.invite("koffie", "planning")
    env.press("groen")
    env.press("groen")
    assert env.c.state == ZELFSTANDIG
    env.press("blauw")
    assert env.c.state == BEGELEIDING and env.c.step == 0


def test_repeat_does_not_advance(env):  # FR-05
    env.c.invite("koffie", "planning")
    env.press("groen")
    env.press("blauw")
    env.press("groen")
    env.press("geel")
    assert env.c.step == 1
    assert "Zet het kopje onder het koffiezetapparaat." in last_spoken(env)


def test_repeat_invitation(env):  # FR-05
    env.c.invite("koffie", "planning")
    env.press("blauw")
    assert env.c.state == UITNODIGING
    assert "Wil je koffiezetten?" in last_spoken(env)[0]


def test_pause_resume_keeps_step_and_stop_clears_postpone(env):  # FR-06
    env.c.invite("koffie", "planning")
    env.press("groen")
    env.press("blauw")
    env.press("groen")
    env.press("blauw")
    assert env.c.state == PAUZE and env.c.panel.light == "pulseren"
    env.press("blauw")
    assert env.c.state == BEGELEIDING and env.c.step == 1
    env.press("rood")
    assert env.c.state == RUST and env.audio.stopped >= 1


def test_stop_during_speech_interrupts_audio(env):  # FR-06
    env.c.invite("koffie", "planning")
    stops = env.audio.stopped
    env.press("rood")
    assert env.c.state == RUST
    env.c.stop("app")
    assert env.audio.stopped > stops


def test_stop_in_rest_clears_planned_postpone(env):  # FR-06
    env.c.invite("koffie", "planning")
    env.press("geel")
    assert env.c.postponed
    env.c.stop("app")
    assert env.c.postponed is None


def test_rapid_presses_count_once(env):  # TR-04
    env.c.invite("koffie", "planning")
    env.press("groen")
    env.press("blauw")
    env.clock.advance(1)
    env.c.press("groen")
    env.c.press("groen")  # binnen 0,3 s: genegeerd
    assert env.c.step == 1


def test_missing_audio_blocks_start_and_reports(env):  # FR-09, TR-10
    env.speech.missing.add("Druk op de knop van het apparaat.")
    ok, message = env.c.invite("koffie", "planning")
    assert not ok and env.c.state == RUST
    assert "ontbreekt" in message
    assert env.c.panel.light == "fout"
    env.c.clear_error()
    assert env.c.panel.light == "uit"


def test_green_in_rest_lets_user_start_on_own_initiative(env):
    env.press("groen")
    assert env.c.state == UITNODIGING and env.c.source == "gebruiker"


def test_session_timeout_returns_to_rest(env):
    env.c.invite("koffie", "planning")
    env.press("groen")
    env.press("groen")
    env.finish_audio()
    env.clock.advance(90 * 60 + 1)
    env.c.tick()
    assert env.c.state == RUST
