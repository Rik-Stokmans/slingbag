"""Knoppen- en lampadapter: koppelt de GPIO-pinnen van de Pi aan de software (TR-04).

Aansluiting (BCM-nummers, fysieke pin tussen haakjes):
    groen GPIO17 (11) · geel GPIO27 (13) · blauw GPIO22 (15) · rood GPIO23 (16)
    statuslampje GPIO24 (18) via 330 Ω · GND (9, 14, 20 of 6)
Elke knop zit tussen zijn GPIO-pin en GND; de interne pull-up houdt de pin hoog
tot de knop wordt ingedrukt. Er is dus geen externe weerstand nodig voor de knoppen.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

BUTTON_PINS = {"groen": 17, "geel": 27, "blauw": 22, "rood": 23}
LED_PIN = 24
SHUTDOWN_HOLD_S = 5  # groen en rood tegelijk vasthouden = veilig afsluiten (voor de begeleider)

# Lamppatronen per toestand: (aan-tijd, uit-tijd) in seconden; None = continu.
LIGHT_PATTERNS = {
    "uit": None,
    "aan": None,
    "knipperen": (0.6, 0.6),   # uitnodiging wacht op een antwoord
    "pulseren": (1.5, 1.5),    # pauze
    "fout": (0.1, 0.1),        # instellingen of audio ontbreken
}


class SimulatedPanel:
    """Zonder GPIO (bijvoorbeeld op de Mac) komen knopdrukken uit het testpaneel in de app."""

    simulated = True

    def __init__(self, on_press: Callable[[str], None], on_shutdown: Callable[[], None]):
        self.on_press = on_press
        self.light = "uit"

    def set_light(self, pattern: str) -> None:
        self.light = pattern

    def close(self) -> None:
        pass


class GpioPanel:
    simulated = False

    def __init__(self, on_press: Callable[[str], None], on_shutdown: Callable[[], None]):
        from gpiozero import Button, PWMLED

        self.on_press = on_press
        self.on_shutdown = on_shutdown
        self.light = "uit"
        self.buttons = {}
        for name, pin in BUTTON_PINS.items():
            # bounce_time vangt contactdender af; de interactielogica heeft daarnaast een korte vergrendeling.
            button = Button(pin, pull_up=True, bounce_time=0.05, hold_time=SHUTDOWN_HOLD_S)
            button.when_pressed = lambda _b=None, n=name: self.on_press(n)
            button.when_held = self._held
            self.buttons[name] = button
        self.led = PWMLED(LED_PIN)

    def _held(self) -> None:
        if self.buttons["groen"].is_held and self.buttons["rood"].is_held:
            log.info("Groen en rood vastgehouden: afsluiten")
            self.on_shutdown()

    def set_light(self, pattern: str) -> None:
        if pattern == self.light:
            return
        self.light = pattern
        if pattern == "uit":
            self.led.off()
        elif pattern == "aan":
            self.led.value = 0.6
        elif pattern == "pulseren":
            on, off = LIGHT_PATTERNS[pattern]
            self.led.pulse(fade_in_time=on, fade_out_time=off)
        else:
            on, off = LIGHT_PATTERNS[pattern]
            self.led.blink(on_time=on, off_time=off)

    def close(self) -> None:
        self.led.off()
        for button in self.buttons.values():
            button.close()
        self.led.close()


def create_panel(on_press, on_shutdown, simulate: bool):
    if simulate:
        return SimulatedPanel(on_press, on_shutdown)
    try:
        return GpioPanel(on_press, on_shutdown)
    except Exception as exc:  # gpiozero ontbreekt of er is geen GPIO-chip
        log.warning("GPIO niet beschikbaar (%s); val terug op het testpaneel.", exc)
        return SimulatedPanel(on_press, on_shutdown)
