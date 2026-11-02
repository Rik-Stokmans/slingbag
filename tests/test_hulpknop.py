"""Tests voor de hulpknop: zonder hardware, microfoon of internet (nep-model, nep-stem)."""

import copy
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from slingbag import prompts
from slingbag.assistent import UIT, HelpAssistant
from slingbag.begeleider import in_window, is_available, next_available
from slingbag.config import DEFAULT_CONFIG, ConfigStore, validate
from slingbag.geheugen import MemoryStore
from slingbag.interaction import BEGELEIDING, HULPGESPREK, RUST, InteractionController
from slingbag.logbook import Logbook
from slingbag.microfoon import FRAME_BYTES, Endpointer, Microphone, level_db
from slingbag.openrouter import AIUnavailable, ChatResult, KeyStore, parse_stream
from slingbag.stem import SentenceSplitter, clean_for_speech, write_sounds

from test_interaction import FakeAudio, FakeClock, FakePanel, FakeSpeech


# --- instellingen ------------------------------------------------------------------------

def test_help_defaults_are_valid_and_merged_into_old_config(tmp_path):
    old = copy.deepcopy(DEFAULT_CONFIG)
    del old["hulpknop"]
    (tmp_path / "config.json").write_text(json.dumps(old))
    store = ConfigStore(tmp_path / "config.json")
    assert store.get()["hulpknop"]["begeleider"]["pushmelding"]["server"] == "https://ntfy.sh"
    assert validate(store.get()) == []


def test_invalid_help_settings_are_rejected():
    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["hulpknop"]["gebruiker"]["aanspreekvorm"] = "jij"
    bad["hulpknop"]["begeleider"]["tijden"] = [{"dagen": [], "van": "25:00", "tot": "08:00"}]
    bad["hulpknop"]["begeleider"]["pushmelding"] = {"actief": True, "server": "http://x", "onderwerp": "a b"}
    bad["hulpknop"]["spreekpauze_s"] = 0.1
    errors = validate(bad)
    assert len(errors) == 6


def test_key_is_stored_privately_and_never_returned(tmp_path):
    keys = KeyStore(tmp_path / "geheim" / "openrouter.key")
    assert keys.summary() == {"ingesteld": False, "eind": None}
    keys.set("sk-or-v1-abcdefghijklmnopqrstuvwxyz1234")
    assert keys.summary() == {"ingesteld": True, "eind": "1234"}
    assert oct((tmp_path / "geheim" / "openrouter.key").stat().st_mode)[-3:] == "600"
    keys.set("")
    assert not keys.get()


# --- begeleider -------------------------------------------------------------------------

def settings_with(windows, reachable=True):
    s = copy.deepcopy(DEFAULT_CONFIG["hulpknop"])
    s["begeleider"].update(naam="Anna", bereikbaar=reachable, tijden=windows)
    return s


def test_availability_windows_including_overnight():
    monday_23 = datetime(2026, 11, 2, 23, 0)
    tuesday_06 = datetime(2026, 11, 3, 6, 0)
    night = {"dagen": [1], "van": "22:00", "tot": "07:00"}
    assert in_window(night, monday_23) and in_window(night, tuesday_06)
    assert not in_window(night, datetime(2026, 11, 3, 23, 0))  # dinsdagavond niet gekozen
    assert is_available(settings_with([]), monday_23)  # geen tijden = altijd
    assert not is_available(settings_with([], reachable=False), monday_23)


def test_next_available_moment():
    s = settings_with([{"dagen": [1, 2, 3, 4, 5], "van": "09:00", "tot": "17:00"}])
    assert next_available(s, datetime(2026, 11, 2, 20, 10)) == datetime(2026, 11, 3, 9, 0)
    assert next_available(s, datetime(2026, 11, 7, 12, 0)) == datetime(2026, 11, 9, 9, 0)  # na het weekend


# --- geheugen ----------------------------------------------------------------------------

def test_memory_changes_never_touch_caretaker_entries(tmp_path):
    memory = MemoryStore(tmp_path, FakeClock())
    own = memory.add_memory("Dochter Anna komt op zondag.", "persoon", source="begeleider")
    talk = memory.add_memory("Drinkt koffie zwart.", "voorkeur", source="gesprek")
    counts = memory.apply_changes({
        "toevoegen": [{"tekst": "Wandelt graag in het park.", "soort": "voorkeur"}],
        "bijwerken": [{"id": own["id"], "tekst": "overschreven"}, {"id": talk["id"], "tekst": "Drinkt koffie met melk."}],
        "verwijderen": [own["id"]],
    })
    assert counts == {"toegevoegd": 1, "bijgewerkt": 1, "verwijderd": 0}
    texts = {m["tekst"] for m in memory.memories()}
    assert texts == {"Dochter Anna komt op zondag.", "Drinkt koffie met melk.", "Wandelt graag in het park."}


