from __future__ import annotations

import json
from typing import Any

import openai

from chatbox.providers.base import ChatMessage, ModelReply, ProviderError, keepalive_limits


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str,
        reasoning_effort: str | None = None,
        max_tokens: int = 4000,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
        client: openai.OpenAI | None = None,
    ) -> None:
        self.model = model
        # "none" turns reasoning off: faster and cheaper, and plenty for short answers.
        # Unset means the model's default, which on current models reasons (and bills for it).
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        # Credentials resolve from OPENAI_API_KEY (loaded from .env by the CLI).
        opts: dict[str, Any] = {"max_retries": max_retries}
        if timeout_seconds is not None:
            opts["timeout"] = float(timeout_seconds)
        if client is None:
            opts["http_client"] = openai.DefaultHttpxClient(limits=keepalive_limits(openai))
        self._client = client or openai.OpenAI(**opts)

    def warm(self) -> None:
        """Open the connection now (a free request), so the next call skips the setup."""
        self._client.models.retrieve(self.model)

    def _create(self, system: str, messages: list[ChatMessage], extra: dict):
        if self.reasoning_effort:
            extra = {**extra, "reasoning_effort": self.reasoning_effort}
        try:
            return self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=self.max_tokens,
                messages=[{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.text} for m in messages],
                **extra,
            )
        except openai.APITimeoutError as e:
            raise ProviderError("timed out") from e
        except openai.APIConnectionError as e:
            raise ProviderError(f"network error: {e}") from e
        except openai.RateLimitError as e:
            raise ProviderError("rate limited") from e
        except openai.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}") from e

    def generate(self, system: str, messages: list[ChatMessage]) -> ModelReply:
        response = self._create(system, messages, {})
        choice = response.choices[0]
        # OpenAI reports a safety refusal as a separate `refusal` string, not a stop reason.
        refused = bool(choice.message.refusal) or choice.finish_reason == "content_filter"
        usage = response.usage
        return ModelReply(
            text=(choice.message.content or "").strip(),
            model=response.model,
            refused=refused,
            stop_reason="refusal" if refused else choice.finish_reason,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )

    def generate_structured(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, Any]:
        # Strict mode guarantees the output matches the schema; it needs every property
        # required and additionalProperties false, which the guardrail schemas already are.
        response = self._create(system, messages, {"response_format": {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema, "strict": True},
        }})
        choice = response.choices[0]
        if choice.message.refusal:
            raise ProviderError(f"structured output refused: {choice.message.refusal}")
        if choice.finish_reason != "stop":
            raise ProviderError(f"structured output stopped early: {choice.finish_reason}")
        try:
            data = json.loads(choice.message.content or "")
        except json.JSONDecodeError as e:
            raise ProviderError(f"unparseable structured output: {e}") from e
        if not isinstance(data, dict):
            raise ProviderError("structured output is not a JSON object")
        return data
