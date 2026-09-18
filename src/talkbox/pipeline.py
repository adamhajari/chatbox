"""The answer pipeline:

    question -> limits (layer 6) -> classify input (layer 3) -> canned reply (layer 5)
             -> model answer (layers 1-2) -> check output (layer 4) -> log (layer 7, optional)

A Pipeline is one chat session: follow-up questions see the earlier exchanges.
Nothing carries over between sessions (yet; see PLAN.md D9).

Layers 3, 4 and 5 are Phase 2. For now they are stubs that let everything through;
each is a small function/Protocol so Phase 2 can drop in a real implementation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Protocol

from talkbox.limits import check_limits, local_now
from talkbox.log import DailyCounter, ExchangeLog, ExchangeRecord
from talkbox.policy import Policy
from talkbox.prompt import compile_system_prompt
from talkbox.providers.base import ChatMessage, ModelProvider, ModelReply, ProviderError


# ---- Extension points (Phase 2) -------------------------------------------------

@dataclass(frozen=True)
class Classification:
    decision: Literal["allow", "redirect", "refuse"]
    topic_id: str | None = None  # which policy topic matched, if any
    detail: str = ""


class InputClassifier(Protocol):
    def classify(self, question: str, policy: Policy) -> Classification: ...


class AllowAllClassifier:
    """Phase 1 stub for layer 3."""

    def classify(self, question: str, policy: Policy) -> Classification:
        return Classification("allow", detail="stub: classification not implemented yet")


@dataclass(frozen=True)
class OutputVerdict:
    passed: bool
    detail: str = ""


class OutputChecker(Protocol):
    def check(self, question: str, answer: str, policy: Policy) -> OutputVerdict: ...


class PassAllOutputChecker:
    """Phase 1 stub for layer 4."""

    def check(self, question: str, answer: str, policy: Policy) -> OutputVerdict:
        return OutputVerdict(True, detail="stub: output check not implemented yet")


def canned_reply_for(classification: Classification, policy: Policy) -> str | None:
    """Layer 5: parent-approved wording for redirect/refuse decisions. Wired up now so
    Phase 2 only needs a real classifier; with the stub classifier it never fires."""
    if classification.decision == "refuse":
        return policy.canned_replies.blocked_topic
    if classification.decision == "redirect":
        for t in policy.topics.redirect_to_parent:
            if t.id == classification.topic_id:
                return t.reply
        return policy.canned_replies.blocked_topic
    return None


# ---- Pipeline -----------------------------------------------------------------

@dataclass
class Answer:
    text: str
    answered_by: Literal["model", "canned"]
    steps: list[dict] = field(default_factory=list)
    exchange_id: int | None = None


class Pipeline:
    def __init__(
        self,
        policy: Policy,
        provider: ModelProvider,
        counter: DailyCounter,
        log: ExchangeLog | None = None,
        max_history_exchanges: int = 20,
        classifier: InputClassifier | None = None,
        output_checker: OutputChecker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.policy = policy
        self.provider = provider
        self.counter = counter
        self.log = log
        self.max_history_exchanges = max_history_exchanges
        self.history: list[ChatMessage] = []  # alternating user/assistant, this session only
        self.classifier = classifier or AllowAllClassifier()
        self.output_checker = output_checker or PassAllOutputChecker()
        self.clock = clock
        self.system_prompt = compile_system_prompt(policy)

    def ask(self, question: str) -> Answer:
        started = time.monotonic()
        now = self.clock()
        local_date = local_now(self.policy, now).date().isoformat()
        steps: list[dict] = []

        def step(name: str, decision: str, detail: str = "") -> None:
            steps.append({"step": name, "decision": decision, "detail": detail})

        answer_text, answered_by, model_reply = self._run(question, now, local_date, step)

        # Questions turned away by the limits don't count and don't join the conversation.
        if steps[0]["decision"] == "allow":
            self.counter.increment(local_date)
            self._remember(question, answer_text)

        if self.log is None:
            return Answer(answer_text, answered_by, steps)
        record = ExchangeRecord(
            ts_utc=now.astimezone(timezone.utc),
            local_date=local_date,
            question=question,
            answer=answer_text,
            answered_by=answered_by,
            policy_version=self.policy.version_label(),
            provider=self.provider.name if model_reply else None,
            model=model_reply.model if model_reply else None,
            latency_ms=round((time.monotonic() - started) * 1000),
            input_tokens=model_reply.input_tokens if model_reply else None,
            output_tokens=model_reply.output_tokens if model_reply else None,
            steps=steps,
        )
        exchange_id = self.log.record(record)
        return Answer(answer_text, answered_by, steps, exchange_id)

    def _remember(self, question: str, answer: str) -> None:
        # Canned replies are kept too, so a follow-up "why?" sees what was said.
        self.history += [ChatMessage("user", question), ChatMessage("assistant", answer)]
        del self.history[: max(0, len(self.history) - 2 * self.max_history_exchanges)]

    def _run(
        self, question: str, now: datetime, local_date: str, step: Callable[..., None]
    ) -> tuple[str, Literal["model", "canned"], ModelReply | None]:
        policy = self.policy

        # Layer 6: schedule and daily cap.
        used = self.counter.get(local_date)
        limit = check_limits(policy, now, used)
        if not limit.allowed:
            step("limits", "deny", limit.reason)
            return limit.canned_reply, "canned", None
        step("limits", "allow", f"{used + 1}/{policy.limits.daily_questions} today")

        # Layer 3: input classification (stub).
        c = self.classifier.classify(question, policy)
        step("classify", c.decision, c.detail if not c.topic_id else f"{c.topic_id}: {c.detail}")

        # Layer 5: canned replies for sensitive categories.
        canned = canned_reply_for(c, policy)
        if canned is not None:
            step("canned", "use", c.topic_id or c.decision)
            return canned, "canned", None
        step("canned", "skip")

        # Layers 1-2: model answer under the compiled policy prompt.
        try:
            messages = [*self.history, ChatMessage("user", question)]
            reply = self.provider.generate(self.system_prompt, messages)
        except ProviderError as e:
            step("generate", "error", str(e))
            return policy.canned_replies.something_went_wrong, "canned", None
        if reply.refused or not reply.text:
            step("generate", "refused" if reply.refused else "empty", reply.stop_reason or "")
            return policy.canned_replies.blocked_topic, "canned", reply
        step("generate", "ok", reply.stop_reason or "")

        # Layer 4: output check (stub).
        verdict = self.output_checker.check(question, reply.text, policy)
        if not verdict.passed:
            step("output_check", "fail", verdict.detail)
            return policy.canned_replies.blocked_topic, "canned", reply
        step("output_check", "pass", verdict.detail)

        return reply.text, "model", reply
