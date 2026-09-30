#!/usr/bin/env python3
"""Host observer and local backup runner. Never deploys or writes marketplace data."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_backup_manager import atomic_json, locked, read_json
from scripts.verified_sqlite_backup import BackupError

STATE = Path.home() / '.local/share/seller-hub/local-operations'
CONTAINER = 'seller-platform'
PUBLIC_URL = 'https://seller-platform.tech/login'
CODES = {
    'container_unknown': 'не удалось проверить контейнер',
    'container_not_running': 'приложение остановлено',
    'container_unhealthy': 'контейнер не проходит проверку здоровья',
    'public_https_unhealthy': 'страница входа недоступна по HTTPS',
    'probe_unknown': 'внутреннее наблюдение недоступно',
    'scheduler_unhealthy': 'нет подтверждённого прогресса scheduler',
    'scheduler_unknown': 'состояние scheduler неизвестно',
    'disk_reserve_low': 'свободного места меньше 2 GiB',
    'backup_capacity_low': 'недостаточно места для следующей проверяемой копии',
    'database_observation_unknown': 'не удалось проверить размер и доступность БД',
    'backup_missing': 'нет принятой проверяемой копии БД',
    'backup_stale': 'последней копии БД больше 30 часов',
    'backup_metadata_unknown': 'не подтверждены metadata резервной копии',
    'backup_overdue': 'создание резервной копии длится больше 35 минут',
    'backup_last_run_failed': 'последний запуск резервного копирования не завершился успешно',
}


def command(args, *, source=None, timeout=20):
    """Bound output without ever printing subprocess errors, env or raw bodies."""
    with tempfile.TemporaryFile() as input_file:
        if source is not None:
            input_file.write(source.encode())
            input_file.seek(0)
        process = subprocess.Popen(args, stdin=input_file, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        collected = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ValueError('command_timeout')
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    collected.extend(chunk)
                    if len(collected) > 65536:
                        raise ValueError('command_output_limit')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('command_timeout')
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise ValueError('command_timeout') from None
            if process.returncode != 0:
                raise ValueError('command_failed')
            return json.loads(collected)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            process.stdout.close()


def bundle(action):
    # Reviewed host sources also work with the accepted production image;
    # no copy into runtime, image rebuild, app import or scheduler bootstrap.
    lines = ['import sys,types,json', "sys.path.insert(0, '/app')", 'import scripts']
    for name in ('verified_sqlite_backup', 'local_backup_manager', 'local_operations_probe'):
        path = ROOT / 'scripts' / (name + '.py')
        lines.extend([f"m=types.ModuleType('scripts.{name}')", f"m.__file__={str(path)!r}",
                      f"sys.modules['scripts.{name}']=m", f"setattr(scripts, {name!r}, m)",
                      f"exec(compile({path.read_text()!r}, {name!r}, 'exec'), m.__dict__)"])
    if action == 'probe':
        lines.append('print(json.dumps(m.probe()))')
    elif action == 'backup':
        # SIGALRM bounds the container process too: killing a Docker CLI alone
        # does not reliably terminate its exec process. Existing helper has its
        # own cooperative disk/deadline checks and flock remains the authority.
        lines.extend(['import signal', 'signal.alarm(1860)',
            'from scripts.local_backup_manager import create_managed',
            "r=create_managed('/app/data/seller_platform.db', '/app/data/backups/managed-daily')",
            'print(json.dumps(r))'])
    else:
        raise ValueError('invalid_action')
    return '\n'.join(lines)


def container_call(action):
    return command(['docker', 'exec', '-i', '--user', 'app', CONTAINER, 'python', '-'],
                   source=bundle(action), timeout=1900 if action == 'backup' else 15)


def maintain_photo_cache():
    source = """import json,shutil
from services.photo_cache import PhotoCacheManager,PHOTO_CACHE_DIR
cache=PhotoCacheManager()
cache.stop_workers()
before=shutil.disk_usage(PHOTO_CACHE_DIR).free
cache._run_cache_maintenance()
print(json.dumps({'free_bytes':shutil.disk_usage(PHOTO_CACHE_DIR).free,
                  'deleted_bytes':cache._stats['maintenance_deleted_bytes']}))
