"""Local, bounded evidence of a live scheduler owner and executor progress.

A recent file or live PID alone is never a healthy signal. Observers do not
create files, elect a scheduler, release another owner's lock or run jobs.
"""
import json
import os
from pathlib import Path
import stat
import tempfile
import time

INTERVAL_SECONDS = 30
STALE_SECONDS = 90
MAX_BYTES = 2048


def lock_path():
    return Path(os.environ.get('SCHEDULER_LOCK_FILE', '/tmp/seller-platform-scheduler.lock'))


def _birth(pid):
    if type(pid) is not int or not 1 <= pid <= 2**31:
        return None
    try:
        with open(f'/proc/{pid}/stat', 'rb') as handle:
            raw = handle.read(4096)
        value = raw.rsplit(b')', 1)[1].split()[19].decode('ascii')
        return value if value.isdecimal() else None
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def publish(handle, *, path=None, now=None):
    """Called only by the elected owner, with its existing flock handle."""
    path = Path(path) if path is not None else lock_path()
    if handle is None or handle.closed:
        return False
    source, target = os.fstat(handle.fileno()), path.stat(follow_symlinks=False)
    if not stat.S_ISREG(target.st_mode) or (source.st_dev, source.st_ino) != (target.st_dev, target.st_ino):
        return False
    pid, birth = os.getpid(), _birth(os.getpid())
    if birth is None:
        return False
    payload = json.dumps({'version':1, 'pid':pid, 'birth':birth, 'device':source.st_dev,
                          'inode':source.st_ino, 'observed_at':time.time() if now is None else now}).encode('ascii')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-heartbeat-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
        os.replace(temporary, str(path)+'.heartbeat.json')
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return True


def _open_regular(path, cap):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap or info.st_uid != os.geteuid():
            raise ValueError('Unexpected scheduler evidence file')
        return os.fdopen(fd, 'rb'), info
    except BaseException:
        os.close(fd)
        raise


def observe(*, path=None, now=None, disabled=None):
    """Public result contains no PID, process identity, filesystem path or raw error."""
    if disabled is None:
        disabled = os.environ.get('SKIP_SCHEDULER') == '1'
    if disabled:
        return {'state':'disabled', 'age_seconds':None, 'checked_at':time.time() if now is None else now}
    path = Path(path) if path is not None else lock_path()
    now = time.time() if now is None else now
    result = {'state':'unknown', 'age_seconds':None, 'checked_at':now}
    try:
        import fcntl
        owner, info = _open_regular(path, 32)
        with owner:
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
                return {**result, 'state':'stopped'}
            pid_text = owner.read(32).strip()
            if not pid_text.isdigit():
                return result
            pid = int(pid_text)
            heartbeat, _ = _open_regular(str(path)+'.heartbeat.json', MAX_BYTES)
            with heartbeat:
                evidence = json.loads(heartbeat.read(MAX_BYTES+1))
            birth = _birth(pid)
            if (not birth or not isinstance(evidence, dict) or type(evidence.get('version')) is not int or evidence['version'] != 1
                    or type(evidence.get('pid')) is not int or evidence['pid'] != pid
                    or evidence.get('birth') != birth
                    or evidence.get('device') != info.st_dev or evidence.get('inode') != info.st_ino):
                return result
            stamp = evidence.get('observed_at')
            if type(stamp) not in (int,float) or not 0 <= stamp <= now+5:
                return result
            age = max(0, now-stamp)
            # Confirm the same lock pathname and owner after reading the heartbeat.
            current = path.stat(follow_symlinks=False)
            owner.seek(0)
            if (current.st_dev,current.st_ino) != (info.st_dev,info.st_ino) or owner.read(32).strip() != pid_text:
                return result
            return {**result, 'state':'healthy' if age <= STALE_SECONDS else 'stale', 'age_seconds':int(age)}
    except (OSError, ValueError, TypeError, ImportError):
        return result
