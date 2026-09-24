"""Speech interfaces. The voice turn only talks to `SpeechToText` and `TextToSpeech`,
so swapping vendors (or going local with Whisper/Piper) means adding one class here.

Audio everywhere is raw 16-bit little-endian mono PCM (`bytes`), so adapters and
services never need a shared audio library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Protocol


class SpeechError(Exception):
    """A speech service failed (network, quota, bad credentials, ...)."""


@dataclass(frozen=True)
class Transcript:
    text: str  # "" when no words were recognized
    confidence: float | None = None


class SpeechToText(Protocol):
    name: str
    model: str

    def transcribe(self, audio: Iterable[bytes], sample_rate: int) -> Transcript:
        """Stream `audio` to the service as it arrives; return once `audio` ends and the
        final transcript is in. Raises SpeechError."""
        ...


class TextToSpeech(Protocol):
    name: str
    voice: str
    sample_rate: int  # of the PCM it yields

    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield PCM chunks as soon as the service produces them. Raises SpeechError."""
        ...
