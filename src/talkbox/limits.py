"""Guardrail layer 6: availability schedule and daily question cap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from talkbox.policy import WEEKDAYS, Policy


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str  # "ok" | "outside_schedule" | "daily_limit_reached"
    canned_reply: str | None = None


def local_now(policy: Policy, now: datetime) -> datetime:
    """`now` must be timezone-aware; returns it in the policy's timezone."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ZoneInfo(policy.schedule.timezone))


def within_schedule(policy: Policy, now: datetime) -> bool:
    local = local_now(policy, now)
    day = WEEKDAYS[local.weekday()]
    t = local.time()
    return any(
        day in w.days and w.start_time <= t < w.end_time for w in policy.schedule.windows
    )


def check_limits(policy: Policy, now: datetime, questions_answered_today: int) -> LimitDecision:
    if not within_schedule(policy, now):
        return LimitDecision(False, "outside_schedule", policy.canned_replies.outside_schedule)
    if questions_answered_today >= policy.limits.daily_questions:
        return LimitDecision(False, "daily_limit_reached", policy.canned_replies.daily_limit_reached)
    return LimitDecision(True, "ok")
