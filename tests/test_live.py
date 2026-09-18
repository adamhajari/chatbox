"""Optional smoke test against the real API. Run with: pytest -m live"""

import os

import pytest
from dotenv import load_dotenv

from talkbox.config import load_settings
from talkbox.pipeline import Pipeline
from talkbox.providers import make_provider
from tests.conftest import ROOT, WED_NOON

load_dotenv(ROOT / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]


def test_real_answer(policy, counter):
    s = load_settings(ROOT / "talkbox.toml")
    provider = make_provider(s.provider_name, s.provider_settings)
    a = Pipeline(policy, provider, counter, clock=lambda: WED_NOON).ask("Why do cats purr?")
    assert a.answered_by == "model", a.steps
    assert 0 < len(a.text.split()) <= policy.answers.max_words * 1.5
