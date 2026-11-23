"""Private SQLite cache and physical-attempt reservation tests."""

from datetime import datetime, timedelta, timezone
import os
import sqlite3
import stat

import pytest

from services.openai_decisions import (
    DecisionAnswer,
    DecisionOutcome,
    DecisionQuestion,
)
from services.openai_decisions_cache import (
    DecisionWorkItem,
    OpenAIDecisionsCache,
)


NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def item(index, *, name=None):
    return DecisionWorkItem(
        fingerprint=f'{index:064x}',
        question=DecisionQuestion(
            name=name or f'product_{index}',
            instructions='Select one known option.',
            choices=('keep', 'change'),
        ),
    )


def cache(tmp_path, **kwargs):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700, parents=True)
    return OpenAIDecisionsCache(
        str(private / 'decisions.sqlite3'), max_calls_per_day=kwargs.pop('max_calls_per_day', 10),
        **kwargs,
    )


def success(reservation, *, choices=None):
    choices = choices or ['keep'] * len(reservation.claimed)
    return DecisionOutcome(
        'success',
        answers=tuple(
            DecisionAnswer(work.question.name, 'answered', choice, 0.75)
            for work, choice in zip(reservation.claimed, choices)
        ),
    )


def test_reserves_missing_questions_and_caches_answers_by_exact_fingerprint(tmp_path):
    ledger = cache(tmp_path)
    a, b = item(1), item(2)
    first = ledger.reserve_batch((a, b), now=NOW)
    assert first.kind == 'ready'
    assert first.claimed == (a, b)
    assert first.cached_answers == ()
    assert stat.S_IMODE((tmp_path / 'private' / 'decisions.sqlite3').stat().st_mode) == 0o600
    assert ledger.finish_batch(first, success(first), now=NOW + timedelta(seconds=2)).kind == 'success'

    second = ledger.reserve_batch((a, b), now=NOW + timedelta(minutes=1))
    assert second.kind == 'cached'
    assert second.claimed == ()
    assert second.blocked == ()
    assert [(fingerprint, answer.choice) for fingerprint, answer in second.cached_answers] == [
        (a.fingerprint, 'keep'), (b.fingerprint, 'keep'),
    ]

    missing = item(3)
    mixed = ledger.reserve_batch((a, missing), now=NOW + timedelta(minutes=2))
    assert mixed.kind == 'ready'
    assert mixed.claimed == (missing,)
    assert mixed.cached_answers == ((a.fingerprint, second.cached_answers[0][1]),)
    assert ledger.finish_batch(
        mixed, success(mixed), now=NOW + timedelta(minutes=2, seconds=1),
    ).kind == 'success'

    # The caller-provided exact fingerprint is the cache key; changing it
    # creates a missing question even when its display name is unchanged.
    changed = DecisionWorkItem(f'{4:064x}', a.question)
    changed_reservation = ledger.reserve_batch((changed,), now=NOW + timedelta(minutes=3))
    assert changed_reservation.kind == 'ready'
    assert changed_reservation.claimed == (changed,)
    assert changed_reservation.cached_answers == ()


def test_parallel_reservation_cannot_dispatch_duplicate_questions(tmp_path):
    path = str(tmp_path / 'private' / 'decisions.sqlite3')
    first_ledger = OpenAIDecisionsCache(path, max_calls_per_day=10)
    second_ledger = OpenAIDecisionsCache(path, max_calls_per_day=10)
    work = (item(5),)

    first = first_ledger.reserve_batch(work, now=NOW)
    duplicate = second_ledger.reserve_batch(work, now=NOW + timedelta(seconds=1))
    assert first.kind == 'ready'
    assert duplicate.kind == 'busy'
    assert duplicate.claimed == ()
    assert duplicate.blocked[0].state == 'busy'


