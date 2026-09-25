"""OpenAI text-to-speech (gpt-4o-mini-tts), streamed as raw PCM.

Credentials: OPENAI_API_KEY, loaded from .env by the CLI. OpenAI's usage policies
require telling listeners that the voice is AI-generated, not a human.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterator

from chatbox.speech.base import SpeechError

# "pcm" from the speech endpoint is always 24 kHz, 16-bit little-endian mono, no header.
_PCM_RATE = 24_000

DEFAULT_INSTRUCTIONS = (
    "Speak warmly and gently at a relaxed pace, like a friendly helper talking to a "
    "curious young child. Sound interested, not sing-song."
)


class OpenAITextToSpeech:
    name = "openai"
    sample_rate = _PCM_RATE

    def __init__(
        self,
        voice: str = "marin",
        model: str = "gpt-4o-mini-tts",
        instructions: str = DEFAULT_INSTRUCTIONS,
        language: str = "en-US",  # unused: the model speaks the language of the text
        timeout_seconds: float = 10.0,
        client=None,
    ) -> None:
        self.voice, self.model, self.instructions = voice, model, instructions
        self.timeout_seconds = timeout_seconds
        if client is None:
            import openai

            from chatbox.providers.base import keepalive_limits

            # One retry at most: a second failure is better answered with the error cue
            # than with a long silence.
            client = openai.OpenAI(
                max_retries=1, http_client=openai.DefaultHttpxClient(limits=keepalive_limits(openai)))
        self._client = client

    def warm(self) -> None:
        """Open the connection now (a free request), so speaking starts sooner."""
        self._client.models.retrieve(self.model)

    def _stream(self, text: str) -> Iterator[bytes]:
        with self._client.audio.speech.with_streaming_response.create(
            model=self.model, voice=self.voice, input=text, instructions=self.instructions,
            response_format="pcm", timeout=self.timeout_seconds,
        ) as response:
            yield from response.iter_bytes()

    def synthesize(self, text: str) -> Iterator[bytes]:
        # Same shape as GoogleTextToSpeech: read the stream as fast as it arrives (in a
        # thread) so a long answer can't hit the deadline while the speaker is playing.
        chunks: queue.Queue = queue.Queue()
        done = object()

        def pull() -> None:
            import openai

            leftover = b""
            try:
                for chunk in self._stream(text):
                    # HTTP chunks can split a 16-bit sample; only hand out whole samples.
                    chunk = leftover + chunk
                    cut = len(chunk) - len(chunk) % 2
                    leftover = chunk[cut:]
                    if cut:
                        chunks.put(chunk[:cut])
                chunks.put(done)
            except openai.OpenAIError as e:
                chunks.put(SpeechError(f"{type(e).__name__}: {e}"))
            except Exception as e:  # noqa: BLE001 - surface anything else to the caller
                chunks.put(SpeechError(f"{type(e).__name__}: {e}"))

        threading.Thread(target=pull, daemon=True, name="chatbox-tts").start()
        while (item := chunks.get()) is not done:
            if isinstance(item, SpeechError):
                raise item
            yield item