"""
    return command(['docker', 'exec', '-i', '--user', 'app',
                    '-e', 'PHOTO_CACHE_MAX_BYTES=1073741824',
                    '-e', 'PHOTO_CACHE_PRUNE_TO_BYTES=536870912',
                    CONTAINER, 'python', '-'], source=source, timeout=60)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_https():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(PUBLIC_URL, headers={'User-Agent': 'SellerHub-LocalObserver/1'}),
                     timeout=8) as response:
        body = response.read(32769)
        # A short bounded prefix must be recognisably our login form.
        return response.status == 200 and b'<form' in body and b'password' in body


def observe_host(now=None):
    now = time.time() if now is None else now
    result = {'version': 1, 'observed_epoch': now, 'issues': [], 'startup_grace': False}
    try:
        state = command(['docker', 'inspect', '--format', '{{json .State}}', CONTAINER], timeout=8)
        running = state.get('Running') is True
        if not running:
            result['issues'].append('container_not_running')
        else:
            started = datetime.fromisoformat(state['StartedAt'].replace('Z', '+00:00')).timestamp()
            age = now - started
            health = state.get('Health', {}).get('Status')
            result['startup_grace'] = health == 'starting' and 0 <= age < 900
            result['container_health'] = health if health in {'starting', 'healthy', 'unhealthy'} else 'unknown'
            if health != 'healthy' and not result['startup_grace']:
                result['issues'].append('container_unhealthy')
        result['running'] = running
    except (ValueError, OSError, KeyError, TypeError):
        result['issues'].append('container_unknown')
    try:
        if not public_https() and not result['startup_grace']:
            result['issues'].append('public_https_unhealthy')
    except (OSError, ValueError, urllib.error.URLError):
        if not result['startup_grace']:
            result['issues'].append('public_https_unhealthy')
    if result.get('running'):
        try:
            inner = container_call('probe')
            if not isinstance(inner, dict) or inner.get('version') != 1 or not isinstance(inner.get('issues'), list):
                raise ValueError('invalid_probe')
            if any(code not in CODES for code in inner['issues']):
                raise ValueError('invalid_issue')
            result['local'] = inner
            result['issues'].extend(code for code in inner['issues']
                if not (result['startup_grace'] and code in {'scheduler_unhealthy', 'scheduler_unknown'}))
        except (OSError, ValueError, TypeError):
            result['issues'].append('probe_unknown')
    return result


def transition(previous, observed, now):
    """Three consecutive bad samples; two fully healthy ones for recovery."""
    if previous is None:
        previous = {'version': 1, 'candidate': [], 'streak': 0, 'confirmed': [], 'last_epoch': now}
    if (not isinstance(previous, dict) or previous.get('version') != 1
            or any(not isinstance(previous.get(k), list) for k in ('candidate', 'confirmed'))
            or any(code not in CODES for k in ('candidate', 'confirmed') for code in previous[k])
            or type(previous.get('streak')) is not int or not 0 <= previous['streak'] <= 3
            or type(previous.get('last_epoch')) not in (int, float)):
        raise BackupError('invalid_observer_state')
    issues = sorted(set(observed['issues']))
    active_since = previous.get('backup_active_since')
    if observed.get('local', {}).get('backup_active'):
        if type(active_since) not in (int, float) or not 0 <= active_since <= now:
            active_since = now
        if now - active_since > 2100:
            issues = sorted(set([*issues, 'backup_overdue']))
    else:
        active_since = None
    consecutive = 0 <= now - previous['last_epoch'] <= 180
    streak = min(3, previous['streak'] + 1) if consecutive and issues == previous['candidate'] else 1
    confirmed, event = previous['confirmed'], None
    # A starting container is not a healthy recovery observation.
    if observed.get('startup_grace') and not issues:
        streak = 0
    if issues != confirmed and streak >= (3 if issues else 2):
        confirmed = issues
        event = 'incident' if issues else 'recovery'
    result = {'version': 1, 'candidate': issues, 'streak': streak, 'confirmed': confirmed,
              'last_epoch': now, 'backup_active_since': active_since,
              'notification': previous.get('notification')}
    return result, event


def notify(state_dir, event, codes):
    if event == 'incident':
        message = 'Seller Hub · контроль production\nТребует внимания: ' + '; '.join(CODES[c] for c in codes) + '.\nАвтоматический перезапуск и повтор API-записей не выполнялись.'
    else:
        message = 'Seller Hub · контроль production\nНаблюдавшаяся проблема устранена: HTTPS, контейнер, scheduler, доступность БД и локальные backup-проверки снова проходят. Это контроль с того же хоста.'
    path = state_dir / 'notification.txt'
    path.write_text(message, encoding='utf-8')
    os.chmod(path, 0o600)
    try:
        return command([str(ROOT / 'venv/bin/python'), str(ROOT / 'scripts/notify_task_status.py'),
                        '--message-file', str(path)], timeout=50)
    except (ValueError, OSError):
        return {'sent': False, 'delivery': 'unconfirmed_no_retry'}
    finally:
        path.unlink(missing_ok=True)


def observe_once(state_dir, *, send=True):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state_dir / '.observer.lock'):
        path = state_dir / 'observer.json'
        previous = read_json(path) if path.exists() else None
        observed = observe_host()
        receipt_path = state_dir / 'backup-run.json'
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if (receipt.get('status') == 'failed' or (receipt.get('status') == 'running'
                    and time.time() - receipt.get('started_epoch', 0) > 2100)):
                observed['issues'].append('backup_last_run_failed')
        state, event = transition(previous, observed, time.time())
        if event:
            # Durable reservation BEFORE external side effect. Unknown delivery
            # is visible, not retried automatically on restart or next timer tick.
            state['notification'] = {'event': event, 'status': 'reserved' if send else 'suppressed', 'epoch': time.time()}
        atomic_json(path, state)
        atomic_json(state_dir / 'observation.json', observed)
        if event and send:
            result = notify(state_dir, event, state['confirmed'])
            state['notification'] = {**state['notification'], 'status': 'delivered' if result.get('sent') else 'unconfirmed',
                                     'delivered': result.get('delivered'), 'recipients': result.get('recipients')}
            atomic_json(path, state)
        return {'status': 'attention' if observed['issues'] else ('starting' if observed['startup_grace'] else 'healthy'),
                'issues': sorted(set(observed['issues'])), 'event': event, 'notification': state['notification']}


def run_backup(state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state_dir / '.backup-run.lock'):
        path = state_dir / 'backup-run.json'
        started = time.time()
        atomic_json(path, {'status': 'running', 'started_epoch': started})
        try:
            cache = maintain_photo_cache()
            result = container_call('backup')
            if result.get('status') != 'complete' or result.get('restore_file_verified') is not True:
                raise ValueError('backup_unconfirmed')
            receipt = {'status': 'complete', 'started_epoch': started, 'completed_epoch': time.time(),
                       'snapshot_utc': result['snapshot_utc'], 'archive': result['archive'],
                       'compressed_bytes': result['compressed_bytes'], 'uncompressed_bytes': result['uncompressed_bytes'],
                       'managed_copies': result['managed_copies'], 'retired_owned_copies': result['retired_owned_copies'],
                       'round_trip_verified': True, 'cache_deleted_bytes': cache['deleted_bytes'],
                       'archive_sha256': result['archive_sha256'], 'sha256': result['sha256']}
        except (OSError, ValueError, KeyError, TypeError):
            receipt = {'status': 'failed', 'started_epoch': started, 'completed_epoch': time.time(),
                       'code': 'backup_not_confirmed_no_retry'}
        atomic_json(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['observe', 'backup', 'probe'])
    parser.add_argument('--state-dir', type=Path, default=STATE)
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args()
    try:
        if args.state_dir.is_symlink() or args.state_dir.absolute().resolve() != args.state_dir.absolute():
            raise ValueError('state_symlink')
        if args.action == 'probe':
            result = observe_host()
        elif args.action == 'backup':
            result = run_backup(args.state_dir)
        else:
            result = observe_once(args.state_dir, send=not args.no_notify)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('status') == 'failed' else 0
    except (OSError, ValueError, BackupError):
        print(json.dumps({'status': 'failed', 'code': 'local_observation_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
