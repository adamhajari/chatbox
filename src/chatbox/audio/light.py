"""The status light: what Chatbox is doing, for a child who can't read yet (PLAN.md
section 1 — all feedback to the kids is audio or lights).

`LitSpeaker` wraps any `Speaker` and drives a `Light` from the cues and playback that
already pass through it, so the voice turn needs no changes and stays free of hardware:
the cue it plays to say "I'm listening" lights the LED green by the same act.

    listening   green    the button is down, audio is being captured
    thinking    amber    released; speech-to-text, the checks and the answer are running
    speaking    blue     the answer is being played
    error       red      something failed and the child heard an error sound
    idle        off
"""

from __future__ import annotations

from typing import Iterable, Literal, Protocol

State = Literal["idle", "listening", "thinking", "speaking", "error"]

# Which state each of the voice turn's cues means.
_CUE_STATE: dict[str, State] = {
    "listening": "listening",
    "stopped": "thinking",   # button released; the pipeline takes over
    "cancel": "idle",        # too short to be a real press
    "error": "error",
}


class Light(Protocol):
    def show(self, state: State) -> None: ...

    def close(self) -> None: ...


class NoLight:
    """Used when no LED is wired, so nothing else needs to check."""

    def show(self, state: State) -> None:
        pass

    def close(self) -> None:
        pass


class LitSpeaker:
    """A `Speaker` that also drives a `Light`. Delegates all audio unchanged."""

    def __init__(self, speaker, light: Light) -> None:
        self.speaker, self.light = speaker, light

    def cue(self, name: str, **kwargs) -> None:
        # The light is set first: a cue may block until its sound has played.
        self.light.show(_CUE_STATE.get(name, "idle"))
        self.speaker.cue(name, **kwargs)

    def play(self, audio: Iterable[bytes], sample_rate: int) -> None:
        self.light.show("speaking")
        try:
            self.speaker.play(audio, sample_rate)
        finally:
            # Back to idle even if playback raised, so the light never lies. An error
            # cue may follow and turn it red.
            self.light.show("idle")

    def close(self) -> None:
        self.light.close()
