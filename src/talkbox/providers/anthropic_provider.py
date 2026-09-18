from __future__ import annotations

import anthropic

from talkbox.providers.base import ChatMessage, ModelReply, ProviderError


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str,
        effort: str | None = None,
        max_tokens: int = 4000,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # Credentials resolve from ANTHROPIC_API_KEY (loaded from .env by the CLI).
        self._client = client or anthropic.Anthropic()

    def generate(self, system: str, messages: list[ChatMessage]) -> ModelReply:
        # Haiku 4.5 rejects `effort`, so only send it when configured.
        extra = {"output_config": {"effort": self.effort}} if self.effort else {}
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": m.role, "content": m.text} for m in messages],
                **extra,
            )
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"network error: {e}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("rate limited") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}") from e

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return ModelReply(
            text=text,
            model=response.model,
            refused=response.stop_reason == "refusal",
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
