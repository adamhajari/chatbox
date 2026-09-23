#!/usr/bin/env python3
"""Prove the 2.2" SPI display is wired correctly, before any feature work.

Talkbox's own screen adapter is separate; this only exercises the panel, so the wiring
can be checked on its own. Run it on the Pi:

    .venv/bin/python scripts/screen_check.py
    .venv/bin/python scripts/screen_check.py --dc 25 --reset 27 --cs 0
    .venv/bin/python scripts/screen_check.py --rotation 180 --baudrate 16000000
    .venv/bin/python scripts/screen_check.py --image some-photo.jpg
    .venv/bin/python scripts/screen_check.py --backlight 12     # also blink the backlight

Pins default to the Phase 7 assignment (PLAN.md D25): hardware SPI0 (CS GPIO8,
SCK GPIO11, MOSI GPIO10) plus D/C on GPIO25 and RESET on GPIO27, chosen to leave
GPIO17 (button), GPIO22/23/24 (status LED) and GPIO18/19/21 (the amplifier's I2S
lines) alone. VCC goes to 3V3 — not 5 V.

The display's LED pin is the backlight. Wired to 3V3 it is simply always on; on a GPIO
(GPIO12 in this build) `--backlight 12` blinks it, so the pin can be proved before
Talkbox relies on it. `--backlight-active-low` is for a P-MOSFET high-side switch,
where the pin lights the backlight by going low.

SPI has to be on first: `sudo raspi-config` -> Interface Options -> SPI -> Yes, then
reboot. `ls /dev/spidev*` should then list spidev0.0.

The panel shows no text here either (PLAN.md D28): every pass is a colour or a shape.
"""

from __future__ import annotations

import argparse
import sys
import time

# The panel's own glass, which rotation never changes.
PANEL_WIDTH, PANEL_HEIGHT = 240, 320

# Step 1: solid colours, named as they come up. Wrong colours mean the panel is
# talking, which is most of what this script is for.
COLOURS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("white", (255, 255, 255)),
    ("black", (0, 0, 0)),
]

