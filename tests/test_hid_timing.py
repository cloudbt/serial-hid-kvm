"""Tests for configurable HID timing on Keyboard and Mouse."""

from serial_hid_kvm.hid_keyboard import Keyboard
from serial_hid_kvm.hid_mouse import Mouse


class FakeDev:
    """Minimal CH9329 stand-in that records raw packets."""

    is_open = True

    def __init__(self):
        self.sent = []

    def send(self, packet):
        self.sent.append(packet)

    def release_all(self):
        pass


def test_keyboard_default_timing():
    kb = Keyboard(FakeDev())
    t = kb.get_timing()
    assert t["char_delay"] == 0.02
    assert t["key_hold"] == 0.01
    assert t["combo_mod"] == 0.0


def test_keyboard_set_timing_roundtrip():
    kb = Keyboard(FakeDev())
    kb.set_timing(char_delay=0.0, key_hold=0.0, type_shift=0.05)
    t = kb.get_timing()
    assert t["char_delay"] == 0.0
    assert t["key_hold"] == 0.0
    assert t["type_shift"] == 0.05


def test_keyboard_set_timing_clamps_negative():
    kb = Keyboard(FakeDev())
    kb.set_timing(char_delay=-1.0)
    assert kb.get_timing()["char_delay"] == 0.0


def test_type_text_emits_press_and_release_per_char():
    dev = FakeDev()
    kb = Keyboard(dev)
    kb.set_timing(char_delay=0.0, type_key_hold=0.0, type_shift=0.0)
    kb.type_text("ab", raw=True)
    # 2 chars * (press + release) = 4 packets, no staged modifier
    assert len(dev.sent) == 4


def test_type_shift_stages_modifier_when_set():
    dev = FakeDev()
    kb = Keyboard(dev)
    kb.set_timing(char_delay=0.0, type_key_hold=0.0, type_shift=0.001)
    kb.type_text("A", raw=True)  # uppercase needs shift -> staged
    # modifier-only + modifier+key + release = 3 packets
    assert len(dev.sent) == 3


def test_mouse_default_and_set_timing():
    m = Mouse(FakeDev())
    assert m.get_timing() == {"click_hold": 0.01, "click_after": 0.0}
    m.set_timing(click_hold=0.0, click_after=0.0)
    assert m.get_timing() == {"click_hold": 0.0, "click_after": 0.0}


def test_mouse_click_emits_press_and_release():
    dev = FakeDev()
    m = Mouse(dev)
    m.set_timing(click_hold=0.0, click_after=0.0)
    m.click("left", 100, 200)
    assert len(dev.sent) == 2
