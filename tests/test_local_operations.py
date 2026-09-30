import fcntl
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import local_backup_manager as manager
from scripts import local_operations_probe as probe
from scripts import local_operations as ops
from scripts import verified_sqlite_backup as backup


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'source.sqlite'
    with sqlite3.connect(path) as writer:
        writer.execute('CREATE TABLE facts (value TEXT)')
        writer.execute("INSERT INTO facts VALUES ('preserve me')")
    return path


def make(db, output):
    return manager.create_managed(db, output, archive_limit=1024 * 1024, reserve=0)


def test_rotation_keeps_two_only_after_third_verified_and_preserves_foreign(db, tmp_path):
    root = tmp_path / 'old-backups'
    root.mkdir()
    sentinel = root / 'old.sqlite.gz'
    sentinel.write_bytes(b'old archive')
    output = root / 'managed-daily'
    first = make(db, output)
    unknown = output / 'operator-original.txt'
    unknown.write_bytes(b'not managed')
    second = make(db, output)
    third = make(db, output)
    assert third['managed_copies'] == 2 and third['retired_owned_copies'] == 1
    assert not Path(first['archive_path']).exists()
    assert not Path(first['manifest_path']).exists()
    assert all(Path(r['archive_path']).is_file() for r in (second, third))
    assert sentinel.read_bytes() == b'old archive' and unknown.read_bytes() == b'not managed'
    assert probe.latest_copy([root, output], 2_000_000_000)['hash_rechecked_now'] is False


def test_failure_and_low_space_never_retire_existing_copies(db, tmp_path, monkeypatch):
    output = tmp_path / 'managed'
    make(db, output)
    make(db, output)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(backup.BackupError, match='insufficient_space'):
        make(db, output)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_corrupt_round_trip_preserves_previous(db, tmp_path, monkeypatch):
    output = tmp_path / 'managed'
    first = make(db, output)
    before = manager.read_json(output / 'ownership.json')
    def fail(*a, **kw):
        raise backup.BackupError('restore_digest_mismatch')
    monkeypatch.setattr(backup, '_restore_archive', fail)
    with pytest.raises(backup.BackupError, match='restore_digest_mismatch'):
        make(db, output)
    assert manager.read_json(output / 'ownership.json') == before
    assert Path(first['archive_path']).exists()


def test_nonempty_new_directory_never_adopts_archives(db, tmp_path):
    out = tmp_path / 'managed'
    out.mkdir(mode=0o700)
    (out / 'old').write_text('unchanged')
    with pytest.raises(backup.BackupError, match='unowned_directory'):
        make(db, out)
    assert (out / 'old').read_text() == 'unchanged'


@pytest.mark.parametrize('corrupt', ['traversal', 'symlink', 'duplicate', 'archive_size'])
def test_corrupt_owned_state_refuses_before_new_copy(db, tmp_path, corrupt):
    out = tmp_path / 'managed'
    first = make(db, out)
    index = out / 'ownership.json'
    state = manager.read_json(index)
    if corrupt == 'traversal':
        state['copies'][0]['manifest'] = '../victim.sqlite.json'
    elif corrupt == 'duplicate':
        state['copies'] *= 2
    elif corrupt == 'symlink':
        target = Path(first['manifest_path'])
        outside = tmp_path / 'outside.json'
        target.rename(outside)
        target.symlink_to(outside)
    else:
        Path(first['archive_path']).write_bytes(b'changed')
    manager.atomic_json(index, state)
    count = len(list(out.iterdir()))
    with pytest.raises((backup.BackupError, OSError)):
        make(db, out)
    assert len(list(out.iterdir())) == count


def test_manager_lock_and_backup_lock_are_distinct_and_do_not_release_others(db, tmp_path):
    out = tmp_path / 'managed'
    make(db, out)
    with manager.locked(out / '.manager.lock'):
        with pytest.raises(backup.BackupError, match='manager_already_running'):
            make(db, out)
    with backup.backup_lock(db):
        assert probe.active_backup(db)
        with pytest.raises(backup.BackupError, match='backup_already_running'):
            make(db, out)
        assert probe.active_backup(db)
    assert not probe.active_backup(db)


def test_probe_does_not_create_missing_database_or_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'observe', lambda: {'state': 'healthy'})
    result = probe.probe(tmp_path / 'missing.sqlite', tmp_path / 'backups')
    assert set(result['issues']) == {'database_observation_unknown', 'backup_missing'}
    assert list(tmp_path.iterdir()) == []


