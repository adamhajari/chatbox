import json

import pytest

from talkbox.pipeline import Classification, OutputVerdict, Pipeline
from talkbox.policy import Policy
from tests.conftest import WED_NOON, FakeProvider, at


@pytest.fixture
def make(counter, log):
    def _make(policy, provider=None, when=WED_NOON, logging=True, **kw):
        provider = provider or FakeProvider()
        p = Pipeline(policy, provider, counter, log if logging else None, clock=lambda: when, **kw)
        return p, provider
    return _make


def rows(log):
    return list(reversed(log.recent(100)))


def test_answer_is_logged(policy, log, make):
    p, provider = make(policy)
    a = p.ask("Why is the sky blue?")
    assert a.answered_by == "model" and "blue" in a.text
    system, messages = provider.calls[0]
    assert system == p.system_prompt and messages[0].text == "Why is the sky blue?"

    [r] = rows(log)
    assert r["question"] == "Why is the sky blue?"
    assert r["answer"] == a.text
    assert r["policy_version"] == policy.version_label()
    assert r["model"] == "fake-model" and r["provider"] == "fake"
    assert r["latency_ms"] >= 0
    assert r["ts_utc"].endswith("+00:00")
    assert r["local_date"] == "2026-09-16"
    steps = [(s["step"], s["decision"]) for s in json.loads(r["steps_json"])]
    assert steps == [("limits", "allow"), ("classify", "allow"), ("canned", "skip"),
                     ("generate", "ok"), ("output_check", "pass")]


def test_outside_schedule_gets_canned_reply_and_no_model_call(policy, log, make):
    p, provider = make(policy, when=at(2026, 9, 16, 21))
    a = p.ask("Are you awake?")
    assert a.text == policy.canned_replies.outside_schedule and a.answered_by == "canned"
    assert provider.calls == []
    [r] = rows(log)
    assert r["model"] is None
    assert json.loads(r["steps_json"]) == [
        {"step": "limits", "decision": "deny", "detail": "outside_schedule"}]


def test_daily_cap_enforced_and_denials_not_counted(policy_data, counter, make):
    policy_data["limits"]["daily_questions"] = 2
    policy = Policy.model_validate(policy_data)
    p, provider = make(policy)
    assert p.ask("one").answered_by == "model"
    assert p.ask("two").answered_by == "model"
    for _ in range(3):
        assert p.ask("more").text == policy.canned_replies.daily_limit_reached
    assert len(provider.calls) == 2
    assert counter.get("2026-09-16") == 2
    # Next day, the count resets.
    p2, _ = make(policy, when=at(2026, 9, 17, 9))
    assert p2.ask("new day").answered_by == "model"


def test_refusal_gets_canned_reply(policy, log, make):
    p, _ = make(policy, provider=FakeProvider(refused=True, text=""))
    a = p.ask("something")
    assert a.text == policy.canned_replies.blocked_topic
    assert ("generate", "refused") in [(s["step"], s["decision"]) for s in a.steps]


def test_provider_error_gets_canned_reply(policy, log, make):
    p, _ = make(policy, provider=FakeProvider(error="network down"))
    a = p.ask("something")
    assert a.text == policy.canned_replies.something_went_wrong
    assert rows(log)[0]["answer"] == a.text


class RedirectDeath:
    def classify(self, question, policy):
        return Classification("redirect", "death_and_loss", "test")


class FailAll:
    def check(self, question, answer, policy):
        return OutputVerdict(False, "test")


def test_classifier_extension_point_uses_parent_reply(policy, log, make):
    p, provider = make(policy, classifier=RedirectDeath())
    a = p.ask("Where did grandpa go?")
    expected = next(t.reply for t in policy.topics.redirect_to_parent if t.id == "death_and_loss")
    assert a.text == expected and provider.calls == []


def test_output_check_extension_point(policy, log, make):
    p, _ = make(policy, output_checker=FailAll())
    a = p.ask("hi")
    assert a.text == policy.canned_replies.blocked_topic
    assert rows(log)[0]["model"] == "fake-model"


def test_log_and_counter_persist_to_file(policy, tmp_path):
    from talkbox.log import DailyCounter, ExchangeLog
    path = tmp_path / "sub" / "t.db"
    counter, log = DailyCounter(path), ExchangeLog(path)
    Pipeline(policy, FakeProvider(), counter, log, clock=lambda: WED_NOON).ask("hi")
    counter.close(), log.close()
    again, counter = ExchangeLog(path), DailyCounter(path)
    assert len(again.recent()) == 1 and counter.get("2026-09-16") == 1
    again.close(), counter.close()


def test_logging_off_stores_no_text_but_still_counts(policy, log, counter, make):
    p, _ = make(policy, logging=False)
    a = p.ask("secret question")
    assert a.answered_by == "model" and a.exchange_id is None
    assert log.recent() == []
    assert counter.get("2026-09-16") == 1


def test_follow_ups_see_session_history(policy, make):
    p, provider = make(policy)
    p.ask("Why is the sky blue?")
    p.ask("Why?")
    _, messages = provider.calls[1]
    assert [(m.role, m.text) for m in messages] == [
        ("user", "Why is the sky blue?"),
        ("assistant", FakeProvider().text),
        ("user", "Why?"),
    ]


def test_history_capped_and_denials_excluded(policy_data, make):
    policy_data["limits"]["daily_questions"] = 4
    policy = Policy.model_validate(policy_data)
    p, provider = make(policy, max_history_exchanges=2)
    for q in ["a", "b", "c", "d", "e"]:
        p.ask(q)  # "e" is over the cap
    assert [m.text for m in p.history if m.role == "user"] == ["c", "d"]
    answer = FakeProvider().text
    assert [m.text for m in provider.calls[-1][1]] == ["b", answer, "c", answer, "d"]


def test_canned_replies_join_history(policy, make):
    p, _ = make(policy, classifier=RedirectDeath())
    a = p.ask("Where did grandpa go?")
    assert p.history[-1].text == a.text
