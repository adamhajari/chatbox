import copy
import os
import threading

import pytest
import yaml
from pydantic import ValidationError

from chatbox.guardrails import Classification
from chatbox.pipeline import Pipeline
from chatbox.policy import load_policy
from chatbox.policy_store import PolicyStore, StaleEditError
from tests.conftest import TEST_POLICY, WED_NOON, AllowAll, FakeProvider, PassAll


@pytest.fixture
def policy_file(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("# Parents: read me first.\n# Second line.\n" + TEST_POLICY.read_text())
    return path


@pytest.fixture
def store(policy_file):
    return PolicyStore.load(policy_file)


def edited(store, change) -> dict:
    data = copy.deepcopy(store.policy.model_dump(mode="json"))
    change(data)
    return data


def test_save_validates_writes_then_applies(store, policy_file):
    old_version = store.policy.policy_version
    saved = store.save(edited(store, lambda d: d["persona"].update(name="Zed")))

    assert saved.persona.name == "Zed" and store.policy is saved
    assert saved.policy_version == old_version + 1
    assert load_policy(policy_file) == store.policy  # file and memory match
    assert "Zed" in store.snapshot().system_prompt
    assert policy_file.read_text().startswith("# Parents: read me first.\n# Second line.\n")


def test_version_bumps_on_every_save_whatever_the_edit_says(store):
    start = store.policy.policy_version
    store.save(edited(store, lambda d: d.update(policy_version=999)))
    store.save(edited(store, lambda d: None))
    assert store.policy.policy_version == start + 2


@pytest.mark.parametrize("change", [
    lambda d: d["topics"]["redirect_to_parent"][0].update(reply=""),        # empty reply
    lambda d: d["schedule"]["windows"][0].update(start="18:00", end="09:00"),  # ends before start
    lambda d: d["topics"]["blocked"].append(dict(d["topics"]["allowed"][0])),  # duplicate id
    lambda d: d["canned_replies"].update(didnt_catch_that=""),
    lambda d: d["ai_disclosure"].update(always_honest_about_being_a_computer=False),
])
def test_invalid_edit_changes_nothing(store, policy_file, change):
    before_file, before = policy_file.read_bytes(), store.snapshot()
    with pytest.raises(ValidationError):
        store.save(edited(store, change))
    assert policy_file.read_bytes() == before_file
    assert store.snapshot() is before


def test_failed_write_leaves_old_file_and_policy(store, policy_file, monkeypatch):
    before_file, before = policy_file.read_bytes(), store.snapshot()

    def crash(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.save(edited(store, lambda d: d["persona"].update(name="Zed")))
    assert policy_file.read_bytes() == before_file
    assert store.snapshot() is before
    assert os.listdir(policy_file.parent) == [policy_file.name]  # temp file cleaned up


def test_write_is_a_rename_of_a_complete_file(store, policy_file, monkeypatch):
    real_replace, seen = os.replace, []

    def spy(src, dst):
        seen.append(yaml.safe_load(open(src).read())["persona"]["name"])  # complete before rename
        assert policy_file.read_text().count("Zed") == 0  # target untouched until the rename
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.save(edited(store, lambda d: d["persona"].update(name="Zed")))
    assert seen == ["Zed"]


def test_stale_edit_is_refused(store, policy_file):
    base = store.policy.policy_version
    store.save(edited(store, lambda d: None), base)
    before_file = policy_file.read_bytes()
    with pytest.raises(StaleEditError):
        store.save(edited(store, lambda d: d["persona"].update(name="Zed")), base)
    assert policy_file.read_bytes() == before_file


# ---- live updates reaching the pipeline ---------------------------------------

class RecordingClassifier:
    def __init__(self, result=None, during=None):
        self.result = result or Classification("allow", detail="test")
        self.during, self.policies = during, []

    def classify(self, question, history, policy):
        self.policies.append(policy)
        if self.during:
            self.during()
        return self.result


def make(store, classifier, provider=None, counter=None):
    return Pipeline(store, provider or FakeProvider(), counter, classifier=classifier,
                    output_checker=PassAll(), clock=lambda: WED_NOON)


def test_next_question_uses_saved_policy(store, counter):
    provider, classifier = FakeProvider(), RecordingClassifier()
    p = make(store, classifier, provider, counter)
    p.ask("hi")
    assert "video_games" not in provider.calls[-1][0]

    store.save(edited(store, lambda d: d["topics"]["blocked"].append(
        {"id": "video_games", "label": "Video games", "description": "Games on screens"})))
    p.ask("hi again")

    assert "Video games: Games on screens" in provider.calls[-1][0]  # new system prompt
    assert provider.calls[-1][0] == store.snapshot().system_prompt
    assert "video_games" in [t.id for t in classifier.policies[-1].topics.blocked]  # new topics


def test_next_question_uses_new_redirect_reply_and_limits(store, counter):
    redirect = Classification("redirect", "death_and_loss", "test")
    p = make(store, RecordingClassifier(redirect), counter=counter)
    store.save(edited(store, lambda d: d["topics"]["redirect_to_parent"][0].update(
        id="death_and_loss", reply="Go find Grandma.")))
    assert p.ask("what happens when you die?").text == "Go find Grandma."

    store.save(edited(store, lambda d: d["limits"].update(daily_questions=0)))
    answer = p.ask("one more?")
    assert answer.text == store.policy.canned_replies.daily_limit_reached


def test_question_in_progress_keeps_its_policy(store, counter):
    reply_before = store.policy.topics.redirect_to_parent[0].reply
    topic = store.policy.topics.redirect_to_parent[0].id
    prompt_before = store.snapshot().system_prompt
    saved = threading.Event()

    def save_mid_question():  # a parent saves while the classifier is running
        store.save(edited(store, lambda d: d["topics"]["redirect_to_parent"][0].update(
            reply="New reply.")))
        saved.set()

    provider = FakeProvider()
    p = make(store, RecordingClassifier(Classification("redirect", topic, "t"), save_mid_question),
             provider, counter)
    answer = p.ask("a parent topic")

    assert saved.is_set()
    assert answer.text == reply_before           # finished under the policy it started with
    assert provider.calls[0][0] == prompt_before
    assert p.ask("again").text == "New reply."   # the next one sees the save


def test_pipeline_still_accepts_a_plain_policy(policy, counter):
    p = Pipeline(policy, FakeProvider(), counter, classifier=AllowAll(), output_checker=PassAll(),
                 clock=lambda: WED_NOON)
    assert p.policy is policy and p.ask("hi").answered_by == "model"
