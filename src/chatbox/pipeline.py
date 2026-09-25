"""The answer pipeline:

    question -> pause -> limits (layer 6) -> classify input (layer 3) -> canned reply (layer 5)
             -> model answer (layers 1-2) -> check output (layer 4) -> log (layer 7, optional)

The classifier and the model answer run concurrently to save time; the answer is
discarded unless the classifier says "allow". The output check sees the whole answer
before anything is returned.

Fail closed: if the classifier or the output check errors, times out, or returns
unusable data, the child gets a canned reply, never an unchecked answer.

A Pipeline is one chat session: follow-up questions see the earlier exchanges.
Nothing carries over between sessions (yet; see PLAN.md D9).

The policy comes from a PolicyStore, so a save from the parent web UI applies from the
next question (D20). Each question uses the snapshot taken when it started.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from chatbox.guardrails import (
    Classification,
    InputClassifier,
    OutputChecker,
    OutputVerdict,
    canned_reply_for,
)
from chatbox.limits import check_limits, local_now
from chatbox.log import Controls, DailyCounter, ExchangeLog, ExchangeRecord
from chatbox.policy import MAX_REMEMBER_EXCHANGES, Policy
from chatbox.policy_store import PolicySnapshot, PolicyStore
from chatbox.providers.base import ChatMessage, ModelProvider, ModelReply

_log = logging.getLogger(__name__)

__all__ = ["Answer", "Classification", "OutputVerdict", "Pipeline", "canned_reply_for"]

# Workers finish in the background after a deadline passes; the pool is shared so a
# stuck call never blocks the next question.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chatbox")


@dataclass(frozen=True)
class _Outcome:
    value: Any = None
    error: BaseException | None = None
    ms: int = 0


def _timed(fn: Callable[..., Any], *args: Any) -> _Outcome:
    t = time.monotonic()
    try:
        value = fn(*args)
        return _Outcome(value, None, round((time.monotonic() - t) * 1000))
    except Exception as e:  # noqa: BLE001 - every failure must fail closed
        return _Outcome(None, e, round((time.monotonic() - t) * 1000))


def _wait(future: Future, deadline_s: float | None, started: float) -> _Outcome:
    """Result of a `_timed` future, or a timeout outcome once `deadline_s` passes
    (measured from `started`)."""
    remaining = None if deadline_s is None else max(0.0, deadline_s - (time.monotonic() - started))
    try:
        return future.result(timeout=remaining)
    except FutureTimeout:
        ms = round((time.monotonic() - started) * 1000)
        return _Outcome(None, TimeoutError(f"timed out after {deadline_s:g}s"), ms)


def _picture_note(cls: Classification) -> str:
    """What the classifier picked for the screen, so a log shows why a picture was (or
    wasn't) the one it was: " [picture: shark vs crocodile, animal]"."""
    if not cls.subject:
        return ""
    subject = f"{cls.subject} vs {cls.opponent}" if cls.opponent else cls.subject
    return f" [picture: {subject}, {cls.picture_category or 'no category'}]"


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# What every question gets while a parent has paused Chatbox from the settings page.
PAUSED_REPLY = "I'm currently resting. Let's talk later."


# ---- Pipeline -----------------------------------------------------------------

@dataclass
class Answer:
    text: str
    answered_by: Literal["model", "canned"]
    steps: list[dict] = field(default_factory=list)
    exchange_id: int | None = None
    latency_ms: int = 0


def warm_up(*things) -> None:
    """Call `warm()` on each thing that has one, each in its own daemon thread."""
    for thing in things:
        if warm := getattr(thing, "warm", None):
            threading.Thread(target=_quietly, args=(warm,), daemon=True,
                             name="chatbox-warm").start()


def _quietly(fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - a warm-up is only ever an optimisation
        _log.debug("warm-up failed (harmless): %s", e)


class Pipeline:
    def __init__(
        self,
        policy: Policy | PolicyStore,
        provider: ModelProvider,
        counter: DailyCounter,
        log: ExchangeLog | None = None,
        *,
        classifier: InputClassifier,
        output_checker: OutputChecker | None,  # None = layer 4 turned off in settings
        classify_timeout_s: float | None = 4.0,
        check_timeout_s: float | None = 4.0,
        generate_timeout_s: float | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        controls: Controls | None = None,  # None = no pause switch
    ) -> None:
        self.controls = controls
        self.store = policy if isinstance(policy, PolicyStore) else PolicyStore(policy)
        self.provider = provider
        self.counter = counter
        self.classifier = classifier
        self.output_checker = output_checker
        self.log = log
        self.classify_timeout_s = classify_timeout_s
        self.check_timeout_s = check_timeout_s
        self.generate_timeout_s = generate_timeout_s
        self.history: list[ChatMessage] = []  # alternating user/assistant, this session only
        self.clock = clock

    @property
    def policy(self) -> Policy:
        """The policy the next question will use."""
        return self.store.policy

    def warm(self) -> None:
        """Open connections for the calls a question makes (answer, classifier, output
        check), in the background, so they're ready when the question arrives. Returns
        at once; failures are ignored, since the real call just connects as usual."""
        warm_up(self.provider, self.classifier, self.output_checker)

    @property
    def system_prompt(self) -> str:
        return self.store.snapshot().system_prompt

    def ask(self, question: str) -> Answer:
        started = time.monotonic()
        snap = self.store.snapshot()  # this question keeps it, even if a save lands mid-way
        now = self.clock()
        local_date = local_now(snap.policy, now).date().isoformat()
        steps: list[dict] = []

        def step(name: str, decision: str, detail: str = "", ms: int | None = None) -> None:
            steps.append({"step": name, "decision": decision, "detail": detail, "ms": ms})

        answer_text, answered_by, model_reply = self._run(snap, question, now, local_date, step)
        latency_ms = round((time.monotonic() - started) * 1000)

        # Questions turned away by the limits don't count and don't join the conversation.
        if steps[0]["decision"] == "allow":
            self.counter.increment(local_date)
            self._remember(question, answer_text)

        if self.log is None:
            return Answer(answer_text, answered_by, steps, None, latency_ms)
        record = ExchangeRecord(
            ts_utc=now.astimezone(timezone.utc),
            local_date=local_date,
            question=question,
            answer=answer_text,
            answered_by=answered_by,
            policy_version=snap.policy.version_label(),
            provider=self.provider.name if model_reply else None,
            model=model_reply.model if model_reply else None,
            latency_ms=latency_ms,
            input_tokens=model_reply.input_tokens if model_reply else None,
            output_tokens=model_reply.output_tokens if model_reply else None,
            steps=steps,
        )
        exchange_id = self.log.record(record)
        return Answer(answer_text, answered_by, steps, exchange_id, latency_ms)

    def _remember(self, question: str, answer: str) -> None:
        # Canned replies are kept too, so a follow-up "why?" sees what was said.
        self.history += [ChatMessage("user", question), ChatMessage("assistant", answer)]
        # Keep the most any policy allows, so raising the setting mid-session takes effect.
        del self.history[: max(0, len(self.history) - 2 * MAX_REMEMBER_EXCHANGES)]

    def _run(
        self, snap: PolicySnapshot, question: str, now: datetime, local_date: str,
        step: Callable[..., None],
    ) -> tuple[str, Literal["model", "canned"], ModelReply | None]:
        policy = snap.policy
        went_wrong = policy.canned_replies.something_went_wrong

        # A parent's pause beats everything; paused questions don't count toward the cap.
        if self.controls is not None and self.controls.paused:
            step("pause", "deny", "paused from the settings page", 0)
            return PAUSED_REPLY, "canned", None

        # Layer 6: schedule and daily cap.
        used = self.counter.get(local_date)
        limit = check_limits(policy, now, used)
        if not limit.allowed:
            step("limits", "deny", limit.reason, 0)
            return limit.canned_reply, "canned", None
        step("limits", "allow", f"{used + 1}/{policy.limits.daily_questions} today", 0)

        # Layer 3 and layers 1-2 run concurrently; the answer is only used on "allow".
        # The classifier sees everything kept and trims to its own setting, so it judges
        # follow-ups in context even when the answering model is told to forget.
        history = list(self.history)
        keep = 2 * policy.answers.remember_exchanges
        messages = [*(history[-keep:] if keep else []), ChatMessage("user", question)]
        started = time.monotonic()
        classify_f = _POOL.submit(_timed, self.classifier.classify, question, history, policy)
        generate_f = _POOL.submit(_timed, self.provider.generate, snap.system_prompt, messages)

        c = _wait(classify_f, self.classify_timeout_s, started)
        if c.error is not None:
            step("classify", "error", f"failed closed: {_err(c.error)}", c.ms)
            step("generate", "discarded", "classifier failed", None)
            return went_wrong, "canned", None
        cls: Classification = c.value
        detail = f"{cls.topic_id}: {cls.detail}" if cls.topic_id else cls.detail
        step("classify", cls.decision, detail + _picture_note(cls), c.ms)

        # Layer 5: canned replies for sensitive categories.
        canned = canned_reply_for(cls, policy)
        if canned is not None:
            step("canned", "use", cls.topic_id or cls.decision, 0)
            step("generate", "discarded", f"classifier said {cls.decision}", None)
            return canned, "canned", None
        step("canned", "skip", "", 0)

        g = _wait(generate_f, self.generate_timeout_s, started)
        if g.error is not None:
            step("generate", "error", _err(g.error), g.ms)
            return went_wrong, "canned", None
        reply: ModelReply = g.value
        if reply.refused or not reply.text:
            step("generate", "refused" if reply.refused else "empty", reply.stop_reason or "", g.ms)
            return policy.canned_replies.blocked_topic, "canned", reply
        step("generate", "ok", reply.stop_reason or "", g.ms)

        # Layer 4: output check on the whole answer.
        if self.output_checker is None:
            step("output_check", "skipped", "turned off in chatbox.toml", 0)
            return reply.text, "model", reply
        check_started = time.monotonic()
        check_f = _POOL.submit(_timed, self.output_checker.check, question, reply.text, history, policy)
        v = _wait(check_f, self.check_timeout_s, check_started)
        if v.error is not None:
            step("output_check", "error", f"failed closed: {_err(v.error)}", v.ms)
            return went_wrong, "canned", reply
        verdict: OutputVerdict = v.value
        if not verdict.passed:
            step("output_check", "fail", verdict.detail, v.ms)
            return policy.canned_replies.blocked_topic, "canned", reply
        step("output_check", "pass", verdict.detail, v.ms)

        return reply.text, "model", reply


def build_pipeline(
    policy: Policy | PolicyStore, settings: Any, counter: DailyCounter, log: ExchangeLog | None = None,
    controls: Controls | None = None,
) -> Pipeline:
    """Wire up the answering model and both guardrail checks from chatbox.toml settings."""
    from chatbox.guardrails import ModelClassifier, ModelOutputChecker
    from chatbox.providers import make_provider

    g = settings.guardrails
    name = settings.provider_name
    return Pipeline(
        policy,
        make_provider(name, settings.provider_settings),
        counter,
        log,
        classifier=ModelClassifier(make_provider(name, g.classifier.provider_settings),
                                   g.history_exchanges),
        output_checker=ModelOutputChecker(make_provider(name, g.output_check.provider_settings),
                                          g.history_exchanges, g.length_tolerance)
        if g.output_check.enabled else None,
        classify_timeout_s=g.classifier.timeout_seconds,
        check_timeout_s=g.output_check.timeout_seconds,
        generate_timeout_s=settings.provider_settings.get("timeout_seconds"),
        controls=controls,
    )
