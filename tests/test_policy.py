import pytest
from pydantic import ValidationError

from talkbox.policy import Policy, load_policy, policy_json_schema
from tests.conftest import DEFAULT_POLICY


def test_default_policy_is_valid():
    p = load_policy(DEFAULT_POLICY)
    assert p.household_age == 6
    assert p.topics.redirect_to_parent and all(t.reply for t in p.topics.redirect_to_parent)


def _invalid(data):
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_rejects_unknown_keys(policy_data):
    policy_data["persona"]["nmae"] = "typo"
    _invalid(policy_data)


def test_rejects_bad_age(policy_data):
    policy_data["household_age"] = 40
    _invalid(policy_data)


def test_ai_disclosure_cannot_be_disabled(policy_data):
    policy_data["ai_disclosure"]["always_honest_about_being_a_computer"] = False
    _invalid(policy_data)


def test_redirect_requires_reply(policy_data):
    del policy_data["topics"]["redirect_to_parent"][0]["reply"]
    _invalid(policy_data)


def test_duplicate_topic_ids(policy_data):
    policy_data["topics"]["blocked"].append(dict(policy_data["topics"]["allowed"][0]))
    _invalid(policy_data)


def test_bad_topic_id(policy_data):
    policy_data["topics"]["allowed"][0]["id"] = "Has Spaces"
    _invalid(policy_data)


@pytest.mark.parametrize("start,end", [("7:00", "19:00"), ("19:00", "07:00"), ("25:00", "26:00")])
def test_bad_windows(policy_data, start, end):
    policy_data["schedule"]["windows"][0].update(start=start, end=end)
    _invalid(policy_data)


def test_bad_timezone(policy_data):
    policy_data["schedule"]["timezone"] = "Mars/Olympus"
    _invalid(policy_data)


def test_version_label_changes_with_content(policy_data):
    a = Policy.model_validate(policy_data)
    policy_data["limits"]["daily_questions"] = 5
    b = Policy.model_validate(policy_data)
    assert a.version_label() != b.version_label()
    assert a.version_label().startswith("v1-")


def test_json_schema_available():
    schema = policy_json_schema()
    assert "household_age" in schema["properties"]
