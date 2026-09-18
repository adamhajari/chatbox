from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from talkbox.log import DailyCounter, ExchangeLog
from talkbox.policy import Policy
from talkbox.providers.base import ModelReply, ProviderError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "policies" / "default.yaml"
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def policy_data() -> dict:
    return yaml.safe_load(DEFAULT_POLICY.read_text())


@pytest.fixture
def policy(policy_data) -> Policy:
    return Policy.model_validate(policy_data)


@pytest.fixture
def log():
    log = ExchangeLog(":memory:")
    yield log
    log.close()


@pytest.fixture
def counter():
    c = DailyCounter(":memory:")
    yield c
    c.close()


def at(y, mo, d, h, mi=0) -> datetime:
    """Local time in the default policy's timezone."""
    return datetime(y, mo, d, h, mi, tzinfo=LA)


# 2026-09-16 is a Wednesday.
WED_NOON = at(2026, 9, 16, 12)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, text="The sky is blue because of sunlight.", refused=False, error=None):
        self.text, self.refused, self.error = text, refused, error
        self.calls = []

    def generate(self, system, messages):
        self.calls.append((system, messages))
        if self.error:
            raise ProviderError(self.error)
        return ModelReply(self.text, self.model, refused=self.refused,
                          stop_reason="refusal" if self.refused else "end_turn",
                          input_tokens=100, output_tokens=12)
