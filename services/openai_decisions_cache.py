"""Private SQLite result cache and one-attempt reservation for Decisions.

The caller supplies an absolute private database path and exact per-question
fingerprints. This ledger stores only fingerprints, normalized answers,
attempt counters, leases, and bounded cooldown metadata; it never stores input
facts, API keys, raw provider bodies, or exception text.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from typing import Optional, Sequence, Tuple

from services.openai_decisions import (
    MAX_RETRY_AFTER_SECONDS,
    DecisionAnswer,
    DecisionChoice,
    DecisionOutcome,
    DecisionQuestion,
)


MAX_BATCH_QUESTIONS = 32
MAX_DAILY_CALL_LIMIT = 1000
MAX_CACHE_ENTRIES = 100_000
MAX_LEASE_SECONDS = 300
MAX_BUSY_TIMEOUT_MS = 1000
ATTEMPT_RETENTION_DAYS = 30
MISSING_RETRY_AFTER_COOLDOWN_SECONDS = 5 * 60
AUTH_FAILURE_COOLDOWN_SECONDS = 60 * 60
HEX_FINGERPRINT_RE = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class DecisionWorkItem:
    fingerprint: str
    question: DecisionQuestion


@dataclass(frozen=True)
class DecisionBlockedItem:
    fingerprint: str
    state: str
    retry_at: Optional[datetime] = None


@dataclass(frozen=True)
class DecisionBatchReservation:
    kind: str
    requested: Tuple[DecisionWorkItem, ...] = ()
    token: Optional[str] = None
    claimed: Tuple[DecisionWorkItem, ...] = ()
    cached_answers: Tuple[Tuple[str, DecisionAnswer], ...] = ()
    blocked: Tuple[DecisionBlockedItem, ...] = ()
    retry_at: Optional[datetime] = None
    safe_code: Optional[str] = None
    _previous: Tuple[Tuple[str, Optional[str], Optional[datetime], int], ...] = ()


@dataclass(frozen=True)
class DecisionBatchFinish:
    kind: str
    retry_at: Optional[datetime] = None
    safe_code: Optional[str] = None


class _PhysicalCallLock:
    """Exclusive OS lock on the private SQLite carrier file."""

    def __init__(self, descriptor: int):
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if type(value) is not datetime:
        raise ValueError('invalid_time')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _parse_stamp(value: Optional[str]) -> Optional[datetime]:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return _utc(parsed)


def _option_values(question: DecisionQuestion) -> Tuple[object, ...]:
    values = []
    for option in question.choices:
        value = option.value if type(option) is DecisionChoice else option
        values.append(value)
    return tuple(values)


def _same_typed_value(left, right) -> bool:
    return type(left) is type(right) and left == right


def _valid_question_items(items: Sequence[DecisionWorkItem]) -> bool:
    if type(items) not in (list, tuple) or not 1 <= len(items) <= MAX_BATCH_QUESTIONS:
        return False
    fingerprints = set()
    names = set()
    for item in items:
        if type(item) is not DecisionWorkItem or type(item.fingerprint) is not str:
            return False
        if HEX_FINGERPRINT_RE.fullmatch(item.fingerprint) is None or item.fingerprint in fingerprints:
            return False
        fingerprints.add(item.fingerprint)
        question = item.question
        if type(question) is not DecisionQuestion or type(question.name) is not str:
            return False
        if not question.name.strip() or question.name in names:
            return False
        names.add(question.name)
        if type(question.instructions) is not str or not question.instructions.strip():
            return False
        if type(question.choices) not in (tuple, list) or not 2 <= len(question.choices) <= 64:
            return False
        seen = set()
        for option in question.choices:
            value = option.value if type(option) is DecisionChoice else option
            description = option.description if type(option) is DecisionChoice else None
            if type(value) not in (str, bool):
                return False
            if type(value) is str:
                try:
                    if len(value.encode('utf-8')) > 512:
                        return False
                except UnicodeError:
                    return False
            key = (type(value), value)
            if key in seen:
                return False
            seen.add(key)
            if description is not None and type(description) is not str:
                return False
    return True


def _valid_answer(answer: DecisionAnswer, item: DecisionWorkItem) -> bool:
    if type(answer) is not DecisionAnswer or answer.name != item.question.name:
        return False
    if answer.status == 'refused':
        return answer.choice is None and answer.confidence is None
    if answer.status != 'answered':
        return False
    if type(answer.choice) not in (str, bool) or not any(
        _same_typed_value(answer.choice, value) for value in _option_values(item.question)
    ):
        return False
    confidence = answer.confidence
    if type(confidence) not in (int, float):
        return False
    try:
        confidence = float(confidence)
    except (ValueError, OverflowError):
        return False
    return math.isfinite(confidence) and 0 <= confidence <= 1


def _answer_json(answer: DecisionAnswer) -> str:
    confidence = float(answer.confidence) if answer.confidence is not None else None
    return json.dumps({
        'name': answer.name,
        'status': answer.status,
        'choice': answer.choice,
        'confidence': confidence,
    }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _decode_answer(raw: Optional[str], item: DecisionWorkItem) -> Optional[DecisionAnswer]:
    if type(raw) is not str:
        return None
    try:
        value = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, RecursionError):
        return None
    if type(value) is not dict:
        return None
    answer = DecisionAnswer(
        name=value.get('name'), status=value.get('status'),
        choice=value.get('choice'), confidence=value.get('confidence'),
    )
    return answer if _valid_answer(answer, item) else None


class OpenAIDecisionsCache:
    """Bounded one-global-call ledger and durable per-question result cache."""

    def __init__(
        self,
        path: str,
        *,
        max_calls_per_day: int,
        lease_seconds: int = 120,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        busy_timeout_ms: int = 50,
    ):
        self._path = path
        self._max_calls_per_day = max_calls_per_day
        self._lease_seconds = lease_seconds
        self._max_cache_entries = max_cache_entries
        self._busy_timeout_ms = busy_timeout_ms
        self._configuration_valid = self._valid_configuration()

    def _valid_configuration(self) -> bool:
        if type(self._path) is not str or not self._path or not os.path.isabs(self._path):
            return False
        if self._path == ':memory:' or not os.path.basename(self._path):
            return False
        if (type(self._max_calls_per_day) is not int
                or not 1 <= self._max_calls_per_day <= MAX_DAILY_CALL_LIMIT):
            return False
        if type(self._lease_seconds) is not int or not 1 <= self._lease_seconds <= MAX_LEASE_SECONDS:
            return False
        if (type(self._max_cache_entries) is not int
                or not 1 <= self._max_cache_entries <= MAX_CACHE_ENTRIES):
            return False
        if (type(self._busy_timeout_ms) is not int
                or not 0 <= self._busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS):
            return False
        return True

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        parent_info = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise PermissionError('private_directory_required')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self._path, flags, 0o600)
        try:
            file_info = os.fstat(descriptor)
            if not stat.S_ISREG(file_info.st_mode):
                raise PermissionError('regular_file_required')
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(
            self._path, timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f'PRAGMA busy_timeout={self._busy_timeout_ms}')
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def acquire_physical_call_lock(self) -> Optional[_PhysicalCallLock]:
        """Try a process-wide lock on the existing private DB file.

        The caller must already have durably reserved the batch. ``None`` is
        a busy lock; other failures mean no physical request may start. The
        descriptor is closed by the caller in ``finally`` and by the OS if
        the process exits unexpectedly.
        """
        if not self._configuration_valid:
            raise PermissionError('invalid_private_cache_configuration')
        parent = os.path.dirname(self._path)
        parent_info = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise PermissionError('private_directory_required')
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self._path, flags)
        try:
            file_info = os.fstat(descriptor)
            if not stat.S_ISREG(file_info.st_mode):
                raise PermissionError('regular_file_required')
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return None
            return _PhysicalCallLock(descriptor)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS decision_question_cache (
                fingerprint TEXT PRIMARY KEY CHECK(length(fingerprint)=64),
                state TEXT NOT NULL CHECK(state IN
                    ('reserved','answered','refused','unknown','deferred')),
                claim_token TEXT,
                lease_deadline TEXT,
                cooldown_until TEXT,
                answer_json TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                updated_at TEXT NOT NULL,
                CHECK((claim_token IS NULL) = (lease_deadline IS NULL))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS decision_global_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                claim_token TEXT,
                lease_deadline TEXT,
                cooldown_until TEXT,
                updated_at TEXT NOT NULL,
                CHECK((claim_token IS NULL) = (lease_deadline IS NULL))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS decision_physical_attempts (
                token TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                utc_day TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('reserved','completed','refused','rate_limited','unknown','released'))
            )
        """)
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_decision_attempts_day '
            'ON decision_physical_attempts(utc_day, state)'
        )
        connection.execute(
            'INSERT OR IGNORE INTO decision_global_state '
            '(singleton, claim_token, lease_deadline, cooldown_until, updated_at) '
            "VALUES(1, NULL, NULL, NULL, '1970-01-01T00:00:00+00:00')"
        )

    @staticmethod
    def _expire_active(connection: sqlite3.Connection, now: datetime) -> None:
        global_row = connection.execute(
            'SELECT claim_token, lease_deadline FROM decision_global_state WHERE singleton=1'
        ).fetchone()
        if not global_row or not global_row[0]:
            return
        token, deadline_text = global_row
        deadline = _parse_stamp(deadline_text)
        if deadline is not None and deadline > now:
            return
        now_text = _stamp(now)
        connection.execute(
            "UPDATE decision_question_cache SET state='unknown', claim_token=NULL, "
            'lease_deadline=NULL, cooldown_until=NULL, answer_json=NULL, updated_at=? '
            "WHERE claim_token=? AND state='reserved'",
            (now_text, token),
        )
        connection.execute(
            "UPDATE decision_physical_attempts SET state='unknown' "
            "WHERE token=? AND state='reserved'", (token,),
        )
        connection.execute(
            'UPDATE decision_global_state SET claim_token=NULL, lease_deadline=NULL, updated_at=? '
            'WHERE singleton=1 AND claim_token=?',
            (now_text, token),
        )

    @staticmethod
    def _expire_orphaned_rows(connection: sqlite3.Connection, now: datetime) -> None:
        global_row = connection.execute(
            'SELECT claim_token FROM decision_global_state WHERE singleton=1'
        ).fetchone()
        active_token = global_row[0] if global_row else None
        now_text = _stamp(now)
        if active_token is None:
            connection.execute(
                "UPDATE decision_question_cache SET state='unknown', claim_token=NULL, "
                'lease_deadline=NULL, cooldown_until=NULL, answer_json=NULL, updated_at=? '
                "WHERE state='reserved'",
                (now_text,),
            )
            connection.execute(
                "UPDATE decision_physical_attempts SET state='unknown' WHERE state='reserved'"
            )
        else:
            connection.execute(
                "UPDATE decision_question_cache SET state='unknown', claim_token=NULL, "
                'lease_deadline=NULL, cooldown_until=NULL, answer_json=NULL, updated_at=? '
                "WHERE state='reserved' AND claim_token != ?",
                (now_text, active_token),
            )
            connection.execute(
                "UPDATE decision_physical_attempts SET state='unknown' "
                "WHERE state='reserved' AND token != ?",
                (active_token,),
            )

    @staticmethod
    def _load_cached(
        connection: sqlite3.Connection,
        items: Tuple[DecisionWorkItem, ...],
        now: datetime,
    ) -> Tuple[list, list, list]:
        cached = []
        eligible = []
        blocked = []
        for item in items:
            row = connection.execute(
                'SELECT state, claim_token, lease_deadline, cooldown_until, answer_json '
                'FROM decision_question_cache WHERE fingerprint=?',
                (item.fingerprint,),
            ).fetchone()
            if row is None:
                eligible.append((item, None, None))
                continue
            state, token, deadline_text, cooldown_text, answer_raw = row
            if state in ('answered', 'refused'):
                answer = _decode_answer(answer_raw, item)
                if answer is None or answer.status != ('refused' if state == 'refused' else 'answered'):
                    connection.execute(
                        "UPDATE decision_question_cache SET state='unknown', answer_json=NULL, "
                        'updated_at=? WHERE fingerprint=?',
                        (_stamp(now), item.fingerprint),
                    )
                    blocked.append(DecisionBlockedItem(item.fingerprint, 'unknown'))
                else:
                    cached.append((item.fingerprint, answer))
                continue
            if state == 'unknown':
                blocked.append(DecisionBlockedItem(item.fingerprint, 'unknown'))
                continue
            if state == 'reserved':
                deadline = _parse_stamp(deadline_text)
                blocked.append(DecisionBlockedItem(item.fingerprint, 'busy', deadline))
                continue
            if state == 'deferred':
                cooldown = _parse_stamp(cooldown_text)
                if cooldown is None:
                    connection.execute(
                        "UPDATE decision_question_cache SET state='unknown', cooldown_until=NULL, "
                        'updated_at=? WHERE fingerprint=?',
                        (_stamp(now), item.fingerprint),
                    )
                    blocked.append(DecisionBlockedItem(item.fingerprint, 'unknown'))
                elif cooldown > now:
                    blocked.append(DecisionBlockedItem(item.fingerprint, 'deferred', cooldown))
                else:
                    eligible.append((item, 'deferred', cooldown))
                continue
            blocked.append(DecisionBlockedItem(item.fingerprint, 'unknown'))
        return cached, eligible, blocked

    @staticmethod
    def _reservation_kind(cached, blocked) -> str:
        states = {item.state for item in blocked}
        if 'busy' in states:
            return 'busy'
        if 'deferred' in states:
            return 'deferred'
        if 'unknown' in states:
            return 'unknown'
        if cached:
            return 'cached'
        return 'blocked'

    def reserve_batch(
        self,
        items: Sequence[DecisionWorkItem],
        *,
        now: Optional[datetime] = None,
    ) -> DecisionBatchReservation:
        if not self._configuration_valid or not _valid_question_items(items):
            return DecisionBatchReservation('invalid', safe_code='invalid_reservation')
        try:
            current = _utc(now) or datetime.now(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return DecisionBatchReservation('invalid', safe_code='invalid_clock')
        current_text = _stamp(current)
        request_items = tuple(items)

        connection = None
        try:
            connection = self._connect()
            connection.execute('BEGIN IMMEDIATE')
            self._create_schema(connection)
            self._expire_active(connection, current)
            self._expire_orphaned_rows(connection, current)

            global_row = connection.execute(
                'SELECT claim_token, lease_deadline, cooldown_until '
                'FROM decision_global_state WHERE singleton=1'
            ).fetchone()
            active_token, active_deadline_text, global_cooldown_text = global_row
            cached, eligible, blocked = self._load_cached(connection, request_items, current)

            if not eligible:
                result = DecisionBatchReservation(
                    self._reservation_kind(cached, blocked),
                    requested=request_items,
                    cached_answers=tuple(cached),
                    blocked=tuple(blocked),
                    retry_at=min((b.retry_at for b in blocked if b.retry_at), default=None),
                )
                connection.commit()
                return result

            if active_token:
                active_deadline = _parse_stamp(active_deadline_text)
                blocked.extend(
                    DecisionBlockedItem(item.fingerprint, 'busy', active_deadline)
                    for item, _state, _cooldown in eligible
                )
                result = DecisionBatchReservation(
                    'busy', requested=request_items,
                    cached_answers=tuple(cached), blocked=tuple(blocked),
                    retry_at=active_deadline,
                )
                connection.commit()
                return result

            global_cooldown = _parse_stamp(global_cooldown_text)
            if global_cooldown is not None and global_cooldown > current:
                blocked.extend(
                    DecisionBlockedItem(item.fingerprint, 'deferred', global_cooldown)
                    for item, _state, _cooldown in eligible
                )
                result = DecisionBatchReservation(
                    'deferred', requested=request_items,
                    cached_answers=tuple(cached), blocked=tuple(blocked),
                    retry_at=global_cooldown,
                )
                connection.commit()
                return result
            if global_cooldown_text is not None:
                connection.execute(
                    'UPDATE decision_global_state SET cooldown_until=NULL, updated_at=? '
                    'WHERE singleton=1', (current_text,),
                )

            utc_day = current.date().isoformat()
            connection.execute(
                'DELETE FROM decision_physical_attempts WHERE utc_day < ?',
                ((current.date() - timedelta(days=ATTEMPT_RETENTION_DAYS)).isoformat(),),
            )
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM decision_physical_attempts "
                "WHERE utc_day=? AND state != 'released'",
                (utc_day,),
            ).fetchone()[0]
            if attempt_count >= self._max_calls_per_day:
                blocked.extend(
                    DecisionBlockedItem(item.fingerprint, 'budget_exhausted')
                    for item, _state, _cooldown in eligible
                )
                result = DecisionBatchReservation(
                    'budget_exhausted', requested=request_items,
                    cached_answers=tuple(cached), blocked=tuple(blocked),
                    safe_code='daily_call_limit',
                )
                connection.commit()
                return result

            existing_count = connection.execute(
                'SELECT COUNT(*) FROM decision_question_cache'
            ).fetchone()[0]
            new_count = sum(1 for _item, old_state, _cooldown in eligible if old_state is None)
            if existing_count + new_count > self._max_cache_entries:
                blocked.extend(
                    DecisionBlockedItem(item.fingerprint, 'cache_full')
                    for item, _state, _cooldown in eligible
                )
                result = DecisionBatchReservation(
                    'cache_full', requested=request_items,
                    cached_answers=tuple(cached), blocked=tuple(blocked),
                    safe_code='cache_capacity',
                )
                connection.commit()
                return result

            token = secrets.token_hex(32)
            deadline = current + timedelta(seconds=self._lease_seconds)
            deadline_text = _stamp(deadline)
            connection.execute(
                'INSERT INTO decision_physical_attempts(token, started_at, utc_day, state) '
                "VALUES(?, ?, ?, 'reserved')",
                (token, current_text, utc_day),
            )
            connection.execute(
                'UPDATE decision_global_state SET claim_token=?, lease_deadline=?, updated_at=? '
                'WHERE singleton=1 AND claim_token IS NULL',
                (token, deadline_text, current_text),
            )
            if connection.execute('SELECT changes()').fetchone()[0] != 1:
                connection.rollback()
                return DecisionBatchReservation('busy', safe_code='global_claim_busy')

            previous = []
            claimed = []
            for item, old_state, old_cooldown in eligible:
                row = connection.execute(
                    'SELECT attempt_count FROM decision_question_cache WHERE fingerprint=?',
                    (item.fingerprint,),
                ).fetchone()
                old_attempts = row[0] if row else 0
                previous.append((item.fingerprint, old_state, old_cooldown, old_attempts))
                if row is None:
                    connection.execute(
                        'INSERT INTO decision_question_cache '
                        '(fingerprint,state,claim_token,lease_deadline,cooldown_until,answer_json,attempt_count,updated_at) '
                        "VALUES(?, 'reserved', ?, ?, NULL, NULL, 1, ?)",
                        (item.fingerprint, token, deadline_text, current_text),
                    )
                else:
                    connection.execute(
                        "UPDATE decision_question_cache SET state='reserved', claim_token=?, "
                        'lease_deadline=?, cooldown_until=NULL, answer_json=NULL, '
                        'attempt_count=attempt_count+1, updated_at=? WHERE fingerprint=?',
                        (token, deadline_text, current_text, item.fingerprint),
                    )
                claimed.append(item)

            connection.commit()
            return DecisionBatchReservation(
                'ready', requested=request_items, token=token,
                claimed=tuple(claimed), cached_answers=tuple(cached),
                blocked=tuple(blocked), _previous=tuple(previous),
            )
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            busy = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
            return DecisionBatchReservation(
                'busy' if busy else 'unavailable',
                safe_code='ledger_busy' if busy else 'ledger_unavailable',
            )
        except Exception:
            if connection is not None:
                connection.rollback()
            return DecisionBatchReservation('unavailable', safe_code='ledger_unavailable')
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _unknown_claim(connection, token: str, now: datetime) -> None:
        now_text = _stamp(now)
        connection.execute(
            "UPDATE decision_question_cache SET state='unknown', claim_token=NULL, "
            'lease_deadline=NULL, cooldown_until=NULL, answer_json=NULL, updated_at=? '
            "WHERE claim_token=? AND state='reserved'", (now_text, token),
        )
        connection.execute(
            "UPDATE decision_physical_attempts SET state='unknown' "
            "WHERE token=? AND state='reserved'", (token,),
        )
        connection.execute(
            'UPDATE decision_global_state SET claim_token=NULL, lease_deadline=NULL, updated_at=? '
            'WHERE singleton=1 AND claim_token=?', (now_text, token),
        )

    @classmethod
    def _unknown_claim_with_cooldown(
        cls,
        connection: sqlite3.Connection,
        token: str,
        now: datetime,
        cooldown_seconds: int,
    ) -> Optional[datetime]:
        cls._unknown_claim(connection, token, now)
        try:
            retry_at = now + timedelta(seconds=cooldown_seconds)
        except OverflowError:
            return None
        connection.execute(
            'UPDATE decision_global_state SET cooldown_until=?, updated_at=? '
            'WHERE singleton=1', (_stamp(retry_at), _stamp(now)),
        )
        return retry_at

    @staticmethod
    def _release_unstarted(
        connection: sqlite3.Connection,
        reservation: DecisionBatchReservation,
        now: datetime,
    ) -> bool:
        token = reservation.token
        previous = {fingerprint: (state, cooldown, attempts)
                    for fingerprint, state, cooldown, attempts in reservation._previous}
        claims = connection.execute(
            "SELECT fingerprint FROM decision_question_cache "
            "WHERE claim_token=? AND state='reserved' ORDER BY rowid",
            (token,),
        ).fetchall()
        if {row[0] for row in claims} != set(previous):
            return False
        now_text = _stamp(now)
        for fingerprint, (state, cooldown, attempts) in previous.items():
            if state is None:
                connection.execute(
                    'DELETE FROM decision_question_cache WHERE fingerprint=? AND claim_token=?',
                    (fingerprint, token),
                )
            else:
                connection.execute(
                    "UPDATE decision_question_cache SET state='deferred', claim_token=NULL, "
                    'lease_deadline=NULL, cooldown_until=?, answer_json=NULL, attempt_count=?, updated_at=? '
                    'WHERE fingerprint=? AND claim_token=?',
                    (_stamp(cooldown) if cooldown else None, attempts, now_text, fingerprint, token),
                )
        connection.execute(
            "UPDATE decision_physical_attempts SET state='released' "
            "WHERE token=? AND state='reserved'", (token,),
        )
        connection.execute(
            'UPDATE decision_global_state SET claim_token=NULL, lease_deadline=NULL, updated_at=? '
            'WHERE singleton=1 AND claim_token=?', (now_text, token),
        )
        return True

    def finish_batch(
        self,
        reservation: DecisionBatchReservation,
        outcome: DecisionOutcome,
        *,
        now: Optional[datetime] = None,
    ) -> DecisionBatchFinish:
        if (type(reservation) is not DecisionBatchReservation or reservation.kind != 'ready'
                or type(reservation.token) is not str or not reservation.token
                or not reservation.claimed):
            return DecisionBatchFinish('invalid', safe_code='invalid_reservation')
        try:
            current = _utc(now) or datetime.now(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return DecisionBatchFinish('invalid', safe_code='invalid_clock')

        connection = None
        try:
            connection = self._connect()
            connection.execute('BEGIN IMMEDIATE')
            self._create_schema(connection)
            global_row = connection.execute(
                'SELECT claim_token, lease_deadline, cooldown_until '
                'FROM decision_global_state WHERE singleton=1'
            ).fetchone()
            active_token, deadline_text, old_cooldown_text = global_row
            if active_token != reservation.token:
                connection.rollback()
                return DecisionBatchFinish('stale', safe_code='claim_not_active')
            deadline = _parse_stamp(deadline_text)
            if deadline is None or deadline <= current:
                self._unknown_claim(connection, reservation.token, current)
                connection.commit()
                return DecisionBatchFinish('unknown', safe_code='claim_expired')

            if type(outcome) is not DecisionOutcome:
                self._unknown_claim(connection, reservation.token, current)
                connection.commit()
                return DecisionBatchFinish('unknown', safe_code='invalid_outcome')

            if outcome.kind == 'configuration_error':
                claimed_fingerprints = [item.fingerprint for item in reservation.claimed]
                previous_fingerprints = [entry[0] for entry in reservation._previous]
                rows = connection.execute(
                    "SELECT fingerprint FROM decision_question_cache "
                    "WHERE claim_token=? AND state='reserved'",
                    (reservation.token,),
                ).fetchall()
                if (len(claimed_fingerprints) != len(set(claimed_fingerprints))
                        or set(claimed_fingerprints) != set(previous_fingerprints)
                        or set(claimed_fingerprints) != {row[0] for row in rows}):
                    self._unknown_claim(connection, reservation.token, current)
                    connection.commit()
                    return DecisionBatchFinish('unknown', safe_code='claim_mismatch')
                released = self._release_unstarted(connection, reservation, current)
                if not released:
                    self._unknown_claim(connection, reservation.token, current)
                    connection.commit()
                    return DecisionBatchFinish('unknown', safe_code='claim_mismatch')
                connection.commit()
                return DecisionBatchFinish('released', safe_code='request_not_started')

            claimed_rows = connection.execute(
                "SELECT fingerprint FROM decision_question_cache "
                "WHERE claim_token=? AND state='reserved' ORDER BY rowid",
                (reservation.token,),
            ).fetchall()
            if {row[0] for row in claimed_rows} != {item.fingerprint for item in reservation.claimed}:
                self._unknown_claim(connection, reservation.token, current)
                connection.commit()
                return DecisionBatchFinish('unknown', safe_code='claim_mismatch')

            if outcome.kind == 'rate_limited':
                retry_after = outcome.retry_after_seconds
                if (outcome.http_status != 429 or type(retry_after) is not int
                        or not 0 <= retry_after <= MAX_RETRY_AFTER_SECONDS):
                    if outcome.http_status == 429:
                        retry_at = self._unknown_claim_with_cooldown(
                            connection,
                            reservation.token,
                            current,
                            MISSING_RETRY_AFTER_COOLDOWN_SECONDS,
                        )
                        connection.commit()
                        return DecisionBatchFinish(
                            'unknown', retry_at=retry_at,
                            safe_code='provider_rate_limited_without_valid_advice',
                        )
                    self._unknown_claim(connection, reservation.token, current)
                    connection.commit()
                    return DecisionBatchFinish('unknown', safe_code='invalid_retry_advice')
                delay = max(1, retry_after)
                try:
                    retry_at = current + timedelta(seconds=delay)
                except OverflowError:
                    self._unknown_claim(connection, reservation.token, current)
                    connection.commit()
                    return DecisionBatchFinish('unknown', safe_code='retry_advice_out_of_range')
                retry_text = _stamp(retry_at)
                for item in reservation.claimed:
                    connection.execute(
                        "UPDATE decision_question_cache SET state='deferred', claim_token=NULL, "
                        'lease_deadline=NULL, cooldown_until=?, answer_json=NULL, updated_at=? '
                        "WHERE fingerprint=? AND claim_token=? AND state='reserved'",
                        (retry_text, _stamp(current), item.fingerprint, reservation.token),
                    )
                global_cooldown = _parse_stamp(old_cooldown_text)
                if global_cooldown is not None and global_cooldown > retry_at:
                    retry_at = global_cooldown
                    retry_text = _stamp(retry_at)
                    connection.execute(
                        "UPDATE decision_question_cache SET cooldown_until=? "
                        "WHERE state='deferred' AND cooldown_until < ?",
                        (retry_text, retry_text),
                    )
                connection.execute(
                    "UPDATE decision_physical_attempts SET state='rate_limited' "
                    "WHERE token=? AND state='reserved'", (reservation.token,),
                )
                connection.execute(
                    'UPDATE decision_global_state SET claim_token=NULL, lease_deadline=NULL, '
                    'cooldown_until=?, updated_at=? WHERE singleton=1 AND claim_token=?',
                    (retry_text, _stamp(current), reservation.token),
                )
                connection.commit()
                return DecisionBatchFinish('deferred', retry_at=retry_at, safe_code='provider_rate_limited')

            if outcome.kind == 'http_error' and outcome.http_status in (401, 403):
                retry_at = self._unknown_claim_with_cooldown(
                    connection,
                    reservation.token,
                    current,
                    AUTH_FAILURE_COOLDOWN_SECONDS,
                )
                connection.commit()
                return DecisionBatchFinish(
                    'unknown', retry_at=retry_at,
                    safe_code='provider_auth_failure',
                )

            answers = outcome.answers
            expected_kind = None
            if type(answers) is tuple and len(answers) == len(reservation.claimed):
                if all(_valid_answer(answer, item) for answer, item in zip(answers, reservation.claimed)):
                    answered_count = sum(answer.status == 'answered' for answer in answers)
                    expected_kind = ('success' if answered_count == len(answers) else
                                     'refused' if answered_count == 0 else 'partial_refusal')
            if expected_kind is None or outcome.kind != expected_kind:
                self._unknown_claim(connection, reservation.token, current)
                connection.commit()
                return DecisionBatchFinish('unknown', safe_code='invalid_decision_result')

            now_text = _stamp(current)
            for answer, item in zip(answers, reservation.claimed):
                state = 'refused' if answer.status == 'refused' else 'answered'
                connection.execute(
                    'UPDATE decision_question_cache SET state=?, claim_token=NULL, '
                    'lease_deadline=NULL, cooldown_until=NULL, answer_json=?, updated_at=? '
                    "WHERE fingerprint=? AND claim_token=? AND state='reserved'",
                    (state, _answer_json(answer), now_text, item.fingerprint, reservation.token),
                )
            attempt_state = 'refused' if expected_kind == 'refused' else 'completed'
            connection.execute(
                'UPDATE decision_physical_attempts SET state=? '
                "WHERE token=? AND state='reserved'", (attempt_state, reservation.token),
            )
            connection.execute(
                'UPDATE decision_global_state SET claim_token=NULL, lease_deadline=NULL, updated_at=? '
                'WHERE singleton=1 AND claim_token=?', (now_text, reservation.token),
            )
            connection.commit()
            return DecisionBatchFinish(expected_kind)
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            busy = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
            return DecisionBatchFinish('busy' if busy else 'unavailable',
                                       safe_code='ledger_busy' if busy else 'ledger_unavailable')
        except Exception:
            if connection is not None:
                connection.rollback()
            return DecisionBatchFinish('unavailable', safe_code='ledger_unavailable')
        finally:
            if connection is not None:
                connection.close()
