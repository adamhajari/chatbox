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

MAX_REMEMBER_EXCHANGES = 50

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CATEGORY_RE = re.compile(r"^[a-z][a-z ]{0,29}$")

# What a policy without a `pictures` section gets. Each is a word Wikipedia's search
# can use to pick the right article: "python animal", not the programming language.
DEFAULT_PICTURE_CATEGORIES = ["animal", "plant", "food", "place", "space", "vehicle",
                              "landmark", "instrument", "science", "geography", "travel"]


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
    # Earlier question/answer pairs from this session sent along with a new question,
    # so a follow-up like "why?" makes sense. 0 = each question stands alone.
    remember_exchanges: int = Field(default=20, ge=0, le=MAX_REMEMBER_EXCHANGES)


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


class Pictures(_Strict):
    # The only kinds of thing the screen shows a picture of. The classifier picks one
    # for each question (or none), and it is added to the search, so a subject is
    # looked up as "python animal". Empty = no pictures at all.
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_PICTURE_CATEGORIES))

    @field_validator("categories", mode="before")
    @classmethod
    def _tidy(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        tidied = [" ".join(str(c).lower().split()) for c in v]
        return list(dict.fromkeys(c for c in tidied if c))

    @field_validator("categories")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c == "none" or not _CATEGORY_RE.match(c)]
        if bad:
            raise ValueError(f"use short plain words (letters and spaces, up to 30 characters, "
                             f"not \"none\"); can't use: {', '.join(bad)}")
        return v


class AnimalGuessing(_Strict):
    enabled: bool = True
    # Finished games per day: the animal was guessed, or the kid gave up and heard it. A
    # game stopped partway through doesn't count, and no game question counts toward
    # limits.daily_questions.
    daily_games: int = Field(default=5, ge=0, le=100)
    # Said when a kid asks to play while the game is off or today's games are used up.
    unavailable_reply: str = Field(
        default="We can't play the animal guessing game right now. "
                "You can still ask me questions!", min_length=1)


Grade = Literal["K", "1", "2", "3", "4", "5"]


class Reading(_Strict):
    enabled: bool = True
    # Finished games per day. A game is five words; it's finished once the last one has
    # been read or told. As with every game, none of it counts toward daily_questions.
    daily_games: int = Field(default=5, ge=0, le=100)
    # Reading level of the words shown: kindergarten ("K") to 5th grade ("5").
    grade: Grade = "K"
    unavailable_reply: str = Field(
        default="We can't play the reading game right now. You can still ask me questions!",
        min_length=1)

    @field_validator("grade", mode="before")
    @classmethod
    def _grade(cls, v: object) -> object:
        # YAML reads `grade: 2` as a number and `grade: k` in lower case.
        return str(v).upper() if isinstance(v, (int, str)) else v


class Games(_Strict):
    animal_guessing: AnimalGuessing = Field(default_factory=AnimalGuessing)
    reading: Reading = Field(default_factory=Reading)


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
    pictures: Pictures = Field(default_factory=Pictures)
    games: Games = Field(default_factory=Games)

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
