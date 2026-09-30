"""Shared, non-sleeping Ozon transport budget and provider cooldown.

The local 40/client/s and 20/method/s ceilings are conservative platform budgets,
not a claim that all Ozon methods have the same upstream quota. An observed 429
pauses the entire Client-Id, including other keys, workers and endpoint families.
The small independent SQLite ledger survives application/container restarts.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time


class OzonRateBudget:
    CLIENT_LIMIT = 40
    ENDPOINT_LIMIT = 20
    WINDOW_SECONDS = 1.0

    def __init__(self, client_id, *, directory=None, clock=time.time):
        self.scope = hashlib.sha256(('ozon-client:' + client_id).encode()).hexdigest()
        self.directory = Path(directory or os.environ.get('OZON_RATE_LIMIT_DIR', 'data/ozon_api_limits'))
        self.clock = clock

    def _connection(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / 'budget.sqlite3'
        # Ledger contains only hashed identity, bounded timestamps and method
        # names. No credentials, payload, provider body or main ORM transaction.
        connection = sqlite3.connect(path, timeout=0.2, isolation_level=None)
        try:
            os.chmod(path, 0o600)
            connection.execute('CREATE TABLE IF NOT EXISTS budgets (scope TEXT PRIMARY KEY, state TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            return connection
        except Exception:
            connection.close()
            raise

    def _load(self, connection):
        row = connection.execute('SELECT state FROM budgets WHERE scope=?', (self.scope,)).fetchone()
        if row is None:
            return {'until':0, 'calls':[]}
        if len(row[0]) > 16000:
            raise ValueError('Invalid Ozon budget state')
        state = json.loads(row[0])
        if (not isinstance(state, dict) or set(state) != {'until', 'calls'}
                or type(state['until']) not in (int, float) or not math.isfinite(state['until'])
                or not isinstance(state['calls'], list) or len(state['calls']) > self.CLIENT_LIMIT):
            raise ValueError('Invalid Ozon budget state')
        for row in state['calls']:
            if (not isinstance(row, list) or len(row) != 2
                    or type(row[0]) not in (int, float) or not math.isfinite(row[0])
                    or not isinstance(row[1], str) or len(row[1]) > 100):
                raise ValueError('Invalid Ozon budget call')
        return state

    def _save(self, connection, state):
        connection.execute('INSERT INTO budgets(scope,state) VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET state=excluded.state',
                           (self.scope, json.dumps(state, separators=(',', ':'))))
        connection.commit()

    def reserve(self, endpoint):
        """Return a delay, or zero after atomically reserving one physical call."""
        connection = None
        try:
            connection = self._connection()
            state = self._load(connection)
            now = self.clock()
            if state['until'] > now:
                return state['until'] - now
            calls = [row for row in state['calls'] if row[0] > now - self.WINDOW_SECONDS]
            same = [row for row in calls if row[1] == endpoint]
            if len(calls) >= self.CLIENT_LIMIT or len(same) >= self.ENDPOINT_LIMIT:
                limited = calls if len(calls) >= self.CLIENT_LIMIT else same
                return max(0.05, limited[0][0] + self.WINDOW_SECONDS - now)
            state['calls'] = calls + [[now, endpoint]]
            self._save(connection, state)
            return 0.0
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # An unavailable/corrupt ledger must never turn into unlimited HTTP.
            return 60.0
        finally:
            if connection is not None:
                connection.close()

    def defer(self, retry_after):
        """Persist the longest observed provider pause, without sleeping."""
        delay = retry_after if type(retry_after) in (int, float) and math.isfinite(retry_after) else 60.0
        delay = max(1.0, delay)
        connection = None
        try:
            connection = self._connection()
            state = self._load(connection)
            until = self.clock() + delay
            if not math.isfinite(until):
                until = float.fromhex('0x1.fffffffffffffp+1023')
            state['until'] = max(state['until'], until)
            self._save(connection, state)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # The caller still receives the full Retry-After and persists its
            # workflow deadline. Requests fail closed while the ledger is down.
            return False
        finally:
            if connection is not None:
                connection.close()
        return True
