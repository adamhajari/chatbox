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
    screen: ScreenSettings | None = None  # None = no screen; nothing else to check
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
class ScreenSettings:
    """The picture screen (PLAN.md D28). Absent or `enabled = false` means no screen."""

    dc_gpio: int = 25            # data/command pin
    reset_gpio: int = 27
    cs: int = 0                  # SPI0 chip select: 0 = CE0 (GPIO8), 1 = CE1 (GPIO7)
    baudrate: int = 24_000_000
    rotation: int = 0
    # The backlight on a GPIO, so a blank screen is genuinely dark. None = wired to
    # 3V3 and always on. `active_high` is False for a P-MOSFET high-side switch.
    backlight_gpio: int | None = None
    backlight_active_high: bool = True
    timeout_seconds: float = 3.0  # hard ceiling on a lookup; speech never waits for it
    cache_dir: Path | None = None  # None = "pictures" beside the database


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


def _screen(data: dict, base: Path) -> ScreenSettings | None:
    """[screen] in talkbox.toml (the Pi's values belong in talkbox.local.toml).

    No section, or `enabled = false`, means no screen at all: nothing is imported,
    nothing is fetched and no pin is opened.
    """
    if "screen" not in data:
        return None
    sc = dict(data["screen"])
    if not bool(sc.pop("enabled", False)):
        return None
    cache = sc.get("cache_dir")
    return ScreenSettings(
        dc_gpio=int(sc.get("dc_gpio", 25)),
        reset_gpio=int(sc.get("reset_gpio", 27)),
        cs=int(sc.get("cs", 0)),
        baudrate=int(sc.get("baudrate", 24_000_000)),
        rotation=int(sc.get("rotation", 0)),
        backlight_gpio=None if sc.get("backlight_gpio") is None else int(sc["backlight_gpio"]),
        backlight_active_high=bool(sc.get("backlight_active_high", True)),
        timeout_seconds=float(sc.get("timeout_seconds", 3.0)),
        cache_dir=base / cache if cache else None,
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
                       "speech", "voice", "screen"})


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
        screen=_screen(data, base),
        web=WebSettings(
            host=str(data.get("web", {}).get("host", "auto")),
            port=int(data.get("web", {}).get("port", 8321)),
        ),
        local_config=local_used,
    )
