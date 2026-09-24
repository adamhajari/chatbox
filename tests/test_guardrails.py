"""Guardrail layers 3-5 with the model mocked: classification outcomes, concurrency,
output-check failures, and every fail-closed path."""

import threading
import time

import pytest

from chatbox.guardrails import (
    Classification,
    GuardrailError,
    ModelClassifier,
    ModelOutputChecker,
    OutputVerdict,
    classifier_schema,
    classifier_system_prompt,
    length_problem,
    output_check_system_prompt,
)
from chatbox.pipeline import Pipeline
from chatbox.policy import Policy
from chatbox.providers.base import ChatMessage, ProviderError
from tests.conftest import WED_NOON, AllowAll, FakeProvider, PassAll

SECRET = "UNCHECKED MODEL ANSWER"


def redirect_reply(policy, topic_id):
    return next(t.reply for t in policy.topics.redirect_to_parent if t.id == topic_id)


def cls(decision, topic_id="none", reason="because"):
    return {"decision": decision, "topic_id": topic_id, "reason": reason}


def ok_verdict():
    return {"passed": True, "problems": [], "reason": "fine"}


@pytest.fixture
def make(counter):
    def _make(policy, *, answer=FakeProvider(SECRET), classifier=None, checker=None, **kw):
        p = Pipeline(policy, answer, counter, clock=lambda: WED_NOON,
                     classifier=classifier or AllowAll(), output_checker=checker or PassAll(), **kw)
        return p
    return _make


def decisions(a):
    return [(s["step"], s["decision"]) for s in a.steps]


# ---- Layer 3 + 5: each classification outcome, end to end ------------------------

def test_allow_returns_model_answer(policy, make):
    c = ModelClassifier(FakeProvider(structured=cls("allow", "animals")))
    a = make(policy, classifier=c).ask("Why do cats purr?")
    assert a.text == SECRET and a.answered_by == "model"
    assert decisions(a)[1:] == [("classify", "allow"), ("canned", "skip"),
                                ("generate", "ok"), ("output_check", "pass")]


def test_redirect_gets_that_topics_parent_reply(policy, make):
    c = ModelClassifier(FakeProvider(structured=cls("redirect", "death_and_loss", "about dying")))
    a = make(policy, classifier=c).ask("Where did grandpa go when he died?")
    assert a.text == redirect_reply(policy, "death_and_loss") and SECRET not in a.text
    step = a.steps[1]
    assert step["decision"] == "redirect" and "death_and_loss" in step["detail"]
    assert "about dying" in step["detail"]
    assert ("generate", "discarded") in decisions(a)


def test_refuse_gets_blocked_reply(policy, make):
    c = ModelClassifier(FakeProvider(structured=cls("refuse", "violence_and_weapons")))
    a = make(policy, classifier=c).ask("How do guns work?")
    assert a.text == policy.canned_replies.blocked_topic and a.answered_by == "canned"
    assert ("generate", "discarded") in decisions(a)


def test_classifier_reads_topics_from_policy(policy_data):
    policy_data["topics"]["blocked"].append(
        {"id": "video_games", "label": "Video games", "description": "Minecraft, Roblox"})
    policy = Policy.model_validate(policy_data)
    assert "video_games" in classifier_system_prompt(policy)
    assert "video_games" in classifier_schema(policy)["properties"]["topic_id"]["enum"]
    assert "video_games" in output_check_system_prompt(policy)


def test_classifier_sees_recent_history(policy):
    fp = FakeProvider(structured=cls("allow"))
    history = [ChatMessage("user", f"q{i}") if i % 2 == 0 else ChatMessage("assistant", f"a{i}")
               for i in range(20)]
    ModelClassifier(fp, history_exchanges=2).classify("what about with a knife?", history, policy)
    _, [msg], _ = fp.structured_calls[0]
    assert "q16" in msg.text and "a19" in msg.text and "q14" not in msg.text
    assert "what about with a knife?" in msg.text


def test_pipeline_passes_session_history_to_classifier(policy, make):
    seen = []

    class Spy:
        def classify(self, question, history, policy):
            seen.append([m.text for m in history])
            return Classification("allow")

    p = make(policy, classifier=Spy())
    p.ask("How do you cut an apple?")
    p.ask("What about with a knife?")
    assert seen[1] == ["How do you cut an apple?", SECRET]


# ---- Concurrency ----------------------------------------------------------------

