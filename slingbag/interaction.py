"""Interactiecontroller: de toestandsmachine van de activiteitenhulp (FR-02 t/m FR-06, FR-09, TR-05).

Vier knoppen krijgen per toestand een betekenis:
    groen = ja / volgende   geel = later / herhaal   blauw = hulp / pauze   rood = nee / stop
De controller wordt alleen vanuit de gebeurtenislus van de service aangeroepen;
daardoor verwerkt hij altijd één gebeurtenis tegelijk.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta
from typing import Callable

from .speech import SpeechUnavailable
from .texts import render

RUST = "rust"
UITNODIGING = "uitnodiging"
KEUZE = "keuze"
ZELFSTANDIG = "zelfstandig"
BEGELEIDING = "begeleiding"
PAUZE = "pauze"
AFRONDEN = "afronden"

LIGHT_FOR_STATE = {
    RUST: "uit", UITNODIGING: "knipperen", KEUZE: "aan", ZELFSTANDIG: "aan",
    BEGELEIDING: "aan", PAUZE: "pulseren", AFRONDEN: "aan",
}
PRESS_LOCKOUT_S = 0.3  # één druk = één keuze, ook bij snel herhaald drukken


class SystemClock:
    def now(self) -> datetime:
        return datetime.now()

    def monotonic(self) -> float:
        return time.monotonic()


def session_items(config: dict, activity: dict) -> list:
    """Alle teksten en opnames die een sessie van deze activiteit kan uitspreken."""
    texts = config.get("teksten", {})
    name = activity["naam"]
    minutes = config["tijden"]["uitstel_min"]
    items = [activity["uitnodiging"], *activity["stappen"],
             activity.get("afsluiting") or render(texts, "afgerond")]
    for key in ("knoppen_uitnodiging", "keuze_hulp", "zelfstandig", "knoppen_stap", "pauze",
                "hervat", "laatste_stap", "weigering", "stop", "afgerond"):
        items.append(render(texts, key))
    items.append(render(texts, "afronden_vraag", activiteit=name))
    items.append(render(texts, "uitstel", minuten=minutes))
    return [i for i in items if i and (isinstance(i, str) or i.get("tekst") or i.get("opname"))]


class InteractionController:
    def __init__(self, config_store, speech, audio, panel, logbook,
                 clock=None, notify_audio_done: Callable[[object], None] | None = None):
        self.config_store = config_store
        self.speech = speech
        self.audio = audio
        self.panel = panel
        self.logbook = logbook
        self.clock = clock or SystemClock()
        self.notify_audio_done = notify_audio_done or (lambda token: None)

        # Na inschakelen altijd in rust, zonder een oude aanwijzing te herhalen (FR-09).
        self.state = RUST
        self.activity: dict | None = None
        self.step = 0
        self.session = ""
        self.source = ""
        self.paused_from = BEGELEIDING
        self.before_finish = ZELFSTANDIG
        self.postponed: tuple[str, datetime] | None = None
        self.error: str | None = None
        self._token: object = None
        self._pending_wait: tuple[float, Callable[[], None]] | None = None
        self._deadline: tuple[float, Callable[[], None]] | None = None
        self._last_press = -1.0
        self._explained_steps = False
        self._update_light()

    # --- hulpfuncties ---------------------------------------------------------

    @property
    def config(self) -> dict:
        return self.config_store.get()

    def _text(self, key: str, **values) -> str:
        return render(self.config.get("teksten", {}), key, **values)

    def _log(self, event: str, detail: str = "") -> None:
        self.logbook.write(
            self.clock.now(), self.session, event, self.state,
            self.activity["id"] if self.activity else "",
            self.step + 1 if self.state in (BEGELEIDING, PAUZE) else "", detail,
        )

    def _update_light(self) -> None:
        pattern = LIGHT_FOR_STATE[self.state]
        if self.state == RUST and self.error:
            pattern = "fout"
        self.panel.set_light(pattern)

    def _set_state(self, state: str) -> None:
        self.state = state
        self._deadline = None
        self._pending_wait = None
        self._update_light()

    def _say(self, items: list, wait_s: float | None = None,
             on_timeout: Callable[[], None] | None = None, chime: bool = False) -> None:
        """Speelt teksten/opnames af; de wachttijd gaat pas in als de spraak klaar is."""
        config = self.config
        paths = [self.speech.chime_path] if chime and config.get("aandachtsgeluid") else []
        for item in items:
            try:
                paths.append(self.speech.resolve(item, config["stem"]))
            except SpeechUnavailable as exc:
                self._log("fout", str(exc))
        self._token = object()
        self._deadline = None
        self._pending_wait = (wait_s, on_timeout) if wait_s and on_timeout else None
        self.audio.play(paths, self._token, self.notify_audio_done)

    def _minutes(self, key: str) -> float:
        return self.config["tijden"][key] * 60

    def _session_timeout(self) -> None:
        self._end("time-out", "geen knopdruk binnen de sessieduur")

    def _end(self, event: str, detail: str = "") -> None:
        self._log(event, detail)
        self._set_state(RUST)
        self.activity = None
        self.session = ""
        self.step = 0

    # --- invoer van buitenaf ----------------------------------------------------

    def audio_finished(self, token: object) -> None:
        if token is self._token and self._pending_wait:
            seconds, action = self._pending_wait
            self._pending_wait = None
            self._deadline = (self.clock.monotonic() + seconds, action)

    def tick(self) -> None:
        if self._deadline and self.clock.monotonic() >= self._deadline[0]:
            action = self._deadline[1]
            self._deadline = None
            action()
        if self.postponed and self.clock.now() >= self.postponed[1]:
            activity_id, _ = self.postponed
            self.postponed = None
            if self.state == RUST:
                self.invite(activity_id, "uitstel")
            else:
                self._log("uitstel_vervallen", "er liep al een sessie")

    def invite(self, activity_id: str, source: str) -> tuple[bool, str]:
        """Start een uitnodiging; tijdens een sessie start geen tweede (FR-02)."""
        if self.state != RUST:
            self.logbook.write(self.clock.now(), self.session, "uitnodiging_overgeslagen", self.state,
                               activity_id, "", f"bron: {source}; sessie actief")
            return False, "Er loopt al een sessie."
        activity = self.config_store.activity(activity_id)
        if activity is None:
            return False, "Deze activiteit bestaat niet."
        problem = self._preflight(activity)
        if problem:
            self.error = f"{activity['naam']}: {problem}"
            self.logbook.write(self.clock.now(), "", "fout", RUST, activity_id, "", self.error)
            self._update_light()
            return False, self.error
        self.error = None
        if source != "uitstel":
            self.postponed = None
        self.activity = activity
        self.session = secrets.token_hex(3)
        self.source = source
        self.step = 0
        self._explained_steps = False
        self._set_state(UITNODIGING)
        self._log("uitnodiging", f"bron: {source}")
        self._speak_invitation(chime=True)
        return True, "Uitnodiging gestart."

    def _preflight(self, activity: dict) -> str | None:
        """Controleert vóór de start of alle benodigde audio er is (FR-09, TR-10)."""
        voice = self.config["stem"]
        for item in session_items(self.config, activity):
            try:
                self.speech.resolve(item, voice)
            except (SpeechUnavailable, OSError) as exc:
                return str(exc)
        return None

    def clear_error(self) -> None:
        """Na het opslaan van nieuwe instellingen opnieuw proberen."""
        self.error = None
        self._update_light()

    def stop(self, detail: str = "") -> None:
        """Stoppen kan altijd, ook tijdens spraak, en wist een gepland uitstel (FR-06)."""
        if self.state == RUST and not self.postponed:
            self.audio.stop()
            return
        self.postponed = None
        self.audio.stop()
        if self.state != RUST:
            self._say([self._text("stop")])
            self._end("gestopt", detail)
        else:
            self._log("uitstel_gewist", detail)

    def press(self, button: str) -> None:
        now = self.clock.monotonic()
        if now - self._last_press < PRESS_LOCKOUT_S:
            return
        self._last_press = now
        handler = getattr(self, f"_press_{self.state}", None)
        if handler:
            handler(button)

    # --- per toestand -----------------------------------------------------------

    def _answer_wait(self) -> float:
        return self.config["tijden"]["antwoord_wachttijd_s"]

    def _speak_invitation(self, chime: bool = False) -> None:
        items = [self.activity["uitnodiging"]]
        if self.config.get("knopuitleg"):
            items.append(self._text("knoppen_uitnodiging"))
        self._say(items, self._answer_wait(), self._invitation_timeout, chime=chime)

    def _invitation_timeout(self) -> None:
        # Zonder antwoord begint geen activiteit (FR-03).
        self._end("geen_antwoord")

    def _press_rust(self, button: str) -> None:
        if button == "groen":
            activity_id = self._default_activity_id()
            if activity_id:
                self.invite(activity_id, "gebruiker")
        elif button == "rood":
            self.audio.stop()

    def _default_activity_id(self) -> str | None:
        from .scheduler import Planner
        config = self.config
        upcoming = Planner.next_moment(config["planning"], self.clock.now())
        if upcoming:
            return upcoming[1]["activiteit"]
        return config["activiteiten"][0]["id"] if config["activiteiten"] else None

    def _press_uitnodiging(self, button: str) -> None:
        if button == "groen":
            self._log("keuze", "beginnen")
            self._set_state(KEUZE)
            self._say([self._text("keuze_hulp")], self._answer_wait(), self._start_independent)
        elif button == "geel":
            if self.source == "uitstel":
                # Maximaal één nieuwe uitnodiging na uitstel (TR-06).
                self._say([self._text("weigering")])
                self._end("keuze", "later, tweede keer: geen nieuwe uitnodiging")
                return
            minutes = self.config["tijden"]["uitstel_min"]
            self.postponed = (self.activity["id"], self.clock.now() + timedelta(minutes=minutes))
            self._say([self._text("uitstel", minuten=minutes)])
            self._end("keuze", f"later, over {minutes} min")
        elif button == "rood":
            self.postponed = None
            self._say([self._text("weigering")])
            self._end("keuze", "nee")
        elif button == "blauw":
            # Beginnen met hulp: meteen de eerste stap.
            self._log("keuze", "beginnen met hulp")
            self._go_to_step(0)

    def _start_independent(self) -> None:
        self._set_state(ZELFSTANDIG)
        self._say([self._text("zelfstandig")], self._minutes("sessie_max_min"), self._session_timeout)

    def _press_keuze(self, button: str) -> None:
        if button == "groen":
            self._log("keuze", "zelf doen")
            self._start_independent()
        elif button == "blauw":
            self._log("keuze", "hulp")
            self._go_to_step(0)
        elif button == "geel":
            self._log("herhaal")
            self._say([self._text("keuze_hulp")], self._answer_wait(), self._start_independent)
        elif button == "rood":
            self.stop("rood")

    def _press_zelfstandig(self, button: str) -> None:
        if button == "blauw":
            self._log("hulp_gevraagd")
            self._go_to_step(0)
        elif button == "groen":
            self._ask_finished(ZELFSTANDIG, [])
        elif button == "geel":
            self._log("herhaal")
            self._start_independent()
        elif button == "rood":
            self.stop("rood")

    def _go_to_step(self, index: int, prefix: list | None = None) -> None:
        """Speelt één stap; de volgende stap komt pas na een bewuste keuze (FR-04)."""
        self.step = index
        self._set_state(BEGELEIDING)
        self._log("stap")
        items = list(prefix or [])
        items.append(self.activity["stappen"][index])
        if self.config.get("knopuitleg") and not self._explained_steps:
            items.append(self._text("knoppen_stap"))
            self._explained_steps = True
        self._say(items, self._minutes("sessie_max_min"), self._session_timeout)

    def _press_begeleiding(self, button: str) -> None:
        if button == "groen":
            if self.step + 1 < len(self.activity["stappen"]):
                self._go_to_step(self.step + 1)
            else:
                self._ask_finished(BEGELEIDING, [self._text("laatste_stap")])
        elif button == "geel":
            # Herhalen gaat niet door naar de volgende stap (FR-05).
            self._log("herhaal")
            self._go_to_step(self.step)
        elif button == "blauw":
            self._log("pauze")
            self.paused_from = BEGELEIDING
            self._set_state(PAUZE)
            self._say([self._text("pauze")], self._minutes("pauze_max_min"), self._pause_timeout)
        elif button == "rood":
            self.stop("rood")

    def _press_pauze(self, button: str) -> None:
        if button in ("blauw", "groen"):
            # Hervatten behoudt de stap (FR-06).
            self._log("hervat")
            self._go_to_step(self.step, prefix=[self._text("hervat")])
        elif button == "geel":
            self._say([self._text("pauze")], self._minutes("pauze_max_min"), self._pause_timeout)
        elif button == "rood":
            self.stop("rood")

    def _ask_finished(self, came_from: str, prefix: list) -> None:
        # De gebruiker bevestigt zelf de afronding (FR-04).
        self.before_finish = came_from
        self._set_state(AFRONDEN)
        self._say([*prefix, self._text("afronden_vraag", activiteit=self.activity["naam"])],
                  self._answer_wait(), self._back_from_finish)

    def _back_from_finish(self) -> None:
        # Geen antwoord op de afrondvraag: stil terug naar waar de gebruiker was.
        self._set_state(self.before_finish)
        self._deadline = (self.clock.monotonic() + self._minutes("sessie_max_min"), self._session_timeout)

    def _pause_timeout(self) -> None:
        self._end("time-out", "pauze te lang")

    def _press_afronden(self, button: str) -> None:
        if button == "groen":
            closing = self.activity.get("afsluiting") or self._text("afgerond")
            if isinstance(closing, dict) and not (closing.get("tekst") or closing.get("opname")):
                closing = self._text("afgerond")
            self._say([closing])
            self._end("afgerond")
        elif button == "geel":
            self._log("herhaal")
            self._say([self._text("afronden_vraag", activiteit=self.activity["naam"])],
                      self._answer_wait(), self._back_from_finish)
        elif button == "blauw":
            # Nog niet klaar: hulp bij de huidige stap, of vanaf stap 1 na zelfstandig werken.
            self._log("nog_niet_klaar")
            self._go_to_step(self.step if self.before_finish == BEGELEIDING else 0)
        elif button == "rood":
            self.stop("rood")

    # --- status voor de app -----------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "toestand": self.state,
            "activiteit": self.activity["naam"] if self.activity else None,
            "activiteit_id": self.activity["id"] if self.activity else None,
            "stap": self.step + 1 if self.state in (BEGELEIDING, PAUZE) else None,
            "stappen": len(self.activity["stappen"]) if self.activity else None,
            "sessie": self.session or None,
            "uitstel_tot": self.postponed[1].isoformat(timespec="minutes") if self.postponed else None,
            "fout": self.error,
            "lamp": getattr(self.panel, "light", None),
        }
