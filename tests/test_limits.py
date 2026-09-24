from datetime import timezone

import pytest

from chatbox.limits import check_limits, within_schedule
from tests.conftest import WED_NOON, at


@pytest.mark.parametrize("when,expected", [
    (at(2026, 9, 16, 12), True),       # Wed noon
    (at(2026, 9, 16, 7, 0), True),     # weekday start is inclusive
    (at(2026, 9, 16, 6, 59), False),
    (at(2026, 9, 16, 19, 30), False),  # end is exclusive
    (at(2026, 9, 19, 7, 15), False),   # Saturday starts later
    (at(2026, 9, 19, 7, 30), True),
    (at(2026, 9, 16, 23, 0), False),
])
def test_schedule(policy, when, expected):
    assert within_schedule(policy, when) is expected


def test_schedule_uses_policy_timezone(policy):
    # 19:00 UTC on Wed = noon in Los Angeles.
    assert within_schedule(policy, WED_NOON.astimezone(timezone.utc))


def test_naive_datetime_rejected(policy):
    with pytest.raises(ValueError):
        within_schedule(policy, WED_NOON.replace(tzinfo=None))


def test_daily_cap(policy):
    cap = policy.limits.daily_questions
    assert check_limits(policy, WED_NOON, cap - 1).allowed
    d = check_limits(policy, WED_NOON, cap)
    assert not d.allowed and d.reason == "daily_limit_reached"
    assert d.canned_reply == policy.canned_replies.daily_limit_reached


def test_outside_schedule_reply(policy):
    d = check_limits(policy, at(2026, 9, 16, 22), 0)
    assert d.reason == "outside_schedule"
    assert d.canned_reply == policy.canned_replies.outside_schedule
