"""Google Cloud Speech-to-Text v2 and Text-to-Speech, both streaming.

Credentials: GOOGLE_APPLICATION_CREDENTIALS (service-account key file) and
GOOGLE_CLOUD_PROJECT, loaded from .env by the CLI. Audio logging is off by default
on Google's side (it is opt-in), and nothing here stores audio.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Iterable, Iterator

from google.api_core import exceptions as gexc
from google.api_core.client_options import ClientOptions

from talkbox.speech.base import SpeechError, Transcript

# Streaming requests may carry at most 25,600 bytes of audio each.
_MAX_CHUNK = 25_600


def _speech_error(e: Exception) -> SpeechError:
    return SpeechError(f"{type(e).__name__}: {e}")


class GoogleSpeechToText:
    name = "google"

    def __init__(
        self,
        model: str = "long",
        location: str = "global",
        language: str = "en-US",
        stream_limit_seconds: float = 60.0,
        project: str | None = None,
        client=None,
    ) -> None:
        self.model, self.location, self.language = model, location, language
        # Cap on the whole stream, recording included. The voice turn enforces the
        # (much shorter) deadline for the transcript after the button is released.
        self.stream_limit_seconds = stream_limit_seconds
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not self.project:
            raise SpeechError("GOOGLE_CLOUD_PROJECT is not set (see README: speech setup)")
        if client is None:
            from google.cloud.speech_v2 import SpeechClient

            endpoint = ("speech.googleapis.com" if location == "global"
                        else f"{location}-speech.googleapis.com")
            client = SpeechClient(client_options=ClientOptions(api_endpoint=endpoint))
        self._client = client

    def _requests(self, audio: Iterable[bytes], sample_rate: int):
        from google.cloud.speech_v2 import types

        config = types.RecognitionConfig(
            explicit_decoding_config=types.ExplicitDecodingConfig(
                encoding=types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=sample_rate,
                audio_channel_count=1,
            ),
            language_codes=[self.language],
            model=self.model,
            features=types.RecognitionFeatures(enable_automatic_punctuation=True),
        )
        yield types.StreamingRecognizeRequest(
            recognizer=f"projects/{self.project}/locations/{self.location}/recognizers/_",
            streaming_config=types.StreamingRecognitionConfig(config=config),
        )
        for chunk in audio:
            for i in range(0, len(chunk), _MAX_CHUNK):
                yield types.StreamingRecognizeRequest(audio=chunk[i:i + _MAX_CHUNK])

    def transcribe(self, audio: Iterable[bytes], sample_rate: int) -> Transcript:
        parts: list[str] = []
        confidences: list[float] = []
        try:
            for response in self._client.streaming_recognize(
                requests=self._requests(audio, sample_rate), timeout=self.stream_limit_seconds
            ):
                for result in response.results:
                    if result.is_final and result.alternatives:
                        alt = result.alternatives[0]
                        parts.append(alt.transcript.strip())
                        if alt.confidence:
                            confidences.append(alt.confidence)
        except gexc.GoogleAPIError as e:
            raise _speech_error(e) from e
        text = " ".join(p for p in parts if p)
        return Transcript(text, min(confidences) if confidences else None)


class GoogleTextToSpeech:
    name = "google"

    def __init__(
        self,
        voice: str = "en-US-Chirp3-HD-Leda",
        language: str = "en-US",
        sample_rate: int = 24_000,
        timeout_seconds: float = 10.0,
        client=None,
    ) -> None:
        self.voice, self.language, self.sample_rate = voice, language, sample_rate
        self.timeout_seconds = timeout_seconds
        if client is None:
            from google.cloud import texttospeech

            client = texttospeech.TextToSpeechClient()
        self._client = client

    def _requests(self, text: str):
        from google.cloud import texttospeech as tts

        yield tts.StreamingSynthesizeRequest(streaming_config=tts.StreamingSynthesizeConfig(
            voice=tts.VoiceSelectionParams(language_code=self.language, name=self.voice),
            streaming_audio_config=tts.StreamingAudioConfig(
                audio_encoding=tts.AudioEncoding.PCM, sample_rate_hertz=self.sample_rate),
        ))
        yield tts.StreamingSynthesizeRequest(input=tts.StreamingSynthesisInput(text=text))

    def synthesize(self, text: str) -> Iterator[bytes]:
        # The deadline covers the whole stream, and playback is slower than synthesis,
        # so read the stream as fast as it arrives (in a thread) and hand chunks out
        # from a buffer. Otherwise a long answer times out partway through playback.
        chunks: queue.Queue = queue.Queue()
        done = object()

        def pull() -> None:
            try:
                for response in self._client.streaming_synthesize(
                    self._requests(text), timeout=self.timeout_seconds
                ):
                    if response.audio_content:
                        chunks.put(response.audio_content)
                chunks.put(done)
            except gexc.GoogleAPIError as e:
                chunks.put(_speech_error(e))
            except Exception as e:  # noqa: BLE001 - surface anything else to the caller
                chunks.put(SpeechError(f"{type(e).__name__}: {e}"))

        threading.Thread(target=pull, daemon=True, name="talkbox-tts").start()
        while (item := chunks.get()) is not done:
            if isinstance(item, SpeechError):
                raise item
            yield item
