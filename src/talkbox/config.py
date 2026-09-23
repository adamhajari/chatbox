"""Runtime settings from talkbox.toml (provider, model names, file paths).

Anything that differs per machine — audio device names, GPIO pins, the web host —
belongs in `talkbox.local.toml` beside it, which git ignores. It is merged over the
tracked file section by section, so it only needs the handful of keys that differ.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    policy_path: Path
    database_path: Path
    provider_name: str
    provider_settings: dict
    logging_enabled: bool
    max_history_exchanges: int
    guardrails: GuardrailSettings
    speech: SpeechSettings | None = None  # None when talkbox.toml has no [speech]
    web: WebSettings = field(default_factory=lambda: WebSettings())  # [web]: parent UI
    local_config: Path | None = None  # the talkbox.local.toml that was merged in, if any


@dataclass(frozen=True)
class WebSettings:
    host: str = "auto"  # "auto" = this computer's home-network address
    port: int = 8321


@dataclass(frozen=True)
class CheckSettings:
    provider_settings: dict  # model, max_tokens, max_retries, ... for this check's provider
    timeout_seconds: float   # hard deadline; past it the pipeline fails closed
    enabled: bool = True


@dataclass(frozen=True)
class GuardrailSettings:
    history_exchanges: int   # how many recent exchanges the checks see
    length_tolerance: float  # fraction over the policy's length limits still accepted
    classifier: CheckSettings
    output_check: CheckSettings


@dataclass(frozen=True)
class SpeechSettings:
    stt_name: str
    stt_settings: dict    # passed to the speech-to-text class
    tts_name: str
    tts_settings: dict    # passed to the text-to-speech class
    voice: dict           # [voice]: push-to-talk and turn settings


def _speech(data: dict) -> SpeechSettings | None:
    if "speech" not in data:
        return None
    sp = data["speech"]
    language = sp.get("language", "en-US")
    stt, tts = sp["stt"], sp["tts"]
    return SpeechSettings(
        stt_name=stt["name"],
        stt_settings={"language": language, **stt.get(stt["name"], {})},
        tts_name=tts["name"],
        tts_settings={"language": language, **tts.get(tts["name"], {})},
        voice=dict(data.get("voice", {})),
    )


def _check(data: dict) -> CheckSettings:
    settings = dict(data)
    enabled = bool(settings.pop("enabled", True))
    timeout = float(settings.get("timeout_seconds", 4.0))
    settings["timeout_seconds"] = timeout
    return CheckSettings(settings, timeout, enabled)


def _merge(base: dict, override: dict) -> dict:
    """`override` wins, but only for the keys it names: a local file that sets one
    device name must not wipe out the rest of the section it sits in."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# Every section talkbox.toml understands. A key outside one of these does nothing, and
# the most likely reason is a [section] header left commented out.
_SECTIONS = frozenset({"paths", "web", "chat", "logging", "provider", "guardrails",
                       "speech", "voice"})


class ConfigError(Exception):
    """A settings file is malformed in a way that would otherwise pass silently."""


def _check_sections(data: dict, path: Path) -> None:
    stray = sorted(k for k in data if k not in _SECTIONS)
    if stray:
        raise ConfigError(
            f"{path.name} has {', '.join(repr(k) for k in stray)} outside any section, "
            f"where nothing reads them.\nDid you leave a [section] header commented out? "
            f"Known sections: {', '.join(sorted(_SECTIONS))}.")


def local_config_path(path: str | Path = "talkbox.toml") -> Path:
    """talkbox.toml -> talkbox.local.toml, beside it."""
    return Path(path).with_suffix(".local.toml")


def load_settings(path: str | Path = "talkbox.toml") -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    local = local_config_path(path)
    local_used = None
    if local.exists():
        overrides = tomllib.loads(local.read_text(encoding="utf-8"))
        _check_sections(overrides, local)
        data = _merge(data, overrides)
        local_used = local
    base = path.parent
    provider = data["provider"]
    name = provider["name"]
    g = data["guardrails"]
    return Settings(
        policy_path=base / data["paths"]["policy"],
        database_path=base / data["paths"]["database"],
        provider_name=name,
        provider_settings=dict(provider.get(name, {})),
        logging_enabled=bool(data.get("logging", {}).get("enabled", False)),
        max_history_exchanges=int(data.get("chat", {}).get("max_history_exchanges", 20)),
        guardrails=GuardrailSettings(
            history_exchanges=int(g.get("history_exchanges", 6)),
            length_tolerance=float(g.get("length_tolerance", 0.25)),
            classifier=_check(g["classifier"]),
            output_check=_check(g["output_check"]),
        ),
        speech=_speech(data),
        web=WebSettings(
            host=str(data.get("web", {}).get("host", "auto")),
            port=int(data.get("web", {}).get("port", 8321)),
        ),
        local_config=local_used,
    )
