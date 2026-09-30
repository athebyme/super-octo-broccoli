"""Durable provider read cooldowns shared by Ozon write reconciliation.

Only a timestamp is added to the existing operation summary. The caller owns
the transaction, deadline and outcome; this module never performs I/O.
"""

from datetime import datetime, timedelta
import json
import math


_KEY = "provider_read_not_before"


def _summary(operation):
    value = json.loads(operation.request_summary_json or "{}")
    if not isinstance(value, dict):
        raise ValueError("Invalid operation summary")
    return value


def _not_before(summary):
    value = summary.get(_KEY)
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            return result
    except (TypeError, ValueError):
        pass
    # Corrupt durable metadata must not allow an early provider request.
    return datetime.max


def provider_read_deferred(operation, now):
    due = _not_before(_summary(operation))
    return due is not None and now < due


def read_retry_at(operation, *, now, interval, retry_after=None):
    """Keep the full finite provider delay, including fractional seconds.

    Unrepresentable positive delays fail closed at datetime.max. Invalid
    optional hints use the workflow's ordinary interval. Previously observed
    cooldowns can only grow, even across restarts or manual reconciliation.
    """
    summary = _summary(operation)
    provider_due = _not_before(summary)
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and retry_after > 0
        and (isinstance(retry_after, int) or math.isfinite(retry_after))
    ):
        try:
            observed = now + timedelta(seconds=math.ceil(retry_after))
        except OverflowError:
            observed = datetime.max
        provider_due = max(provider_due or now, observed)
        summary[_KEY] = provider_due.isoformat()
        operation.request_summary_json = json.dumps(
            summary, ensure_ascii=False, separators=(",", ":")
        )
    try:
        due = now + interval
    except OverflowError:
        due = datetime.max
    return max(due, provider_due or now)
