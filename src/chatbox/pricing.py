"""What a question cost: token prices per model (chatbox.toml [pricing]) applied to the
usage each model call reports. An estimate: it uses the prices in the file, which a
parent keeps current, not the vendor's bill."""

from __future__ import annotations

from dataclasses import dataclass

from chatbox.providers.base import Usage


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cached_input: float | None = None  # None = cached tokens bill at the input price


def parse_prices(section: dict) -> dict[str, Price]:
    """[pricing] in chatbox.toml: `"model" = { input = 1.0, output = 5.0 }`."""
    prices = {}
    for model, p in section.items():
        cached = p.get("cached_input")
        prices[model] = Price(float(p["input"]), float(p["output"]),
                              None if cached is None else float(cached))
    return prices


def price_for(model: str, prices: dict[str, Price]) -> Price | None:
    """The price for `model`. APIs report a dated snapshot ("claude-haiku-4-5-20251001"),
    so the longest listed name the model starts with counts too."""
    if model in prices:
        return prices[model]
    matches = [name for name in prices if model.startswith(name + "-")]
    return prices[max(matches, key=len)] if matches else None


def cost_usd(usage: Usage, prices: dict[str, Price]) -> float | None:
    """None when the model has no listed price or the vendor didn't report usage."""
    price = price_for(usage.model, prices)
    if price is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cached_rate = price.input if price.cached_input is None else price.cached_input
    return ((usage.input_tokens - cached) * price.input + cached * cached_rate
            + usage.output_tokens * price.output) / 1_000_000


def format_cost(usd: float | None) -> str:
    """"$0.000150": a question costs a fraction of a cent, so show millionths."""
    return "cost unknown" if usd is None else f"${usd:.6f}"
