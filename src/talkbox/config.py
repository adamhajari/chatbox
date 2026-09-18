"""Runtime settings from talkbox.toml (provider, model names, file paths)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    policy_path: Path
    database_path: Path
    provider_name: str
    provider_settings: dict
    logging_enabled: bool
    max_history_exchanges: int


def load_settings(path: str | Path = "talkbox.toml") -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    provider = data["provider"]
    name = provider["name"]
    return Settings(
        policy_path=base / data["paths"]["policy"],
        database_path=base / data["paths"]["database"],
        provider_name=name,
        provider_settings=dict(provider.get(name, {})),
        logging_enabled=bool(data.get("logging", {}).get("enabled", False)),
        max_history_exchanges=int(data.get("chat", {}).get("max_history_exchanges", 20)),
    )
