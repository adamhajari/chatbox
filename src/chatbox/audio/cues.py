"""Short tones for audio feedback (neither child reads yet, so every state is a sound)."""

from __future__ import annotations

import math
from array import array

# (frequency Hz, seconds) per note
_CUES: dict[str, list[tuple[float, float]]] = {
    "listening": [(660, 0.07), (880, 0.09)],   # rising: "go ahead"
    "stopped": [(880, 0.07), (660, 0.09)],     # falling: "got it"
    "cancel": [(440, 0.08)],                   # soft blip: tap too short, nothing sent
    "error": [(330, 0.15), (262, 0.25)],       # low and slow: something went wrong
}


def cue_pcm(name: str, sample_rate: int, volume: float = 0.25) -> bytes:
    out = array("h")
    fade = int(sample_rate * 0.008)  # avoid clicks
    for freq, seconds in _CUES[name]:
        n = int(sample_rate * seconds)
        for i in range(n):
            env = min(1.0, i / fade, (n - 1 - i) / fade)
            out.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * i / sample_rate)))
    return out.tobytes()
