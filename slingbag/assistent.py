"""Hulpknop-assistent: een gesproken gesprek met een AI-model, gestart met de blauwe knop.

Verloop van een gesprek:
    blauwe knop → 'ping' (verbonden) → begroeting → luisteren → 'gehoord'-tikje → spraak naar tekst
    → antwoord wordt zin voor zin uitgesproken terwijl het model nog schrijft → weer luisteren …
Het gesprek stopt als de gebruiker gedag zegt, op rood drukt, lang stil is of na de maximale duur.

Knoppen tijdens het gesprek (zelfde betekenis als in de rest van de slingbag):
    blauw/groen = ik wil nu praten (onderbreekt de stem) of: ik ben klaar met praten
    groen zonder iets te zeggen = "ja"
    geel = herhaal het laatste antwoord
    rood = stop het gesprek

Het gesprek draait in een eigen thread; de interactiecontroller blijft zo vrij voor de knoppen.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Callable

from . import prompts
from .begeleider import caretaker_name, describe_moment, is_available, next_available
from .microfoon import Endpointer
from .openrouter import AIUnavailable
from .scheduler import Planner
from .stem import SentenceSplitter

log = logging.getLogger(__name__)

UIT, VERBINDEN, LUISTEREN, DENKEN, SPREKEN = "uit", "verbinden", "luisteren", "denken", "spreken"
LIGHT_FOR_PHASE = {VERBINDEN: "aan", LUISTEREN: "aan", DENKEN: "pulseren", SPREKEN: "aan"}
MAX_TOOL_ROUNDS = 4


class HelpAssistant:
    def __init__(self, config_store, client, voice, microphone, memory, notifier, panel, logbook, clock,
                 on_ended: Callable[[str | None], None], simulate: bool = False):
        self.config_store = config_store
        self.client = client
        self.voice = voice
        self.mic = microphone
        self.memory = memory
        self.notifier = notifier
        self.panel = panel
        self.logbook = logbook
        self.clock = clock
        self.on_ended = on_ended  # wordt aangeroepen met de activiteit die daarna moet starten (of None)
        self.simulate = simulate

        self.phase = UIT
        self.session = ""
        self.started_at: datetime | None = None
        self.transcript: list[dict] = []
        self.last_error: str | None = None
        self.last_answer: list = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._skip = threading.Event()        # onderbreek de stem of het nadenken
        self._end_turn = threading.Event()    # klaar met praten
        self._repeat = threading.Event()
        self._repeat_after = False
        self._green = False
        self._typed: queue.Queue = queue.Queue()
        self._messages: queue.Queue = queue.Queue()  # berichten van de begeleider om voor te lezen
        self._next_activity: str | None = None
        self._stop_reason = ""
        self._lock = threading.Lock()

    # --- instellingen -------------------------------------------------------------

    @property
    def config(self) -> dict:
        return self.config_store.get()

    @property
    def settings(self) -> dict:
        return self.config["hulpknop"]

    def enabled(self) -> bool:
        return bool(self.settings.get("actief"))

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _log(self, event: str, detail: str = "") -> None:
        self.logbook.write(self.clock.now(), self.session, event, "hulpgesprek", "", "", detail)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        if phase in LIGHT_FOR_PHASE:
            self.panel.set_light(LIGHT_FOR_PHASE[phase])

    # --- bediening (vanuit de gebeurtenislus of de web-API) ----------------------------

    def start(self, session: str, source: str) -> bool:
        with self._lock:
            if self.active:
                return False
            self._stop.clear()
            self._skip.clear()
            self._end_turn.clear()
            self._repeat.clear()
            self._repeat_after = False
            self._green = False
            self._next_activity = None
            self._stop_reason = ""
            self.session = session
            self.transcript = []
            self.last_answer = []
            self.last_error = None
            self.started_at = self.clock.now()
            self._set_phase(VERBINDEN)
            self._thread = threading.Thread(target=self._run, args=(source,), name="hulpgesprek", daemon=True)
            self._thread.start()
            return True

    def stop(self, reason: str = "") -> None:
        if self.active:
            self._stop_reason = reason
            self._stop.set()
            self.voice.audio.stop()

    def button(self, color: str) -> None:
        phase = self.phase
        if color == "rood":
            self.stop("rood")
        elif color in ("blauw", "groen"):
            if phase in (SPREKEN, DENKEN, VERBINDEN):
                self._skip.set()
                self.voice.audio.stop()
            elif phase == LUISTEREN:
                self._green = color == "groen"
                self._end_turn.set()
        elif color == "geel":
            if phase == LUISTEREN:
                self._repeat.set()
            elif phase == SPREKEN:
                self._repeat_after = True

    def say_text(self, text: str) -> bool:
        """Getypte invoer uit de app (testen zonder microfoon); telt als wat de gebruiker zegt."""
        text = " ".join(text.split())
        if not text or not self.active:
            return False
        self._typed.put(text)
        if self.phase in (SPREKEN, DENKEN):
            self._skip.set()
            self.voice.audio.stop()
        return True

    def caretaker_message(self, text: str) -> None:
        """Bericht van de begeleider: wordt bij de eerstvolgende gelegenheid in het gesprek voorgelezen."""
        self._messages.put(text)
        if self.phase == LUISTEREN:
            self._end_turn.set()

    def announce(self, text: str) -> None:
        """Leest een bericht voor buiten een gesprek (de slingbag is in rust)."""
        def run():
            self.voice.say(text, sound="verbonden")
            self.panel.set_light("uit")
        threading.Thread(target=run, name="bericht", daemon=True).start()

    def snapshot(self) -> dict:
        show = self.settings.get("gesprekken_bewaren")
        return {
            "actief": self.enabled(),
            "fase": self.phase,
            "gesprek": self.transcript[-12:] if show else None,
            "fout": self.last_error,
            "sleutel": self.client.available(),
            "microfoon": self.mic.available(),
            "stem": self.voice.mode(),
            "begeleider_bereikbaar": is_available(self.settings, self.clock.now()),
            "open_hulpvragen": len([r for r in self.memory.open_requests() if r["status"] == "open"]),
        }

    # --- het gesprek ----------------------------------------------------------------------

    def _interrupted(self) -> bool:
        return self._stop.is_set() or self._skip.is_set()

    def _run(self, source: str) -> None:
        ended_by = "einde"
        try:
            self.voice.reset()
            self._log("hulpgesprek_start", f"bron: {source}")
            self.voice.sound("verbonden", self._stop.is_set)
            problem = self._readiness()
            if problem:
                self._fallback_to_human(problem)
                ended_by = "geen_ai"
                return
            ended_by = self._conversation()
        except Exception as exc:  # het gesprek mag de service nooit laten vastlopen
            log.exception("Hulpgesprek mislukt")
            self.last_error = str(exc)
            try:
                self._fallback_to_human(f"onverwachte fout: {exc}")
            except Exception:
                log.exception("Terugvaloptie mislukt")
            ended_by = "fout"
        finally:
            if self._stop.is_set():
                ended_by = self._stop_reason or "gestopt"
                if self._stop_reason == "rood":
                    self.voice.audio.stop()
                    self.voice.say(self._phrase("stop"), cache=True)
            self.voice.sound("afsluiten")
            duration = (self.clock.now() - self.started_at).total_seconds() if self.started_at else 0
            user_turns = sum(1 for t in self.transcript if t["rol"] == "gebruiker")
            self._log("hulpgesprek_einde", f"{ended_by}; {int(duration)} s; {user_turns} beurten")
            transcript, started = list(self.transcript), self.started_at
            self._set_phase(UIT)
            self.voice.reset()
            self.on_ended(self._next_activity)
            if user_turns:
                threading.Thread(target=self._after_conversation, args=(transcript, started),
                                 name="geheugen", daemon=True).start()

    def _readiness(self) -> str | None:
        if not self.client.available():
            return "er is geen OpenRouter-sleutel ingesteld"
        if not self.mic.available() and not self.simulate:
            return "er is geen microfoon gevonden"
        return None

    def _phrase(self, key: str) -> str:
        formal = self.settings["gebruiker"].get("aanspreekvorm") == "u"
        name = caretaker_name(self.settings)
        phrases = {
            "stop": "Ik stop het gesprek. Tot straks.",
            "niet_verstaan": "Sorry, dat verstond ik niet goed. Wil je het nog een keer zeggen?",
            "nog_daar": "Ik luister nog. Zeg het maar rustig.",
            "stil_einde": "Ik hoor niets meer. Druk op de blauwe knop als je me weer nodig hebt. Tot straks.",
            "lang": "We praten al een tijdje. Ik stop nu. Druk op blauw als je me weer nodig hebt.",
            "storing": "Het lukt me even niet om na te denken.",
            "bericht": f"{name.capitalize() if name == 'je begeleider' else name} laat weten:",
        }
        text = phrases[key]
        if formal:
            text = (text.replace("Wil je", "Wilt u").replace("je me", "u mij").replace("je het", "u het")
                    .replace("Zeg het maar", "Zegt u het maar").replace("Druk op", "Drukt u op"))
        return text

    def _conversation(self) -> str:
        settings = self.settings
        now = self.clock.now()
        hello = prompts.greeting(settings, now)
        self._set_phase(SPREKEN)
        self.last_answer = self.voice.say(hello, cache=True, interrupted=self._interrupted)
        self._skip.clear()
        self._add_turn("assistent", hello)
        system = self._system_prompt(now, hello)
        history: list[dict] = []
        deadline = time.monotonic() + settings["gesprek_max_min"] * 60
        silent_rounds = 0

        while not self._stop.is_set():
            self._read_caretaker_messages(history)
            try:
                heard = self._listen()
            except AIUnavailable as exc:
                self._fallback_to_human(f"spraakherkenning niet bereikbaar ({exc})", in_conversation=True)
                return "geen_ai"
            if self._stop.is_set():
                break
            if heard == "__herhaal__":
                self._replay()
                continue
            if heard == "__bericht__":
                continue
            if heard is None:
                silent_rounds += 1
                self._set_phase(SPREKEN)
                if silent_rounds == 1:
                    self.voice.say(self._phrase("nog_daar"), cache=True, interrupted=self._interrupted)
                    self._skip.clear()
                    continue
                self.voice.say(self._phrase("stil_einde"), cache=True, interrupted=self._stop.is_set)
                return "stilte"
            silent_rounds = 0
            if not heard:
                self._set_phase(SPREKEN)
                self.voice.say(self._phrase("niet_verstaan"), cache=True, interrupted=self._interrupted)
                self._skip.clear()
                continue
            self._add_turn("gebruiker", heard)
            history.append({"role": "user", "content": heard})
            if self._respond(system, history):
                return "afgerond" if not self._next_activity else "activiteit"
            if time.monotonic() > deadline:
                self._set_phase(SPREKEN)
                self.voice.say(self._phrase("lang"), cache=True, interrupted=self._stop.is_set)
                return "maximale_duur"
        return "gestopt"

    def _system_prompt(self, now: datetime, hello: str) -> list[dict]:
        config = self.config
        today = [f"{entry['tijd']} {self._activity_name(entry['activiteit'])}"
                 for entry in sorted(config["planning"], key=lambda e: e["tijd"])
                 if entry.get("actief") and now.isoweekday() in entry["dagen"]]
        text = prompts.system_prompt(config, self.memory.memories(), self.memory.open_requests(), now, today)
        text += f"\nJe hebt het gesprek net geopend met: \"{hello}\""
        # Cache-markering: bij elke volgende beurt hoeft het model de lange instructie niet opnieuw te lezen.
        return [{"role": "system", "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]}]

    def _activity_name(self, activity_id: str) -> str:
        activity = self.config_store.activity(activity_id)
        return activity["naam"] if activity else activity_id

    def _add_turn(self, role: str, text: str) -> None:
        self.transcript.append({"rol": role, "tekst": text, "tijd": self.clock.now().isoformat(timespec="seconds")})

    def _read_caretaker_messages(self, history: list[dict]) -> None:
        while not self._messages.empty():
            message = self._messages.get()
            text = f"{self._phrase('bericht')} {message}"
            self._set_phase(SPREKEN)
            self.voice.say(text, sound="gehoord", interrupted=self._stop.is_set)
            self._add_turn("assistent", text)
            # Het model moet weten wat er net is voorgelezen.
            history.append({"role": "user", "content": f"(Systeem: je hebt de gebruiker net dit bericht van "
                                                        f"de begeleider voorgelezen: \"{message}\". "
                                                        f"De gebruiker heeft nog niets gezegd.)"})
            history.append({"role": "assistant", "content": text})

    def _replay(self) -> None:
        self._repeat.clear()
        self._set_phase(SPREKEN)
        self.voice.play(list(self.last_answer), self._interrupted)
        self._skip.clear()

    # --- luisteren --------------------------------------------------------------------------

    def _listen(self) -> str | None:
        """Geeft de tekst, '' (niet verstaan), None (stilte) of een interne code."""
        self._set_phase(LUISTEREN)
        self._end_turn.clear()
        self._skip.clear()
        self._green = False
        if not self._typed.empty():
            return self._typed.get()

        def control() -> str | None:
            if self._stop.is_set():
                return "stop"
            if not self._typed.empty():
                return "tekst"
            if self._repeat.is_set():
                return "herhaal"
            if not self._messages.empty():
                return "bericht"
            if self._end_turn.is_set():
                return "klaar"
            return None

        settings = self.settings
        if self.mic.available():
            endpointer = Endpointer(pause_s=settings["spreekpauze_s"], no_speech_s=settings["stilte_s"])
            turn = self.mic.listen(endpointer, control, on_speech_start=lambda: None)
        else:
            turn = self._wait_without_microphone(control, settings["stilte_s"])

        if turn.status == "afgebroken":
            return {"tekst": None, "herhaal": "__herhaal__", "bericht": "__bericht__"}.get(turn.reason) \
                if turn.reason != "tekst" else self._typed.get()
        if turn.status == "fout":
            self.last_error = turn.reason
            turn = self._wait_without_microphone(control, settings["stilte_s"])
            if turn.status == "afgebroken":
                return self._typed.get() if turn.reason == "tekst" else None
        if turn.status == "stilte":
            return "Ja." if self._green else None

        self._set_phase(DENKEN)
        self.voice.sound("gehoord")
        try:
            return self.client.transcribe(turn.wav)
        except AIUnavailable as exc:
            self.last_error = str(exc)
            raise

    def _wait_without_microphone(self, control, seconds: float):
        """Zonder microfoon (testmodus): wacht op getypte tekst uit de app of een knop."""
        from .microfoon import Turn
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            command = control()
            if command == "klaar":
                return Turn("stilte")
            if command:
                return Turn("afgebroken", reason=command)
            time.sleep(0.05)
        return Turn("stilte")

    # --- antwoorden -------------------------------------------------------------------------

    def _respond(self, system: list[dict], history: list[dict]) -> bool:
        """Laat het model antwoorden (met tools). Geeft True als het gesprek moet stoppen."""
        for _ in range(MAX_TOOL_ROUNDS):
            self._set_phase(DENKEN)
            self._skip.clear()
            self._repeat_after = False
            utterance = self.voice.utterance(self._interrupted)
            began = time.monotonic()
            first: dict = {}

            def add_sentence(sentence: str) -> None:
                if "zin" not in first:
                    first["zin"] = time.monotonic() - began
                    log.info("Eerste zin na %.2f s: %s", first["zin"], sentence[:60])
                utterance.add(sentence)

            splitter = SentenceSplitter(add_sentence)

            def on_text(chunk: str) -> None:
                if "token" not in first:
                    first["token"] = time.monotonic() - began
                    log.info("Eerste tekst van het model na %.2f s", first["token"])
                splitter.feed(chunk)
                if utterance.started_speaking.is_set():
                    self._set_phase(SPREKEN)

            try:
                result = self.client.chat_stream(system + history, prompts.TOOLS, on_text=on_text,
                                                 should_stop=self._interrupted)
            except AIUnavailable as exc:
                utterance.finish()
                utterance.wait()
                self.last_error = str(exc)
                log.warning("Model niet bereikbaar: %s", exc)
                self._fallback_to_human(f"AI niet bereikbaar tijdens gesprek ({exc})", in_conversation=True)
                return True
            splitter.flush()
            utterance.finish()
            if result.text.strip():
                self._add_turn("assistent", result.text.strip())
            message = {"role": "assistant", "content": result.text or ""}
            if result.tool_calls:
                message["tool_calls"] = result.tool_calls
            history.append(message)

            end = False
            needs_followup = False
            for call in result.tool_calls:
                outcome, stop_after, followup = self._run_tool(call, spoke=bool(result.text.strip()))
                history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(outcome, ensure_ascii=False)})
                end = end or stop_after
                needs_followup = needs_followup or followup
            self._phase_while_waiting(utterance)
            self.last_answer = list(utterance.paths) or self.last_answer
            if self._repeat_after and not self._stop.is_set():
                self._replay()
            if self._skip.is_set():
                # Onderbroken: de gebruiker wil iets zeggen. Wat al gezegd is staat in de geschiedenis.
                self._skip.clear()
                return end
            if end or not needs_followup:
                return end
        return False

    def _phase_while_waiting(self, utterance) -> None:
        while utterance._player.is_alive():
            if utterance.started_speaking.is_set():
                self._set_phase(SPREKEN)
            utterance._player.join(0.1)
        utterance.wait()

    def _run_tool(self, call: dict, spoke: bool) -> tuple[dict, bool, bool]:
        """Voert een tool uit. Geeft (resultaat, gesprek stoppen, model moet nog reageren)."""
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        if name == "verbind_met_begeleider":
            return self._contact_caretaker(str(args.get("reden", "")), bool(args.get("spoed"))), False, True
        if name == "start_activiteit":
            activity = self.config_store.activity(str(args.get("activiteit_id", "")))
            if activity is None:
                return {"ok": False, "fout": "Deze activiteit bestaat niet."}, False, True
            self._next_activity = activity["id"]
            self._log("hulp_activiteit", activity["id"])
            if not spoke:
                self.voice.say(f"Goed. We gaan samen {activity['naam']}. Luister naar de eerste stap.",
                               interrupted=self._stop.is_set)
            return {"ok": True}, True, False
        if name == "gesprek_beeindigen":
            return {"ok": True}, True, False
        return {"ok": False, "fout": f"Onbekende tool {name}."}, False, True

    def _contact_caretaker(self, reason: str, urgent: bool) -> dict:
        settings = self.settings
        now = self.clock.now()
        available = is_available(settings, now)
        name = caretaker_name(settings)
        request = self.memory.add_help_request(reason, urgent, available)
        pushed = self.notifier.send(urgent) if (available or urgent) else False
        self._log("hulp_begeleider", f"{'spoed' if urgent else 'geen spoed'}; "
                                     f"{'bereikbaar' if available else 'niet bereikbaar'}; "
                                     f"push {'ja' if pushed else 'nee'}")
        result = {
            "begeleider": name,
            "bereikbaar": available,
            "hulpvraag_id": request["id"],
            "melding_in_app": True,
            "pushmelding_verstuurd": pushed,
        }
        if available:
            result["wat_je_kunt_zeggen"] = (
                f"{name} heeft een bericht gekregen en ziet het zo snel mogelijk. Je weet nog niet of "
                f"en wanneer {name} reageert; dat hoort de gebruiker via de slingbag zodra {name} antwoordt.")
        else:
            nxt = next_available(settings, now)
            result["weer_bereikbaar"] = describe_moment(nxt, now) if nxt else "onbekend"
            result["wat_je_kunt_zeggen"] = (
                f"{name} is nu niet bereikbaar. Het bericht staat klaar en {name} ziet het later.")
        if urgent:
            result["noodplan"] = settings["gebruiker"].get("noodplan") or "Bel 112 bij direct gevaar."
        return result

    def _fallback_to_human(self, problem: str, in_conversation: bool = False) -> None:
        """De AI kan niet helpen (geen sleutel, internet of microfoon): waarschuw een mens en zeg dat eerlijk."""
        settings = self.settings
        now = self.clock.now()
        available = is_available(settings, now)
        name = caretaker_name(settings)
        self.memory.add_help_request(f"Hulpknop ingedrukt, maar {problem}.", False, available)
        pushed = self.notifier.send(False) if available else False
        self._log("hulp_terugval", f"{problem}; {'bereikbaar' if available else 'niet bereikbaar'}; "
                                   f"push {'ja' if pushed else 'nee'}")
        lead = self._phrase("storing") if in_conversation else "Ik kan je nu niet goed helpen."
        if available:
            text = f"{lead} Ik heb {name} een bericht gestuurd."
        else:
            nxt = next_available(settings, now)
            text = f"{lead} {name.capitalize() if name == 'je begeleider' else name} is nu niet bereikbaar"
            text += f", wel weer {describe_moment(nxt, now)}." if nxt else "."
        text += f" {settings['gebruiker'].get('noodplan') or 'Bel 112 bij direct gevaar.'}"
        self._set_phase(SPREKEN)
        self._add_turn("assistent", text)
        self.voice.say(text, interrupted=self._stop.is_set)

    # --- na het gesprek -----------------------------------------------------------------------

    def _after_conversation(self, transcript: list[dict], started: datetime) -> None:
        settings = self.settings
        if settings.get("gesprekken_bewaren"):
            self.memory.save_conversation(started, transcript, settings["bewaartermijn_dagen"])
        if not settings.get("geheugen_bijwerken", True) or not self.client.available():
            return
        try:
            answer = self.client.chat([{"role": "user", "content": prompts.memory_prompt(
                self.memory.memories(), transcript, self.clock.now())}], json_mode=True)
            counts = self.memory.apply_changes(prompts.parse_json(answer))
            self.logbook.write(self.clock.now(), self.session, "geheugen_bijgewerkt", "rust", "", "",
                               ", ".join(f"{v} {k}" for k, v in counts.items()))
        except AIUnavailable as exc:
            log.warning("Geheugen bijwerken mislukt: %s", exc)