def test_conversations_are_removed_after_retention(tmp_path):
    clock = FakeClock()
    memory = MemoryStore(tmp_path, clock)
    memory.save_conversation(clock.now() - timedelta(days=10), [{"rol": "gebruiker", "tekst": "hoi"}], 30)
    memory.save_conversation(clock.now(), [{"rol": "gebruiker", "tekst": "hoi"}], 30)
    assert len(memory.conversations()) == 2
    assert memory.purge_conversations(7) == 1
    assert len(memory.conversations()) == 1


def test_help_request_and_caretaker_reply(tmp_path):
    memory = MemoryStore(tmp_path, FakeClock())
    item = memory.add_help_request("Weet niet hoe de wasmachine aan moet.", False, True)
    memory.respond(item["id"], "gezien", "Ik bel je zo.")
    assert memory.open_requests()[0]["reactie"] == "Ik bel je zo."
    memory.respond(item["id"], "afgehandeld", None)
    assert memory.open_requests() == []


# --- prompt -------------------------------------------------------------------------------

def test_system_prompt_contains_profile_memories_and_honest_availability():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["hulpknop"]["gebruiker"].update(naam="Jan", aanspreekvorm="u", over="Woont alleen in Eindhoven.")
    config["hulpknop"]["begeleider"].update(naam="Anna", relatie="dochter",
                                             tijden=[{"dagen": [1, 2, 3, 4, 5, 6, 7], "van": "09:00", "tot": "21:00"}])
    text = prompts.system_prompt(config, [{"tekst": "Houdt van wandelen.", "soort": "voorkeur",
                                           "bijgewerkt": "2026-11-01"}], [], datetime(2026, 11, 2, 22, 30), [])
    assert "Jan" in text and "Woont alleen in Eindhoven." in text and "Houdt van wandelen." in text
    assert 'Spreek de gebruiker aan met "u"' in text
    assert "Anna (dochter). Anna is nu niet bereikbaar, wel weer morgen om 09:00." in text
    assert "maandag 2 november 2026" in text
    assert prompts.greeting(config["hulpknop"], datetime(2026, 11, 2, 9, 0)).startswith("Goedemorgen Jan")


def test_parse_json_tolerates_text_around_it():
    assert prompts.parse_json('Hier: {"toevoegen": []} klaar') == {"toevoegen": []}
    assert prompts.parse_json("geen json") == {}


# --- streaming en spraak -------------------------------------------------------------------

def sse(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})


def test_stream_parser_joins_text_and_tool_call_fragments():
    lines = [": OPENROUTER PROCESSING",
             sse({"content": "Ik waarschuw ", "reasoning": "denken"}),
             sse({"content": "Anna."}),
             sse({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "verbind_met_begeleider", "arguments": ""}}]}),
             sse({"tool_calls": [{"index": 0, "function": {"arguments": '{"reden": "val", '}}]}),
             sse({"tool_calls": [{"index": 0, "function": {"arguments": '"spoed": true}'}}]}, "tool_calls"),
             "data: [DONE]"]
    seen = []
    result = parse_stream(lines, seen.append)
    assert result.text == "Ik waarschuw Anna." and seen == ["Ik waarschuw ", "Anna."]
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"reden": "val", "spoed": True}


def test_sentence_splitter_speaks_first_sentence_early_and_keeps_times_whole():
    out = []
    splitter = SentenceSplitter(out.append)
    for chunk in ["Goed zo. Het is ", "nu 10.", "30 uur. Pak **eerst** het ", "kopje"]:
        splitter.feed(chunk)
    assert out == ["Goed zo.", "Het is nu 10.30 uur."]
    splitter.flush()
    assert out[-1] == "Pak eerst het kopje"
    assert clean_for_speech("- Stap één 😊\n- Stap twee") == "Stap één Stap twee"


def test_sounds_are_generated(tmp_path):
    sounds = write_sounds(tmp_path)
    assert set(sounds) == {"verbonden", "gehoord", "nadenken", "afsluiten"}
    assert all(p.stat().st_size > 1000 for p in sounds.values())


