import fcntl
import json
import os
from pathlib import Path

import pytest

from services import scheduler_heartbeat as beat


@pytest.fixture
def owner(tmp_path):
    path=tmp_path/'scheduler.lock'
    with path.open('w+') as handle:
        handle.write(str(os.getpid()));handle.flush()
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert beat.publish(handle,path=path,now=1000)
        yield path,handle


def test_requires_both_live_exclusive_owner_and_recent_progress(owner):
    path,handle=owner
    assert beat.observe(path=path,now=1001,disabled=False)['state']=='healthy'
    assert beat.observe(path=path,now=1091,disabled=False)['state']=='stale'
    assert beat.observe(path=path,now=1091,disabled=False)['age_seconds']==91
    fcntl.flock(handle,fcntl.LOCK_UN)
    assert beat.observe(path=path,now=1001,disabled=False)['state']=='stopped'
    assert path.read_text()==str(os.getpid())


@pytest.mark.parametrize('field,value', [('pid',999999999),('birth','0'),('version',True),('inode',0),('observed_at',True),('observed_at',1010),('observed_at',float('nan'))])
def test_reused_pid_changed_lock_and_malformed_stamp_are_not_healthy(owner,field,value):
    path,_=owner;file=Path(str(path)+'.heartbeat.json');data=json.loads(file.read_text());data[field]=value;file.write_text(json.dumps(data))
    result=beat.observe(path=path,now=1001,disabled=False)
    assert result['state']=='unknown'
    assert not {'pid','birth','device','inode','path'} & result.keys()


def test_missing_evidence_is_unknown_and_observer_never_creates_it(tmp_path, owner):
    missing=tmp_path/'missing.lock';assert beat.observe(path=missing,disabled=False)['state']=='unknown';assert not missing.exists()
    path,_=owner;Path(str(path)+'.heartbeat.json').unlink()
    assert beat.observe(path=path,disabled=False)['state']=='unknown'
    assert not Path(str(path)+'.heartbeat.json').exists()
    assert beat.observe(path=path,disabled=True)['state']=='disabled'


def test_replaced_lock_path_and_symlink_cannot_bless_previous_owner(owner,tmp_path):
    path,handle=owner
    path.rename(tmp_path/'old.lock');path.write_text(str(os.getpid()))
    assert not beat.publish(handle,path=path,now=1001)
    assert beat.observe(path=path,now=1001,disabled=False)['state']=='stopped'
    path.unlink();path.symlink_to(tmp_path/'old.lock')
    assert beat.observe(path=path,now=1001,disabled=False)['state']=='unknown'


def test_atomic_heartbeat_is_private_and_oversized_file_is_rejected(owner):
    path,handle=owner
    target=Path(str(path)+'.heartbeat.json')
    assert target.stat().st_mode & 0o777==0o600
    target.write_bytes(b'0'*(beat.MAX_BYTES+1))
    assert beat.observe(path=path,now=1001,disabled=False)['state']=='unknown'
    assert beat.publish(handle,path=path,now=1001)
    assert beat.observe(path=path,now=1002,disabled=False)['state']=='healthy'
    assert not list(path.parent.glob('.scheduler.lock-heartbeat-*'))


def test_scheduler_callback_uses_only_elected_running_owner(owner,monkeypatch):
    from types import SimpleNamespace
    from services import product_sync_scheduler as scheduler
    path,handle=owner
    monkeypatch.setenv('SCHEDULER_LOCK_FILE',str(path))
    monkeypatch.setattr(scheduler,'scheduler',SimpleNamespace(running=False))
    monkeypatch.setattr(scheduler,'_scheduler_lock_handle',handle)
    before=Path(str(path)+'.heartbeat.json').read_bytes();scheduler.publish_scheduler_heartbeat()
    assert Path(str(path)+'.heartbeat.json').read_bytes()==before
    scheduler.scheduler.running=True;scheduler.publish_scheduler_heartbeat()
    assert beat.observe(path=path,disabled=False)['state']=='healthy'
