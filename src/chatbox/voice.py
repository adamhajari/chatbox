"""One spoken turn (pipeline B, PLAN.md D3):

    recording -> speech-to-text -> text pipeline (limits, checks, answer) -> text-to-speech

This is core code: it never touches a microphone, speaker or keyboard. An input/output
adapter (the laptop's in `chatbox.audio`, the Pi's later) hands it the recording as it
is captured and a `Speaker` to play through.

Guardrails stay in front of the speaker: nothing is synthesized until the text pipeline
has returned its final, checked text. Every path ends in something the child hears
(an answer, a canned reply, or an error sound), never silence.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Literal, Protocol

from chatbox.pipeline import Answer, Pipeline
from chatbox.speech import SpeechToText, TextToSpeech, Transcript

Cue = Literal["listening", "stopped", "cancel", "error"]

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chatbox-stt")


class Speaker(Protocol):
    def cue(self, name: Cue) -> None:
        """Play a short sound. May return before it finishes."""
        ...

    def play(self, audio: Iterable[bytes], sample_rate: int) -> None:
        """Play PCM chunks as they arrive; return when done. Errors raised while
        iterating `audio` must propagate."""
        ...


@dataclass(frozen=True)
class VoiceSettings:
    min_press_seconds: float = 0.3      # shorter presses are treated as accidental taps
    silence_rms: float = 200.0          # recordings quieter than this skip speech-to-text
    stt_timeout_seconds: float = 5.0    # deadline for the transcript after release


@dataclass
class VoiceResult:
    transcript: str | None
    spoken: str | None                  # text the child heard (None: only a cue)
    answer: Answer | None = None        # set when the text pipeline ran
    steps: list[dict] = field(default_factory=list)
    speech_start_ms: int | None = None  # end of question -> first audio (D4)


class _Recording:
    """Passes audio through to speech-to-text while measuring length and loudness,
    and notes when the recording ends (the button was released)."""

    def __init__(self, audio: Iterable[bytes], sample_rate: int) -> None:
        self._it = iter(audio)
        self.sample_rate = sample_rate
        self.samples = 0
        self._sum_sq = 0.0
        self._lock = threading.Lock()
        self.ended = threading.Event()
        self.ended_at: float | None = None
        self.last_read = time.monotonic()

    def __iter__(self) -> Iterator[bytes]:
        while True:
            with self._lock:
                if self.ended.is_set():
                    return
                try:
                    chunk = next(self._it)
                    self.last_read = time.monotonic()
                except StopIteration:
                    self.ended_at = time.monotonic()
                    self.ended.set()
                    return
                pcm = array("h", chunk[: len(chunk) - len(chunk) % 2])
                self.samples += len(pcm)
                self._sum_sq += sum(s * s for s in pcm)
            yield chunk

    def drain(self) -> None:
        for _ in self:
            pass

    @property
    def seconds(self) -> float:
        return self.samples / self.sample_rate

    @property
    def rms(self) -> float:
        return math.sqrt(self._sum_sq / self.samples) if self.samples else 0.0


def _ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


class VoiceTurn:
    def __init__(
        self,
        pipeline: Pipeline,
        stt: SpeechToText,
        tts: TextToSpeech,
        speaker: Speaker,
        settings: VoiceSettings = VoiceSettings(),
    ) -> None:
        self.pipeline, self.stt, self.tts, self.speaker = pipeline, stt, tts, speaker
        self.settings = settings

    @property
    def policy(self):
        return self.pipeline.policy

    def run(self, audio: Iterable[bytes], sample_rate: int) -> VoiceResult:
        """`audio` yields PCM while the button is held and ends on release."""
        steps: list[dict] = []

        def step(name: str, decision: str, detail: str = "", ms: int | None = None) -> None:
            steps.append({"step": name, "decision": decision, "detail": detail, "ms": ms})

        rec = _Recording(audio, sample_rate)
        transcript, error = self._transcribe(rec)
        released = rec.ended_at or time.monotonic()
        stt_ms = _ms(released)
        s = self.settings
        length = f"{rec.seconds:.1f}s audio"

        if rec.seconds < s.min_press_seconds:
            step("stt", "too_short", f"{length}; treated as an accidental tap", stt_ms)
            self.speaker.cue("cancel")
            return VoiceResult(None, None, steps=steps)
        if error is not None:
            step("stt", "error", f"{length}; {error}", stt_ms)
            return self._speak(self.policy.canned_replies.something_went_wrong, None, None,
                               steps, step, released)
        if rec.rms < s.silence_rms:
            step("stt", "silence", f"{length}; level {rec.rms:.0f} < {s.silence_rms:g}", stt_ms)
            return self._speak(self.policy.canned_replies.didnt_catch_that, None, None,
                               steps, step, released)
        assert transcript is not None
        if not transcript.text.strip():
            step("stt", "empty", f"{length}; no words recognized", stt_ms)
            return self._speak(self.policy.canned_replies.didnt_catch_that, "", None,
                               steps, step, released)
        conf = "" if transcript.confidence is None else f" (confidence {transcript.confidence:.2f})"
        step("stt", "ok", f"{length}: {transcript.text!r}{conf}", stt_ms)

        # The text pipeline is unchanged; its final, checked text is all that gets spoken.
        answer = self.pipeline.ask(transcript.text)
        steps.extend(answer.steps)
        step("pipeline", answer.answered_by, "", answer.latency_ms)
        return self._speak(answer.text, transcript.text, answer, steps, step, released)

    def _transcribe(self, rec: _Recording) -> tuple[Transcript | None, str | None]:
        """Stream the recording to speech-to-text while it's being made. Always returns
        after the recording has ended, even if the service failed partway through."""
        future = _POOL.submit(self.stt.transcribe, rec, rec.sample_rate)
        stalled = self.settings.stt_timeout_seconds
        while not rec.ended.wait(0.05):
            # Failed early, or stopped reading audio: keep recording until release anyway.
            if future.done() or time.monotonic() - rec.last_read > stalled:
                rec.drain()
        try:
            return future.result(timeout=self.settings.stt_timeout_seconds), None
        except FutureTimeout:
            return None, f"no transcript within {self.settings.stt_timeout_seconds:g}s"
        except Exception as e:  # noqa: BLE001 - the child still hears a reply
            return None, f"{type(e).__name__}: {e}"

    def _speak(self, text, transcript, answer, steps, step, released) -> VoiceResult:
        result = VoiceResult(transcript, text, answer, steps)
        started = time.monotonic()
        first: list[float] = []

        def audio() -> Iterator[bytes]:
            for chunk in self.tts.synthesize(text):
                if not first:
                    first.append(time.monotonic())
                yield chunk

        try:
            self.speaker.play(audio(), self.tts.sample_rate)
        except Exception as e:  # noqa: BLE001 - fall back to a sound, never silence
            step("tts", "error", f"{type(e).__name__}: {e}", _ms(started))
            if first:
                result.speech_start_ms = round((first[0] - released) * 1000)
            self.speaker.cue("error")
            return result
        if not first:
            step("tts", "empty", "no audio returned", _ms(started))
            self.speaker.cue("error")
            return result
        step("tts", "ok", f"{self.tts.name}/{self.tts.voice}; first audio",
             round((first[0] - started) * 1000))
        result.speech_start_ms = round((first[0] - released) * 1000)
        step("speech_start", "ok", "end of question -> first audio (D4: <= 5000 ms)",
             result.speech_start_ms)
        return result
