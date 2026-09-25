import json
import sqlite3

import pytest

from chatbox.guardrails import ModelClassifier, ModelOutputChecker
from chatbox.log import ExchangeLog
from chatbox.pricing import Price, cost_usd, format_cost, parse_prices, price_for
from chatbox.providers.base import Usage
from tests.conftest import FakeProvider
from tests.test_pipeline_and_log import make, rows  # noqa: F401 - fixture

PRICES = {"fake-model": Price(1.0, 5.0), "claude-haiku-4-5": Price(1.0, 5.0),
          "gpt-6-luna": Price(0.10, 0.50, cached_input=0.01)}


def test_prices_parse_from_config():
    assert parse_prices({"m": {"input": 1, "output": 5}}) == {"m": Price(1.0, 5.0)}
    assert parse_prices({"m": {"input": 1, "output": 5, "cached_input": 0.1}})["m"].cached_input == 0.1


def test_dated_snapshot_names_find_their_price():
    assert price_for("claude-haiku-4-5-20251001", PRICES) == PRICES["claude-haiku-4-5"]
    assert price_for("gpt-6-luna", PRICES) == PRICES["gpt-6-luna"]
    assert price_for("gpt-6-luna-pro", PRICES) is not None  # prefix match; list it to override
    assert price_for("claude-haiku-4", PRICES) is None


def test_cost_counts_cached_tokens_at_the_cached_rate():
    assert cost_usd(Usage("claude-haiku-4-5", 1000, 100), PRICES) == pytest.approx(0.0015)
    # 1000 input, 800 of them cached: 200 * 0.10 + 800 * 0.01 + 100 * 0.50, per million.
    assert cost_usd(Usage("gpt-6-luna", 1000, 100, 800), PRICES) == pytest.approx(0.000078)
    assert cost_usd(Usage("unlisted", 1000, 100), PRICES) is None
    assert cost_usd(Usage("gpt-6-luna", None, None), PRICES) is None


def test_format_cost():
    assert format_cost(0.000078) == "$0.000078"
    assert format_cost(None) == "cost unknown"


def test_every_model_call_is_counted_and_priced(policy, log, make):  # noqa: F811
    classifier = ModelClassifier(FakeProvider(structured={
        "decision": "allow", "topic_id": "none", "reason": "fine", "subject": "",
        "picture_category": "", "opponent": ""}))
    checker = ModelOutputChecker(FakeProvider(structured={
        "passed": True, "problems": [], "reason": "fine"}))
    p, _ = make(policy, classifier=classifier, output_checker=checker, prices=PRICES)
    a = p.ask("Why is the sky blue?")
    # FakeProvider: structured calls use 200/20 tokens, answers 100/12.
    assert [u["call"] for u in a.usage] == ["classify", "answer", "output_check"]
    assert (a.input_tokens, a.output_tokens) == (500, 52)
    assert a.cost_usd == pytest.approx((500 * 1.0 + 52 * 5.0) / 1e6)
    [r] = rows(log)
    assert (r["input_tokens"], r["output_tokens"]) == (500, 52)
    assert r["cost_usd"] == pytest.approx(a.cost_usd)
    assert json.loads(r["usage_json"]) == a.usage


def test_an_unpriced_model_leaves_the_cost_unknown(policy, log, make):  # noqa: F811
    p, _ = make(policy, prices={})
    a = p.ask("Why is the sky blue?")
    assert a.input_tokens == 100 and a.cost_usd is None
    assert rows(log)[0]["cost_usd"] is None


def test_a_discarded_answer_is_still_counted_once_finished(policy, log, make):  # noqa: F811
    class SlowRefuse:
        def classify(self, question, history, policy):
            import time
            time.sleep(0.2)  # the answer finishes first
            from chatbox.guardrails import Classification
            return Classification("refuse", "violence_and_weapons", "no",
                                  usage=Usage("fake-model", 200, 20))

    p, _ = make(policy, classifier=SlowRefuse(), prices=PRICES)
    a = p.ask("something blocked")
    assert a.answered_by == "canned"
    assert [u["call"] for u in a.usage] == ["classify", "answer (discarded)"]


def test_questions_with_no_model_call_cost_nothing(policy, log, make):  # noqa: F811
    from tests.conftest import at
    p, _ = make(policy, when=at(2026, 9, 16, 21), prices=PRICES)  # outside the schedule
    a = p.ask("Are you awake?")
    assert a.usage == [] and a.cost_usd == 0
    assert json.loads(rows(log)[0]["usage_json"]) == []


def test_an_older_database_gains_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE exchanges (id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL, local_date TEXT NOT NULL, question TEXT NOT NULL,
        answer TEXT NOT NULL, answered_by TEXT NOT NULL, policy_version TEXT NOT NULL,
        provider TEXT, model TEXT, latency_ms INTEGER NOT NULL, input_tokens INTEGER,
        output_tokens INTEGER, steps_json TEXT NOT NULL)""")
    conn.execute("""INSERT INTO exchanges (ts_utc, local_date, question, answer, answered_by,
        policy_version, latency_ms, steps_json) VALUES ('t', 'd', 'q', 'a', 'model', 'v', 1, '[]')""")
    conn.commit()
    conn.close()
    log = ExchangeLog(db)
    [r] = log.recent()
    assert r["cost_usd"] is None and r["usage_json"] is None
    log.close()
