#!/usr/bin/env python3
"""Put a real answer's picture on the screen, with no microphone or speaker.

`scripts/screen_check.py` proves the panel. This proves everything above it — the
classifier reporting a subject and picture category, Wikipedia, the cache and the display — on a Pi whose
audio hardware isn't wired yet (PLAN.md phase 6b). Run it on the Pi:

    .venv/bin/python scripts/screen_demo.py "why is the sky blue?"
    .venv/bin/python scripts/screen_demo.py --subject python --category animal   # skip the model
    .venv/bin/python scripts/screen_demo.py --subject octopus --no-screen
    .venv/bin/python scripts/screen_demo.py --subject shark --opponent crocodile --category animal
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
    "who would win, a shark or a crocodile?",   # an animal battle: two pictures in turn
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*", help="what a child would ask")
    ap.add_argument("--subject", help="skip the classifier and look this up directly")
    ap.add_argument("--category", help="with --subject: the picture category the "
                                       "classifier would have picked (for example animal)")
    ap.add_argument("--opponent", help="with --subject: the second animal of an animal "
                                       "battle, shown in turn with the first")
    ap.add_argument("--loop", action="store_true", help="run the default questions")
    ap.add_argument("--no-screen", action="store_true",
                    help="fetch but don't open the display (works on the laptop)")
    ap.add_argument("--backlight", type=int, metavar="GPIO",
                    help="backlight pin, overriding chatbox.toml (as screen_check.py)")
    ap.add_argument("--backlight-active-low", action="store_true",
                    help="the pin lights the backlight by going LOW (P-MOSFET switch)")
    ap.add_argument("--seconds", type=float, default=6.0, help="how long to hold each question's picture(s)")
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
        reset = ("software" if screen_settings.reset_gpio is None
                 else f"GPIO{screen_settings.reset_gpio}")
        print(f"screen: D/C GPIO{screen_settings.dc_gpio} · RESET {reset} · "
              f"backlight {light}")

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
        from chatbox.config import ensure_policy
        from chatbox.policy import load_policy
        from chatbox.providers import make_provider

        policy = load_policy(ensure_policy(settings)[0])
        classifier = ModelClassifier(
            make_provider(settings.provider_name,
                          settings.guardrails.classifier.provider_settings),
            settings.guardrails.history_exchanges)

    def subjects_for(question: str) -> tuple[list[str], str | None]:
        """What the real pipeline would hand the screen for this question: one subject,
        or two for an animal battle."""
        started = time.monotonic()
        try:
            classification = classifier.classify(question, [], policy)
        except Exception as e:
            print(f"  classify FAILED ({type(e).__name__}: {e}) — in the real thing this "
                  "fails closed and the child hears the error reply")
            return [], None
        ms = (time.monotonic() - started) * 1000
        # Only an "allow" shows a picture: nothing about a blocked question is
        # illustrated (see chatbox/audio/picture.py).
        c = classification
        print(f"  classify {ms:6.0f} ms  {c.decision}, subject={c.subject!r}, "
              f"category={c.picture_category!r}"
              + (f", opponent={c.opponent!r}" if c.opponent else ""))
        if c.decision != "allow" or not c.subject or not c.picture_category:
            return [], None
        return [x for x in (c.subject, c.opponent) if x], c.picture_category

    def put_up(subjects: list[str], category: str | None) -> None:
        if not subjects:
            print("  no subject, or none of the policy's picture categories — the screen "
                  "stays dark, and the answer is unchanged\n")
            if display is not None:
                display.blank()
                time.sleep(min(args.seconds, 2.0))
            return
        pictures = []
        for subject in subjects:
            started = time.monotonic()
            picture = finder.find(subject, category)
            ms = (time.monotonic() - started) * 1000
            if picture is None:
                print(f"  fetch    {ms:6.0f} ms  {subject!r}: no picture (no article, no "
                      "image, or the network is down)")
                continue
            name = picture.title or picture.subject   # the cache keeps the subject, not the title
            print(f"  fetch    {ms:6.0f} ms  {name!r} from {picture.source}")
            pictures.append(picture)
        if not pictures:
            print()
            if display is not None:
                display.blank()
            return
        if display is None:
            Path(finder.cache_dir).mkdir(parents=True, exist_ok=True)
            for n, picture in enumerate(pictures, 1):
                path = Path(finder.cache_dir) / f"screen-demo-{n}.png"
                picture.image.save(path)
                print(f"  saved    {path} (no screen opened)")
            print()
            return
        started = time.monotonic()
        display.show(pictures[0].image)
        print(f"  draw     {(time.monotonic() - started) * 1000:6.0f} ms  on the panel now")
        if len(pictures) > 1:
            print(f"           swapping every {screen_settings.swap_seconds:g}s, "
                  f"as while an answer is spoken")
        print()
        # Hold for --seconds, taking turns the way PictureShow does during an answer.
        until, n = time.monotonic() + args.seconds, 0
        while (left := until - time.monotonic()) > 0:
            time.sleep(min(left, screen_settings.swap_seconds))
            if len(pictures) > 1 and time.monotonic() < until:
                n += 1
                display.show(pictures[n % len(pictures)].image)

    try:
        if args.subject is not None:
            put_up([x for x in (args.subject, args.opponent) if x], args.category)
        else:
            for question in (args.question or (QUESTIONS if args.loop else [QUESTIONS[0]])):
                print(f"{question!r}")
                put_up(*subjects_for(question))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if display is not None:
            display.close()
            print("screen blanked.")


if __name__ == "__main__":
    main()