def test_classifier_and_answer_run_concurrently(policy, make):
    both_running = threading.Barrier(2, timeout=2)

    class SlowClassifier:
        def classify(self, question, history, policy):
            both_running.wait()  # only returns if generate is running at the same time
            return Classification("allow")

    class SlowAnswer(FakeProvider):
        def generate(self, system, messages):
            both_running.wait()
            return super().generate(system, messages)

    a = make(policy, answer=SlowAnswer(SECRET), classifier=SlowClassifier()).ask("hi")
    assert a.text == SECRET


def test_redirect_never_returns_answer_even_if_answer_is_ready_first(policy, make):
    class SlowRedirect:
        def classify(self, question, history, policy):
            time.sleep(0.2)
            return Classification("redirect", "death_and_loss", "slow")

    fast = FakeProvider(SECRET)
    a = make(policy, answer=fast, classifier=SlowRedirect()).ask("Why do pets die?")
    assert fast.calls  # the answer was generated...
    assert SECRET not in a.text  # ...and discarded
    assert a.text == redirect_reply(policy, "death_and_loss")
    assert a.answered_by == "canned"


def test_redirect_answers_fast_even_if_model_is_slow(policy, make):
    c = ModelClassifier(FakeProvider(structured=cls("refuse", "adult_content")))
    t = time.monotonic()
    a = make(policy, answer=FakeProvider(SECRET, delay=1.0), classifier=c).ask("?")
    assert time.monotonic() - t < 0.5
    assert a.text == policy.canned_replies.blocked_topic


# ---- Layer 4: output check ------------------------------------------------------

def test_output_check_failure_returns_blocked_reply(policy, make):
    checker = ModelOutputChecker(FakeProvider(structured={
        "passed": False, "problems": ["blocked_content"], "reason": "describes weapons"}))
    a = make(policy, checker=checker).ask("Tell me about knights")
    assert a.text == policy.canned_replies.blocked_topic
    step = a.steps[-1]
    assert step["step"] == "output_check" and step["decision"] == "fail"
    assert "blocked_content" in step["detail"] and "describes weapons" in step["detail"]


def test_output_check_passed_true_with_problems_still_fails(policy, make):
    checker = ModelOutputChecker(FakeProvider(structured={
        "passed": True, "problems": ["claims_to_be_human"], "reason": "said it has a body"}))
    a = make(policy, checker=checker).ask("Are you real?")
    assert a.text == policy.canned_replies.blocked_topic


def test_output_check_sees_whole_answer_and_question(policy, make):
    fp = FakeProvider(structured=ok_verdict())
    a = make(policy, checker=ModelOutputChecker(fp)).ask("Why is the sky blue?")
    assert a.text == SECRET
    _, [msg], _ = fp.structured_calls[0]
    assert SECRET in msg.text and "Why is the sky blue?" in msg.text


def test_too_long_answer_fails_without_model_call(policy, make):
    fp = FakeProvider(structured=ok_verdict())
    long = " ".join(["word"] * (policy.answers.max_words * 2)) + "."
    a = make(policy, answer=FakeProvider(long), checker=ModelOutputChecker(fp)).ask("?")
    assert a.text == policy.canned_replies.blocked_topic
    assert "too long" in a.steps[-1]["detail"] and fp.structured_calls == []


def test_output_check_can_be_turned_off(policy, counter):
    p = Pipeline(policy, FakeProvider(SECRET), counter, clock=lambda: WED_NOON,
                 classifier=AllowAll(), output_checker=None)
    a = p.ask("hi")
    assert a.text == SECRET and a.steps[-1]["decision"] == "skipped"


def test_output_check_enabled_setting(tmp_path):
    from chatbox.config import load_settings
    from tests.conftest import ROOT
    text = (ROOT / "chatbox.toml").read_text()
    assert not load_settings(ROOT / "chatbox.toml").guardrails.output_check.enabled
    cfg = tmp_path / "chatbox.toml"
    head, section = text.split("[guardrails.output_check]", 1)
    cfg.write_text(head + "[guardrails.output_check]"
                   + section.replace("enabled = false", "enabled = true", 1))
    s = load_settings(cfg)
    assert s.guardrails.output_check.enabled
    assert "enabled" not in s.guardrails.output_check.provider_settings


def test_length_tolerance(policy):
    limit = policy.answers.max_words
    just_over = " ".join(["w"] * (limit + 1)) + "."
    assert length_problem(just_over, policy, 0.25) is None
    assert length_problem(just_over, policy, 0.0) is not None
    many = "Hi. " * (policy.answers.max_sentences * 2)
    assert "sentences" in length_problem(many, policy, 0.25)


