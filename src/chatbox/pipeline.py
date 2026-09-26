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

A session starts in question-answering mode. When the classifier hears a request to
play the animal guessing game, it switches to game mode (chatbox.animal_game) until the
child says they're done, or a round ends without "play again": answers then come from
the game's prompt, with the same guardrails around them.

The policy comes from a PolicyStore, so a save from the parent web UI applies from the
next question (D20). Each question uses the snapshot taken when it started.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from chatbox.animal_game import (
    START_REPLY,
    AnimalGame,
    game_system_prompt,
    names_animal,
    stop_reply,
)
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
from chatbox.pricing import Price, cost_usd
from chatbox.providers.base import ChatMessage, ModelProvider, ModelReply, Usage

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
    # One entry per model call: {"call", "model", "input_tokens", "output_tokens",
    # "cached_input_tokens", "cost_usd"}.
    usage: list[dict] = field(default_factory=list)
    cost_usd: float | None = None  # all calls together; None if any call's is unknown

    @property
    def input_tokens(self) -> int | None:
        return _total(self.usage, "input_tokens")

    @property
    def output_tokens(self) -> int | None:
        return _total(self.usage, "output_tokens")


def _total(usage: list[dict], key: str) -> int | None:
    known = [u[key] for u in usage if u[key] is not None]
    return sum(known) if known else None


def _discarded_usage(future: Future) -> Usage | None:
    """A discarded answer is still billed. Count it if it has already finished; one still
    running is left out, since nothing waits for it."""
    if not future.done():
        return None
    outcome: _Outcome = future.result()
    return outcome.value.usage if isinstance(outcome.value, ModelReply) else None


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
        prices: dict[str, Price] | None = None,  # chatbox.toml [pricing], for the cost log
        rng: random.Random | None = None,  # picks the guessing game's animal
    ) -> None:
        self.controls = controls
        self.prices = prices or {}
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
        self.game: AnimalGame | None = None  # None = question-answering mode
        self.last_animal: str | None = None  # so "play again" never gets the same one
        self.rng = rng or random.Random()
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

        usage: list[dict] = []

        def spent(call: str, u: Usage | None) -> None:
            if u is not None:
                usage.append({"call": call, "model": u.model, "input_tokens": u.input_tokens,
                              "output_tokens": u.output_tokens,
                              "cached_input_tokens": u.cached_input_tokens,
                              "cost_usd": cost_usd(u, self.prices)})

        answer_text, answered_by, model_reply = self._run(
            snap, question, now, local_date, step, spent)
        latency_ms = round((time.monotonic() - started) * 1000)
        costs = [u["cost_usd"] for u in usage]
        total_cost = None if None in costs else sum(costs)

        # An answer that names the secret animal ends the round (it was guessed, or the
        # child gave up), so the next question is answered normally unless it's "play again".
        game = self.game
        if game is not None and answered_by == "model" and names_animal(answer_text, game.animal):
            self.game = None
            step("game", "over", f"the answer named the secret animal, {game.animal}", 0)

        # Questions turned away by the limits don't count and don't join the conversation.
        if steps[0]["decision"] == "allow":
            self.counter.increment(local_date)
            self._remember(question, answer_text)

        answer = Answer(answer_text, answered_by, steps, None, latency_ms, usage, total_cost)
        if self.log is None:
            return answer
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
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            steps=steps,
            cost_usd=total_cost,
            usage=usage,
        )
        answer.exchange_id = self.log.record(record)
        return answer

    def _remember(self, question: str, answer: str) -> None:
        # Canned replies are kept too, so a follow-up "why?" sees what was said.
        exchange = [ChatMessage("user", question), ChatMessage("assistant", answer)]
        # The game's own list starts with the request that started it, so the answering
        # model only sees the game; the classifier sees everything, to spot a mode change.
        for history in (self.history, *([self.game.history] if self.game else [])):
            history += exchange
            # Keep the most any policy allows, so raising the setting mid-session takes effect.
            del history[: max(0, len(history) - 2 * MAX_REMEMBER_EXCHANGES)]

    def _run(
        self, snap: PolicySnapshot, question: str, now: datetime, local_date: str,
        step: Callable[..., None], spent: Callable[[str, Usage | None], None],
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
        game = self.game
        keep = 2 * policy.answers.remember_exchanges
        earlier = list(game.history) if game else history
        messages = [*(earlier[-keep:] if keep else []), ChatMessage("user", question)]
        system = game_system_prompt(policy, game.animal) if game else snap.system_prompt
        started = time.monotonic()
        classify_f = _POOL.submit(_timed, self.classifier.classify, question, history, policy)
        generate_f = _POOL.submit(_timed, self.provider.generate, system, messages)

        c = _wait(classify_f, self.classify_timeout_s, started)
        if c.error is not None:
            step("classify", "error", f"failed closed: {_err(c.error)}", c.ms)
            step("generate", "discarded", "classifier failed", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return went_wrong, "canned", None
        cls: Classification = c.value
        spent("classify", cls.usage)
        detail = f"{cls.topic_id}: {cls.detail}" if cls.topic_id else cls.detail
        step("classify", cls.decision, detail + _picture_note(cls), c.ms)

        # Layer 5: canned replies for sensitive categories.
        canned = canned_reply_for(cls, policy)
        if canned is not None:
            step("canned", "use", cls.topic_id or cls.decision, 0)
            step("generate", "discarded", f"classifier said {cls.decision}", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return canned, "canned", None
        step("canned", "skip", "", 0)

        # Starting or stopping the guessing game gets a fixed reply, with no model call.
        if cls.mode_change == "start_animal_game":
            self.game = AnimalGame.start(self.rng, self.last_animal)
            self.last_animal = self.game.animal
            step("game", "start", f"secret animal: {self.game.animal}", 0)
            step("generate", "discarded", "the guessing game started", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return START_REPLY, "canned", None
        if cls.mode_change == "stop_animal_game" and game is not None:
            self.game = None
            step("game", "stop", f"secret animal was {game.animal}", 0)
            step("generate", "discarded", "the guessing game stopped", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return stop_reply(game.animal), "canned", None
        if game is not None:
            step("game", "play", f"secret animal: {game.animal}", 0)

        g = _wait(generate_f, self.generate_timeout_s, started)
        if g.error is not None:
            step("generate", "error", _err(g.error), g.ms)
            return went_wrong, "canned", None
        reply: ModelReply = g.value
        spent("answer", reply.usage)
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
        spent("output_check", verdict.usage)
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
        prices=settings.prices,
    )
