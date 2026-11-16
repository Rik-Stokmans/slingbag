"""Bluetooth-speaker: koppelen, verbonden blijven en afspelen zonder weggevallen woorden.

Werkt met BlueZ (``bluetoothctl``) en bluez-alsa (ALSA-apparaat ``bluealsa``).

Een bluetoothverbinding start pas na een fractie van een seconde met geluid. Daarom
houdt :class:`BluetoothSpeaker` één doorlopende geluidsstroom open: vóór het eerste
geluid gaat er een korte stilte overheen, en tussen zinnen en bestanden door blijft de
stroom stilte sturen. Zo valt het eerste woord niet weg. Na een tijd zonder geluid gaat
de stroom dicht. Als *wakker houden* aan staat, stuurt de slingbag af en toe een
onhoorbaar zacht signaal, zodat de speaker zichzelf niet uitschakelt.
"""

from __future__ import annotations

import array
import logging
import os
import queue
import random
import re
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_DEVICE_LINE = re.compile(r"Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5}) ([^\r\n]*)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
_UNNAMED = re.compile(r"^[0-9A-F]{2}(-[0-9A-F]{2}){5}$")

RATE = 44100
CHANNELS = 2
FRAME = 2 * CHANNELS                 # bytes per sample-paar (S16_LE stereo)
BYTES_PER_S = RATE * FRAME
CHUNK = 4096                         # ~23 ms per schrijfactie
BUFFER_US = 300_000                  # buffer van aplay
PIPE_BYTES = 8192                    # kleine pijp: stoppen en opnieuw beginnen gaan snel

PREROLL_S = 0.8                      # stilte na het openen, zodat het eerste woord niet wegvalt
TAIL_S = 0.6                         # stilte na het laatste geluid, voordat 'klaar' wordt gemeld
HOLD_S = 20.0                        # stroom zo lang openhouden na het laatste geluid
CHECK_S = 30.0                       # zo vaak de verbinding controleren als de stroom dicht is
KEEP_AWAKE_S = 240.0                 # zo vaak een onhoorbaar signaal sturen tegen automatisch uitschakelen
KEEP_AWAKE_SIGNAL_S = 1.5


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * BYTES_PER_S) // FRAME * FRAME)


def soft_noise(seconds: float) -> bytes:
    """Ruis van ±2 (ongeveer -84 dB): niet te horen, maar geen digitale stilte."""
    count = int(seconds * RATE) * CHANNELS
    rng = random.Random(7)
    return array.array("h", (rng.randint(-2, 2) for _ in range(count))).tobytes()


def volume_gain(volume: int) -> float:
    """Zelfde verloop als de volumeschuif op de 3,5mm-uitgang (``amixer -M``)."""
    return round((max(0, min(100, volume)) / 100) ** 3, 4)


def clean(output: str) -> str:
    """Haalt kleurcodes en prompts van ``bluetoothctl`` weg."""
    return _ANSI.sub("", output or "")


def parse_devices(output: str) -> list[dict]:
    """``bluetoothctl devices`` → [{adres, naam}]. Apparaten zonder naam krijgen een lege naam."""
    found: dict[str, str] = {}
    for address, name in _DEVICE_LINE.findall(clean(output)):
        name = name.strip()
        if _UNNAMED.match(name) or name.replace("-", ":") == address:
            name = ""
        if name or address not in found:
            found[address] = name
    # Met naam eerst (alfabetisch), daarna de naamloze.
    return [{"adres": a, "naam": n}
            for a, n in sorted(found.items(), key=lambda item: (not item[1], item[1].lower(), item[0]))]


def parse_info(output: str) -> dict:
    """``bluetoothctl info <adres>`` → naam en status (gekoppeld, vertrouwd, verbonden)."""
    output = clean(output)
    known = bool(re.search(r"^Device [0-9A-F:]{17}", output, re.MULTILINE)) and "not available" not in output
    info = {"naam": "", "bekend": known, "gekoppeld": False, "vertrouwd": False, "verbonden": False}
    keys = {"Paired": "gekoppeld", "Trusted": "vertrouwd", "Connected": "verbonden"}
    for line in output.splitlines():
        key, _, value = line.strip().partition(": ")
        if key == "Name":
            info["naam"] = value.strip()
        elif key in keys:
            info[keys[key]] = value.strip() == "yes"
    return info


