from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from services.marketplace_operation_retry import provider_read_deferred, read_retry_at


@pytest.mark.parametrize("hint", [None, True, False, -10, 0, "7200", float("nan"), float("inf")])
def test_invalid_hints_keep_normal_interval(hint):
    operation = SimpleNamespace(request_summary_json='{"offer_id":"safe"}')
    now = datetime(2026, 9, 25)
    assert read_retry_at(operation, now=now, interval=timedelta(seconds=30), retry_after=hint) == now + timedelta(seconds=30)
    assert not provider_read_deferred(operation, now)


@pytest.mark.parametrize("hint", [1e100, 10**1000])
def test_unrepresentable_delay_is_persisted_and_never_shortened(hint):
    operation = SimpleNamespace(request_summary_json='{"offer_id":"safe"}')
    now = datetime(2026, 9, 25)
    assert read_retry_at(operation, now=now, interval=timedelta(seconds=30), retry_after=hint) == datetime.max
    reloaded = SimpleNamespace(request_summary_json=operation.request_summary_json)
    assert provider_read_deferred(reloaded, now + timedelta(days=5))
    assert read_retry_at(reloaded, now=now, interval=timedelta(seconds=30), retry_after=5) == datetime.max
    assert json.loads(reloaded.request_summary_json)["offer_id"] == "safe"


def test_fractional_delay_rounds_up_and_existing_cooldown_survives():
    operation = SimpleNamespace(request_summary_json="{}")
    now = datetime(2026, 9, 25)
    due = now + timedelta(seconds=7201)
    assert read_retry_at(operation, now=now, interval=timedelta(seconds=30), retry_after=7200.01) == due
    assert read_retry_at(operation, now=now, interval=timedelta(seconds=30), retry_after=5) == due
    assert provider_read_deferred(operation, due - timedelta(microseconds=1))
    assert not provider_read_deferred(operation, due)
