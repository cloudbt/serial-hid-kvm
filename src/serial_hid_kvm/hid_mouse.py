"""Mouse control via CH9329 HID emulator."""

import atexit
import logging
import time

from .hid_protocol import CH9329, build_mouse_abs_packet, build_mouse_rel_packet

logger = logging.getLogger(__name__)

BUTTON_MAP = {
    "left": 0x01,
    "right": 0x02,
    "middle": 0x04,
}


class Mouse:
    """Mouse controller using CH9329."""

    def __init__(self, ch9329: CH9329, screen_width: int = 1920,
                 screen_height: int = 1080, invert_x: bool = False,
                 invert_y: bool = False):
        self._dev = ch9329
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._invert_x = invert_x
        self._invert_y = invert_y
        self._buttons: int = 0x00  # Currently pressed button bitmask
        self._click_hold = 0.01  # Press->release hold for click() (seconds)
        self._click_after = 0.0  # Settle delay after click() (seconds)
        atexit.register(self._release_all)

    def set_timing(self, *, click_hold: float | None = None,
                   click_after: float | None = None):
        """Update mouse timing. Values are seconds; None leaves unchanged."""
        if click_hold is not None:
            self._click_hold = max(0.0, click_hold)
        if click_after is not None:
            self._click_after = max(0.0, click_after)

    def get_timing(self) -> dict:
        """Return current mouse timing in seconds."""
        return {"click_hold": self._click_hold, "click_after": self._click_after}

    def _release_all(self):
        try:
            if self._dev.is_open:
                self._dev.release_all()
        except Exception:
            pass

    def _screen_to_abs(self, x: int, y: int) -> tuple[int, int]:
        """Convert screen coordinates to CH9329 absolute coordinates (0-4095)."""
        abs_x = int(x * 4096 / self._screen_width)
        abs_y = int(y * 4096 / self._screen_height)
        if self._invert_x:
            abs_x = 4095 - abs_x
        if self._invert_y:
            abs_y = 4095 - abs_y
        return abs_x, abs_y

    def move_absolute(self, x: int, y: int):
        """Move mouse to absolute screen position (preserves button state).

        Args:
            x: Screen X coordinate
            y: Screen Y coordinate
        """
        abs_x, abs_y = self._screen_to_abs(x, y)
        packet = build_mouse_abs_packet(self._buttons, abs_x, abs_y)
        self._dev.send(packet)

    def move_relative(self, dx: int, dy: int):
        """Move mouse relative to current position (preserves button state).

        Args:
            dx: Horizontal movement (-127 to 127)
            dy: Vertical movement (-127 to 127)
        """
        if self._invert_x:
            dx = -dx
        if self._invert_y:
            dy = -dy
        packet = build_mouse_rel_packet(self._buttons, dx, dy)
        self._dev.send(packet)

    def click(self, button: str = "left", x: int | None = None, y: int | None = None):
        """Click a mouse button, optionally at a position.

        Args:
            button: Button name (left, right, middle)
            x: Optional screen X coordinate (moves to position first)
            y: Optional screen Y coordinate (moves to position first)
        """
        btn_bit = BUTTON_MAP.get(button, 0x01)

        if x is not None and y is not None:
            abs_x, abs_y = self._screen_to_abs(x, y)
            self._dev.send(build_mouse_abs_packet(btn_bit, abs_x, abs_y))
            time.sleep(self._click_hold)
            self._dev.send(build_mouse_abs_packet(0x00, abs_x, abs_y))
        else:
            self._dev.send(build_mouse_rel_packet(btn_bit, 0, 0))
            time.sleep(self._click_hold)
            self._dev.send(build_mouse_rel_packet(0x00, 0, 0))

        if self._click_after > 0:
            time.sleep(self._click_after)

    def mouse_down(self, button: str = "left", x: int | None = None, y: int | None = None):
        """Press and hold a mouse button.

        Args:
            button: Button name (left, right, middle)
            x: Optional screen X coordinate
            y: Optional screen Y coordinate
        """
        btn_bit = BUTTON_MAP.get(button, 0x01)
        self._buttons |= btn_bit

        if x is not None and y is not None:
            abs_x, abs_y = self._screen_to_abs(x, y)
            self._dev.send(build_mouse_abs_packet(self._buttons, abs_x, abs_y))
        else:
            self._dev.send(build_mouse_rel_packet(self._buttons, 0, 0))

    def mouse_up(self, button: str = "left", x: int | None = None, y: int | None = None):
        """Release a mouse button.

        Args:
            button: Button name (left, right, middle)
            x: Optional screen X coordinate
            y: Optional screen Y coordinate
        """
        btn_bit = BUTTON_MAP.get(button, 0x01)
        self._buttons &= ~btn_bit

        if x is not None and y is not None:
            abs_x, abs_y = self._screen_to_abs(x, y)
            self._dev.send(build_mouse_abs_packet(self._buttons, abs_x, abs_y))
        else:
            self._dev.send(build_mouse_rel_packet(self._buttons, 0, 0))

    def scroll(self, amount: int):
        """Scroll the mouse wheel.

        Args:
            amount: Scroll amount (positive=up, negative=down, -127 to 127)
        """
        amount = max(-127, min(127, amount))
        packet = build_mouse_rel_packet(self._buttons, 0, 0, scroll=amount)
        self._dev.send(packet)
