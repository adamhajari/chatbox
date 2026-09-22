"""Turn the editor's HTML form into policy data, and validation errors into plain
language keyed by field.

Field names follow the policy's shape (`persona.name`, `canned_replies.blocked_topic`),
except topics and schedule windows, which are numbered rows: `topic.3.kind`,
`topic.3.label`, `window.0.days`. A topic's `kind` says which list it goes in, so
changing it moves the topic (for example from allowed to blocked).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from talkbox.policy import WEEKDAYS, CannedReplies, Policy

KINDS = ("allowed", "blocked", "redirect_to_parent")
Form = dict[str, list[str]]


def _rows(form: Form, prefix: str) -> list[int]:
    pattern = re.compile(rf"^{prefix}\.(\d+)\.")
    return sorted({int(m.group(1)) for k in form if (m := pattern.match(k))})


def policy_to_data(policy: Policy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


def blank_topic(kind: str) -> dict[str, str]:
    topic = {"id": "", "label": "", "description": ""}
    if kind == "redirect_to_parent":
        topic["reply"] = ""
    return topic


def blank_window() -> dict[str, Any]:
    return {"days": [], "start": "", "end": ""}


def parse_form(form: Form, drop_topic: int | None = None,
               drop_window: int | None = None) -> dict[str, Any]:
    """Policy-shaped data from the form. Values stay as typed (numbers as strings) so
    that validation, not parsing, reports what's wrong. `policy_version` is left out:
    the store sets it on save."""

    def one(name: str) -> str:
        return form.get(name, [""])[0].strip()

    def checked(name: str) -> bool:
        return name in form

    topics: dict[str, list[dict[str, str]]] = {k: [] for k in KINDS}
    for n in _rows(form, "topic"):
        if n == drop_topic:
            continue
        kind = one(f"topic.{n}.kind")
        kind = kind if kind in KINDS else "allowed"
        topic = {f: one(f"topic.{n}.{f}") for f in ("id", "label", "description")}
        if kind == "redirect_to_parent":
            topic["reply"] = one(f"topic.{n}.reply")
        topics[kind].append(topic)

    windows = [
        {"days": [d for d in WEEKDAYS if d in form.get(f"window.{n}.days", [])],
         "start": one(f"window.{n}.start"), "end": one(f"window.{n}.end")}
        for n in _rows(form, "window") if n != drop_window
    ]

    return {
        "schema_version": Policy.model_fields["schema_version"].default,
        "household_age": one("household_age"),
        "persona": {
            "name": one("persona.name"),
            "tone": [t.strip() for t in one("persona.tone").split(",") if t.strip()],
        },
        "answers": {
            "max_sentences": one("answers.max_sentences"),
            "max_words": one("answers.max_words"),
            "say_when_unsure": checked("answers.say_when_unsure"),
            "ask_follow_up_question": checked("answers.ask_follow_up_question"),
        },
        # Always on; the form shows it but can't change it.
        "ai_disclosure": {
            "always_honest_about_being_a_computer": True,
            "disclosure_reply": one("ai_disclosure.disclosure_reply"),
        },
        "topics": topics,
        "schedule": {"timezone": one("schedule.timezone"), "windows": windows},
        "limits": {"daily_questions": one("limits.daily_questions")},
        "canned_replies": {k: one(f"canned_replies.{k}") for k in CannedReplies.model_fields},
    }


def _plain(err: dict[str, Any]) -> str:
    kind, ctx = err["type"], err.get("ctx", {})
    loc = err["loc"]
    if kind == "missing" or kind == "string_too_short":
        return "This can't be empty."
    if kind == "string_too_long":
        return f"Keep this to {ctx['max_length']} characters or fewer."
    if kind in ("int_parsing", "int_type", "int_from_float"):
        return "Enter a whole number."
    if kind == "greater_than_equal":
        return f"Must be at least {ctx['ge']}."
    if kind == "less_than_equal":
        return f"Must be {ctx['le']} or less."
    if kind == "too_short":
        if loc and loc[-1] == "days":
            return "Pick at least one day."
        if loc and loc[-1] == "windows":
            return "Add at least one time window, or Talkbox will never answer."
        return "Add at least one."
    if kind == "value_error":
        msg = str(ctx.get("error", err["msg"]))
        return msg[:1].upper() + msg[1:] + ("" if msg.endswith(".") else ".")
    return err["msg"]


def errors_by_field(exc: ValidationError) -> dict[str, list[str]]:
    """{"topics.blocked.1.label": ["This can't be empty."], ...}. Errors about a whole
    section (duplicate topic ids, a window's times) are keyed by that section's path."""
    out: dict[str, list[str]] = {}
    for err in exc.errors():
        key = ".".join(str(p) for p in err["loc"])
        out.setdefault(key, []).append(_plain(err))
    return out
