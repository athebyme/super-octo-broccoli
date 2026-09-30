"""Private deployment-bot subscriptions and single-attempt broadcast transport.

Standalone: no Flask, production database, scheduler or deployment imports.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import sqlite3
import time
import uuid

import requests

KEYS = ('AUTODEPLOY_TG_BOT_TOKEN', 'AUTODEPLOY_TG_CHAT_ID', 'AUTODEPLOY_TG_PROXY',
        'AUTODEPLOY_TG_STATE_DIR')
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / '.env.autodeploy'


class BotError(Exception):
    """Only fixed codes; requests exceptions contain secret-bearing URLs."""
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(str(code))


def configuration(path=DEFAULT_CONFIG):
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[7:].strip()
            key, separator, value = line.partition('=')
            if separator and key.strip() in KEYS:
                parts = shlex.split(value, comments=True)
                if len(parts) > 1:
                    raise ValueError('invalid configuration')
                values[key.strip()] = parts[0] if parts else ''
    for key in KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
        values.setdefault(key, '')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', values[KEYS[0]]):
        raise ValueError('missing bot token')
    if values[KEYS[1]] and not re.fullmatch(r'-?[1-9]\d*', values[KEYS[1]]):
        raise ValueError('invalid configured chat')
    state_dir = values[KEYS[3]] or str(Path.home() / '.local/share/seller-hub/deploy-telegram')
    if not Path(state_dir).is_absolute():
        raise ValueError('state directory must be absolute')
    values[KEYS[3]] = state_dir
    return values


class Telegram:
    def __init__(self, config, session=None):
        self.bot_id = config[KEYS[0]].split(':', 1)[0]
        self.base = 'https://api.telegram.org/bot' + config[KEYS[0]] + '/'
        self.session = session or requests.Session()
        self.session.trust_env = False
        if config[KEYS[2]]:
            self.session.proxies.update({'http': config[KEYS[2]], 'https': config[KEYS[2]]})

    def call(self, method, payload, *, poll=False):
        if method not in {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage'}:
            raise ValueError('unsupported bot method')
        try:
            response = self.session.post(self.base + method, json=payload,
                timeout=(5, 30 if poll else 15), allow_redirects=False)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise BotError('transport_unconfirmed') from None
        if not isinstance(data, dict):
            raise BotError('invalid_response')
        if response.status_code == 200 and data.get('ok') is True and 'result' in data:
            return data['result']
        code = data.get('error_code', response.status_code)
        if type(code) is not int or not 100 <= code <= 599:
            code = 'provider_rejected'
        params = data.get('parameters')
        delay = params.get('retry_after', 0) if isinstance(params, dict) else 0
        if type(delay) is not int or delay < 0:
            delay = 0
        raise BotError(code, delay)

    def preflight(self):
        identity = self.call('getMe', {})
        if (not isinstance(identity, dict) or str(identity.get('id')) != self.bot_id
                or not identity.get('is_bot') or not isinstance(identity.get('username'), str)):
            raise BotError('invalid_bot_identity')
        webhook = self.call('getWebhookInfo', {})
        if not isinstance(webhook, dict) or 'url' not in webhook:
            raise BotError('invalid_webhook_response')
        if webhook['url']:
            raise BotError('webhook_already_configured')
        return identity['username']


class Store:
    def __init__(self, config):
        directory = Path(config[KEYS[3]])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.bot_id = config[KEYS[0]].split(':', 1)[0]
        self.path = directory / ('bot-' + self.bot_id + '.sqlite')
        self.lock_prefix = directory / ('bot-' + self.bot_id)
        # Precreate with private mode; sqlite journal inherits the DB mode.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id TEXT PRIMARY KEY, active INTEGER NOT NULL,
                    source TEXT NOT NULL, updated_at REAL NOT NULL,
                    last_update_id INTEGER, next_send_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts (
                    event_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_hash TEXT NOT NULL,
                    status TEXT NOT NULL, code TEXT, created_at REAL NOT NULL,
                    PRIMARY KEY(event_id, chat_id));
            ''')
            owner = config[KEYS[1]]
            if owner:
                connection.execute('INSERT OR IGNORE INTO subscribers(chat_id,active,source,updated_at) VALUES(?,1,?,?)',
                                   (owner, 'configured', time.time()))
            # Bounded retention of delivery metadata, never subscriptions/checkpoint.
            connection.execute('DELETE FROM attempts WHERE rowid IN (SELECT rowid FROM attempts WHERE created_at<? LIMIT 1000)',
                               (time.time()-30*86400,))

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lock(self, name, *, nonblocking=False):
        path = str(self.lock_prefix) + '-' + name + '.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise BotError('receiver_already_running') from None
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _meta(connection, key, default='0'):
        row = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    @staticmethod
    def _set(connection, key, value):
        connection.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key, str(value)))

    def offset(self):
        with self.db() as connection:
            return int(self._meta(connection, 'offset'))

    def recipients(self):
        with self.db() as connection:
            return [row['chat_id'] for row in connection.execute('SELECT chat_id FROM subscribers WHERE active=1 ORDER BY chat_id')]

    def apply_updates(self, updates, username):
        if not isinstance(updates, list) or len(updates) > 100:
            raise BotError('invalid_updates')
        ids = [update.get('update_id') if isinstance(update, dict) else None for update in updates]
        if any(type(value) is not int or value < 0 for value in ids) or ids != sorted(set(ids)):
            raise BotError('invalid_updates')
        replies = []
        with self.db() as connection:
            offset = int(self._meta(connection, 'offset'))
            for update in updates:
                uid = update['update_id']
                if uid < offset:
                    continue
                message = update.get('message')
                if isinstance(message, dict):
                    chat = message.get('chat', {})
                    sender = message.get('from', {})
                    text = message.get('text', '')
                    if (isinstance(chat, dict) and chat.get('type') == 'private'
                            and type(chat.get('id')) is int and chat['id'] > 0
                            and isinstance(sender, dict) and type(sender.get('id')) is int and sender['id'] == chat['id']
                            and sender.get('is_bot') is False and isinstance(text, str)):
                        command = text.split(maxsplit=1)[0] if text.split() else ''
                        name, at, target = command.partition('@')
                        if (not at or target.casefold() == username.casefold()) and name in {'/start','/stop','/help'}:
                            chat_id = str(chat['id'])
                            if name != '/help':
                                connection.execute('''INSERT INTO subscribers(chat_id,active,source,updated_at,last_update_id)
                                    VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET active=excluded.active,
                                    source=excluded.source,updated_at=excluded.updated_at,last_update_id=excluded.last_update_id''',
                                    (chat_id, int(name == '/start'), 'start', time.time(), uid))
                            replies.append((uid, chat_id, name))
                membership = update.get('my_chat_member')
                if (isinstance(membership, dict) and isinstance(membership.get('new_chat_member'),dict)
                        and isinstance(membership.get('chat'),dict)
                        and membership['new_chat_member'].get('status') in {'kicked','left'}):
                    chat_id = membership['chat'].get('id')
                    if type(chat_id) is int:
                        connection.execute('UPDATE subscribers SET active=0,updated_at=?,last_update_id=? WHERE chat_id=?',
                                           (time.time(), uid, str(chat_id)))
                offset = uid + 1
            if updates:
                self._set(connection, 'offset', offset)
        return replies

    def send_once(self, telegram, chat_id, message, event_id, *, parse_mode=None, require_active=True):
        """Reserve before HTTP; crash/timeout never authorizes another physical send."""
        digest = hashlib.sha256(message.encode()).hexdigest()
        with self.lock('send'):
            with self.db() as connection:
                prior = connection.execute('SELECT message_hash,status FROM attempts WHERE event_id=? AND chat_id=?',
                                           (event_id, chat_id)).fetchone()
                if prior:
                    if prior['message_hash'] != digest:
                        raise ValueError('event message changed')
                    return 'already_attempted'
                row = connection.execute('SELECT active,next_send_at FROM subscribers WHERE chat_id=?', (chat_id,)).fetchone()
                if require_active and (not row or not row['active']):
                    return 'unsubscribed'
                last_attempt = connection.execute('SELECT max(created_at) FROM attempts WHERE chat_id=?', (chat_id,)).fetchone()[0]
                due = max(float(self._meta(connection, 'cooldown')), float(self._meta(connection, 'next_send_at')),
                          row['next_send_at'] if row else 0, last_attempt+1 if last_attempt else 0)
            delay = due - time.time()
            if delay > 3:
                return 'deferred'
            if delay > 0:
                time.sleep(delay)
            with self.db() as connection:
                # /stop may have committed while we waited for the rate budget.
                if require_active:
                    row = connection.execute('SELECT active FROM subscribers WHERE chat_id=?', (chat_id,)).fetchone()
                    if not row or not row['active']:
                        return 'unsubscribed'
                now = time.time()
                connection.execute('INSERT INTO attempts(event_id,chat_id,message_hash,status,created_at) VALUES(?,?,?,?,?)',
                                   (event_id, chat_id, digest, 'reserved', now))
                self._set(connection, 'next_send_at', now + .2)
                connection.execute('UPDATE subscribers SET next_send_at=? WHERE chat_id=?', (now+1, chat_id))
            payload = {'chat_id':chat_id,'text':message,'disable_web_page_preview':True,'allow_paid_broadcast':False}
            if parse_mode:
                payload['parse_mode'] = parse_mode
            status, code = 'sent', None
            try:
                result = telegram.call('sendMessage', payload)
                if not isinstance(result, dict) or type(result.get('message_id')) is not int:
                    raise BotError('invalid_send_response')
            except BotError as error:
                code = str(error.code)
                status = 'rejected' if type(error.code) is int else 'unconfirmed'
                with self.db() as connection:
                    if error.code == 429:
                        self._set(connection, 'cooldown', max(float(self._meta(connection,'cooldown')), time.time()+max(1,error.retry_after)))
                    elif error.code == 401:
                        self._set(connection, 'cooldown', time.time()+60)
                    elif error.code == 403:
                        connection.execute('UPDATE subscribers SET active=0,updated_at=? WHERE chat_id=?', (time.time(),chat_id))
            with self.db() as connection:
                connection.execute('UPDATE attempts SET status=?,code=? WHERE event_id=? AND chat_id=?',
                                   (status, code, event_id, chat_id))
            return status


def broadcast(store, telegram, message, *, parse_mode=None):
    event_id = 'status-' + uuid.uuid4().hex
    recipients = store.recipients()
    counts = {key:0 for key in ('sent','rejected','unconfirmed','deferred','unsubscribed','already_attempted')}
    for chat_id in recipients:
        result = store.send_once(telegram, chat_id, message, event_id, parse_mode=parse_mode)
        counts[result] += 1
    return {'sent':bool(recipients) and counts['sent']==len(recipients),
            'recipients':len(recipients),'delivered':counts.pop('sent'),**counts}


COMMAND_REPLIES = {
    '/start': 'Вы подписаны на новости разработки и деплои Seller Hub. Буду присылать редкие важные обновления. Отписаться — /stop.',
    '/stop': 'Рассылка отключена. Чтобы снова получать обновления, отправьте /start.',
    '/help': 'Это бот обновлений Seller Hub. /start — подписаться на новости разработки и деплои, /stop — отписаться.',
}


def poll_once(store, telegram, username, *, timeout=20):
    result = telegram.call('getUpdates', {'offset':store.offset(),'limit':100,'timeout':timeout,
                                         'allowed_updates':['message','my_chat_member']}, poll=bool(timeout))
    replies = store.apply_updates(result, username)
    outcomes = []
    for update_id, chat_id, command in replies:
        outcomes.append(store.send_once(telegram, chat_id, COMMAND_REPLIES[command],
                                       'command-'+str(update_id), require_active=False))
    return {'updates':len(result),'commands':len(replies),'replies_confirmed':outcomes.count('sent'),
            'replies_unconfirmed':len(outcomes)-outcomes.count('sent'),'subscribers':len(store.recipients())}
