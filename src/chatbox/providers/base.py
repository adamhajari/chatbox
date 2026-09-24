"""Provider interface. The pipeline only talks to `ModelProvider`, so swapping in
another backend (e.g. Gemini on Vertex AI) means adding one class here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    text: str


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    # True when the provider declined (safety refusal); the pipeline substitutes a canned reply.
    refused: bool = False
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """Any failure talking to the model (network, auth, rate limit, timeout, bad response)."""


class ModelProvider(Protocol):
    name: str
    model: str

    def generate(self, system: str, messages: list[ChatMessage]) -> ModelReply: ...

    def generate_structured(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a JSON object matching `schema` (a plain JSON Schema). Raises
        ProviderError on refusal, truncation, or output that isn't valid JSON."""
        ...
