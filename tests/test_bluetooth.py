"""Tests voor de bluetoothspeaker: zonder bluetooth, bluez-alsa of ffmpeg (nep-onderdelen)."""

import copy
import subprocess
import threading
import time

import pytest

from slingbag import bluetooth
from slingbag.audio import AudioPlayer
from slingbag.bluetooth import (BYTES_PER_S, FRAME, PREROLL_S, TAIL_S, BluetoothSpeaker, parse_devices,
                                parse_info, silence, soft_noise, volume_gain)
from slingbag.config import DEFAULT_CONFIG, validate

JBL = "AA:BB:CC:DD:EE:01"


def test_devices_are_parsed_sorted_and_unnamed_ones_skipped():
    out = (f"Device {JBL} JBL Go 4\n"
           "Device 11:22:33:44:55:66 11-22-33-44-55-66\n"
           "Device 77:88:99:AA:BB:CC Anke's iPhone\n")
    assert parse_devices(out) == [{"adres": "77:88:99:AA:BB:CC", "naam": "Anke's iPhone"},
                                  {"adres": JBL, "naam": "JBL Go 4"}]


def test_info_is_parsed():
    out = f"Device {JBL} (public)\n\tName: JBL Go 4\n\tPaired: yes\n\tTrusted: no\n\tConnected: yes\n"
    assert parse_info(out) == {"naam": "JBL Go 4", "gekoppeld": True, "vertrouwd": False, "verbonden": True}


def test_speaker_settings_are_validated():
    config = copy.deepcopy(DEFAULT_CONFIG)
    assert validate(config) == []
    config["speaker"]["soort"] = "bluetooth"
    assert any("koppel eerst" in e for e in validate(config))
    config["speaker"]["adres"] = "niet-een-adres"
    assert any("bluetoothadres" in e for e in validate(config))
    config["speaker"]["adres"] = JBL
    assert validate(config) == []


def test_volume_follows_the_same_curve_as_the_aux_output():
    assert volume_gain(100) == 1.0
    assert volume_gain(0) == 0.0
    assert volume_gain(70) == pytest.approx(0.343)


def test_keep_awake_signal_is_soft_but_not_digital_silence():
    noise = soft_noise(0.1)
    assert len(noise) % FRAME == 0
    assert any(noise) and max(abs(int.from_bytes(noise[i:i + 2], "little", signed=True))
                              for i in range(0, len(noise), 2)) <= 2


# --- afspelen --------------------------------------------------------------------------------

class FakeControl:
    def __init__(self, connected=True, can_connect=True):
        self.is_connected = connected
        self.can_connect = can_connect
        self.connects = 0

    def available(self):
        return True

    def connected(self, address):
        return self.is_connected

    def connect(self, address):
        self.connects += 1
        self.is_connected = self.can_connect
        return self.is_connected


class FakeSink:
    instances = []

    def __init__(self, address):
        self.address = address
        self.data = bytearray()
        self.closed = False
        FakeSink.instances.append(self)

    def write(self, data):
        self.data += data
        time.sleep(len(data) / BYTES_PER_S / 20)  # iets van het tempo van echt afspelen

    def close(self):
        self.closed = True


def cat_decoder(path, gain):
    return subprocess.Popen(["cat", str(path)], stdout=subprocess.PIPE)


@pytest.fixture
def speaker(monkeypatch):
    monkeypatch.setattr(bluetooth, "HOLD_S", 0.0)  # stroom direct dicht na het afspelen
    FakeSink.instances = []
    control = FakeControl()
    sp = BluetoothSpeaker(control=control, sink_factory=FakeSink, decoder_factory=cat_decoder)
    sp.configure(JBL.lower(), keep_awake=False)
    yield sp
    sp.close()


def play_and_wait(sp, paths, timeout=5):
    done = threading.Event()
    sp.play(paths, done.set)
    assert done.wait(timeout), "afspelen niet afgerond"
    return done


def test_plays_with_silence_before_and_after_in_whole_frames(speaker, tmp_path):
    clip = tmp_path / "zin.raw"
    clip.write_bytes(bytes(range(1, 256)) * 16 + b"\x01")  # 4081 bytes: geen heel aantal frames
    play_and_wait(speaker, [clip, clip])
    sink = FakeSink.instances[0]
    assert sink.address == JBL
    data = bytes(sink.data)
    pre = silence(PREROLL_S)
    assert data.startswith(pre)
    body = data[len(pre):]
    assert body.startswith(clip.read_bytes()[:4080])
    assert len(data) % FRAME == 0
    assert data.endswith(silence(TAIL_S))
    assert not speaker.playing


def test_stop_interrupts_and_skips_done(speaker, tmp_path):
    clip = tmp_path / "lang.raw"
    clip.write_bytes(b"\x05\x00" * BYTES_PER_S * 2)  # 4 seconden
    done = threading.Event()
    speaker.play([clip], done.set)
    time.sleep(0.15)
    assert speaker.playing
    speaker.stop()
    assert not speaker.playing
    time.sleep(0.4)
    assert not done.is_set()


def test_reconnects_before_playing(speaker, tmp_path):
    speaker.control.is_connected = False
    clip = tmp_path / "zin.raw"
    clip.write_bytes(b"\x01\x00" * 400)
    play_and_wait(speaker, [clip])
    assert speaker.control.connects >= 1
    assert speaker.connected


def test_unreachable_speaker_still_finishes_so_the_session_continues(speaker, tmp_path):
    speaker.control.is_connected = False
    speaker.control.can_connect = False
    clip = tmp_path / "zin.raw"
    clip.write_bytes(b"\x01\x00" * 400)
    play_and_wait(speaker, [clip])
    assert not speaker.connected
    assert FakeSink.instances == []


class FakeBluetooth:
    def __init__(self):
        self.address = ""
        self.gain = 0
        self.played = []
        self.stopped = 0
        self.playing = False

    def configure(self, address, keep_awake=True):
        self.address = address

    def play(self, paths, done=None):
        self.played.append(paths)
        if done:
            done()

    def stop(self):
        self.stopped += 1

    def close(self):
        pass


def test_audio_player_switches_between_aux_and_bluetooth():
    fake = FakeBluetooth()
    player = AudioPlayer(bluetooth=fake)
    player.is_mac = False
    player.set_output({"soort": "bluetooth", "adres": JBL, "wakker_houden": True})
    assert player.output == "bluetooth" and fake.address == JBL
    tokens = []
    player.play(["a.wav"], token="t1", on_done=tokens.append)
    assert fake.played == [["a.wav"]] and tokens == ["t1"]
    player.set_output({"soort": "aux", "adres": JBL, "wakker_houden": True})
    assert player.output == "aux" and fake.address == ""