# --- microfoon ------------------------------------------------------------------------------

def frame(amplitude):
    import struct
    return struct.pack("<h", amplitude) * (FRAME_BYTES // 2)


def test_endpointer_waits_for_pause_after_speech():
    ep = Endpointer(pause_s=0.6, no_speech_s=3)
    frames = [frame(30)] * 10 + [frame(8000)] * 20 + [frame(30)] * 30
    results = [ep.feed(level_db(f)) for f in frames]
    assert ep.started and results.index("klaar") == 10 + 20 + 20 - 1


def test_endpointer_reports_silence():
    ep = Endpointer(no_speech_s=1)
    assert "stilte" in [ep.feed(level_db(frame(30))) for _ in range(40)]


def test_microphone_turn_returns_wav_and_can_be_aborted():
    mic = Microphone("geen")
    frames = [frame(30)] * 10 + [frame(8000)] * 20 + [frame(30)] * 60
    turn = mic.listen(Endpointer(pause_s=0.6), lambda: None, frames=iter(frames))
    assert turn.status == "spraak" and turn.wav.startswith(b"RIFF") and 0.6 < turn.seconds < 1.3
    turn = mic.listen(Endpointer(), lambda: "herhaal", frames=iter(frames))
    assert turn.status == "afgebroken" and turn.reason == "herhaal"


# --- assistent met nep-model -------------------------------------------------------------------

class FakeClient:
    def __init__(self, replies, key=True):
        self.replies = list(replies)
        self.key = key
        self.calls = []

    def available(self):
        return self.key

    def chat_stream(self, messages, tools=None, on_text=None, should_stop=None, max_tokens=0):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text, calls = reply
        if text and on_text:
            on_text(text)
        return ChatResult(text=text, tool_calls=[
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)])

    def chat(self, messages, **_):
        return '{"toevoegen": [{"tekst": "Zet graag zelf koffie.", "soort": "voorkeur"}]}'

    def transcribe(self, wav):
        return "getranscribeerd"


class FakeVoice:
    def __init__(self):
        self.said = []
        self.sounds = []
        self.audio = FakeAudio()
        self.audio.playing = False

    def reset(self):
        pass

    def mode(self):
        return "online"

    def say(self, text, cache=False, interrupted=None, sound=None):
        self.said.append(text)
        return [Path("x.wav")]

    def sound(self, name, interrupted=None):
        self.sounds.append(name)

    def play(self, paths, interrupted=None):
        return True

    def utterance(self, interrupted, thinking_after_s=1.8):
        voice = self

        class U:
            paths = [Path("a.wav")]
            started_speaking = threading.Event()
            _player = threading.Thread(target=lambda: None)

            def add(self, sentence):
                voice.said.append(sentence)

            def finish(self):
                pass

            def wait(self, timeout=0):
                pass

        u = U()
        u._player.start()
        return u


class NoMic:
    def available(self):
        return False


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, urgent, wait=False):
        self.sent.append(urgent)
        return True