def test_probe_checks_active_lock_and_capacity_without_claiming_new_restore(db, tmp_path, monkeypatch):
    root = tmp_path / 'backups'
    make(db, root / 'managed-daily')
    monkeypatch.setattr(probe, 'observe', lambda: {'state': 'healthy'})
    monkeypatch.setattr(probe.shutil, 'disk_usage', lambda _: SimpleNamespace(free=3 * backup.GIB))
    assert probe.probe(db, root)['issues'] == ['backup_capacity_low']
    with backup.backup_lock(db):
        result = probe.probe(db, root)
        assert result['issues'] == [] and result['backup_active']
    assert result['latest_backup']['metadata_valid']
    assert not result['latest_backup']['hash_rechecked_now']


def obs(*codes, **kw):
    return {'issues': list(codes), **kw}


def test_incident_recovery_dedup_and_restart():
    state = None
    for n in range(3):
        state, event = ops.transition(state, obs('public_https_unhealthy'), 100 + n * 60)
        assert event == ('incident' if n == 2 else None)
    state = json.loads(json.dumps(state))
    for n in range(3):
        state, event = ops.transition(state, obs('public_https_unhealthy'), 280 + n * 60)
        assert event is None
    state, event = ops.transition(state, obs(), 460)
    assert event is None
    state, event = ops.transition(state, obs(), 520)
    assert event == 'recovery'
    state, event = ops.transition(state, obs(), 580)
    assert event is None


def test_startup_and_missing_samples_do_not_count_as_healthy_recovery():
    state = None
    for now in (100, 160, 220):
        state, _ = ops.transition(state, obs('container_not_running'), now)
    for now in (280, 340, 400):
        state, event = ops.transition(state, obs(startup_grace=True), now)
        assert event is None
    state, event = ops.transition(state, obs(), 460)
    assert event is None
    state, event = ops.transition(state, obs(), 1000)
    assert event is None


def test_active_backup_duration_does_not_hide_stall():
    state = None
    for now in range(100, 2381, 60):
        state, event = ops.transition(state, obs(local={'backup_active': True}), now)
    assert state['confirmed'] == ['backup_overdue']


def test_notification_reservation_survives_unknown_delivery_no_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'observe_host', lambda: obs('container_not_running'))
    calls = []
    def send(path, event, codes):
        saved = manager.read_json(path / 'observer.json')
        assert saved['notification']['status'] == 'reserved'
        calls.append(event)
        return {'sent': False}
    monkeypatch.setattr(ops, 'notify', send)
    for _ in range(8):
        result = ops.observe_once(tmp_path)
    assert calls == ['incident'] and result['notification']['status'] == 'unconfirmed'


def test_host_grace_does_not_hide_backup_fault(monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(ops, 'command', lambda *a, **kw: {'Running': True,
        'StartedAt': ops.datetime.fromtimestamp(now - 30, ops.timezone.utc).isoformat(), 'Health': {'Status': 'starting'}})
    monkeypatch.setattr(ops, 'public_https', lambda: False)
    monkeypatch.setattr(ops, 'container_call', lambda a: {'version': 1, 'issues': ['scheduler_unhealthy', 'backup_stale']})
    result = ops.observe_host(now)
    assert result['issues'] == ['backup_stale'] and result['startup_grace']


def test_no_success_receipt_on_failed_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'maintain_photo_cache', lambda: {'deleted_bytes': 0})
    monkeypatch.setattr(ops, 'container_call', lambda a: {'status': 'failed'})
    result = ops.run_backup(tmp_path)
    assert result['status'] == 'failed'
    assert manager.read_json(tmp_path / 'backup-run.json')['status'] == 'failed'


def test_subprocess_timeout_and_nonzero_do_not_expose_output():
    with pytest.raises(ValueError, match='command_timeout'):
        ops.command(['python3', '-c', 'import time;time.sleep(2)'], timeout=.02)
    with pytest.raises(ValueError, match='command_failed'):
        ops.command(['python3', '-c', "print('secret');raise SystemExit(1)"])


def test_subprocess_output_budget_is_enforced_while_child_is_running():
    with pytest.raises(ValueError, match='command_output_limit'):
        ops.command(['python3', '-c', "import sys,time;sys.stdout.write('x'*70000);sys.stdout.flush();time.sleep(10)"], timeout=1)


def test_stale_running_backup_receipt_is_an_incident(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'observe_host', lambda: obs())
    manager.atomic_json(tmp_path / 'backup-run.json', {'status': 'running', 'started_epoch': 0})
    for _ in range(3):
        result = ops.observe_once(tmp_path, send=False)
    assert result['event'] == 'incident'
    assert result['issues'] == ['backup_last_run_failed']
