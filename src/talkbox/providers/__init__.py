from talkbox.providers.base import ChatMessage, ModelProvider, ModelReply, ProviderError

__all__ = ["ChatMessage", "ModelProvider", "ModelReply", "ProviderError", "make_provider"]


def make_provider(name: str, settings: dict) -> ModelProvider:
    if name == "anthropic":
        from talkbox.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(**settings)
    raise ValueError(f"unknown provider {name!r} (available: anthropic)")