def test_physical_call_lock_is_nonblocking_across_cache_objects_on_same_file(tmp_path):
    first_ledger = cache(tmp_path)
    work = (item(6),)
    reservation = first_ledger.reserve_batch(work, now=NOW)
    assert reservation.kind == 'ready'
    assert first_ledger.finish_batch(
        reservation, success(reservation), now=NOW + timedelta(seconds=1),
    ).kind == 'success'

    second_ledger = OpenAIDecisionsCache(first_ledger._path, max_calls_per_day=10)
    first_lock = first_ledger.acquire_physical_call_lock()
    assert first_lock is not None
    assert second_ledger.acquire_physical_call_lock() is None
    first_lock.release()

    second_lock = second_ledger.acquire_physical_call_lock()
    assert second_lock is not None
    second_lock.release()
    assert not os.path.exists(first_ledger._path + '.provider.lock')


def test_crashed_or_expired_claim_becomes_terminal_unknown(tmp_path):
    ledger = cache(tmp_path, lease_seconds=10)
    work = (item(7),)
    first = ledger.reserve_batch(work, now=NOW)
    assert first.kind == 'ready'

    after_lease = NOW + timedelta(seconds=11)
    retry = ledger.reserve_batch(work, now=after_lease)
    assert retry.kind == 'unknown'
    assert retry.claimed == ()
    assert retry.blocked[0].state == 'unknown'

    with sqlite3.connect(ledger._path) as connection:
        assert connection.execute(
            'SELECT state FROM decision_physical_attempts WHERE token=?', (first.token,),
        ).fetchone() == ('unknown',)


def test_rate_limit_cooldown_is_advisory_persisted_and_never_retried_early(tmp_path):
    ledger = cache(tmp_path)
    work = (item(8),)
    first = ledger.reserve_batch(work, now=NOW)
    limited = DecisionOutcome('rate_limited', http_status=429, retry_after_seconds=60)
    result = ledger.finish_batch(first, limited, now=NOW + timedelta(seconds=1))
    assert result.kind == 'deferred'
    assert result.retry_at == NOW + timedelta(seconds=61)

    early = ledger.reserve_batch(work, now=NOW + timedelta(seconds=60))
    assert early.kind == 'deferred'
    assert early.claimed == ()
    assert early.retry_at == result.retry_at

    # A new explicit invocation after cooldown can make one new bounded call.
    later = ledger.reserve_batch(work, now=result.retry_at)
    assert later.kind == 'ready'
    assert later.claimed == work


@pytest.mark.parametrize('advice', [None, -1, True, 10 * 365 * 24 * 60 * 60 + 1])
def test_invalid_retry_advice_marks_claim_unknown(advice, tmp_path):
    ledger = cache(tmp_path)
    reservation = ledger.reserve_batch((item(9),), now=NOW)
    outcome = DecisionOutcome('rate_limited', http_status=429, retry_after_seconds=advice)
    finished_at = NOW + timedelta(seconds=1)
    result = ledger.finish_batch(reservation, outcome, now=finished_at)
    assert result.kind == 'unknown'
    assert result.retry_at == finished_at + timedelta(minutes=5)
    assert ledger.reserve_batch(
        reservation.claimed, now=result.retry_at,
    ).kind == 'unknown'

    new_work = (item(30),)
    before_cooldown = ledger.reserve_batch(
        new_work, now=result.retry_at - timedelta(seconds=1),
    )
    assert before_cooldown.kind == 'deferred'
    assert before_cooldown.retry_at == result.retry_at
    after_cooldown = ledger.reserve_batch(new_work, now=result.retry_at)
    assert after_cooldown.kind == 'ready'


@pytest.mark.parametrize('status', [401, 403])
def test_auth_failures_pause_new_fingerprints_but_keep_exact_claim_terminal(tmp_path, status):
    ledger = cache(tmp_path)
    failed_work = (item(31),)
    reservation = ledger.reserve_batch(failed_work, now=NOW)
    finished_at = NOW + timedelta(seconds=1)
    result = ledger.finish_batch(
        reservation,
        DecisionOutcome('http_error', http_status=status),
        now=finished_at,
    )
    assert result.kind == 'unknown'
    assert result.safe_code == 'provider_auth_failure'
    assert result.retry_at == finished_at + timedelta(hours=1)

    reopened = OpenAIDecisionsCache(ledger._path, max_calls_per_day=10)
    assert reopened.reserve_batch(
        failed_work, now=result.retry_at,
    ).kind == 'unknown'
    new_work = (item(32),)
    paused = reopened.reserve_batch(
        new_work, now=result.retry_at - timedelta(seconds=1),
    )
    assert paused.kind == 'deferred'
    assert paused.retry_at == result.retry_at
    assert reopened.reserve_batch(new_work, now=result.retry_at).kind == 'ready'