class BluetoothControl:
    """Dunne laag over ``bluetoothctl``. Alle aanroepen hebben een tijdslimiet."""

    def available(self) -> bool:
        return shutil.which("bluetoothctl") is not None

    def _run(self, *args: str, timeout: float = 10) -> str:
        if not self.available():
            return ""
        try:
            result = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True,
                                    timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout
            return out.decode(errors="replace") if isinstance(out, bytes) else (out or "")
        except OSError as exc:
            log.warning("bluetoothctl niet uit te voeren: %s", exc)
            return ""
        return result.stdout

    def power_on(self) -> None:
        self._run("power", "on", timeout=5)

    def scan(self, seconds: int = 12) -> list[dict]:
        self.power_on()
        self._run("--timeout", str(seconds), "scan", "on", timeout=seconds + 5)
        return self.devices()

    def devices(self) -> list[dict]:
        """Alle apparaten die de Pi kent: gevonden bij zoeken of eerder gekoppeld, met hun status."""
        paired = {d["adres"] for d in parse_devices(self._run("devices", "Paired", timeout=5))}
        connected = {d["adres"] for d in parse_devices(self._run("devices", "Connected", timeout=5))}
        result = [dict(d, gekoppeld=d["adres"] in paired, verbonden=d["adres"] in connected)
                  for d in parse_devices(self._run("devices", timeout=5))]
        # Verbonden en gekoppelde apparaten bovenaan; volgorde verder zoals parse_devices.
        result.sort(key=lambda d: (not d["verbonden"], not d["gekoppeld"]))
        return result

    def _find(self, address: str, seconds: int = 20) -> bool:
        """Zoekt kort tot het apparaat er is. BlueZ vergeet een gevonden apparaat ~30 s na het zoeken."""
        if self.info(address)["bekend"]:
            return True
        try:
            proc = subprocess.Popen(["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        found = False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not found:
            time.sleep(1)
            found = self.info(address)["bekend"]
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        return found

    BINARY = "bluetoothctl"
    PAIR_DONE = re.compile(r"Pairing successful|Failed to pair|AlreadyExists|Paired: yes")

    def pair_with_agent(self, address: str, timeout: float = 40) -> str:
        """Koppelt in één bluetoothctl-sessie mét koppelagent.

        Zonder agent staat de Pi op 'Pairable: no' en loopt koppelen vast. De agent
        (NoInputNoOutput) keurt het koppelen goed; vraagt de speaker toch om
        bevestiging of toestemming, dan antwoordt deze sessie 'yes'.
        """
        try:
            proc = subprocess.Popen([self.BINARY], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=0)
        except OSError as exc:
            return f"Failed to pair: {exc}"
        chunks: queue.Queue = queue.Queue()

        def reader():
            while True:
                try:
                    data = os.read(proc.stdout.fileno(), 4096)
                except OSError:
                    data = b""
                chunks.put(data)
                if not data:
                    return

        threading.Thread(target=reader, name="bluetoothctl", daemon=True).start()

        def send(line: str) -> None:
            try:
                proc.stdin.write((line + "\n").encode())
            except OSError:
                pass

        for line in ("agent NoInputNoOutput", "default-agent", "pairable on", f"pair {address}"):
            send(line)
        output = ""
        answered = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if not data:
                break
            output += clean(data.decode(errors="replace"))
            prompts = len(re.findall(r"\(yes/no\)", output))
            while answered < prompts:  # bevestiging of toestemming vragen: altijd ja
                send("yes")
                answered += 1
            if self.PAIR_DONE.search(output):
                time.sleep(1)  # sleutels laten wegschrijven
                break
        send("quit")
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return output

    def remove(self, address: str) -> None:
        self._run("remove", address, timeout=10)

    @staticmethod
    def _reason(output: str) -> str:
        """Laatste foutregel van bluetoothctl, voor in de melding."""
        lines = [line.strip() for line in clean(output).splitlines()
                 if re.search(r"fail|error|not available|rejected|timeout", line, re.IGNORECASE)]
        return f" (melding: {lines[-1][:160]})" if lines else ""

    def info(self, address: str) -> dict:
        return parse_info(self._run("info", address, timeout=5))

    def connected(self, address: str) -> bool:
        return self.info(address)["verbonden"]

    def connect(self, address: str) -> bool:
        self._run("--timeout", "10", "connect", address, timeout=15)
        return self.connected(address)

    def pair(self, address: str) -> tuple[bool, str]:
        """Koppelt, vertrouwt en verbindt. Geeft (gelukt, uitleg)."""
        if not self.available():
            return False, "Bluetooth is niet geïnstalleerd op de slingbag. Werk de software bij."
        self.power_on()
        if not self.info(address)["gekoppeld"]:
            if not self._find(address):
                return False, ("Speaker niet gevonden. Zet hem in koppelmodus (bluetoothknop ingedrukt "
                               "houden tot het lampje knippert) en probeer het opnieuw.")
            out = self.pair_with_agent(address)
            if not self.info(address)["gekoppeld"]:
                log.warning("Koppelen mislukt:%s", self._reason(out) or " " + out.strip()[-300:])
                return False, ("Koppelen lukte niet. Zet de speaker in koppelmodus en probeer het opnieuw."
                               + self._reason(out))
        self._run("trust", address, timeout=5)
        out = self._run("--timeout", "10", "connect", address, timeout=15)
        if not self.connected(address):
            log.warning("Verbinden mislukt:%s", self._reason(out) or " " + clean(out).strip()[-300:])
            return False, ("Gekoppeld, maar verbinden lukte niet. Staat de speaker aan? Lukt het daarna nog "
                           "niet, tik dan op Vergeet en koppel opnieuw." + self._reason(out))
        return True, "Verbonden."


class AplaySink:
    """Doorlopende geluidsstroom naar de speaker via ``aplay`` en bluez-alsa."""

    def __init__(self, address: str):
        self.proc = subprocess.Popen(
            ["aplay", "-q", "-D", f"bluealsa:DEV={address},PROFILE=a2dp", "-t", "raw", "-f", "S16_LE",
             "-r", str(RATE), "-c", str(CHANNELS), f"--buffer-time={BUFFER_US}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            import fcntl
            fcntl.fcntl(self.proc.stdin.fileno(), 1031, PIPE_BYTES)  # F_SETPIPE_SZ (Linux)
        except (ImportError, OSError):
            pass

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = self.proc.stdin.write(view)
            if not written:
                raise BrokenPipeError("speakerstroom gesloten")
            view = view[written:]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def ffmpeg_decoder(path: Path, gain: float) -> subprocess.Popen:
    """Zet elk audiobestand om naar het vaste formaat van de stroom, met het volume erin."""
    return subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", str(path), "-af", f"volume={gain}",
         "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(RATE), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class BluetoothSpeaker:
    """Speelt af op een bluetoothspeaker vanuit één eigen thread.

    ``play`` zet een reeks klaar en onderbreekt wat er speelt; ``done`` wordt alleen
    aangeroepen als de reeks helemaal is afgespeeld, net als bij de 3,5mm-uitgang.
    """

    def __init__(self, control: BluetoothControl | None = None,
                 sink_factory: Callable[[str], object] = AplaySink,
                 decoder_factory: Callable[[Path, float], subprocess.Popen] = ffmpeg_decoder,
                 clock: Callable[[], float] = time.monotonic):
        self.control = control or BluetoothControl()
        self.sink_factory = sink_factory
        self.decoder_factory = decoder_factory
        self.clock = clock
        self.address = ""
        self.gain = volume_gain(70)
        self.keep_awake = True
        self.connected = False
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._generation = 0
        self._job: int | None = None
        self._sink = None
        self._sink_address = ""
        self._last_sound = clock()
        self._last_check = 0.0
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._noise = b""

    # --- bediening (andere threads) ------------------------------------------------------

    def configure(self, address: str, keep_awake: bool = True) -> None:
        address = (address or "").upper()
        with self._lock:
            if address != self.address:
                self._generation += 1
                self._job = None
                self.connected = False
                self._last_check = 0.0
            self.address = address
            self.keep_awake = keep_awake
        if address and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="bluetooth", daemon=True)
            self._thread.start()

    def play(self, paths: list[Path], done: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._generation += 1
            self._job = self._generation
            self._jobs.put((self._generation, list(paths), done))

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._job = None

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._job is not None

    def close(self) -> None:
        self.stop()
        self._closing.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    # --- thread ----------------------------------------------------------------------------

    def _current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _finish(self, generation: int) -> None:
        with self._lock:
            if self._job == generation:
                self._job = None

    def _run(self) -> None:
        while not self._closing.is_set():
            try:
                job = self._jobs.get(timeout=0 if self._sink else 1)
            except queue.Empty:
                job = None
            if self._sink and self._sink_address != self.address:
                self._close_sink()
            if job is not None:
                self._play_job(*job)
            elif self._sink:
                if self.clock() - self._last_sound > HOLD_S:
                    self._close_sink()
                else:
                    self._write(silence(CHUNK / BYTES_PER_S))
            else:
                self._idle()
        self._close_sink()

    def _idle(self) -> None:
        """Stroom dicht: verbinding bewaken en de speaker wakker houden."""
        if not self.address:
            return
        now = self.clock()
        if now - self._last_check >= CHECK_S:
            self._last_check = now
            ok = self.control.connected(self.address) or self.control.connect(self.address)
            if ok and not self.connected:
                log.info("Bluetooth-speaker %s verbonden", self.address)
            self.connected = ok
        if self.keep_awake and self.connected and now - self._last_sound >= KEEP_AWAKE_S:
            if self._open_sink():
                if not self._noise:
                    self._noise = soft_noise(KEEP_AWAKE_SIGNAL_S)
                self._write(self._noise)
                self._write(silence(TAIL_S))
            self._close_sink()
            self._last_sound = self.clock()

    def _open_sink(self) -> bool:
        if self._sink:
            return True
        address = self.address
        if not address:
            return False
        if not (self.control.connected(address) or self.control.connect(address)):
            self.connected = False
            log.warning("Bluetooth-speaker %s niet bereikbaar; staat hij aan?", address)
            return False
        try:
            self._sink = self.sink_factory(address)
        except OSError as exc:
            log.error("Geluidsstroom naar de speaker niet te openen: %s", exc)
            return False
        self._sink_address = address
        self.connected = True
        return self._write(silence(PREROLL_S))

    def _close_sink(self) -> None:
        sink, self._sink = self._sink, None
        if sink:
            sink.close()

    def _write(self, data: bytes) -> bool:
        if not self._sink:
            return False
        try:
            self._sink.write(data)
            return True
        except (BrokenPipeError, OSError, ValueError):
            log.warning("Verbinding met de bluetooth-speaker verbroken")
            self._sink = None
            self.connected = False
            self._last_check = 0.0
            return False

    def _play_job(self, generation: int, paths: list[Path], done) -> None:
        if not self._current(generation):
            return  # al vervangen of gestopt
        ok = self._open_sink()
        for path in paths if ok else []:
            if not self._current(generation) or not self._play_file(path, generation):
                break
        if ok and self._current(generation):
            self._write(silence(TAIL_S))  # zodat 'klaar' pas komt als het geluid echt uit is
        self._last_sound = self.clock()
        if self._current(generation):
            self._finish(generation)
            if done:
                done()

    def _play_file(self, path: Path, generation: int) -> bool:
        """Schrijft één bestand naar de stroom. False als de stroom wegviel of er gestopt is."""
        try:
            decoder = self.decoder_factory(path, self.gain)
        except OSError as exc:
            log.error("Afspelen niet mogelijk: %s", exc)
            return False
        fd = decoder.stdout.fileno()
        rest = b""
        try:
            while True:
                if not self._current(generation):
                    return False
                ready, _, _ = select.select([fd], [], [], 0.02)
                if not ready:
                    # Decoder nog bezig: stilte sturen zodat de stroom niet stokt.
                    if not self._write(silence(0.02)):
                        return False
                    continue
                data = os.read(fd, CHUNK)
                if not data:
                    return True
                data = rest + data
                cut = len(data) - len(data) % FRAME  # alleen hele sample-paren, anders verschuift het geluid
                rest = data[cut:]
                if cut and not self._write(data[:cut]):
                    return False
        finally:
            if decoder.poll() is None:
                decoder.kill()
            decoder.wait()
            decoder.stdout.close()
