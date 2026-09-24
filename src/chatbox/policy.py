"""The parent policy: a structured, validated document that a UI can edit.

Everything a parent controls lives here as data (lists, numbers, short strings),
never as free-form prompt prose. `prompt.compile_system_prompt` turns it into the
system prompt; `limits` enforces the schedule and daily cap.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_Strict):
    name: str = Field(min_length=1, max_length=40)
    tone: list[str] = Field(min_length=1, description="Short adjectives, e.g. 'warm', 'playful'.")


class AnswerStyle(_Strict):
    max_sentences: int = Field(ge=1, le=10)
    max_words: int = Field(ge=10, le=200)
    say_when_unsure: bool = True
    ask_follow_up_question: bool = False


class AIDisclosure(_Strict):
    # Required by Anthropic's conditions for products minors use; not switchable off.
    always_honest_about_being_a_computer: Literal[True] = True
    disclosure_reply: str = Field(min_length=1)


class Topic(_Strict):
    id: str
    label: str = Field(min_length=1)
    description: str = ""

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("use only lowercase letters, numbers and underscores (like outer_space)")
        return v


class RedirectTopic(Topic):
    reply: str = Field(min_length=1, description="Parent-approved canned reply.")


class Topics(_Strict):
    allowed: list[Topic]
    blocked: list[Topic]
    redirect_to_parent: list[RedirectTopic]

    @model_validator(mode="after")
    def _unique_ids(self) -> Topics:
        ids = [t.id for t in (*self.allowed, *self.blocked, *self.redirect_to_parent)]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"each topic needs its own id; used more than once: {', '.join(dupes)}")
        return self


class TimeWindow(_Strict):
    days: list[Weekday] = Field(min_length=1)
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError("use 24-hour time, like 07:30 or 19:00")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.start_time >= self.end_time:
            raise ValueError("the end time must be after the start time (a window can't go past midnight)")
        return self

    @property
    def start_time(self) -> time:
        return time.fromisoformat(self.start)

    @property
    def end_time(self) -> time:
        return time.fromisoformat(self.end)


class Schedule(_Strict):
    timezone: str
    windows: list[TimeWindow] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}; use a name like America/New_York") from e
        return v


class Limits(_Strict):
    daily_questions: int = Field(ge=0, le=1000)


class CannedReplies(_Strict):
    outside_schedule: str = Field(min_length=1)
    daily_limit_reached: str = Field(min_length=1)
    blocked_topic: str = Field(min_length=1)
    something_went_wrong: str = Field(min_length=1)
    # Voice only: spoken when the recording was silent or no words were recognized.
    didnt_catch_that: str = Field(min_length=1)


class Policy(_Strict):
    schema_version: Literal[2] = 2
    policy_version: int = Field(ge=1, description="Bump on every edit; logged with each exchange.")
    household_age: int = Field(ge=2, le=17)
    persona: Persona
    answers: AnswerStyle
    ai_disclosure: AIDisclosure
    topics: Topics
    schedule: Schedule
    limits: Limits
    canned_replies: CannedReplies

    def fingerprint(self) -> str:
        """Short content hash, so a log row identifies the exact policy even if
        someone forgot to bump policy_version."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:8]

    def version_label(self) -> str:
        return f"v{self.policy_version}-{self.fingerprint()}"


def load_policy(path: str | Path) -> Policy:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Policy.model_validate(data)


def policy_json_schema() -> dict:
    """JSON Schema for the policy (for tools and future UIs)."""
    return Policy.model_json_schema()
