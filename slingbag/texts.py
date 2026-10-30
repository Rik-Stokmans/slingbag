"""Vaste zinnen die de slingbag uitspreekt naast de teksten van een activiteit.

De begeleider kan iedere zin overschrijven via ``teksten`` in de configuratie.
Accolades worden ingevuld met ``str.format``: {activiteit} en {minuten}.
"""

DEFAULT_TEXTS = {
    "knoppen_uitnodiging": "Druk op groen om te beginnen. Geel is later. "
                           "Blauw is beginnen met hulp. Rood is nee.",
    "keuze_hulp": "Wil je het zelf doen, of wil je hulp bij de stappen? "
                  "Groen is zelf doen. Blauw is hulp.",
    "zelfstandig": "Fijn. Wil je toch hulp, druk dan op blauw. "
                   "Ben je klaar, druk dan op groen.",
    "knoppen_stap": "Groen is de volgende stap. Geel is herhalen. "
                    "Blauw is pauze. Rood is stoppen.",
    "pauze": "We pauzeren even. Druk op blauw als je verder wilt.",
    "hervat": "We gaan verder.",
    "laatste_stap": "Dat was de laatste stap.",
    "afronden_vraag": "Ben je klaar met {activiteit}? Groen is ja. "
                      "Blauw is nog niet, dan help ik je verder.",
    "uitstel": "Goed, ik vraag het over {minuten} minuten nog een keer.",
    "weigering": "Dat is goed. Dan doen we het een andere keer.",
    "stop": "Ik stop. Tot later.",
    "afgerond": "Goed gedaan!",
}


def render(texts: dict, key: str, **values) -> str:
    """Geeft de ingevulde tekst voor ``key``; eigen teksten gaan voor standaardteksten."""
    template = texts.get(key) or DEFAULT_TEXTS[key]
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        # Een verkeerd ingevulde accolade mag de sessie niet laten vastlopen.
        return template
