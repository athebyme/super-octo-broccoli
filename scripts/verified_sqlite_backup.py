#!/usr/bin/env python3
"""Create and actually restore-check a bounded, private SQLite gzip snapshot.

Stdlib only, including when passed to docker exec python -. No app imports,
source writes, provider calls, retention deletion or restore-in-place.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
import zlib

GIB = 1024 ** 3
CHUNK = 1024 ** 2


class BackupError(RuntimeError):
    """Message is an allowlisted code, never database contents."""


class Budget:
    def __init__(self, directory, reserve, seconds, source_directory=None):
        self.directory, self.reserve = directory, reserve
        self.source_directory = source_directory
        self.started = time.monotonic()
        self.deadline = self.started + seconds

    def check(self, additional=0):
        if time.monotonic() >= self.deadline:
            raise BackupError('deadline_exceeded')
        if shutil.disk_usage(self.directory).free < self.reserve + additional:
            raise BackupError('insufficient_space')
        if (self.source_directory is not None
                and shutil.disk_usage(self.source_directory).free < self.reserve):
            raise BackupError('insufficient_source_space')


@contextmanager
def backup_lock(source):
    descriptor = os.open(str(source) + '.backup.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError('backup_already_running') from exc
        yield
    finally:
        os.close(descriptor)


def _quick_check(path, budget):
    # Only our closed, uniquely owned temp file is immutable, never the source.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    failure = []

    def progress():
        try:
            budget.check()
        except BackupError as exc:
            failure.append(exc)
            return 1
        return 0

    try:
        connection.set_progress_handler(progress, 100000)
        try:
            rows = connection.execute('PRAGMA quick_check').fetchmany(2)
        except sqlite3.Error:
            if failure:
                raise failure[0]
            raise
        if rows != [('ok',)]:
            raise BackupError('sqlite_check_failed')
    finally:
        connection.close()
    budget.check()


class _ArchiveWriter:
    def __init__(self, raw, limit, budget):
        self.raw, self.limit, self.budget = raw, limit, budget
        self.written, self.digest = 0, hashlib.sha256()

    def write(self, data):
        if self.written + len(data) > self.limit:
            raise BackupError('archive_limit_exceeded')
        self.budget.check(len(data))
        written = self.raw.write(data)
        if written != len(data):
            raise BackupError('short_archive_write')
        self.written += written
        self.digest.update(data)
        return written

    def flush(self):
        self.raw.flush()


def _compress(snapshot, archive, archive_limit, budget):
    digest, size = hashlib.sha256(), 0
    with snapshot.open('rb') as source, archive.open('xb') as raw:
        os.chmod(archive, 0o600)
        writer = _ArchiveWriter(raw, archive_limit, budget)
        with gzip.GzipFile(filename='', fileobj=writer, mode='wb',
                           compresslevel=1, mtime=0) as compressed:
            while chunk := source.read(CHUNK):
                budget.check()
                size += len(chunk)
                digest.update(chunk)
                compressed.write(chunk)
        raw.flush()
        os.fsync(raw.fileno())
    return size, digest.hexdigest(), writer.digest.hexdigest()


def _restore_archive(archive, restored, expected_size, expected_sha256, budget):
    size, digest = 0, hashlib.sha256()
    with gzip.open(archive, 'rb') as source, restored.open('xb') as target:
        os.chmod(restored, 0o600)
        while chunk := source.read(CHUNK):
            budget.check(len(chunk))
            size += len(chunk)
            if size > expected_size:
                raise BackupError('restore_size_mismatch')
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise BackupError('restore_digest_mismatch')
    _quick_check(restored, budget)


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_manifest(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError('duplicate_manifest_key')
            result[key] = value
        return result

    with path.open('rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise BackupError('manifest_too_large')
    try:
        record = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise BackupError('invalid_manifest') from exc
    if not isinstance(record, dict):
        raise BackupError('invalid_manifest')
    version = record.get('format_version')
    legacy = 'format_version' not in record
    if not legacy and (type(version) is not int or version != 1):
        raise BackupError('unsupported_manifest_version')
    if (record.get('quick_check') != 'ok' or record.get('round_trip_verified') is not True
            or (legacy and record.get('status') not in (None, 'complete'))
            or (not legacy and (record.get('status') != 'complete'
                                or record.get('restore_file_verified') is not True
                                or record.get('restored_quick_check') != 'ok'))):
        raise BackupError('unverified_manifest')
    for key in ('uncompressed_bytes', 'compressed_bytes'):
        if type(record.get(key)) is not int or record[key] <= 0:
            raise BackupError('invalid_manifest_size')
    for key in ('sha256', 'archive_sha256'):
        value = record.get(key)
        if legacy and key == 'archive_sha256' and value is None:
            continue
        if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
            raise BackupError('invalid_manifest_digest')
    anchor = record.get('snapshot_utc')
    try:
        if not isinstance(anchor, str) or len(anchor) > 80:
            raise ValueError
        if datetime.fromisoformat(anchor).utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise BackupError('invalid_snapshot_time') from exc
    name = record.get('archive', path.with_suffix('.gz').name if legacy else None)
    if not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.(sqlite|db)\.gz', name) is None:
        raise BackupError('invalid_archive_name')
    return {**record, 'archive': name, 'legacy_manifest': legacy}


def restore_backup(manifest_path, destination, *, reserve=2 * GIB, timeout=1800,
                   max_restore_bytes=64 * GIB, emit=lambda record: None):
    """Restore exclusively into a new directory; never replace a live database.

    This stages recovery. It does not start an application, restore encryption
    keys, rewind the provider rate ledger or authorize replaying external writes.
    """
    if reserve < 0 or timeout <= 0 or max_restore_bytes <= 0:
        raise BackupError('invalid_limits')
    manifest = Path(manifest_path).resolve(strict=True)
    if manifest.suffix != '.json':
        raise BackupError('manifest_required')
    record = _restore_manifest(manifest)
    archive = manifest.parent / record['archive']
    if archive.is_symlink() or not archive.is_file():
        raise BackupError('archive_not_regular_file')
    if record['uncompressed_bytes'] > max_restore_bytes:
        raise BackupError('restore_limit_exceeded')
    stat = archive.stat()
    if stat.st_size != record['compressed_bytes']:
        raise BackupError('archive_size_mismatch')
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    requested = Path(destination).absolute()
    if requested.is_symlink() or requested.parent.exists():
        raise BackupError('destination_directory_exists')
    target = requested.resolve()
    if target.parent.exists():
        raise BackupError('destination_directory_exists')
    if target.name == 'restore-receipt.json':
        raise BackupError('invalid_destination_name')
    # Exclusive creation also closes the exists()/mkdir race. A working data
    # directory can never be admitted, including if its DB file is missing.
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
    budget = Budget(target.parent, reserve, timeout, archive.parent)
    try:
        budget.check(record['uncompressed_bytes'])
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=target.parent) as temporary:
            work = Path(temporary)
            emit({'phase': 'archive_verification', 'snapshot_utc': record['snapshot_utc']})
            if record.get('archive_sha256'):
                digest = hashlib.sha256()
                with archive.open('rb') as source:
                    while chunk := source.read(CHUNK):
                        budget.check()
                        digest.update(chunk)
                if digest.hexdigest() != record['archive_sha256']:
                    raise BackupError('archive_digest_mismatch')
            restored = work / 'restored.sqlite'
            emit({'phase': 'restore_verification', 'uncompressed_bytes': record['uncompressed_bytes']})
            _restore_archive(archive, restored, record['uncompressed_bytes'], record['sha256'], budget)
            if archive.is_symlink() or identity(archive.stat()) != identity(stat):
                raise BackupError('archive_changed_during_restore')
            budget.check()
            result = {
                'format_version': 1, 'status': 'complete', 'mode': 'staged_restore',
                'snapshot_utc': record['snapshot_utc'],
                'completed_at_utc': datetime.now(timezone.utc).isoformat(),
                'archive': archive.name, 'legacy_manifest': record['legacy_manifest'],
                'uncompressed_bytes': record['uncompressed_bytes'], 'sha256': record['sha256'],
                'restored_quick_check': 'ok', 'destination': str(target),
                'production_cutover': False, 'provider_replay_authorized': False,
                'encryption_key_restored': False, 'provider_rate_ledger_restored': False,
                'seconds': round(time.monotonic() - budget.started, 3),
            }
            pending_receipt = work / 'receipt.json'
            with pending_receipt.open('x') as handle:
                os.chmod(pending_receipt, 0o600)
                json.dump(result, handle, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            if any(target.with_name(target.name + suffix).exists()
                   for suffix in ('-wal', '-shm', '-journal')):
                raise BackupError('destination_sidecar_exists')
            os.link(restored, target)
            _sync_directory(target.parent)
            os.link(pending_receipt, target.parent / 'restore-receipt.json')
            _sync_directory(target.parent)
            return result
    finally:
        # rmdir only succeeds if empty: never remove another actor's files or
        # a published DB if receipt publication was interrupted.
        try:
            target.parent.rmdir()
        except OSError:
            pass


def create_backup(database, output_dir, *, archive_limit=2 * GIB,
                  reserve=2 * GIB, timeout=1800, emit=lambda record: None):
    """Return a complete manifest only after a real restore in a separate file."""
    if archive_limit <= 0 or reserve < 0 or timeout <= 0:
        raise BackupError('invalid_limits')
    source = Path(database).resolve(strict=True)
    if not source.is_file():
        raise BackupError('source_not_file')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = Budget(output, reserve, timeout, source.parent)
    budget.check()
    with backup_lock(source):
        # Failures delete only this run's private temporary directory. A kill
        # can leave an unaccepted folder; there is no automatic retention purge.
        with tempfile.TemporaryDirectory(prefix='.sqlite-backup-', dir=output) as temporary:
            work = Path(temporary)
            snapshot, archive, restored = (work / name for name in
                                          ('snapshot.sqlite', 'archive.gz', 'restored.sqlite'))
            reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
            writer = None
            try:
                reader.execute('PRAGMA query_only=ON')
                snapshot_utc = datetime.now(timezone.utc).isoformat()
                reader.execute('BEGIN')
                reader.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
                size = (reader.execute('PRAGMA page_count').fetchone()[0]
                        * reader.execute('PRAGMA page_size').fetchone()[0])
                budget.check(size + archive_limit)
                emit({'phase': 'snapshot_pinned', 'snapshot_utc': snapshot_utc,
                      'database_bytes': size})
                snapshot.touch(mode=0o600, exist_ok=False)
                writer = sqlite3.connect(snapshot)
                last_progress = [budget.started]

                def progress(status, remaining, total):
                    budget.check()
                    if time.monotonic() - last_progress[0] >= 30:
                        emit({'phase': 'copy', 'remaining_pages': remaining, 'total_pages': total})
                        last_progress[0] = time.monotonic()

                budget.check()
                reader.backup(writer, pages=4096, progress=progress, sleep=0.05)
            finally:
                if writer is not None:
                    writer.close()
                reader.close()
            # Verify SQLite on the actual restored file below. Its exact hash
            # and size must match this raw snapshot, so a second full scan of
            # the same bytes here would not add evidence.
            emit({'phase': 'snapshot_copied'})
            raw_size, raw_sha256, archive_sha256 = _compress(snapshot, archive, archive_limit, budget)
            if raw_size != size:
                raise BackupError('snapshot_size_mismatch')
            snapshot.unlink()
            emit({'phase': 'restore_verification'})
            _restore_archive(archive, restored, raw_size, raw_sha256, budget)
            restored.unlink()
            budget.check()
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            name = f'seller-platform-{stamp}-{uuid.uuid4().hex[:12]}.sqlite'
            destination, manifest = output / (name + '.gz'), output / (name + '.json')
            record = {
                'format_version': 1, 'status': 'complete',
                'snapshot_utc': snapshot_utc,
                'completed_at_utc': datetime.now(timezone.utc).isoformat(),
                'archive': destination.name, 'uncompressed_bytes': raw_size,
                'compressed_bytes': archive.stat().st_size,
                'sha256': raw_sha256, 'archive_sha256': archive_sha256,
                'quick_check': 'ok', 'quick_check_scope': 'restored_snapshot',
                'restored_quick_check': 'ok',
                'round_trip_verified': True, 'restore_file_verified': True,
                'archive_cap_bytes': archive_limit, 'disk_reserve_bytes': reserve,
                'seconds': round(time.monotonic() - budget.started, 3),
            }
            pending_manifest = work / 'manifest.json'
            with pending_manifest.open('x') as handle:
                os.chmod(pending_manifest, 0o600)
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            # Publish exclusively. A crash between the two links can leave an
            # archive without a manifest; consumers must not admit it as valid.
            os.link(archive, destination)
            _sync_directory(output)
            os.link(pending_manifest, manifest)
            _sync_directory(output)
            return {**record, 'archive_path': str(destination), 'manifest_path': str(manifest)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='/app/data/seller_platform.db')
    parser.add_argument('--output-dir', default='/app/data/backups')
    parser.add_argument('--archive-limit-bytes', type=int, default=2 * GIB)
    parser.add_argument('--reserve-bytes', type=int, default=2 * GIB)
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    parser.add_argument('--restore-manifest')
    parser.add_argument('--destination')
    parser.add_argument('--max-restore-bytes', type=int, default=64 * GIB)
    args = parser.parse_args(argv)
    if bool(args.restore_manifest) != bool(args.destination):
        parser.error('--restore-manifest and --destination must be supplied together')
    try:
        emit = lambda row: print(json.dumps(row), file=sys.stderr, flush=True)
        if args.restore_manifest:
            result = restore_backup(args.restore_manifest, args.destination,
                                    reserve=args.reserve_bytes, timeout=args.timeout_seconds,
                                    max_restore_bytes=args.max_restore_bytes, emit=emit)
        else:
            result = create_backup(args.database, args.output_dir,
                                   archive_limit=args.archive_limit_bytes,
                                   reserve=args.reserve_bytes, timeout=args.timeout_seconds, emit=emit)
    except (BackupError, OSError, sqlite3.Error, EOFError, zlib.error) as exc:
        code = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        print(json.dumps({'status': 'failed', 'code': code}), file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
