#!/usr/bin/env python3
"""Read-only, bounded local facts; no Flask, credentials or provider calls."""
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from datetime import datetime, timezone

from scripts import verified_sqlite_backup as backup
from scripts.local_backup_manager import read_json
from services.scheduler_heartbeat import observe


def active_backup(database):
    try:
        fd = os.open(str(database) + '.backup.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def latest_copy(directories, now):
    latest = None
    count = 0
    for directory in directories:
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise ValueError('backup_directory_symlink')
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 256:
                    raise ValueError('backup_scan_limit')
                if not entry.name.endswith('.sqlite.json'):
                    continue
                path = Path(entry.path)
                read_json(path)
                record = backup._restore_manifest(path)
                archive = directory / record['archive']
                info = archive.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_size != record['compressed_bytes']):
                    raise ValueError('archive_metadata_mismatch')
                stamp = datetime.fromisoformat(record['snapshot_utc']).timestamp()
                if stamp > now + 60:
                    raise ValueError('future_backup_timestamp')
                if latest is None or stamp > latest['snapshot_epoch']:
                    latest = {'snapshot_epoch': stamp, 'age_seconds': max(0, int(now - stamp)),
                              'metadata_valid': True, 'hash_rechecked_now': False,
                              'compressed_bytes': record['compressed_bytes']}
    return latest


def probe(database=Path('/app/data/seller_platform.db'), backup_root=Path('/app/data/backups'), now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    result = {'version': 1, 'observed_epoch': now, 'issues': []}
    try:
        heartbeat = observe()
        result['scheduler'] = heartbeat.get('state', 'unknown')
        if result['scheduler'] != 'healthy':
            result['issues'].append('scheduler_unhealthy')
    except (OSError, ValueError, TypeError):
        result['issues'].append('scheduler_unknown')
    try:
        if database.is_symlink() or not database.is_file():
            raise ValueError('database_missing')
        with sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=0.2) as db:
            db.execute('PRAGMA query_only=ON')
            size = db.execute('PRAGMA page_size').fetchone()[0] * db.execute('PRAGMA page_count').fetchone()[0]
        free = shutil.disk_usage(database.parent).free
        active = active_backup(database)
        result.update(database_bytes=size, free_bytes=free, backup_active=active,
                      backup_required_bytes=size + 4 * backup.GIB)
        if free < 2 * backup.GIB:
            result['issues'].append('disk_reserve_low')
        elif not active and free < result['backup_required_bytes']:
            result['issues'].append('backup_capacity_low')
    except (OSError, ValueError, sqlite3.Error):
        result['issues'].append('database_observation_unknown')
    try:
        result['latest_backup'] = latest_copy([backup_root, backup_root / 'managed-daily'], now)
        if result['latest_backup'] is None:
            result['issues'].append('backup_missing')
        elif result['latest_backup']['age_seconds'] > 30 * 3600:
            result['issues'].append('backup_stale')
    except (OSError, ValueError, backup.BackupError):
        result['issues'].append('backup_metadata_unknown')
    return result