# ---- Fail closed ----------------------------------------------------------------

class Raises:
    def __init__(self, exc):
        self.exc = exc

    def classify(self, *a):
        raise self.exc

    def check(self, *a):
        raise self.exc


class Hangs:
    def classify(self, *a):
        time.sleep(1.0)
        return Classification("allow")

    def check(self, *a):
        time.sleep(1.0)
        return OutputVerdict(True)


BAD_CLASSIFICATIONS = [
    ProviderError("network down"),
    GuardrailError("bad"),
    {"decision": "maybe", "topic_id": "none", "reason": "x"},          # unknown decision
    cls("redirect", "violence_and_weapons"),                            # redirect w/ blocked id
    cls("refuse", "death_and_loss"),                                    # refuse w/ redirect id
    cls("redirect", "none"),                                            # redirect w/o topic
    {"decision": "allow", "topic_id": "none"},                          # missing reason
]


@pytest.mark.parametrize("bad", BAD_CLASSIFICATIONS)
def test_classifier_bad_output_fails_closed(policy, make, bad):
    c = ModelClassifier(FakeProvider(structured=bad))
    a = make(policy, classifier=c).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong and SECRET not in a.text
    assert ("classify", "error") in decisions(a)


def test_classifier_exception_fails_closed(policy, make):
    a = make(policy, classifier=Raises(RuntimeError("boom"))).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong
    assert "boom" in a.steps[1]["detail"]


def test_classifier_timeout_fails_closed(policy, make):
    t = time.monotonic()
    a = make(policy, classifier=Hangs(), classify_timeout_s=0.1).ask("hi")
    assert time.monotonic() - t < 0.6
    assert a.text == policy.canned_replies.something_went_wrong
    assert "timed out" in a.steps[1]["detail"]


BAD_VERDICTS = [
    ProviderError("unparseable structured output"),
    {"passed": "yes", "problems": [], "reason": "x"},
    {"passed": True, "reason": "x"},
    {},
]


@pytest.mark.parametrize("bad", BAD_VERDICTS)
def test_output_check_bad_output_fails_closed(policy, make, bad):
    checker = ModelOutputChecker(FakeProvider(structured=bad))
    a = make(policy, checker=checker).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong and SECRET not in a.text
    assert ("output_check", "error") in decisions(a)


def test_output_check_exception_fails_closed(policy, make):
    a = make(policy, checker=Raises(ValueError("nope"))).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong


def test_output_check_timeout_fails_closed(policy, make):
    a = make(policy, checker=Hangs(), check_timeout_s=0.1).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong
    assert "timed out" in a.steps[-1]["detail"]


def test_answer_timeout_gets_error_reply(policy, make):
    a = make(policy, answer=FakeProvider(SECRET, delay=1.0), generate_timeout_s=0.1).ask("hi")
    assert a.text == policy.canned_replies.something_went_wrong


# ---- Explainability -------------------------------------------------------------

def test_every_step_has_decision_detail_and_time(policy, make):
    a = make(policy).ask("hi")
    for s in a.steps:
        assert set(s) == {"step", "decision", "detail", "ms"}
        assert isinstance(s["ms"], int) and s["ms"] >= 0
    assert a.latency_ms >= 0


# ---- Structured provider (Anthropic adapter) ------------------------------------

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text, stop="end_turn"):
        self.content, self.stop_reason, self.model = [_Block(text)], stop, "m"


class _Client:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def test_anthropic_structured_output_request_and_parse():
    from chatbox.providers.anthropic_provider import AnthropicProvider
    client = _Client(_Resp('{"a": 1}'))
    p = AnthropicProvider("claude-haiku-4-5", client=client)
    schema = {"type": "object"}
    assert p.generate_structured("sys", [ChatMessage("user", "q")], schema) == {"a": 1}
    assert client.kwargs["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "effort" not in client.kwargs["output_config"]


@pytest.mark.parametrize("resp", [_Resp("not json"), _Resp('{"a": 1}', "refusal"),
                                  _Resp('{"a"', "max_tokens"), _Resp("[1]")])
def test_anthropic_structured_output_problems_raise(resp):
    from chatbox.providers.anthropic_provider import AnthropicProvider
    p = AnthropicProvider("claude-haiku-4-5", client=_Client(resp))
    with pytest.raises(ProviderError):
        p.generate_structured("sys", [ChatMessage("user", "q")], {"type": "object"})