FAILURES = """
What each failure looks like:

  nothing at all, backlight dark      the backlight isn't being fed. Check VCC reaches
                                      3V3 and GND a ground pin, and either that LED
                                      reaches 3V3 or that --backlight names its pin.
  backlight on, screen stays white    the panel is powered but not being driven: D/C,
                                      RESET, MOSI or SCK is wrong or loose, or SPI is
                                      off. `ls /dev/spidev*` must list spidev0.0.
  'No such file or directory: spidev' SPI isn't enabled. raspi-config -> Interface
                                      Options -> SPI, then reboot.
  ImportError on board/busio          Blinka isn't installed:
                                      pip install adafruit-blinka
                                      adafruit-circuitpython-rgb-display pillow
  permission denied on /dev/spidev0.0 sudo usermod -aG spi $USER, log out and back in.
  colours named wrong (red shows      an ILI9341 clone with BGR order. Harmless for
  blue and vice versa)                photos; say so and we'll set the driver's flag.
  noise, torn lines, flickering       SPI too fast or the wires too long: re-run with
                                      --baudrate 16000000, and keep leads short.
  the picture is upside down or        the panel is mounted the other way up: find the
  sideways                            --rotation that looks right and put it in
                                      talkbox.local.toml. 90 and 270 are landscape,
                                      0 and 180 portrait.
  a white flash then nothing          RESET is floating: check GPIO27.
  backlight won't go dark             wrong polarity: try --backlight-active-low.
  backlight won't come back on        the GPIO can't supply the backlight current;
                                      it needs a P-MOSFET high-side switch.
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dc", type=int, default=25, help="data/command pin (GPIO)")
    ap.add_argument("--reset", type=int, default=27, help="RESET pin (GPIO)")
    ap.add_argument("--cs", type=int, choices=(0, 1), default=0, help="SPI0 chip select")
    ap.add_argument("--baudrate", type=int, default=24_000_000)
    ap.add_argument("--rotation", type=int, choices=(0, 90, 180, 270), default=90,
                    help="90/270 = landscape (Talkbox's default), 0/180 = portrait")
    ap.add_argument("--backlight", type=int, metavar="GPIO",
                    help="the backlight's GPIO pin, if it isn't wired to 3V3")
    ap.add_argument("--backlight-active-low", action="store_true",
                    help="the pin lights the backlight by going LOW (P-MOSFET switch)")
    ap.add_argument("--image", help="also show this image file, scaled to fit")
    ap.add_argument("--seconds", type=float, default=1.0, help="how long to hold each step")
    args = ap.parse_args()

    try:
        import board
        import busio
        import digitalio
        from adafruit_rgb_display import ili9341
        from PIL import Image, ImageDraw
    except ImportError as e:
        sys.exit(f"A library is missing ({e}). On the Pi:\n"
                 "  .venv/bin/pip install adafruit-blinka "
                 "adafruit-circuitpython-rgb-display pillow\n"
                 "Blinka needs SPI enabled too: sudo raspi-config -> Interface Options "
                 "-> SPI, then reboot.")

    def pin(number: int):
        return digitalio.DigitalInOut(getattr(board, f"D{number}"))

    # The constructor always gets the panel's own dimensions; the images have to be
    # the rotated shape, which is the other way round at 90 and 270.
    WIDTH, HEIGHT = ((PANEL_HEIGHT, PANEL_WIDTH) if args.rotation % 180 == 90
                     else (PANEL_WIDTH, PANEL_HEIGHT))

    try:
        spi = busio.SPI(clock=board.SCK, MOSI=board.MOSI)
        display = ili9341.ILI9341(
            spi, cs=pin(8 if args.cs == 0 else 7), dc=pin(args.dc), rst=pin(args.reset),
            baudrate=args.baudrate, width=PANEL_WIDTH, height=PANEL_HEIGHT,
            rotation=args.rotation,
        )
    except Exception as e:
        sys.exit(f"Couldn't open the display ({e}).\n{FAILURES}")

    backlight = None
    if args.backlight is not None:
        try:
            from gpiozero import DigitalOutputDevice
        except ImportError:
            sys.exit("--backlight needs gpiozero. On the Pi:\n"
                     "  sudo apt install -y python3-dev swig liblgpio-dev\n"
                     "  .venv/bin/pip install gpiozero lgpio")
        try:
            backlight = DigitalOutputDevice(args.backlight,
                                            active_high=not args.backlight_active_low,
                                            initial_value=True)
        except Exception as e:
            sys.exit(f"Couldn't open GPIO{args.backlight} for the backlight ({e}).\n"
                     "Check the pin, that nothing else is using it, and that your user is "
                     "in the gpio group.")

    light = "3V3 (always on)" if backlight is None else (
        f"GPIO{args.backlight} ({'active low' if args.backlight_active_low else 'active high'})")
    print(f"SPI0 CE{args.cs} · D/C GPIO{args.dc} · RESET GPIO{args.reset} · "
          f"{args.baudrate / 1e6:g} MHz · rotation {args.rotation} "
          f"({WIDTH}x{HEIGHT}, {'landscape' if WIDTH > HEIGHT else 'portrait'}) · "
          f"backlight {light}\n")

    print("1. Solid colours. Each should fill the whole panel, edge to edge.")
    for name, rgb in COLOURS:
        print(f"   {name}")
        display.image(Image.new("RGB", (WIDTH, HEIGHT), rgb))
        time.sleep(args.seconds)

    print("\n2. A border and crossed diagonals: proves the whole panel is addressed and "
          "nothing is cut off or shifted.")
    frame = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    draw.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(255, 255, 255), width=3)
    draw.line([0, 0, WIDTH - 1, HEIGHT - 1], fill=(255, 0, 0), width=2)
    draw.line([WIDTH - 1, 0, 0, HEIGHT - 1], fill=(0, 255, 0), width=2)
    # A filled corner square says which way up the panel is.
    draw.rectangle([8, 8, 48, 48], fill=(0, 0, 255))
    display.image(frame)
    print("   The blue square marks the TOP LEFT. If it isn't top left, try --rotation.")
    time.sleep(max(args.seconds, 2.0))

    print("\n3. A colour ramp: banding or speckle here means the SPI clock is too fast "
          "(--baudrate 16000000).")
    ramp = Image.new("RGB", (WIDTH, HEIGHT))
    ramp_draw = ImageDraw.Draw(ramp)
    for y in range(HEIGHT):
        level = round(255 * y / (HEIGHT - 1))
        ramp_draw.line([0, y, WIDTH, y], fill=(level, round(level / 2), 255 - level))
    display.image(ramp)
    time.sleep(max(args.seconds, 2.0))

    if args.image:
        print(f"\n4. {args.image}, scaled to fit and centred — what a real answer looks like.")
        try:
            with Image.open(args.image) as photo:
                photo = photo.convert("RGB")
                photo.thumbnail((WIDTH, HEIGHT), Image.LANCZOS)
                canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
                canvas.paste(photo, ((WIDTH - photo.width) // 2, (HEIGHT - photo.height) // 2))
                display.image(canvas)
        except Exception as e:
            print(f"   couldn't show it ({e})")
        time.sleep(max(args.seconds, 3.0))

    if backlight is not None:
        print("\n5. The backlight, three times, while a white screen is up. It should go "
              "fully dark each time — not dim grey.")
        display.image(Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255)))
        for _ in range(3):
            backlight.off()
            time.sleep(0.6)
            backlight.on()
            time.sleep(0.6)
        print("   If it never went dark, the pin or its polarity is wrong: try "
              "--backlight-active-low.")
        print("   If it never came back on, the pin can't supply the backlight's "
              "current — it needs the P-MOSFET switch (docs/hardware.md).")

    display.fill(0)
    if backlight is not None:
        backlight.off()
        backlight.close()
    print("\nBlanked. If every pass looked right, the wiring is good.")
    print(FAILURES)


if __name__ == "__main__":
    main()
