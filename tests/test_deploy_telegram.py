"""Synthetic Telegram transport: subscribers, checkpoints and exactly one attempt."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
import subprocess
import os

from scripts.deploy_telegram import (KEYS, BotError, Store, Telegram, broadcast,
                                     configuration, poll_once)


@pytest.fixture
def config(tmp_path):
    return dict(zip(KEYS, ('12345:synthetic-token', '11', '', str(tmp_path/'private'))))


def update(uid, chat=22, text='/start', *, kind='private', sender=None):
    return {'update_id':uid,'message':{'chat':{'id':chat,'type':kind},
            'from':{'id':chat if sender is None else sender,'is_bot':False},'text':text}}


def fake_bot(config, *, response=None, error=None):
    bot = Mock(bot_id=config[KEYS[0]].split(':')[0])
    if error:
        bot.call.side_effect = error
    else:
        bot.call.return_value = response or {'message_id':100}
    return bot


def test_configuration_is_literal_and_environment_wins(tmp_path):
    file=tmp_path/'config'
    file.write_text('AUTODEPLOY_TG_BOT_TOKEN=123:file\nAUTODEPLOY_TG_CHAT_ID=11\nIGNORED=$(bad)\n')
    with patch.dict('os.environ', {'AUTODEPLOY_TG_BOT_TOKEN':'456:environment'}, clear=True):
        result=configuration(file)
    assert result[KEYS[0]] == '456:environment'
    assert result[KEYS[1]] == '11'
    file.write_text('AUTODEPLOY_TG_BOT_TOKEN=123:good\nAUTODEPLOY_TG_CHAT_ID=12 13\n')
    with patch.dict('os.environ',{},clear=True), pytest.raises(ValueError): configuration(file)


def test_start_stop_duplicate_and_restart_preserve_opt_out(config):
    store=Store(config)
    assert store.recipients()==['11']
    assert len(store.apply_updates([update(1)],'our_bot'))==1
    assert set(store.recipients())=={'11','22'}
    assert store.apply_updates([update(1)],'our_bot')==[]
    store.apply_updates([update(2,chat=11,text='/stop'),update(3,text='/stop')],'our_bot')
    assert Store(config).recipients()==[]
    assert Store(config).offset()==4
    store.apply_updates([update(4,text='/start welcome')],'our_bot')
    assert store.recipients()==['22']


def test_group_other_bot_forged_sender_and_arbitrary_text_do_not_subscribe(config):
    store=Store(config)
    updates=[update(1,text='hello'), update(2,kind='group'), update(3,text='/start@another_bot'),
             update(4,sender=99),update(5,text='/startled')]
    assert store.apply_updates(updates,'our_bot')==[]
    assert store.recipients()==['11']
    store.apply_updates([update(6,text='/start@OUR_BOT')],'our_bot')
    assert len(store.recipients())==2


def test_invalid_page_cannot_advance_offset_or_add_partially(config):
    store=Store(config)
    for updates in [[update(2),update(1)], [update(1),update(1)], [update(1),{'update_id':True}], {}]:
        with pytest.raises(BotError): store.apply_updates(updates,'our_bot')
    assert store.offset()==0 and store.recipients()==['11']


def test_bot_identity_isolated_but_key_rotation_keeps_subscribers(config):
    store=Store(config);store.apply_updates([update(1)],'our_bot')
    rotated=dict(config);rotated[KEYS[0]]='12345:rotated'
    assert Store(rotated).recipients()==store.recipients()
    changed=dict(config);changed[KEYS[0]]='99999:different';changed[KEYS[1]]=''
    assert Store(changed).recipients()==[]
    assert store.path.stat().st_mode & 0o777==0o600
    assert store.path.parent.stat().st_mode & 0o777==0o700


def test_single_receiver_lock_does_not_steal_live_consumer(config):
    store=Store(config)
    with store.lock('receiver',nonblocking=True):
        with pytest.raises(BotError,match='receiver_already_running'):
            with Store(config).lock('receiver',nonblocking=True): pass


def test_unknown_send_never_repeats_and_message_is_not_stored(config):
    store=Store(config);bot=fake_bot(config,error=BotError('transport_unconfirmed'))
    assert store.send_once(bot,'11','private message','event')=='unconfirmed'
    assert Store(config).send_once(bot,'11','private message','event')=='already_attempted'
    assert bot.call.call_count==1
    with store.db() as connection:
        row=dict(connection.execute('SELECT * FROM attempts').fetchone())
    assert row['status']=='unconfirmed' and 'private message' not in json.dumps(row)
    with pytest.raises(ValueError):store.send_once(bot,'11','changed','event')


def test_reserved_attempt_survives_process_exit_without_retry(config):
    store=Store(config);bot=fake_bot(config,error=SystemExit('synthetic crash'))
    with pytest.raises(SystemExit):store.send_once(bot,'11','status','event')
    bot.call.side_effect=None
    assert Store(config).send_once(bot,'11','status','event')=='already_attempted'
    assert bot.call.call_count==1


def test_broadcast_deduplicates_owner_and_includes_every_start(config):
    store=Store(config);store.apply_updates([update(1,chat=11),update(2,chat=22),update(3,chat=33)],'our_bot')
    bot=fake_bot(config)
    with patch('scripts.deploy_telegram.time.sleep'):
        result=broadcast(store,bot,'Milestone')
    assert result['sent'] and result['recipients']==result['delivered']==3
    assert {call.args[1]['chat_id'] for call in bot.call.call_args_list}=={'11','22','33'}
    assert all(call.args[1]['allow_paid_broadcast'] is False for call in bot.call.call_args_list)


def test_403_unsubscribes_until_new_explicit_start(config):
    store=Store(config);bot=fake_bot(config,error=BotError(403))
    assert store.send_once(bot,'11','status','one')=='rejected'
    assert Store(config).recipients()==[]
    assert store.send_once(bot,'11','status','two')=='unsubscribed'
    store.apply_updates([update(1,chat=11)],'our_bot')
    assert store.recipients()==['11']


def test_429_preserves_cooldown_for_other_recipients_and_processes(config):
    store=Store(config);store.apply_updates([update(1)],'our_bot')
    bot=fake_bot(config,error=BotError(429,3600))
    result=broadcast(store,bot,'status')
    assert result['rejected']==1 and result['deferred']==1 and not result['sent']
    assert Store(config).send_once(bot,'22','later status','another')=='deferred'
    assert bot.call.call_count==1


def test_stop_during_rate_wait_prevents_send(config):
    store=Store(config)
    with store.db() as connection:store._set(connection,'next_send_at',__import__('time').time()+1)
    with patch('scripts.deploy_telegram.time.sleep',side_effect=lambda _:store.apply_updates([update(1,chat=11,text='/stop')],'our_bot')):
        bot=fake_bot(config)
        assert store.send_once(bot,'11','status','event')=='unsubscribed'
    bot.call.assert_not_called()


def test_block_event_disables_known_chat_and_unblock_does_not_opt_in(config):
    store=Store(config)
    def membership(uid,status):return {'update_id':uid,'my_chat_member':{'chat':{'id':11},'new_chat_member':{'status':status}}}
    store.apply_updates([membership(1,'kicked'),membership(2,'member')],'our_bot')
    assert store.recipients()==[]


def test_poll_commits_subscription_and_offset_before_reply_then_acks(config):
    store=Store(config);bot=Mock();calls=[]
    def call(method,payload,**kw):
        calls.append((method,payload))
        if method=='getUpdates':
            return [update(8)] if payload['offset']==0 else []
        assert store.offset()==9 and '22' in store.recipients()
        return {'message_id':1}
    bot.call.side_effect=call
    assert poll_once(store,bot,'our_bot',timeout=0)['commands']==1
    assert poll_once(store,bot,'our_bot',timeout=0)['commands']==0
    assert calls[-1][1]['offset']==9


def test_transport_has_no_retry_redirect_or_sensitive_error(config):
    session=Mock(proxies={})
    session.post.side_effect=requests.ConnectionError('https://secret-token/private-proxy')
    bot=Telegram(config,session)
    with pytest.raises(BotError) as error:bot.call('sendMessage',{})
    assert str(error.value)=='transport_unconfirmed' and session.post.call_count==1
    assert session.post.call_args.kwargs['allow_redirects'] is False
    assert session.post.call_args.kwargs['timeout']==(5,15)


def test_existing_webhook_stops_receiver_without_changing_it(config):
    session=Mock(proxies={})
    session.post.side_effect=[SimpleNamespace(status_code=200,json=lambda:{'ok':True,'result':{'id':12345,'is_bot':True,'username':'our_bot'}}),
                             SimpleNamespace(status_code=200,json=lambda:{'ok':True,'result':{'url':'https://existing.example/webhook'}})]
    bot=Telegram(config,session)
    with pytest.raises(BotError,match='webhook_already_configured'):bot.preflight()
    assert session.post.call_count==2
    assert not any('deleteWebhook' in call.args[0] for call in session.post.call_args_list)


def test_empty_registry_is_not_reported_as_delivery(config):
    config[KEYS[1]]='';store=Store(config);bot=fake_bot(config)
    assert broadcast(store,bot,'message')['sent'] is False
    bot.call.assert_not_called()


def test_autodeploy_uses_shared_sender_without_evaluating_credentials():
    text=(Path(__file__).parents[1]/'scripts/autodeploy.sh').read_text()
    function=text[text.index('tg_send()'):text.index('\ndeploy()')]
    assert 'notify_task_status.py' in function and '--parse-mode HTML' in function
    assert 'curl' not in function and 'source "$PROJECT_DIR/.env.autodeploy"' not in text


def run_autodeploy_fixture(tmp_path, *, local='a'*40, remote='b'*40,
                           remote_is_descendant=True, merge_advances=True,
                           worktree='', status_failure=False):
    bindir=tmp_path/'bin';bindir.mkdir()
    state_file=tmp_path/'git-state.json'
    state_file.write_text(json.dumps({'head':local,'remote':remote,
        'remote_is_descendant':remote_is_descendant,'worktree':worktree,
        'status_failure':status_failure,'merge_advances':merge_advances}))
    git_calls=tmp_path/'git-calls.jsonl';docker_calls=tmp_path/'docker-calls.jsonl'
    git=bindir/'git'
    git.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
args=sys.argv[1:]
base=Path(__file__).parent.parent
state_file=base/'git-state.json'
state=json.loads(state_file.read_text())
with (base/'git-calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='rev-parse':
    if '--abbrev-ref' in args: print('main')
    elif args[-1]=='HEAD': print(state['head'])
    elif args[-1]=='origin/main': print(state['remote'])
    else: raise SystemExit(96)
elif args[0]=='fetch': pass
elif args[0]=='merge-base':
    ok=(args[1]=='--is-ancestor' and args[2]==state['head'] and
        args[3]==state['remote'] and state['remote_is_descendant'])
    raise SystemExit(0 if ok else 1)
elif args[0]=='status':
    if state['status_failure']: raise SystemExit(1)
    print(state['worktree'])
elif args[0]=='merge':
    if (args==['merge','--ff-only',state['remote']] and
        state['remote_is_descendant'] and state['head']!=state['remote']):
        if state['merge_advances']:
            state['head']=state['remote']
            state_file.write_text(json.dumps(state))
    else: raise SystemExit(1)
elif args[0]=='log':
    if '--oneline' in args: print('1234567 synthetic change')
    elif '--format=%an' in args: print('Synthetic Author')
    elif '--format=%s' in args: print('Synthetic change')
    else: raise SystemExit(95)
elif args[0]=='diff': pass
else: raise SystemExit(97)
''')
    git.chmod(0o700)
    docker=bindir/'docker'
    docker.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
args=sys.argv[1:]
with (Path(__file__).parent.parent/'docker-calls.jsonl').open('a') as f:
    f.write(json.dumps(args)+'\\n')
if args and args[0]=='inspect': print('healthy')
''')
    docker.chmod(0o700)
    sleep=bindir/'sleep';sleep.write_text('#!/bin/sh\nexit 0\n');sleep.chmod(0o700)
    fake_python=tmp_path/'venv'/'bin'/'python';fake_python.parent.mkdir(parents=True)
    python_calls=tmp_path/'python-calls.jsonl'
    fake_python.write_text(f'''#!/usr/bin/env python3
import json,sys
from pathlib import Path
with Path({str(python_calls)!r}).open('a') as f:
    f.write(json.dumps(sys.argv[1:])+'\\n')
''')
    fake_python.chmod(0o700)
    script=Path(__file__).parents[1]/'scripts/autodeploy.sh'
    result=subprocess.run(['bash',str(script),'--once','--project-dir',str(tmp_path)],
        env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH']},
        capture_output=True,text=True,timeout=5)
    read_calls=lambda path:[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    state=json.loads(state_file.read_text())
    state['python_calls']=read_calls(python_calls)
    return result,read_calls(git_calls),read_calls(docker_calls),state


def test_autodeploy_does_not_build_when_local_branch_is_ahead(tmp_path):
    result,git_calls,docker_calls,_=run_autodeploy_fixture(
        tmp_path,local='c'*40,remote='a'*40,remote_is_descendant=False)
    assert result.returncode==0
    assert 'not ahead of local HEAD' in result.stdout
    assert not any(args[0]=='merge' for args in git_calls)
    assert not any(args[0]=='pull' for args in git_calls)
    assert docker_calls==[]


def test_autodeploy_builds_once_after_remote_fast_forward(tmp_path):
    old_hash='a'*40;new_hash='b'*40
    result,git_calls,docker_calls,state=run_autodeploy_fixture(
        tmp_path,local=old_hash,remote=new_hash)
    assert result.returncode==0
    assert state['head']==new_hash
    assert ['merge','--ff-only',new_hash] in git_calls
    assert not any(args[0]=='pull' for args in git_calls)
    guarded=[args for args in state['python_calls'] if args and args[0].endswith('/deploy_safety.py')]
    assert len(guarded)==1
    assert guarded[0][1:]==['--project-dir',str(tmp_path)]
    assert not any(args[:3]==['compose','build','seller-platform'] for args in docker_calls)
    assert f"New commits detected on 'main': {old_hash[:7]} -> {new_hash[:7]}" in result.stdout


def test_autodeploy_skips_build_when_fast_forward_did_not_move_head(tmp_path):
    old_hash='a'*40
    result,git_calls,docker_calls,state=run_autodeploy_fixture(
        tmp_path,local=old_hash,remote='b'*40,merge_advances=False)
    assert result.returncode==0
    assert state['head']==old_hash
    assert 'did not advance HEAD' in result.stdout
    assert ['merge','--ff-only','b'*40] in git_calls
    assert docker_calls==[]


@pytest.mark.parametrize(('worktree','status_failure','message'),[
    ('?? local-change.py',False,'Working tree has local changes'),
    ('',True,'Working tree inspection failed'),
])
def test_watcher_cannot_merge_or_deploy_dirty_or_uninspectable_worktree(
        tmp_path,worktree,status_failure,message):
    result,git_calls,docker_calls,_=run_autodeploy_fixture(
        tmp_path,worktree=worktree,status_failure=status_failure)
    assert result.returncode==0 and message in result.stdout
    assert any(args[0]=='status' for args in git_calls)
    assert not any(args[0]=='merge' for args in git_calls)
    assert not any(args[0]=='pull' for args in git_calls)
    assert docker_calls==[]
