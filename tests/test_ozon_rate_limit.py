from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import json
import sqlite3

import pytest

from services.ozon_rate_limit import OzonRateBudget
from services.ozon_api_client import OzonSellerAPIClient, OzonRateLimitError
from services.marketplace_adapters.types import MarketplaceCredentials
from tests.test_ozon_api_client import FakeResponse, FakeSession


def _reserve_process(arguments):
    directory, index = arguments
    return OzonRateBudget('shared-client', directory=directory, clock=lambda:100).reserve('endpoint-' + str(index))


def test_processes_share_client_budget_and_other_client_is_independent(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        delays = list(pool.map(_reserve_process, [(str(tmp_path), i) for i in range(80)]))
    assert sum(delay == 0 for delay in delays) == 40
    assert OzonRateBudget('another-client', directory=tmp_path, clock=lambda:100).reserve('endpoint') == 0
    assert OzonRateBudget('shared-client', directory=tmp_path, clock=lambda:101.1).reserve('endpoint') == 0


def test_endpoint_limit_and_longest_cooldown_survive_new_instance(tmp_path):
    clock = [100.0]
    budget = OzonRateBudget('client', directory=tmp_path, clock=lambda:clock[0])
    assert all(budget.reserve('same-method') == 0 for _ in range(20))
    assert budget.reserve('same-method') == 1
    assert budget.reserve('different-method') == 0
    assert budget.defer(5000)
    assert budget.defer(60)
    replacement = OzonRateBudget('client', directory=tmp_path, clock=lambda:clock[0])
    assert replacement.reserve('any-method') == 5000
    clock[0] += 4999
    assert replacement.reserve('any-method') == 1
    clock[0] += 2
    assert replacement.reserve('any-method') == 0


def test_corrupt_and_busy_ledgers_fail_closed(tmp_path):
    budget = OzonRateBudget('client', directory=tmp_path, clock=lambda:100)
    assert budget.reserve('method') == 0
    connection = sqlite3.connect(tmp_path/'budget.sqlite3')
    connection.execute('BEGIN IMMEDIATE')
    assert budget.reserve('method') == 60
    connection.rollback()
    connection.execute('UPDATE budgets SET state=?', ('not-json',));connection.commit();connection.close()
    assert budget.reserve('method') == 60
    assert not budget.defer(5000)


def test_transport_429_blocks_other_key_and_endpoint_before_http(tmp_path):
    budget = OzonRateBudget('client', directory=tmp_path, clock=lambda:100)
    first = FakeSession([FakeResponse(429, {}, headers={'Retry-After':'5000'})])
    client = OzonSellerAPIClient(MarketplaceCredentials(external_account_id='client', api_key='first-key'),
        session=first, rate_budget=budget, sleep_fn=lambda _:pytest.fail('must not sleep'))
    with pytest.raises(OzonRateLimitError) as original:
        client.request('product_list', {})
    assert original.value.retry_after == 5000
    second = FakeSession([])
    client2 = OzonSellerAPIClient(MarketplaceCredentials(external_account_id='client', api_key='rotated-key'),
        session=second, rate_budget=OzonRateBudget('client', directory=tmp_path, clock=lambda:100))
    with pytest.raises(OzonRateLimitError) as deferred:
        client2.request('product_import', {})
    assert deferred.value.code == 'ozon_local_rate_limited'
    assert deferred.value.retry_after == 5000 and not deferred.value.retriable
    assert not second.calls
    ledger=(tmp_path/'budget.sqlite3').read_bytes()
    assert b'first-key' not in ledger and b'rotated-key' not in ledger
    assert (tmp_path/'budget.sqlite3').stat().st_mode & 0o777 == 0o600
