#!/usr/bin/env python3
"""Scheduled copies: verified round trip, ownership journal, then keep two.

Only files created and recorded by this manager can be retired. Existing
archives outside its private directory are never adopted or removed.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from scripts import verified_sqlite_backup as backup

LIMIT = 65536
KEEP = 2


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise backup.BackupError('state_not_regular')
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise backup.BackupError('state_too_large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise backup.BackupError('duplicate_state_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        backup._sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise backup.BackupError('lock_not_regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise backup.BackupError('manager_already_running') from exc
        yield
    finally:
        os.close(fd)


def owned_record(directory, row):
    if not isinstance(row, dict) or set(row) != {'manifest', 'archive', 'sha256'}:
        raise backup.BackupError('invalid_ownership_record')
    name = row['manifest']
    if (not isinstance(name, str) or Path(name).name != name
            or not name.startswith('seller-platform-') or not name.endswith('.sqlite.json')):
        raise backup.BackupError('invalid_ownership_name')
    manifest = directory / name
    # Parse with the same strict verified-manifest contract as restore.
    read_json(manifest)
    record = backup._restore_manifest(manifest)
    archive = directory / record['archive']
    if (record['legacy_manifest'] or archive.is_symlink()
            or not stat.S_ISREG(archive.stat().st_mode)
            or archive.stat().st_size != record['compressed_bytes']
            or row['archive'] != record['archive'] or row['sha256'] != record['archive_sha256']):
        raise backup.BackupError('ownership_mismatch')
    return record


def create_managed(database, directory, *, archive_limit=2 * backup.GIB,
                   reserve=2 * backup.GIB, timeout=1800, emit=lambda row: None):
    directory = Path(directory).absolute()
    if directory.is_symlink() or directory.resolve() != directory:
        raise backup.BackupError('managed_directory_symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise backup.BackupError('managed_directory_not_private')
    with locked(directory / '.manager.lock'):
        index = directory / 'ownership.json'
        if not index.exists() and not index.is_symlink():
            # A fresh directory only. Do not adopt someone else's archives.
            if any(p.name != '.manager.lock' for p in directory.iterdir()):
                raise backup.BackupError('unowned_directory')
            state = {'version': 1, 'copies': []}
            atomic_json(index, state)
        else:
            state = read_json(index)
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('copies'), list) or len(state['copies']) > 100):
            raise backup.BackupError('invalid_ownership_state')
        rows = state['copies']
        if len({json.dumps(row, sort_keys=True) for row in rows}) != len(rows):
            raise backup.BackupError('duplicate_ownership_record')
        for row in rows:
            owned_record(directory, row)
        result = backup.create_backup(database, directory, archive_limit=archive_limit,
                                      reserve=reserve, timeout=timeout, emit=emit)
        row = {'manifest': Path(result['manifest_path']).name,
               'archive': result['archive'], 'sha256': result['archive_sha256']}
        owned_record(directory, row)
        # Acceptance is persisted before considering any previous owned copy.
        rows = [*rows, row]
        atomic_json(index, {'version': 1, 'copies': rows})
        retired = 0
        for old in rows[:-KEEP]:
            owned_record(directory, old)
            # Remove ownership first. Crash can leak a pair, never re-adopt it
            # or delete a different file on the next run.
            remaining = rows[1:]
            atomic_json(index, {'version': 1, 'copies': remaining})
            (directory / old['manifest']).unlink()
            (directory / old['archive']).unlink()
            backup._sync_directory(directory)
            rows = remaining
            retired += 1
        return {**result, 'managed_copies': len(rows), 'retired_owned_copies': retired}
