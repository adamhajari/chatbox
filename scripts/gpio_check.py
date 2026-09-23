#!/usr/bin/env python3
"""Prove the button and the status LED are wired correctly, before any audio exists.

Talkbox's own hardware adapter comes later; this only exercises the pins, so the
wiring can be checked on its own. Run it on the Pi:

    .venv/bin/python scripts/gpio_check.py                  # common cathode LED
    .venv/bin/python scripts/gpio_check.py --common-anode
    .venv/bin/python scripts/gpio_check.py --button 17 --red 22 --green 23 --blue 24

Pins default to the Phase 6b assignment, chosen to leave GPIO18/19/21 free for the
MAX98357A's I2S lines. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import sys
import time

# The three states the kid needs to tell apart (PLAN.md section 6), as (r, g, b).
STATES = [
    ("listening", (0, 1, 0)),   # green
    ("thinking", (1, 1, 0)),    # amber
    ("speaking", (0, 0, 1)),    # blue
    ("off", (0, 0, 0)),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--button", type=int, default=17)
    ap.add_argument("--red", type=int, default=22)
    ap.add_argument("--green", type=int, default=23)
    ap.add_argument("--blue", type=int, default=24)
    ap.add_argument("--common-anode", action="store_true",
                    help="LED's long leg goes to 3V3 rather than GND (inverts every write)")
    args = ap.parse_args()

    try:
        from gpiozero import RGBLED, Button
    except ImportError:
        sys.exit("gpiozero isn't installed. On the Pi:\n"
                 "  sudo apt install -y python3-dev\n"
                 "  .venv/bin/pip install gpiozero lgpio")

    try:
        # pwm=False keeps this to plain on/off per channel: no software PWM to go wrong,
        # and the three states only need full colours anyway.
        led = RGBLED(red=args.red, green=args.green, blue=args.blue,
                     active_high=not args.common_anode, pwm=False)
        button = Button(args.button, pull_up=True, bounce_time=0.05)
    except Exception as e:
        sys.exit(f"Couldn't open the GPIO pins ({e}).\n"
                 "Check the pin numbers, that nothing else is using them, and that your "
                 "user is in the gpio group (`groups` should list it; log out and back in "
                 "after `sudo usermod -aG gpio $USER`).")

    print(f"button GPIO{args.button} · LED GPIO{args.red}/{args.green}/{args.blue} "
          f"({'common anode' if args.common_anode else 'common cathode'})\n")

    print("1. Each colour on its own for a second. Watch for the name matching the colour.")
    for name, colour in (("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1))):
        print(f"   {name}")
        led.value = colour
        time.sleep(1.0)
    led.off()

    print("\n   If the LED stayed dark the whole time, it's probably common anode: "
          "re-run with --common-anode.")
    print("   If the colours came up in the wrong order, swap the --red/--green/--blue "
          "pins to match.\n")

    print("2. The three states Talkbox will show.")
    for name, colour in STATES:
        print(f"   {name}")
        led.value = colour
        time.sleep(1.2)

    print("\n3. Press and hold the button. It should light green while held.")
    print("   Ctrl-C when you've seen enough.\n")
    try:
        while True:
            button.wait_for_press()
            pressed = time.monotonic()
            led.value = (0, 1, 0)
            button.wait_for_release()
            led.off()
            print(f"   held {time.monotonic() - pressed:.2f} s")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        led.off()
        led.close()
        button.close()


if __name__ == "__main__":
    main()
