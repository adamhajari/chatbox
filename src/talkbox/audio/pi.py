"""Raspberry Pi adapter: the arcade button as push-to-talk (PLAN.md D5, D13).

Unlike the laptop's spacebar, a GPIO button reports press *and* release, so holding
needs no guesswork: `held` simply asks the pin. Wiring and pin choices are in
docs/hardware.md.

Audio in and out still go through sounddevice/PortAudio, the same as the laptop, so
this module is only about the button.
"""

from __future__ import annotations

import time
from typing import Literal


class ButtonPress:
    """A momentary button on a GPIO pin, wired between the pin and ground.

    `gpiozero` is imported lazily so that importing this module costs nothing on a
    machine with no GPIO (a laptop, or the test suite).
    """

    name = "the button"

    def __init__(self, pin: int = 17, bounce_seconds: float = 0.05, button=None) -> None:
        if button is None:
            try:
                from gpiozero import Button
            except ImportError as e:
                raise RuntimeError(
                    "the button needs gpiozero. On the Pi:\n"
                    "  sudo apt install -y python3-dev swig liblgpio-dev\n"
                    "  .venv/bin/pip install gpiozero lgpio"
                ) from e
            try:
                button = Button(pin, pull_up=True, bounce_time=bounce_seconds)
            except Exception as e:
                raise RuntimeError(
                    f"couldn't open GPIO{pin} for the button ({e}). Check the pin number "
                    "in talkbox.toml, that nothing else is using it, and that your user "
                    "is in the gpio group."
                ) from e
        self._button = button
        self.pin = pin

    def poll_command(self, timeout: float) -> Literal["talk"] | None:
        """The button can only ask to talk; 'new' and 'quit' stay on the keyboard."""
        if self._button.is_pressed:
            return "talk"
        time.sleep(timeout)
        return None

    def begin(self) -> None:
        pass  # nothing to reset: the pin is the state

    def poll_hold(self, timeout: float) -> None:
        time.sleep(timeout)

    def held(self, now: float) -> bool:
        return self._button.is_pressed

    @property
    def confirmed(self) -> bool:
        """Always a real press: debouncing is the button's job, and a too-short
        recording is caught by VoiceSettings.min_press_seconds like any other."""
        return True

    def flush(self, timeout: float = 2.0) -> None:
        """Wait for the button to come back up, so a still-held button doesn't
        immediately start another turn."""
        deadline = time.monotonic() + timeout
        while self._button.is_pressed and time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        self._button.close()


class RgbLed:
    """A 5 mm RGB LED on three GPIO pins (docs/hardware.md).

    `common_anode` describes how the LED is wired: the long leg to 3V3 (common anode,
    a low pin lights it) or to ground (common cathode, a high pin lights it). It is a
    setting rather than a constant because a replacement LED may well be the other type.
    """

    # Full-on channels per state; no PWM, so there is no software timing to go wrong.
    COLOURS: dict[str, tuple[int, int, int]] = {
        "idle": (0, 0, 0),
        "listening": (0, 1, 0),   # green
        "thinking": (1, 1, 0),    # amber
        "speaking": (0, 0, 1),    # blue
        "error": (1, 0, 0),       # red
    }

    def __init__(self, red: int = 22, green: int = 23, blue: int = 24,
                 common_anode: bool = True, led=None) -> None:
        if led is None:
            try:
                from gpiozero import RGBLED
            except ImportError as e:
                raise RuntimeError(
                    "the status light needs gpiozero. On the Pi:\n"
                    "  sudo apt install -y python3-dev swig liblgpio-dev\n"
                    "  .venv/bin/pip install gpiozero lgpio"
                ) from e
            try:
                led = RGBLED(red=red, green=green, blue=blue,
                             active_high=not common_anode, pwm=False)
            except Exception as e:
                raise RuntimeError(
                    f"couldn't open GPIO{red}/{green}/{blue} for the status light ({e}). "
                    "Check the pins in talkbox.toml and that your user is in the gpio "
                    "group. `scripts/gpio_check.py` tests the wiring on its own."
                ) from e
        self._led = led

    def show(self, state: str) -> None:
        # An unknown state goes dark rather than raising: the light must never be the
        # thing that breaks a turn.
        self._led.value = self.COLOURS.get(state, (0, 0, 0))

    def close(self) -> None:
        self._led.value = (0, 0, 0)
        self._led.close()
