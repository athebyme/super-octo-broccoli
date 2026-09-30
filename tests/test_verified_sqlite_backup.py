"""Accepted archives are consistent WAL snapshots that were actually restored."""
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
def source(tmp_path):
    path = tmp_path / 'source.sqlite'
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)')
    writer.execute("INSERT INTO facts VALUES (1, 'checkpointed')")
    writer.commit()
    writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    writer.execute("INSERT INTO facts VALUES (2, 'only-in-wal')")
    writer.commit()
    yield path, writer, tmp_path / 'backups'
    writer.close()


def run(source, **kwargs):
    path, _, output = source
    return backup.create_backup(path, output, reserve=0, archive_limit=1024 ** 2, **kwargs)


def test_wal_snapshot_includes_wal_and_excludes_commit_after_pin(source, tmp_path):
    path, writer, output = source
    with sqlite3.connect(path.as_uri() + '?immutable=1', uri=True) as raw:
        assert raw.execute('SELECT count(*) FROM facts').fetchone()[0] == 1

    def after_pin(event):
        if event['phase'] == 'snapshot_pinned':
            writer.execute("UPDATE facts SET value='later' WHERE id=1")
            writer.execute("INSERT INTO facts VALUES (3, 'later')")
            writer.commit()

    record = run(source, emit=after_pin)
    assert record['status'] == 'complete'
    assert record['quick_check_scope'] == 'restored_snapshot'
    assert record['restore_file_verified'] and record['restored_quick_check'] == 'ok'
    archive, manifest = Path(record['archive_path']), Path(record['manifest_path'])
    assert archive.stat().st_mode & 0o777 == manifest.stat().st_mode & 0o777 == 0o600
    data = gzip.decompress(archive.read_bytes())
    assert hashlib.sha256(data).hexdigest() == record['sha256']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == record['archive_sha256']
    restored = tmp_path / 'independent-restore.sqlite'
    restored.write_bytes(data)
    with sqlite3.connect(restored) as connection:
        assert connection.execute('SELECT * FROM facts ORDER BY id').fetchall() == [
            (1, 'checkpointed'), (2, 'only-in-wal')]
    assert writer.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert json.loads(manifest.read_text())['archive'] == archive.name
    assert len(list(output.iterdir())) == 2


def test_repeat_never_overwrites_or_deletes_previous_archives(source):
    first = run(source)
    old = Path(first['archive_path']).read_bytes()
    second = run(source)
    assert first['archive_path'] != second['archive_path']
    assert Path(first['archive_path']).read_bytes() == old
    assert len(list(source[2].iterdir())) == 4


def test_concurrent_backup_is_rejected_without_releasing_original_lock(source):
    with backup.backup_lock(source[0]):
        for _ in range(2):
            with pytest.raises(backup.BackupError, match='backup_already_running'):
                run(source)
    assert run(source)['status'] == 'complete'


def test_capacity_preflight_creates_no_accepted_or_partial_copy(source, monkeypatch):
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1))
    with pytest.raises(backup.BackupError, match='insufficient_space'):
        run(source)
    assert list(source[2].iterdir()) == []
    assert source[1].execute('SELECT count(*) FROM facts').fetchone()[0] == 2


def test_reserve_is_monitored_on_source_filesystem_too(source, monkeypatch):
    def usage(path):
        return SimpleNamespace(free=0 if path == source[0].parent else 10 ** 9)
    monkeypatch.setattr(backup.shutil, 'disk_usage', usage)
    with pytest.raises(backup.BackupError, match='insufficient_source_space'):
        backup.create_backup(source[0], source[2], reserve=10)


def test_limit_failure_preserves_existing_backup_and_cleans_only_owned_temp(source):
    first = run(source)
    before = {p.name: p.read_bytes() for p in source[2].iterdir()}
    with pytest.raises(backup.BackupError, match='archive_limit_exceeded'):
        backup.create_backup(source[0], source[2], reserve=0, archive_limit=12)
    assert {p.name: p.read_bytes() for p in source[2].iterdir()} == before
    assert Path(first['manifest_path']).is_file()


def test_deadline_after_pin_does_not_publish_or_leave_lock_held(source, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(backup.time, 'monotonic', lambda: now[0])
    def expired(event):
        if event['phase'] == 'snapshot_pinned':
            now[0] = 2.0
    with pytest.raises(backup.BackupError, match='deadline_exceeded'):
        run(source, timeout=1, emit=expired)
    assert list(source[2].iterdir()) == []
    assert run(source)['status'] == 'complete'


def test_corrupted_archive_is_actually_detected_before_publication(source, monkeypatch):
    restore = backup._restore_archive
    def corrupt(archive, *args):
        archive.write_bytes(b'not a gzip archive')
        return restore(archive, *args)
    monkeypatch.setattr(backup, '_restore_archive', corrupt)
    with pytest.raises(gzip.BadGzipFile):
        run(source)
    assert list(source[2].iterdir()) == []


def test_restore_digest_mismatch_cannot_be_accepted(source, monkeypatch):
    restore = backup._restore_archive
    def wrong_digest(archive, restored, size, digest, budget):
        return restore(archive, restored, size, '0' * 64, budget)
    monkeypatch.setattr(backup, '_restore_archive', wrong_digest)
    with pytest.raises(backup.BackupError, match='restore_digest_mismatch'):
        run(source)
    assert list(source[2].iterdir()) == []


def test_missing_source_never_creates_an_empty_database(tmp_path):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(FileNotFoundError):
        backup.create_backup(path, tmp_path / 'backups')
    assert not path.exists()


def test_cli_failure_is_machine_readable_without_raw_database_error(tmp_path, capsys):
    assert backup.main(['--database', str(tmp_path / 'missing.sqlite')]) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert json.loads(captured.err) == {'status': 'failed', 'code': 'FileNotFoundError'}


@pytest.mark.parametrize('kwargs', [{'archive_limit': 0}, {'reserve': -1}, {'timeout': 0}])
def test_invalid_budget_is_rejected_before_creating_files(tmp_path, kwargs):
    with pytest.raises(backup.BackupError, match='invalid_limits'):
        backup.create_backup(tmp_path / 'missing.sqlite', tmp_path / 'backups', **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_shell_stopped_container_has_no_unprotected_copy_fallback(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n[ "$1" = inspect ] || exit 99\nprintf "false\\n"\n')
    docker.chmod(0o700)
    result = subprocess.run(['bash', 'scripts/backup_database.sh'], env={**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH']}, text=True, capture_output=True)
    assert result.returncode == 1
    assert 'no raw file-copy fallback' in result.stderr
