"""Raspberry Pi adapter: the arcade button as push-to-talk (PLAN.md D5, D13).

Unlike the laptop's spacebar, a GPIO button reports press *and* release, so holding
needs no guesswork: `held` simply asks the pin. Wiring and pin choices are in
docs/hardware.md.

Audio in and out still go through sounddevice/PortAudio, the same as the laptop, so
this module is only about the parts the Pi has and the laptop doesn't: the button, the
status LED and the screen. Every library they need is imported lazily, so importing
this module costs nothing on a machine with no GPIO.
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


class Screen:
    """The 2.2" SPI display (ILI9341, 240x320) on hardware SPI0 (docs/hardware.md).

    Same shape as the button and the LED above: `adafruit_circuitpython_rgb_display`
    and Blinka are imported lazily, so a laptop and the test suite never need them,
    and a display that isn't there gets a sentence rather than a stack trace.

    The panel is used in portrait, 240 wide by 320 tall, which is how it comes up with
    no rotation and the shape most lead images suit.

    The backlight can be a GPIO (`backlight_gpio`, GPIO12 on this build) instead of a
    permanent 3V3 connection. That is what makes a blank screen genuinely dark: filling
    the panel black still leaves a lit grey rectangle, which is no good in a bedroom.
    `backlight_active_high` says which way round the wiring is -- a pin driving the
    backlight directly lights it high, a P-MOSFET high-side switch lights it low -- the
    same reason `RgbLed` carries `common_anode` rather than assuming.

    With no `backlight_gpio` the backlight is whatever the wiring makes it (on 3V3,
    always on) and this class only fills the panel black, exactly as before.

    `show` and `blank` swallow whatever the driver throws: a picture is decoration
    (PLAN.md D28) and must never be the thing that breaks a turn.
    """

    WIDTH, HEIGHT = 240, 320

    def __init__(self, dc: int = 25, reset: int = 27, cs: int = 0, baudrate: int = 24_000_000,
                 rotation: int = 0, backlight_gpio: int | None = None,
                 backlight_active_high: bool = True, display=None, backlight=None) -> None:
        if display is None:
            display = self._open(dc, reset, cs, baudrate, rotation)
        if backlight is None and backlight_gpio is not None:
            backlight = self._open_backlight(backlight_gpio, backlight_active_high)
        self._display = display
        self._backlight = backlight

    @staticmethod
    def _open(dc: int, reset: int, cs: int, baudrate: int, rotation: int):
        try:
            import board
            import busio
            import digitalio
            from adafruit_rgb_display import ili9341
        except ImportError as e:
            raise RuntimeError(
                "the screen needs Blinka and the display driver. On the Pi:\n"
                "  .venv/bin/pip install adafruit-blinka adafruit-circuitpython-rgb-display pillow\n"
                "and turn SPI on with `sudo raspi-config` -> Interface Options -> SPI, "
                f"then reboot ({e})."
            ) from e

        def pin(number: int):
            return digitalio.DigitalInOut(getattr(board, f"D{number}"))

        try:
            spi = busio.SPI(clock=board.SCK, MOSI=board.MOSI)
            return ili9341.ILI9341(
                spi, cs=pin(8 if cs == 0 else 7), dc=pin(dc), rst=pin(reset),
                baudrate=baudrate, width=Screen.WIDTH, height=Screen.HEIGHT,
                rotation=rotation,
            )
        except Exception as e:
            raise RuntimeError(
                f"couldn't open the display on SPI0 CE{cs} with D/C on GPIO{dc} and RESET "
                f"on GPIO{reset} ({e}). Check SPI is enabled (`ls /dev/spidev*` should "
                "list something), the pins in talkbox.toml, and that your user is in the "
                "spi group. `scripts/screen_check.py` tests the wiring on its own."
            ) from e

    @staticmethod
    def _open_backlight(pin: int, active_high: bool):
        """The backlight on a GPIO, through gpiozero like the button and status LED.

        It starts off, so the screen is dark from start-up until the first answer
        rather than glowing at a child while nothing is happening.
        """
        try:
            from gpiozero import DigitalOutputDevice
        except ImportError as e:
            raise RuntimeError(
                "the screen's backlight pin needs gpiozero. On the Pi:\n"
                "  sudo apt install -y python3-dev swig liblgpio-dev\n"
                "  .venv/bin/pip install gpiozero lgpio\n"
                "Or leave backlight_gpio unset and wire the display's LED pin to 3V3."
            ) from e
        try:
            return DigitalOutputDevice(pin, active_high=active_high, initial_value=False)
        except Exception as e:
            raise RuntimeError(
                f"couldn't open GPIO{pin} for the screen's backlight ({e}). Check the pin "
                "in talkbox.toml, that nothing else is using it, and that your user is in "
                "the gpio group. `scripts/screen_check.py --backlight` tests it on its own."
            ) from e

    def _light(self, on: bool) -> None:
        # A backlight that won't switch is not a reason to lose the turn, or the
        # picture: the panel is still perfectly readable with the light stuck on.
        if self._backlight is None:
            return
        try:
            self._backlight.value = bool(on)
        except Exception:  # noqa: BLE001
            pass

    def show(self, image) -> None:
        try:
            self._display.image(image)
        except Exception:  # noqa: BLE001 - see the class docstring: never break a turn
            return  # nothing was drawn, so the backlight stays off rather than lighting
        self._light(True)

    def blank(self) -> None:
        # Dark first: switching the backlight is instant, while filling the panel over
        # SPI takes long enough to see. This way the picture never flashes black.
        self._light(False)
        try:
            self._display.fill(0)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.blank()
        if self._backlight is not None:
            try:
                self._backlight.close()
            except Exception:  # noqa: BLE001
                pass
