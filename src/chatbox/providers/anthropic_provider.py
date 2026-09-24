from __future__ import annotations

import json
from typing import Any

import anthropic

from chatbox.providers.base import ChatMessage, ModelReply, ProviderError


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str,
        effort: str | None = None,
        max_tokens: int = 4000,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # Credentials resolve from ANTHROPIC_API_KEY (loaded from .env by the CLI).
        opts: dict[str, Any] = {"max_retries": max_retries}
        if timeout_seconds is not None:
            opts["timeout"] = float(timeout_seconds)
        self._client = client or anthropic.Anthropic(**opts)

    def _create(self, system: str, messages: list[ChatMessage], output_config: dict):
        # Haiku 4.5 rejects `effort`, so only send it when configured.
        if self.effort:
            output_config = {**output_config, "effort": self.effort}
        extra = {"output_config": output_config} if output_config else {}
        try:
            return self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": m.role, "content": m.text} for m in messages],
                **extra,
            )
        except anthropic.APITimeoutError as e:
            raise ProviderError("timed out") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"network error: {e}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("rate limited") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}") from e

    def generate(self, system: str, messages: list[ChatMessage]) -> ModelReply:
        response = self._create(system, messages, {})
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return ModelReply(
            text=text,
            model=response.model,
            refused=response.stop_reason == "refusal",
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def generate_structured(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._create(
            system, messages, {"format": {"type": "json_schema", "schema": schema}}
        )
        if response.stop_reason != "end_turn":
            raise ProviderError(f"structured output stopped early: {response.stop_reason}")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"unparseable structured output: {e}") from e
        if not isinstance(data, dict):
            raise ProviderError("structured output is not a JSON object")
        return data
