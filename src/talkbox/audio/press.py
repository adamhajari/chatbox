"""Push-to-talk sources: the things a child can hold down to talk (PLAN.md D5).

The laptop's spacebar and the Pi's arcade button answer the same three questions —
did something start, is it still held, was it a real hold rather than a tap — but they
answer them very differently. A terminal never reports a key release, so a held spacebar
has to be inferred from auto-repeat timing; a GPIO button reports press and release
directly. `Press` hides that difference, and `AnyPress` lets both work at once.

Nothing here touches audio. The recorder asks a `Press` whether to keep recording.
"""

from __future__ import annotations

import time
from typing import Literal, Protocol, Sequence

Command = Literal["talk", "new", "quit"]


class Press(Protocol):
    """One way to start and hold a turn."""

    name: str

    def poll_command(self, timeout: float) -> Command | None:
        """Wait up to `timeout` for this source to ask for something. None if it didn't."""
        ...

    def begin(self) -> None:
        """A turn just started from this source."""
        ...

    def poll_hold(self, timeout: float) -> None:
        """Called repeatedly while recording, to refresh what `held` reports."""
        ...

    def held(self, now: float) -> bool:
        """True while the press that started this turn is still down."""
        ...

    @property
    def confirmed(self) -> bool:
        """True once this is a real hold rather than an accidental tap."""
        ...

    def flush(self) -> None:
        """Drop anything that arrived while Talkbox was busy, so it doesn't start a turn."""
        ...


class AnyPress:
    """Several sources at once: whichever is used first owns the turn.

    Sources are polled in turn with a short timeout rather than waited on in threads,
    because one of them (the keyboard) owns the terminal and can't be read from two
    places at once.
    """

    def __init__(self, sources: Sequence[Press], poll_seconds: float = 0.02) -> None:
        if not sources:
            raise ValueError("AnyPress needs at least one source")
        self.sources = list(sources)
        self.poll_seconds = poll_seconds
        self.active: Press = self.sources[0]

    @property
    def name(self) -> str:
        return " or ".join(s.name for s in self.sources)

    def poll_command(self, timeout: float) -> Command | None:
        """Round-robin the sources until one asks for something."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for source in self.sources:
                command = source.poll_command(self.poll_seconds)
                if command is not None:
                    self.active = source
                    return command
            if deadline is not None and time.monotonic() > deadline:
                return None

    # Once a turn starts, everything else is the source that started it.
    def begin(self) -> None:
        self.active.begin()

    def poll_hold(self, timeout: float) -> None:
        self.active.poll_hold(timeout)

    def held(self, now: float) -> bool:
        return self.active.held(now)

    @property
    def confirmed(self) -> bool:
        return self.active.confirmed

    def flush(self) -> None:
        for source in self.sources:
            source.flush()
