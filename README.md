# Slingbag

[![tests](https://github.com/Rik-Stokmans/slingbag/actions/workflows/tests.yml/badge.svg?branch=testing)](https://github.com/Rik-Stokmans/slingbag/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/Rik-Stokmans/slingbag?label=release)](https://github.com/Rik-Stokmans/slingbag/releases/latest)

Software voor het slingbag-prototype: een activiteitenhulp voor mensen met dementie. De slingbag nodigt op vaste momenten uit tot een dagelijkse activiteit, zoals koffiezetten, en begeleidt die stap voor stap met gesproken aanwijzingen. De gebruiker antwoordt met vier gekleurde knoppen. De begeleider stelt alles in via een web-app op de telefoon die de Pi zelf aanbiedt.

Het prototype draait op een Raspberry Pi 3B met een breadboard, vier knoppen, een statuslampje en een actieve speaker. De spraak is offline (Piper, Nederlandse stemmen) en kan per tekst vervangen worden door een eigen opname. Dit is een studentenproject van Fontys Hogescholen.

**Nieuwste versie:** [laatste release](https://github.com/Rik-Stokmans/slingbag/releases/latest)

## Snel installeren

Op een Raspberry Pi 3B met Raspberry Pi OS Lite (64-bit) en internet (zie [SD-kaart voorbereiden](#eerste-keer-sd-kaart-voorbereiden)):

```sh
ssh rik@slingbag.local
curl -fsSL https://github.com/Rik-Stokmans/slingbag/releases/latest/download/install.sh | bash
```

Hetzelfde commando werkt de software later bij naar de nieuwste release.

## Mappen en C4-componenten

| Pad | C4-component | Requirements |
| --- | --- | --- |
| `slingbag/service.py` | Slingbag-service: gebeurtenislus die alles verbindt | TR-01, TR-05 |
| `slingbag/interaction.py` | Interactiecontroller (toestandsmachine) | FR-02–FR-06, FR-09, TR-05 |
| `slingbag/scheduler.py` | Planner | FR-02, TR-06 |
| `slingbag/hardware.py` | Knoppen- en lampadapter (GPIO) | TR-04 |
| `slingbag/audio.py` | Audiospeler (ALSA, 3,5mm) | FR-01, FR-06, TR-03 |
| `slingbag/speech.py` | Spraakbibliotheek (Piper + eigen opnames) | FR-01, FR-07, TR-02 |
| `slingbag/config.py` | Instellingenopslag en controle | FR-07, FR-09, TR-07, TR-10 |
| `slingbag/logbook.py` | Interactielogboek (CSV) | testen per requirement |
| `slingbag/api.py` | Web-API (HTTP/JSON) | FR-07, TR-07 |
| `web/` | Begeleidersapp (web-app voor de telefoon) | FR-07 |
| `vendor/` | Piper-engine (Linux arm64) en drie Nederlandse stemmen; niet in de repository, `deploy/download-vendor.sh` haalt ze op en controleert ze met `SHA256SUMS` | FR-01, FR-08 |
| `install.sh` | Installatie vanaf de nieuwste release | — |
| `deploy/` | Installatie op de Pi, deployscript voor ontwikkelen, downloadscript voor de stemmen | — |
| `tests/` | Tests per requirement | — |

## Wat heb je nodig (ISSD)

| Onderdeel | Aantal | Opmerking |
| --- | --- | --- |
| Raspberry Pi 3B + microSD (≥ 16 GB) | 1 | al aanwezig |
| USB-microSD-kaartlezer | 1 | als je computer geen SD-slot heeft |
| Voeding 5 V / 2,5 A met micro-USB | 1 | officiële Pi 3-voeding; powerbank ≥ 2,4 A voor draagbaar gebruik |
| Breadboard (830 of 400 gaatjes) | 1 | |
| Drukknop (tactile switch 6×6 of 12×12 mm) | 4 | liefst met kapjes groen, geel, blauw en rood; anders gekleurde stickers |
| LED 5 mm (wit of geel) | 1 | statuslampje |
| Weerstand 330 Ω (220–470 Ω mag) | 1 | voor de LED; de knoppen hebben geen weerstand nodig |
| Jumperdraden male-female | 6 | van de Pi-header naar het breadboard |
| Jumperdraden male-male | ±8 | op het breadboard |
| Actieve speaker met 3,5mm-jack | 1 | met eigen versterker; gevoed via USB of eigen accu |
| Ethernetkabel (RJ45) | 1 | alleen nodig als de Pi niet via wifi bereikbaar is |

## Aansluiten

Zet de Pi **uit** voordat je iets aansluit. Pinnummers zijn de fysieke pinnen op de 40-pins header (pin 1 is het vierkante soldeereilandje, aan de kant van de microSD-kaart).

| Onderdeel | GPIO (BCM) | Fysieke pin | Naar |
| --- | --- | --- | --- |
| Knop groen (ja / volgende) | GPIO17 | 11 | andere poot knop → GND-rail |
| Knop geel (later / herhaal) | GPIO27 | 13 | andere poot knop → GND-rail |
| Knop blauw (hulp / pauze) | GPIO22 | 15 | andere poot knop → GND-rail |
| Knop rood (nee / stop) | GPIO23 | 16 | andere poot knop → GND-rail |
| Statuslampje | GPIO24 | 18 | 330 Ω → lange poot LED (anode); korte poot (kathode) → GND-rail |
| GND | — | 9 (of 6, 14, 20) | blauwe min-rail van het breadboard |
| Speaker | — | 3,5mm-jack | aux-kabel; speaker heeft eigen voeding |

Plaats elke knop over de middengleuf van het breadboard en gebruik twee **diagonaal tegenover elkaar liggende** pootjes. Dan zit je altijd over het schakelende contact. De software zet de interne pull-up aan: de pin staat hoog tot de knop hem met GND verbindt.

## Eerste keer: SD-kaart voorbereiden

1. Installeer [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (macOS: `brew install --cask raspberry-pi-imager`).
2. Kies **Raspberry Pi 3**, **Raspberry Pi OS Lite (64-bit)** en de microSD-kaart (via de kaartlezer).
3. Stel bij *OS-aanpassingen* in:
   - hostnaam `slingbag`
   - gebruiker `rik` met een eigen wachtwoord
   - wifi: je thuisnetwerk, land **NL** (nodig voor internet bij het installeren)
   - tijdzone `Europe/Amsterdam`
   - **SSH aan**, *alleen publieke sleutel*: de sleutel van je computer (`~/.ssh/id_ed25519.pub`) wordt automatisch voorgesteld
4. Schrijf de kaart, steek hem in de Pi en sluit de voeding aan. Na de eerste opstart (±2 minuten) is de Pi in je wifinetwerk bereikbaar als `slingbag.local`. Daarna volg je [Snel installeren](#snel-installeren).

## Installeren of bijwerken

**Vanaf de nieuwste release** (aanbevolen): voer op de Pi het commando uit [Snel installeren](#snel-installeren) uit.

**Vanaf een werkkopie** (voor ontwikkelen): vanaf je computer kopieert `deploy/deploy.sh` de lokale bestanden naar de Pi en installeert ze.

```sh
./deploy/deploy.sh                       # standaard rik@slingbag.local
PI=rik@192.168.1.50 ./deploy/deploy.sh   # ander adres
JUMP=rik@laptop ./deploy/deploy.sh       # via een laptop waar de Pi met een kabel aan hangt
```

Is de Pi niet via wifi bereikbaar, verbind hem dan met een Ethernetkabel met de router of rechtstreeks met je computer en controleer de verbinding met `ping slingbag.local`.

Beide manieren voeren `deploy/install-pi.sh` uit. Dat installeert de pakketten (Flask, gpiozero, ALSA, ffmpeg), haalt Piper en de stemmen op, maakt de systemd-service `slingbag` en een terugval-wifinetwerk aan. Dat laatste is alleen actief als er geen bekend netwerk in de buurt is: **Slingbag**, wachtwoord `slingbag-demo`. De eerste keer duurt dit ongeveer 10 minuten; bijwerken daarna ongeveer een minuut.

## De app gebruiken

- Thuis: open `http://slingbag.local` op een telefoon in hetzelfde wifinetwerk.
- Onderweg/demo: verbind de telefoon met het wifinetwerk **Slingbag** en open `http://10.42.0.1`.
- Zet de pagina via *Deel → Zet op beginscherm* (iPhone) of *Toevoegen aan startscherm* (Android) op het beginscherm.
- Eigen opname: neem eerst op met *Dictafoon*/*Recorder* en kies het bestand via **🎙 Opname**. Opnemen in de browser zelf vereist HTTPS en werkt daarom niet in dit lokale netwerk.
- De app zet de klok van de Pi gelijk met de telefoon als die meer dan een minuut afwijkt (de Pi 3B heeft geen klokbatterij).
- Uitzetten: knop in de app, of groen + rood samen 5 seconden vasthouden. Wacht 20 seconden voordat je de stroom loskoppelt.

## Ontwikkelen en testen

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                          # tests per requirement
.venv/bin/python -m slingbag --simulatie --poort 8080  # open http://localhost:8080
```

In de simulatie komen knopdrukken uit het testpaneel van de app en speelt de computer het geluid af (macOS: `afplay`). Voor spraak in de simulatie haal je eerst de stemmen op met `./deploy/download-vendor.sh`.

## Branches, versies en releases

| Branch | Doel | Regels | Release |
| --- | --- | --- | --- |
| `testing` | Ontwikkelen en testen | Direct committen mag. | Elke push: tests, daarna pre-release `v<versie>-beta.<n>` |
| `main` | Vrijgegeven versie | Alleen via een pull request vanuit `testing`; de checks `tests`, `bronbranch` en `versie` moeten slagen. | Elke merge: tests, daarna release `v<versie>` als *latest* |

Versienummers volgen [Semantic Versioning](https://semver.org/lang/nl/): `MAJOR.MINOR.PATCH` in `slingbag/__init__.py`.

- **PATCH** (0.2.0 → 0.2.1): foutherstel zonder gedragsverandering voor de gebruiker.
- **MINOR** (0.2.0 → 0.3.0): nieuwe functie of ander knopgedrag; bestaande instellingen blijven werken.
- **MAJOR** (0.x → 1.0.0): instellingenbestand of API verandert zo dat oude instellingen niet meer werken. Zolang de versie met 0 begint, is de software nog in ontwikkeling.

Werkwijze:

```sh
git switch testing
# Eerste wijziging na een release: versie ophogen in slingbag/__init__.py
# wijzigen, testen, committen
git push                                    # → v0.3.0-beta.1, v0.3.0-beta.2, ...
gh pr create --base main --head testing --fill
gh pr merge --merge                         # → v0.3.0 (latest)
```

Een beta proberen op de Pi:

```sh
curl -fsSL https://github.com/Rik-Stokmans/slingbag/releases/latest/download/install.sh | SLINGBAG_VERSIE=beta bash
```

`SLINGBAG_VERSIE=v0.2.0` installeert een vaste versie, bijvoorbeeld om terug te gaan.

## Problemen oplossen

| Probleem | Oplossing |
| --- | --- |
| `slingbag.local` niet gevonden | Wacht 2 minuten na opstarten; controleer kabel en `ping slingbag.local`. Het IP-adres staat ook in je router. |
| Geen geluid | Speaker aan en volume omhoog? `ssh rik@slingbag.local 'aplay -l'` moet *Headphones* tonen. Test via *Instellingen → Laat horen*. |
| Knop reageert niet | Controleer of de twee gebruikte pootjes diagonaal liggen en of de GND-rail met pin 9 is verbonden. Logboek: `journalctl -u slingbag -f`. |
| Melding te lage spanning | Gebruik een voeding of powerbank van minstens 2,5 A en een korte, dikke USB-kabel. |
| Lampje knippert snel | Instellingen of audio onvolledig: zie de foutmelding in de app (tab *Nu*). |

## Externe onderdelen

- [Piper](https://github.com/rhasspy/piper) (MIT-licentie), release 2023.11.14-2 voor Linux arm64.
- Nederlandse stemmen *pim*, *ronnie* en *mls* (medium) uit [piper-voices](https://huggingface.co/rhasspy/piper-voices); de licentie staat per stem in de bijbehorende modelkaart.
- Op de Pi: Flask, Waitress en gpiozero uit de Raspberry Pi OS-pakketbron.