@pytest.fixture
def helpenv(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = store.get()
    config["hulpknop"]["gebruiker"]["naam"] = "Jan"
    config["hulpknop"]["begeleider"]["naam"] = "Anna"
    config["hulpknop"]["stilte_s"] = 4
    store.save(config)
    clock = FakeClock()
    logbook = Logbook(tmp_path / "logboek.csv")
    controller = InteractionController(store, FakeSpeech(), FakeAudio(), FakePanel(), logbook, clock=clock)
    ended = []

    def make(replies, key=True):
        client = FakeClient(replies, key)
        voice = FakeVoice()
        assistant = HelpAssistant(store, client, voice, NoMic(), MemoryStore(tmp_path, clock), FakeNotifier(),
                                  FakePanel(), logbook, clock, simulate=True,
                                  on_ended=lambda activity: (ended.append(activity), controller.help_ended(activity)))
        controller.assistant = assistant
        return assistant, client, voice

    class Env:
        pass

    e = Env()
    e.store, e.controller, e.make, e.ended, e.tmp = store, controller, make, ended, tmp_path
    return e


def wait_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_blue_in_rest_starts_conversation_with_ping_and_greeting(helpenv):
    assistant, client, voice = helpenv.make([("Graag gedaan. Dag Jan!", [("gesprek_beeindigen", {})])])
    helpenv.controller.press("blauw")
    assert helpenv.controller.state == HULPGESPREK
    assert wait_until(lambda: assistant.phase == "luisteren")
    assert voice.sounds[0] == "verbonden"
    assert voice.said[0].endswith("Jan, ik ben er. Waarmee kan ik je helpen?")
    assert assistant.say_text("Dank je, dat was het.")
    assert wait_until(lambda: helpenv.ended)
    assert helpenv.controller.state == RUST and assistant.phase == UIT
    assert voice.sounds[-1] == "afsluiten"
    assert client.calls[0][-1] == {"role": "user", "content": "Dank je, dat was het."}
    # Na het gesprek worden herinneringen lokaal bijgewerkt.
    memory = MemoryStore(helpenv.tmp, FakeClock())
    assert wait_until(lambda: any(m["tekst"] == "Zet graag zelf koffie." for m in memory.memories()))


def test_escalation_stores_request_and_model_hears_honest_result(helpenv):
    assistant, client, voice = helpenv.make([
        ("Ik waarschuw Anna.", [("verbind_met_begeleider", {"reden": "Gevallen", "spoed": True})]),
        ("Anna heeft een bericht. Bel 112.", []),
        ("Dag.", [("gesprek_beeindigen", {})]),
    ])
    helpenv.controller.start_help("app")
    assert wait_until(lambda: assistant.phase == "luisteren")
    assistant.say_text("Ik ben gevallen.")
    assert wait_until(lambda: len(client.calls) == 2 and assistant.phase == "luisteren")
    tool_message = client.calls[1][-1]
    result = json.loads(tool_message["content"])
    assert tool_message["role"] == "tool" and result["bereikbaar"] is True and "noodplan" in result
    assert assistant.notifier.sent == [True]
    request = MemoryStore(helpenv.tmp, FakeClock()).open_requests()[0]
    assert request["spoed"] and request["reden"] == "Gevallen"
    assistant.say_text("Dag")
    assert wait_until(lambda: helpenv.ended)


def test_start_activity_from_conversation_goes_straight_to_step_one(helpenv):
    assistant, _, _ = helpenv.make([("Goed, we gaan koffiezetten.", [("start_activiteit", {"activiteit_id": "koffie"})])])
    helpenv.controller.start_help("knop")
    assert wait_until(lambda: assistant.phase == "luisteren")
    assistant.say_text("Ik wil koffie zetten maar weet niet hoe.")
    assert wait_until(lambda: helpenv.ended == ["koffie"])
    assert helpenv.controller.state == BEGELEIDING and helpenv.controller.step == 0


def test_without_key_the_caretaker_is_warned_and_told_honestly(helpenv):
    assistant, _, voice = helpenv.make([], key=False)
    helpenv.controller.start_help("knop")
    assert wait_until(lambda: helpenv.ended)
    assert "Ik heb Anna een bericht gestuurd." in voice.said[-1]
    assert "112" in voice.said[-1]
    assert MemoryStore(helpenv.tmp, FakeClock()).open_requests()


def test_model_failure_mid_conversation_falls_back_to_human(helpenv):
    assistant, _, voice = helpenv.make([AIUnavailable("geen internet")])
    helpenv.controller.start_help("knop")
    assert wait_until(lambda: assistant.phase == "luisteren")
    assistant.say_text("Help")
    assert wait_until(lambda: helpenv.ended)
    assert voice.said[-1].startswith("Het lukt me even niet om na te denken.")


def test_red_stops_conversation_and_silence_ends_it(helpenv):
    assistant, _, voice = helpenv.make([])
    helpenv.controller.start_help("knop")
    assert wait_until(lambda: assistant.phase == "luisteren")
    helpenv.controller.press("rood")
    assert wait_until(lambda: helpenv.ended)
    assert "Ik stop het gesprek. Tot straks." in voice.said
    assert helpenv.controller.state == RUST


def test_no_invitation_during_conversation(helpenv):
    assistant, _, _ = helpenv.make([])
    helpenv.controller.start_help("knop")
    ok, _ = helpenv.controller.invite("koffie", "planning")
    assert not ok
    helpenv.controller.press("rood")
    assert wait_until(lambda: helpenv.ended)


def test_help_disabled_means_blue_does_nothing_in_rest(helpenv):
    config = helpenv.store.get()
    config["hulpknop"]["actief"] = False
    helpenv.store.save(config)
    helpenv.make([])
    helpenv.controller.press("blauw")
    assert helpenv.controller.state == RUST