def test_config_error_releases_only_unstarted_claim_and_does_not_spend_daily_cap(tmp_path):
    ledger = cache(tmp_path, max_calls_per_day=1)
    work = (item(10),)
    first = ledger.reserve_batch(work, now=NOW)
    assert ledger.finish_batch(
        first, DecisionOutcome('configuration_error', safe_code='invalid_request'), now=NOW,
    ).kind == 'released'

    second = ledger.reserve_batch(work, now=NOW + timedelta(seconds=1))
    assert second.kind == 'ready'
    assert ledger.finish_batch(second, success(second), now=NOW + timedelta(seconds=2)).kind == 'success'
    assert ledger.reserve_batch((item(11),), now=NOW + timedelta(seconds=3)).kind == 'budget_exhausted'


def test_refusal_is_cached_as_abstention_not_a_missing_answer(tmp_path):
    ledger = cache(tmp_path)
    work = (item(12),)
    reservation = ledger.reserve_batch(work, now=NOW)
    refusal = DecisionOutcome(
        'refused', answers=(DecisionAnswer(work[0].question.name, 'refused'),),
    )
    assert ledger.finish_batch(reservation, refusal, now=NOW + timedelta(seconds=1)).kind == 'refused'
    cached = ledger.reserve_batch(work, now=NOW + timedelta(minutes=1))
    assert cached.kind == 'cached'
    assert cached.cached_answers[0][1].status == 'refused'
    assert cached.cached_answers[0][1].choice is None


def test_wrong_question_answer_or_malformed_result_is_terminal_unknown(tmp_path):
    ledger = cache(tmp_path)
    work = (item(13),)
    reservation = ledger.reserve_batch(work, now=NOW)
    wrong = DecisionOutcome(
        'success', answers=(DecisionAnswer('some_other_question', 'answered', 'keep', 0.8),),
    )
    assert ledger.finish_batch(reservation, wrong, now=NOW + timedelta(seconds=1)).kind == 'unknown'
    assert ledger.reserve_batch(work, now=NOW + timedelta(minutes=1)).kind == 'unknown'


def test_private_path_and_fingerprint_validation_fail_closed(tmp_path):
    public_dir = tmp_path / 'public'
    public_dir.mkdir(mode=0o700)
    public_dir.chmod(0o755)
    public_path = str(public_dir / 'db.sqlite3')
    assert OpenAIDecisionsCache(public_path, max_calls_per_day=2).reserve_batch(
        (item(14),), now=NOW,
    ).kind == 'unavailable'
    assert not (public_dir / 'db.sqlite3').exists()

    ledger = cache(tmp_path / 'other')
    uppercase = DecisionWorkItem('A' * 64, item(15).question)
    assert ledger.reserve_batch((uppercase,), now=NOW).kind == 'invalid'
    assert stat.S_IMODE((tmp_path / 'other' / 'private').stat().st_mode) == 0o700

    malformed_text = DecisionWorkItem(
        f'{15:064x}',
        DecisionQuestion('bad_unicode', 'Choose.', ('ok', '\ud800')),
    )
    assert ledger.reserve_batch((malformed_text,), now=NOW).kind == 'invalid'


def test_database_contains_only_fingerprints_and_normalized_choices(tmp_path):
    ledger = cache(tmp_path)
    work = (item(16),)
    reservation = ledger.reserve_batch(work, now=NOW)
    assert ledger.finish_batch(reservation, success(reservation), now=NOW + timedelta(seconds=1)).kind == 'success'

    with open(ledger._path, 'rb') as database_file:
        raw = database_file.read()
    assert work[0].fingerprint.encode() in raw
    assert b'keep' in raw
    for forbidden in (b'input_json', b'secret seller fact', b'provider response body', b'api_key'):
        assert forbidden not in raw
