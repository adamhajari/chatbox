"""Runtime settings from talkbox.toml (provider, model names, file paths)."""

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


def load_settings(path: str | Path = "talkbox.toml") -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
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
    )
