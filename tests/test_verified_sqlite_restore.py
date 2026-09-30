"""Restore never overwrites live files; only a checked new copy is published."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from scripts import verified_sqlite_backup as backup


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / 'live' / 'seller_platform.db'
    source.parent.mkdir()
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE operations (id INTEGER PRIMARY KEY, attempts INTEGER, outcome TEXT)')
    writer.execute("INSERT INTO operations VALUES (7, 1, 'uncertain')")
    writer.commit()
    record = backup.create_backup(source, tmp_path / 'archives', reserve=0, archive_limit=1024 ** 2)
    yield source, Path(record['manifest_path']), Path(record['archive_path'])
    writer.close()


def restore(archive, tmp_path, **kwargs):
    return backup.restore_backup(archive[1], tmp_path / 'recovery' / 'seller_platform.db', reserve=0, **kwargs)


def test_checked_restore_preserves_live_db_wal_archive_and_history(archive, tmp_path):
    source, manifest, compressed = archive
    paths = [source, Path(str(source) + '-wal'), manifest, compressed]
    before = {p: p.read_bytes() for p in paths}
    result = restore(archive, tmp_path)
    assert result['status'] == 'complete' and result['mode'] == 'staged_restore'
    assert result['production_cutover'] is False and result['provider_replay_authorized'] is False
    assert result['encryption_key_restored'] is False and result['provider_rate_ledger_restored'] is False
    target = Path(result['destination'])
    assert hashlib.sha256(target.read_bytes()).hexdigest() == result['sha256']
    with sqlite3.connect(target) as connection:
        assert connection.execute('SELECT * FROM operations').fetchall() == [(7, 1, 'uncertain')]
    assert all(p.read_bytes() == old for p, old in before.items())
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    receipt = json.loads((target.parent / 'restore-receipt.json').read_text())
    assert receipt == result
    assert not list(target.parent.glob('.restore-*'))


@pytest.mark.parametrize('has_database', [True, False])
def test_existing_data_directory_is_never_admitted_even_if_db_missing(archive, tmp_path, has_database):
    parent = tmp_path / 'existing'
    parent.mkdir()
    target = parent / 'seller_platform.db'
    if has_database:
        target.write_bytes(b'live database')
    wal = Path(str(target) + '-wal'); wal.write_bytes(b'live wal')
    before = {p.name: p.read_bytes() for p in parent.iterdir()}
    with pytest.raises(backup.BackupError, match='destination_directory_exists'):
        backup.restore_backup(archive[1], target, reserve=0)
    assert {p.name: p.read_bytes() for p in parent.iterdir()} == before


def test_broken_target_symlink_is_not_followed(archive, tmp_path):
    target = tmp_path / 'link.sqlite'
    target.symlink_to(tmp_path / 'elsewhere' / 'database.sqlite')
    with pytest.raises(backup.BackupError, match='destination_directory_exists'):
        backup.restore_backup(archive[1], target, reserve=0)
    assert target.is_symlink() and not (tmp_path / 'elsewhere').exists()


def test_legacy_verified_release_manifest_is_supported(archive, tmp_path):
    record = json.loads(archive[1].read_text())
    legacy = {k: record[k] for k in ['snapshot_utc', 'quick_check', 'uncompressed_bytes', 'compressed_bytes', 'sha256', 'round_trip_verified']}
    archive[1].write_text(json.dumps(legacy))
    result = restore(archive, tmp_path)
    assert result['legacy_manifest'] and result['restored_quick_check'] == 'ok'


@pytest.mark.parametrize('change,code', [
    ({'archive': '../elsewhere.sqlite.gz'}, 'invalid_archive_name'),
    ({'archive': '/tmp/elsewhere.sqlite.gz'}, 'invalid_archive_name'),
    ({'status': 'failed'}, 'unverified_manifest'),
    ({'format_version': 2}, 'unsupported_manifest_version'),
    ({'format_version': True}, 'unsupported_manifest_version'),
    ({'uncompressed_bytes': True}, 'invalid_manifest_size'),
    ({'sha256': 'missing'}, 'invalid_manifest_digest'),
    ({'snapshot_utc': '2026-09-25'}, 'invalid_snapshot_time'),
    ({'compressed_bytes': 1}, 'archive_size_mismatch'),
    ({'archive_sha256': '0' * 64}, 'archive_digest_mismatch'),
    ({'sha256': '0' * 64}, 'restore_digest_mismatch'),
    ({'uncompressed_bytes': 1}, 'restore_size_mismatch'),
])
def test_invalid_manifest_or_bytes_never_publish_database(archive, tmp_path, change, code):
    record = json.loads(archive[1].read_text()); record.update(change)
    archive[1].write_text(json.dumps(record))
    with pytest.raises(backup.BackupError, match=code):
        restore(archive, tmp_path)
    assert not (tmp_path / 'recovery').exists()


def test_duplicate_keys_and_oversized_manifests_are_rejected(archive, tmp_path):
    archive[1].write_text('{"format_version":1,"format_version":1}')
    with pytest.raises(backup.BackupError, match='duplicate_manifest_key'):
        restore(archive, tmp_path)
    archive[1].write_text(' ' * 65537)
    with pytest.raises(backup.BackupError, match='manifest_too_large'):
        restore(archive, tmp_path)


def test_legacy_failed_manifest_is_not_admitted(archive, tmp_path):
    record = json.loads(archive[1].read_text()); del record['format_version']; record['status'] = 'failed'
    archive[1].write_text(json.dumps(record))
    with pytest.raises(backup.BackupError, match='unverified_manifest'):
        restore(archive, tmp_path)


def test_corrupt_legacy_gzip_is_detected_without_archive_hash(archive, tmp_path):
    record = json.loads(archive[1].read_text()); del record['format_version']; del record['archive_sha256']
    data = bytearray(archive[2].read_bytes()); data[:2] = b'XX'; archive[2].write_bytes(data)
    archive[1].write_text(json.dumps(record))
    with pytest.raises(gzip.BadGzipFile):
        restore(archive, tmp_path)
    assert not (tmp_path / 'recovery').exists()


def test_archive_symlink_cannot_redirect_outside_manifest_directory(archive, tmp_path):
    data = archive[2].read_bytes(); archive[2].unlink()
    other = tmp_path / 'elsewhere.gz'; other.write_bytes(data); archive[2].symlink_to(other)
    with pytest.raises(backup.BackupError, match='archive_not_regular_file'):
        restore(archive, tmp_path)


def test_resource_budgets_reject_without_an_accepted_copy(archive, tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match='restore_limit_exceeded'):
        restore(archive, tmp_path, max_restore_bytes=1)
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1))
    with pytest.raises(backup.BackupError, match='insufficient_space'):
        restore(archive, tmp_path)
    assert not (tmp_path / 'recovery').exists()


def test_deadline_cleanup_preserves_archive(archive, tmp_path, monkeypatch):
    before = archive[2].read_bytes(); now = [0]
    monkeypatch.setattr(backup.time, 'monotonic', lambda: now[0])
    def expired(event):
        now[0] = 3
    with pytest.raises(backup.BackupError, match='deadline_exceeded'):
        restore(archive, tmp_path, timeout=1, emit=expired)
    assert archive[2].read_bytes() == before and not (tmp_path / 'recovery').exists()


def test_changed_archive_after_verification_is_rejected(archive, tmp_path, monkeypatch):
    original = backup._restore_archive
    def changed(path, *args):
        original(path, *args)
        with path.open('ab') as handle:
            handle.write(b'changed')
    monkeypatch.setattr(backup, '_restore_archive', changed)
    with pytest.raises(backup.BackupError, match='archive_changed_during_restore'):
        restore(archive, tmp_path)
    assert not (tmp_path / 'recovery').exists()


def test_cli_restore_mode_reports_staging_not_cutover(archive, tmp_path, capsys):
    assert backup.main(['--restore-manifest', str(archive[1]), '--destination', str(tmp_path/'recovery'/'seller_platform.db'), '--reserve-bytes', '0']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'staged_restore' and not result['production_cutover']


def test_restore_shell_no_longer_uses_legacy_copy_or_restart(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n[ "$1" = inspect ] || exit 99\nprintf "false\\n"\n')
    docker.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']}
    result = subprocess.run(['bash', 'scripts/restore_database.sh', 'backup.json', '/new/db.sqlite'], env=env, text=True, capture_output=True)
    assert result.returncode == 1 and 'isolated recovery environment' in result.stderr
    result = subprocess.run(['bash', 'scripts/restore_database.sh', 'backup.db'], env=env, text=True, capture_output=True)
    assert result.returncode == 2 and 'manifest.json' in result.stderr
