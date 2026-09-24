#!/usr/bin/env python3
"""Put a real answer's picture on the screen, with no microphone or speaker.

`scripts/screen_check.py` proves the panel. This proves everything above it — the
classifier reporting a subject, Wikipedia, the cache and the display — on a Pi whose
audio hardware isn't wired yet (PLAN.md phase 6b). Run it on the Pi:

    .venv/bin/python scripts/screen_demo.py "why is the sky blue?"
    .venv/bin/python scripts/screen_demo.py --subject octopus     # skip the model
    .venv/bin/python scripts/screen_demo.py --subject octopus --no-screen
    .venv/bin/python scripts/screen_demo.py --loop                # the default questions

Pins, the timeout and the backlight come from `chatbox.toml` plus `chatbox.local.toml`,
the same as `chatbox talk`, so this tests the settings too. `--no-screen` fetches
without a panel, which also works on the laptop.

Every stage is timed. The number that matters is **fetch**: it runs off the answering
path in the real thing (D4), so it is reported but never waited on.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

QUESTIONS = [
    "why is the sky blue?",
    "how do octopuses change colour?",
    "what is a volcano?",
    "why do we have to sleep?",       # no subject: the screen should stay dark
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*", help="what a child would ask")
    ap.add_argument("--subject", help="skip the classifier and look this up directly")
    ap.add_argument("--loop", action="store_true", help="run the default questions")
    ap.add_argument("--no-screen", action="store_true",
                    help="fetch but don't open the display (works on the laptop)")
    ap.add_argument("--backlight", type=int, metavar="GPIO",
                    help="backlight pin, overriding chatbox.toml (as screen_check.py)")
    ap.add_argument("--backlight-active-low", action="store_true",
                    help="the pin lights the backlight by going LOW (P-MOSFET switch)")
    ap.add_argument("--seconds", type=float, default=6.0, help="how long to hold each picture")
    ap.add_argument("--config", default=str(ROOT / "chatbox.toml"))
    args = ap.parse_args()

    from dotenv import load_dotenv

    from dataclasses import replace

    from chatbox.config import ScreenSettings, load_settings
    from chatbox.pictures import PictureFinder

    load_dotenv(ROOT / ".env")
    settings = load_settings(args.config)
    screen_settings = settings.screen or ScreenSettings()
    if args.backlight is not None:
        screen_settings = replace(screen_settings, backlight_gpio=args.backlight,
                                  backlight_active_high=not args.backlight_active_low)
    if settings.screen is None and not args.no_screen:
        print("note: [screen] enabled is false, so these are the default pins.")
        if screen_settings.backlight_gpio is None:
            # The failure this prevents: the picture is drawn correctly onto a panel
            # whose backlight was never switched on, which looks like nothing happened.
            print("      The backlight is unset, so if it is on a GPIO rather than 3V3\n"
                  "      the screen will stay DARK. Pass --backlight 12, or set\n"
                  "      backlight_gpio in chatbox.local.toml.")
        print()

    display = None
    if not args.no_screen:
        from chatbox.audio.pi import Screen

        try:
            display = Screen(screen_settings.dc_gpio, screen_settings.reset_gpio,
                             screen_settings.cs, screen_settings.baudrate,
                             screen_settings.rotation, screen_settings.backlight_gpio,
                             screen_settings.backlight_active_high)
        except RuntimeError as e:
            sys.exit(f"{e}\n\nRun `scripts/screen_check.py` first: it tests the panel "
                     "on its own.")
        light = ("3V3, always on" if screen_settings.backlight_gpio is None
                 else f"GPIO{screen_settings.backlight_gpio}")
        print(f"screen: D/C GPIO{screen_settings.dc_gpio} · RESET "
              f"GPIO{screen_settings.reset_gpio} · backlight {light}")

    cache = screen_settings.cache_dir or settings.database_path.parent / "pictures"
    from chatbox.audio.pi import image_size

    size = display.size if display is not None else image_size(screen_settings.rotation)
    finder = PictureFinder(cache, screen_settings.timeout_seconds, size)
    print(f"pictures {size[0]}x{size[1]} "
          f"({'landscape' if size[0] > size[1] else 'portrait'}), cached in {cache}, "
          f"{screen_settings.timeout_seconds:g}s timeout\n")

    classifier = None
    if args.subject is None:
        from chatbox.guardrails import ModelClassifier
        from chatbox.policy import load_policy
        from chatbox.providers import make_provider

        policy = load_policy(settings.policy_path)
        classifier = ModelClassifier(
            make_provider(settings.provider_name,
                          settings.guardrails.classifier.provider_settings),
            settings.guardrails.history_exchanges)

    def subject_for(question: str) -> str | None:
        """What the real pipeline would hand the screen for this question."""
        started = time.monotonic()
        try:
            classification = classifier.classify(question, [], policy)
        except Exception as e:
            print(f"  classify FAILED ({type(e).__name__}: {e}) — in the real thing this "
                  "fails closed and the child hears the error reply")
            return None
        ms = (time.monotonic() - started) * 1000
        # Only an "allow" shows a picture: nothing about a blocked question is
        # illustrated (see chatbox/audio/picture.py).
        subject = classification.subject if classification.decision == "allow" else None
        print(f"  classify {ms:6.0f} ms  {classification.decision}, subject={subject!r}")
        return subject

    def put_up(subject: str | None) -> None:
        if not subject:
            print("  no subject — the screen stays dark, and the answer is unchanged\n")
            if display is not None:
                display.blank()
                time.sleep(min(args.seconds, 2.0))
            return
        started = time.monotonic()
        picture = finder.find(subject)
        ms = (time.monotonic() - started) * 1000
        if picture is None:
            print(f"  fetch    {ms:6.0f} ms  no picture (no article, no image, or the "
                  "network is down)\n")
            if display is not None:
                display.blank()
            return
        name = picture.title or picture.subject   # the cache keeps the subject, not the title
        print(f"  fetch    {ms:6.0f} ms  {name!r} from {picture.source}")
        if display is None:
            path = Path(finder.cache_dir) / "screen-demo.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            picture.image.save(path)
            print(f"  saved    {path} (no screen opened)\n")
            return
        started = time.monotonic()
        display.show(picture.image)
        print(f"  draw     {(time.monotonic() - started) * 1000:6.0f} ms  on the panel now\n")
        time.sleep(args.seconds)

    try:
        if args.subject is not None:
            put_up(args.subject)
        else:
            for question in (args.question or (QUESTIONS if args.loop else [QUESTIONS[0]])):
                print(f"{question!r}")
                put_up(subject_for(question))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if display is not None:
            display.close()
            print("screen blanked.")


if __name__ == "__main__":
    main()
