import pytest
from pydantic import ValidationError

from chatbox.policy import Policy, load_policy, policy_json_schema
from tests.conftest import DEFAULT_POLICY


def test_default_policy_is_valid():
    p = load_policy(DEFAULT_POLICY)
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


# ---- this machine's policy: policies/local.yaml, made from the tracked default ----------

def _settings_in(tmp_path):
    import shutil

    from chatbox.config import load_settings
    from tests.conftest import ROOT

    shutil.copy(ROOT / "chatbox.toml", tmp_path / "chatbox.toml")
    (tmp_path / "policies").mkdir()
    shutil.copy(DEFAULT_POLICY, tmp_path / "policies" / "default.yaml")
    return load_settings(tmp_path / "chatbox.toml")


def test_settings_point_at_the_local_policy_and_its_default(tmp_path):
    s = _settings_in(tmp_path)
    assert s.policy_path == tmp_path / "policies" / "local.yaml"
    assert s.policy_default_path == tmp_path / "policies" / "default.yaml"


def test_local_policy_is_created_from_the_default_once(tmp_path):
    from chatbox.config import ensure_policy

    s = _settings_in(tmp_path)
    path, created = ensure_policy(s)
    assert created and path == s.policy_path
    assert path.read_text() == s.policy_default_path.read_text()
    assert load_policy(path) == load_policy(DEFAULT_POLICY)

    path.write_text(path.read_text().replace("household_age: 15", "household_age: 6"))
    again, created = ensure_policy(s)  # an existing copy is never overwritten
    assert not created and "household_age: 6" in again.read_text()


def test_settings_page_saves_go_to_the_local_copy_not_the_default(tmp_path):
    from chatbox.config import ensure_policy
    from chatbox.policy_store import PolicyStore

    s = _settings_in(tmp_path)
    before = s.policy_default_path.read_text()
    store = PolicyStore.load(ensure_policy(s)[0])
    data = store.policy.model_dump(mode="json")
    data["household_age"] = 7
    store.save(data)
    assert load_policy(s.policy_path).household_age == 7
    assert s.policy_default_path.read_text() == before


def test_no_default_and_no_local_policy_is_left_to_the_caller(tmp_path):
    from chatbox.config import ensure_policy

    s = _settings_in(tmp_path)
    s.policy_default_path.unlink()
    path, created = ensure_policy(s)
    assert not created and not path.exists()  # the CLI then says "Policy file not found"
