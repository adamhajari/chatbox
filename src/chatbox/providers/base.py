"""Provider interface. The pipeline only talks to `ModelProvider`, so swapping in
another backend (e.g. Gemini on Vertex AI) means adding one class here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]

# How long an idle HTTPS connection to a model or speech API is kept for reuse. The SDKs
# default to 5 s, shorter than a child takes to ask a question, so every question paid
# for new connections. Warm-ups on the button press (`warm()`) open them while the child
# is still talking; this keeps them open until the real calls go out.
KEEPALIVE_SECONDS = 60.0


def keepalive_limits(sdk):
    """Connection limits for `sdk.DefaultHttpxClient` (sdk = the anthropic or openai
    module). Built from whichever HTTP library that SDK version uses: httpx2 for
    anthropic 1.x / openai 3.x, httpx before that."""
    import importlib

    base = next(c for c in sdk.DefaultHttpxClient.__mro__[1:] if c.__name__ == "Client")
    http = importlib.import_module(base.__module__.split(".")[0])
    return http.Limits(max_connections=20, max_keepalive_connections=10,
                       keepalive_expiry=KEEPALIVE_SECONDS)


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    text: str


@dataclass(frozen=True)
class Usage:
    """Tokens one model call was billed for, as the vendor reported them."""

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # The part of input_tokens read from the vendor's prompt cache, which bills cheaper.
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    # True when the provider declined (safety refusal); the pipeline substitutes a canned reply.
    refused: bool = False
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int = 0

    @property
    def usage(self) -> Usage:
        return Usage(self.model, self.input_tokens, self.output_tokens, self.cached_input_tokens)


@dataclass(frozen=True)
class StructuredReply:
    data: dict[str, Any]
    usage: Usage | None = None


class ProviderError(Exception):
    """Any failure talking to the model (network, auth, rate limit, timeout, bad response)."""


class ModelProvider(Protocol):
    name: str
    model: str

    # Optional: `def warm(self) -> None` opens the connection ahead of a call (see
    # KEEPALIVE_SECONDS). Callers look it up with getattr and ignore its failures.

    def generate(self, system: str, messages: list[ChatMessage]) -> ModelReply: ...

    def generate_structured(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any]
    ) -> StructuredReply:
        """Return a JSON object matching `schema` (a plain JSON Schema), with the call's
        token usage. Raises ProviderError on refusal, truncation, or output that isn't
        valid JSON."""
        ...
