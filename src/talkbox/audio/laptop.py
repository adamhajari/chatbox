"""Laptop adapter: hold the spacebar to talk (PLAN.md D5), built-in mic and speakers.

A terminal only reports key presses, not releases. Holding a key makes the OS repeat
it, so "held" means repeats keep arriving and "released" means they stopped. A press
with no repeat at all is a tap, and sends nothing.

Audio goes through sounddevice/PortAudio, which works the same on macOS and the Pi.
Nothing is written to disk.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal

from talkbox.audio.cues import cue_pcm


@dataclass(frozen=True)
class PushToTalkSettings:
    sample_rate: int = 16_000
    max_seconds: float = 30.0            # a stuck key can't record forever
    # How long to wait for the keyboard's first auto-repeat before calling it a tap.
    # macOS's slowest "Delay until repeat" setting needs up to ~2 s; the default ~0.5 s.
    key_repeat_wait_seconds: float = 0.6
    # Once repeats are arriving, a gap this long means the key was released.
    release_gap_seconds: float = 0.2
    input_device: str | int | None = None   # None = system default
    output_device: str | int | None = None
    # GPIO pin of the Pi's arcade button (docs/hardware.md). None = spacebar only.
    button_gpio: int | None = None
    # GPIO pins of the status LED as [red, green, blue]. None = no light.
    led_gpio: list[int] | None = None
    led_common_anode: bool = True   # long leg to 3V3; False = common cathode (to GND)


class HoldDetector:
    """Decides from key-repeat timing whether the key is still held."""

    def __init__(self, repeat_wait: float, release_gap: float, now: float) -> None:
        self.repeat_wait, self.release_gap = repeat_wait, release_gap
        self.last = now
        self.repeats = 0

    def key(self, now: float) -> None:
        self.last = now
        self.repeats += 1

    @property
    def confirmed(self) -> bool:
        """True once the key has repeated, i.e. it's a hold rather than a tap."""
        return self.repeats > 0

    def released(self, now: float) -> bool:
        gap = self.release_gap if self.repeats else self.repeat_wait
        return now - self.last > gap


class Keyboard:
    """Unbuffered, unechoed key reads from the terminal (restored on exit)."""

    def __enter__(self) -> Keyboard:
        if not sys.stdin.isatty():
            raise RuntimeError("talkbox talk needs an interactive terminal (for the spacebar)")
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def read(self, timeout: float | None) -> str | None:
        """All keys typed so far (or within `timeout`), or None."""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self._fd, 1024).decode(errors="ignore")

    def flush(self) -> None:
        """Drop keys pressed while Talkbox was busy, so they don't start a new turn."""
        termios.tcflush(self._fd, termios.TCIFLUSH)

    def wait_command(self) -> Literal["talk", "new", "quit"]:
        while True:
            keys = (self.read(None) or "").lower()
            if "q" in keys or "\x04" in keys:
                return "quit"
            if "n" in keys:
                return "new"
            if " " in keys:
                return "talk"


class KeyboardPress:
    """The spacebar as a push-to-talk source (PLAN.md D5, laptop side).

    A terminal reports presses but never releases, so "still held" means auto-repeats
    keep arriving and "released" means they stopped. A press with no repeat at all is a
    tap and sends nothing.
    """

    name = "SPACE"

    def __init__(self, keyboard: Keyboard, repeat_wait: float, release_gap: float) -> None:
        self.keyboard = keyboard
        self.repeat_wait, self.release_gap = repeat_wait, release_gap
        self._hold = HoldDetector(repeat_wait, release_gap, time.monotonic())

    def poll_command(self, timeout: float) -> Literal["talk", "new", "quit"] | None:
        keys = (self.keyboard.read(timeout) or "").lower()
        if "q" in keys or "\x04" in keys:
            return "quit"
        if "n" in keys:
            return "new"
        if " " in keys:
            return "talk"
        return None

    def begin(self) -> None:
        self._hold = HoldDetector(self.repeat_wait, self.release_gap, time.monotonic())

    def poll_hold(self, timeout: float) -> None:
        if " " in (self.keyboard.read(timeout) or ""):
            self._hold.key(time.monotonic())

    def held(self, now: float) -> bool:
        return not self._hold.released(now)

    @property
    def confirmed(self) -> bool:
        return self._hold.confirmed

    def flush(self) -> None:
        self.keyboard.flush()


class LaptopSpeaker:
    def __init__(self, device: str | int | None = None, cue_rate: int = 24_000) -> None:
        import sounddevice as sd

        self._sd, self.device, self.cue_rate = sd, device, cue_rate

    def _write(self, chunks: Iterable[bytes], sample_rate: int) -> None:
        with self._sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16",
                                      device=self.device) as stream:
            for chunk in chunks:
                stream.write(chunk)
        # Leaving the `with` block stops the stream once queued audio has played.

    def cue(self, name: str, wait: bool = False) -> None:
        t = threading.Thread(target=self._write, args=([cue_pcm(name, self.cue_rate)], self.cue_rate),
                             daemon=True)
        t.start()
        if wait:
            t.join()

    def play(self, audio: Iterable[bytes], sample_rate: int) -> None:
        self._write(audio, sample_rate)


class PushToTalkMic:
    """Records through sounddevice/PortAudio while a `Press` is held.

    Used on the laptop and on the Pi: PortAudio behaves the same on both, and which
    thing is being held is the `Press`'s problem, not this class's.
    """

    def __init__(self, press, speaker: LaptopSpeaker,
                 settings: PushToTalkSettings = PushToTalkSettings()) -> None:
        import sounddevice as sd

        self._sd, self.press, self.speaker, self.settings = sd, press, speaker, settings

    def record(self) -> Iterator[bytes]:
        """Call right after a press is reported. Yields PCM while it's held; yields
        nothing at all for a tap."""
        s = self.settings
        self.press.begin()
        # Cue first, then open the mic, so the beep isn't recorded.
        self.speaker.cue("listening", wait=True)
        chunks: queue.Queue[bytes] = queue.Queue()
        stream = self._sd.RawInputStream(
            samplerate=s.sample_rate, channels=1, dtype="int16", device=s.input_device,
            blocksize=s.sample_rate // 10, callback=lambda data, *_: chunks.put(bytes(data)))
        held: list[bytes] = []  # audio before the hold is confirmed (dropped on a tap)
        started = time.monotonic()
        with stream:
            while True:
                self.press.poll_hold(0.02)
                now = time.monotonic()
                if not self.press.held(now) or now - started > s.max_seconds:
                    break
                while not chunks.empty():
                    held.append(chunks.get_nowait())
                if self.press.confirmed and held:
                    yield from held
                    held.clear()
        if self.press.confirmed:
            self.speaker.cue("stopped")
            yield from held
            while not chunks.empty():
                yield chunks.get_nowait()
