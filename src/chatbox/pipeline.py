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
play a game (chatbox.games), it switches to that game until the child says they're done
or the game ends without "play again": answers then come from the game, with the same
guardrails around them. Nothing in a game counts toward the day's questions; each
finished game counts toward that game's own daily limit.

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

from chatbox.games import Game, GameReply, games_by_mode
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


# What a turn was, for the daily counts: turned away (not counted, not remembered), a
# question (counts toward limits.daily_questions), or part of a game (doesn't).
Turn = Literal["denied", "question", "game"]

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


def _discarded_usage(future: Future | None) -> Usage | None:
    """A discarded answer is still billed. Count it if it has already finished; one still
    running is left out, since nothing waits for it."""
    if future is None or not future.done():
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
        rng: random.Random | None = None,  # picks each game's animal or words
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
        self.game: Game | None = None  # None = question-answering mode
        self.last_games: dict[str, Game] = {}  # by id, so "play again" isn't a repeat
        self.rng = rng or random.Random()
        # Called with the word a game wants on the screen whenever it changes, or None
        # when there's none (see chatbox.audio.picture.PictureShow.word).
        self.show_word: Callable[[str | None], None] | None = None
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

        answer_text, answered_by, model_reply, turn = self._run(
            snap, question, now, local_date, step, spent)
        latency_ms = round((time.monotonic() - started) * 1000)
        costs = [u["cost_usd"] for u in usage]
        total_cost = None if None in costs else sum(costs)

        self._update_screen()

        # Questions turned away don't count and don't join the conversation.
        if turn == "question":
            self.counter.increment(local_date)
        if turn != "denied":
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

    def _update_screen(self) -> None:
        if self.show_word is None:
            return
        try:
            self.show_word(self.game.screen_word if self.game else None)
        except Exception as e:  # noqa: BLE001 - the screen never fails a question
            _log.debug("couldn't update the screen: %s", e)

    def _games_left(self, policy: Policy, local_date: str, game_id: str) -> int:
        g = getattr(policy.games, game_id)
        if not g.enabled:
            return 0
        return max(0, g.daily_games - self.counter.games(local_date, game_id))

    def _any_games_left(self, policy: Policy, local_date: str) -> bool:
        return any(self._games_left(policy, local_date, g.id) > 0
                   for g in games_by_mode().values())

    def _advance(self, game: Game, outcome: str, text: str, local_date: str,
                 step: Callable[..., None]) -> str:
        """Apply a game answer the child is about to hear; returns the text to say."""
        before = game.progress()
        closing, over = game.advance(outcome)
        if over:
            self.game = None
            self.counter.increment_games(local_date, game.id)
            step("game", "over", f"finished ({before})", 0)
        elif outcome == "done":
            step("game", "next", game.progress(), 0)
        return f"{text} {closing}" if closing else text

    def _run(
        self, snap: PolicySnapshot, question: str, now: datetime, local_date: str,
        step: Callable[..., None], spent: Callable[[str, Usage | None], None],
    ) -> tuple[str, Literal["model", "canned"], ModelReply | None, Turn]:
        policy = snap.policy
        went_wrong = policy.canned_replies.something_went_wrong

        # A parent's pause beats everything; paused questions don't count toward the cap.
        if self.controls is not None and self.controls.paused:
            step("pause", "deny", "paused from the settings page", 0)
            return PAUSED_REPLY, "canned", None, "denied"

        game = self.game
        if game is not None and not getattr(policy.games, game.id).enabled:
            self.game = game = None  # a parent turned the game off partway through

        # Layer 6: schedule and daily cap. Game questions don't count toward the cap, so a
        # game carries on after the day's questions are used up, and while there are games
        # left a request to play can still start one. Only the classifier can tell that
        # request apart, so it still runs; anything else gets the daily-limit reply.
        used = self.counter.get(local_date)
        cap = policy.limits.daily_questions
        limit = check_limits(policy, now, used)
        over_cap = limit.reason == "daily_limit_reached"
        games_only = over_cap and game is None and self._any_games_left(policy, local_date)
        if not limit.allowed and not (over_cap and game is not None) and not games_only:
            step("limits", "deny", limit.reason, 0)
            return limit.canned_reply, "canned", None, "denied"
        if game is not None:
            step("limits", "allow", "a game question, not counted", 0)
        elif games_only:
            step("limits", "games_only", f"{used}/{cap} questions today; only a game can start", 0)
        else:
            step("limits", "allow", f"{used + 1}/{cap} today", 0)
        turn: Turn = "game" if game is not None else "denied" if games_only else "question"

        # Layer 3 and layers 1-2 run concurrently; the answer is only used on "allow".
        # The classifier sees everything kept and trims to its own setting, so it judges
        # follow-ups in context even when the answering model is told to forget.
        history = list(self.history)
        keep = 2 * policy.answers.remember_exchanges
        earlier = list(game.history) if game else history
        messages = [*(earlier[-keep:] if keep else []), ChatMessage("user", question)]
        started = time.monotonic()
        classify_f = _POOL.submit(_timed, self.classifier.classify, question, history, policy)
        if games_only:
            generate_f = None
        elif game is not None:
            generate_f = _POOL.submit(_timed, game.answer, self.provider,
                                      game.system_prompt(policy), messages)
        else:
            generate_f = _POOL.submit(_timed, self.provider.generate, snap.system_prompt,
                                      messages)

        c = _wait(classify_f, self.classify_timeout_s, started)
        if c.error is not None:
            step("classify", "error", f"failed closed: {_err(c.error)}", c.ms)
            step("generate", "discarded", "classifier failed", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return went_wrong, "canned", None, turn
        cls: Classification = c.value
        spent("classify", cls.usage)
        detail = f"{cls.topic_id}: {cls.detail}" if cls.topic_id else cls.detail
        step("classify", cls.decision, detail + _picture_note(cls), c.ms)

        # Over the question cap, only an allowed request to play gets past here.
        starts = games_by_mode().get(cls.mode_change or "")
        if games_only and not (cls.decision == "allow" and starts is not None):
            step("limits", "deny", f"{limit.reason}: not a request to play", 0)
            return limit.canned_reply, "canned", None, "denied"

        # Layer 5: canned replies for sensitive categories.
        canned = canned_reply_for(cls, policy)
        if canned is not None:
            step("canned", "use", cls.topic_id or cls.decision, 0)
            step("generate", "discarded", f"classifier said {cls.decision}", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return canned, "canned", None, turn
        step("canned", "skip", "", 0)

        # Starting or stopping a game gets a fixed reply, with no model call.
        if starts is not None:
            spent("answer (discarded)", _discarded_usage(generate_f))
            settings = getattr(policy.games, starts.id)
            if self._games_left(policy, local_date, starts.id) == 0:
                why = (f"{settings.daily_games}/{settings.daily_games} games today"
                       if settings.enabled else "turned off in the settings")
                step("game", "unavailable", f"{starts.id}: {why}", 0)
                step("generate", "discarded", "asked to play, but can't", None)
                return settings.unavailable_reply, "canned", None, "game"
            self.game = new = starts.start(self.rng, policy, self.last_games.get(starts.id))
            self.last_games[new.id] = new
            step("game", "start", f"{new.id}, {new.progress()}", 0)
            step("generate", "discarded", "a game started", None)
            return new.start_reply, "canned", None, "game"
        if cls.mode_change == "stop_game" and game is not None:
            self.game = None
            step("game", "stop", f"{game.id}, {game.progress()}", 0)
            step("generate", "discarded", "the game stopped", None)
            spent("answer (discarded)", _discarded_usage(generate_f))
            return game.stop_reply(), "canned", None, "game"
        if game is not None:
            step("game", "play", f"{game.id}, {game.progress()}", 0)

        g = _wait(generate_f, self.generate_timeout_s, started)
        if g.error is not None:
            step("generate", "error", _err(g.error), g.ms)
            return went_wrong, "canned", None, turn
        game_reply: GameReply | None = g.value if game is not None else None
        reply: ModelReply = game_reply.reply if game_reply else g.value
        spent("answer", reply.usage)
        if reply.refused or not reply.text:
            step("generate", "refused" if reply.refused else "empty", reply.stop_reason or "", g.ms)
            return policy.canned_replies.blocked_topic, "canned", reply, turn
        step("generate", "ok", reply.stop_reason or "", g.ms)

        # Layer 4: output check on the whole answer.
        if self.output_checker is None:
            step("output_check", "skipped", "turned off in chatbox.toml", 0)
            return self._said(game_reply, reply.text, local_date, step), "model", reply, turn
        check_started = time.monotonic()
        check_f = _POOL.submit(_timed, self.output_checker.check, question, reply.text, history, policy)
        v = _wait(check_f, self.check_timeout_s, check_started)
        if v.error is not None:
            step("output_check", "error", f"failed closed: {_err(v.error)}", v.ms)
            return went_wrong, "canned", reply, turn
        verdict: OutputVerdict = v.value
        spent("output_check", verdict.usage)
        if not verdict.passed:
            step("output_check", "fail", verdict.detail, v.ms)
            return policy.canned_replies.blocked_topic, "canned", reply, turn
        step("output_check", "pass", verdict.detail, v.ms)

        return self._said(game_reply, reply.text, local_date, step), "model", reply, turn

    def _said(self, game_reply: GameReply | None, text: str, local_date: str,
              step: Callable[..., None]) -> str:
        """The checked answer the child will hear. A game's answer moves the game on
        only now, once it has passed: one the child never hears changes nothing."""
        if game_reply is None or self.game is None:
            return text
        return self._advance(self.game, game_reply.outcome, text, local_date, step)


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
