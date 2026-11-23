"""Private durable storage for bounded supplier color observations.

This store is deliberately separate from the seller application database.
It contains only exact product revisions, opaque external-ID digests and raw
color literals; it never contains a feed URL, response body or credential.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from typing import Any, Optional


RESERVATION_INTERVAL = timedelta(hours=24)
OBSERVATION_FRESHNESS = timedelta(hours=36)
MAX_OBSERVATIONS = 50_000
MAX_COLORS_PER_PRODUCT = 20
MAX_COLOR_LENGTH = 200
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_STORE_BYTES = 256 * 1024 * 1024
STORE_SUFFIX = ".supplier-color-observations.sqlite3"
_SCHEMA_VERSION = 1
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_HEX_256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RESERVATION_US = int(RESERVATION_INTERVAL.total_seconds() * 1_000_000)
_FRESHNESS_US = int(OBSERVATION_FRESHNESS.total_seconds() * 1_000_000)


def _positive_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= _MAX_SQLITE_INTEGER


def _digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_256_RE.fullmatch(value))


def _timestamp_us(value: Any) -> Optional[int]:
    if not isinstance(value, datetime):
        return None
    try:
        offset = value.utcoffset()
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None or offset is None
            else value.astimezone(timezone.utc)
        )
        delta = normalized - _EPOCH
        result = (
            (delta.days * 86_400 + delta.seconds) * 1_000_000
            + delta.microseconds
        )
    except (OverflowError, TypeError, ValueError):
        return None
    if type(result) is not int or result < 0 or result > _MAX_SQLITE_INTEGER:
        return None
    return result


def _validated_observations(observations: Any) -> Optional[list[tuple[int, int, str, str]]]:
    if type(observations) is not dict or len(observations) > MAX_OBSERVATIONS:
        return None
    rows = []
    payload_size = 0
    for product_id, record in observations.items():
        if (
            not _positive_id(product_id)
            or type(record) is not tuple
            or len(record) != 3
        ):
            return None
        revision, external_id_sha256, colors = record
        if (
            not _positive_id(revision)
            or not _digest(external_id_sha256)
            or type(colors) is not tuple
            or not 1 <= len(colors) <= MAX_COLORS_PER_PRODUCT
        ):
            return None
        seen = set()
        for color in colors:
            if (
                not isinstance(color, str)
                or not color
                or len(color) > MAX_COLOR_LENGTH
                or color != color.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in color)
            ):
                return None
            folded = color.casefold()
            if folded in seen:
                return None
            seen.add(folded)
        try:
            encoded = json.dumps(
                list(colors), ensure_ascii=False, separators=(",", ":"),
            )
            payload_size += len(encoded.encode("utf-8")) + 96
        except (TypeError, UnicodeEncodeError):
            return None
        if payload_size > MAX_PAYLOAD_BYTES:
            return None
        rows.append((product_id, revision, external_id_sha256, encoded))
    return rows


class SupplierColorObservationStore:
    """A 0600 SQLite receipt and latest validated observation per supplier."""

    def __init__(self, path: os.PathLike[str] | str):
        if not isinstance(path, (str, os.PathLike)):
            raise ValueError("invalid_store_path")
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("invalid_store_path")
        self.path = Path(os.path.abspath(raw_path))
        if not self.path.name or self.path.name in {".", ".."}:
            raise ValueError("invalid_store_path")

    def reserve(
        self,
        supplier_id: int,
        config_digest: str,
        *,
        now: datetime,
    ) -> Optional[str]:
        """Commit one physical-read reservation, at most once per 24 hours."""
        now_us = _timestamp_us(now)
        if not _positive_id(supplier_id) or not _digest(config_digest) or now_us is None:
            return None
        connection = None
        try:
            connection = self._connect(write=True, create=True)
            if connection is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            previous = connection.execute(
                "SELECT reserved_at_us FROM reservations WHERE supplier_id = ?",
                (supplier_id,),
            ).fetchone()
            if previous is not None:
                previous_us = previous[0]
                if type(previous_us) is not int or now_us < previous_us:
                    connection.rollback()
                    return None
                if now_us - previous_us < _RESERVATION_US:
                    connection.rollback()
                    return None

            token = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO reservations (
                    supplier_id, token, config_digest, reserved_at_us, status
                ) VALUES (?, ?, ?, ?, 'reserved')
                ON CONFLICT(supplier_id) DO UPDATE SET
                    token=excluded.token,
                    config_digest=excluded.config_digest,
                    reserved_at_us=excluded.reserved_at_us,
                    status='reserved'
                """,
                (supplier_id, token, config_digest, now_us),
            )
            connection.commit()
            return token
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return None
        finally:
            if connection is not None:
                connection.close()

    def finish(
        self,
        supplier_id: int,
        token: str,
        config_digest: str,
        observations: dict[int, tuple[int, str, tuple[str, ...]]],
        *,
        observed_at: datetime,
    ) -> bool:
        """Consume the exact reservation and atomically replace its snapshot."""
        observed_us = _timestamp_us(observed_at)
        rows = _validated_observations(observations)
        if (
            not _positive_id(supplier_id)
            or not isinstance(token, str)
            or not _TOKEN_RE.fullmatch(token)
            or not _digest(config_digest)
            or observed_us is None
            or rows is None
        ):
            return False
        connection = None
        try:
            connection = self._connect(write=True, create=False)
            if connection is None:
                return False
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            reservation = connection.execute(
                """
                SELECT token, config_digest, reserved_at_us, status
                  FROM reservations WHERE supplier_id = ?
                """,
                (supplier_id,),
            ).fetchone()
            if (
                reservation is None
                or reservation[0] != token
                or reservation[1] != config_digest
                or reservation[3] != "reserved"
                or type(reservation[2]) is not int
                or observed_us < reservation[2]
                or observed_us - reservation[2] >= _RESERVATION_US
            ):
                connection.rollback()
                return False

            connection.execute(
                "DELETE FROM observation_batches WHERE supplier_id = ?",
                (supplier_id,),
            )
            connection.execute(
                """
                INSERT INTO observation_batches (
                    supplier_id, token, config_digest, observed_at_us, row_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (supplier_id, token, config_digest, observed_us, len(rows)),
            )
            if rows:
                connection.executemany(
                    """
                    INSERT INTO observations (
                        supplier_id, supplier_product_id, content_revision,
                        external_id_sha256, colors_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (supplier_id, product_id, revision, digest, colors_json)
                        for product_id, revision, digest, colors_json in rows
                    ],
                )
            updated = connection.execute(
                """
                UPDATE reservations SET status = 'finished'
                 WHERE supplier_id = ? AND token = ? AND status = 'reserved'
                """,
                (supplier_id, token),
            ).rowcount
            if updated != 1:
                connection.rollback()
                return False
            connection.commit()
            return True
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return False
        finally:
            if connection is not None:
                connection.close()

    def get(
        self,
        supplier_id: int,
        supplier_product_id: int,
        content_revision: int,
        external_id_sha256: str,
        config_digest: str,
        *,
        now: datetime,
    ) -> Optional[list[str]]:
        """Return exact fresh colors without creating or changing the file."""
        now_us = _timestamp_us(now)
        if (
            not _positive_id(supplier_id)
            or not _positive_id(supplier_product_id)
            or not _positive_id(content_revision)
            or not _digest(external_id_sha256)
            or not _digest(config_digest)
            or now_us is None
        ):
            return None
        connection = None
        try:
            connection = self._connect(write=False, create=False)
            if connection is None:
                return None
            connection.execute("PRAGMA query_only = ON")
            if not self._schema_is_current(connection):
                return None
            batch = connection.execute(
                """
                SELECT config_digest, observed_at_us, row_count
                  FROM observation_batches WHERE supplier_id = ?
                """,
                (supplier_id,),
            ).fetchone()
            if batch is None:
                return None
            stored_digest, observed_us, row_count = batch
            if (
                stored_digest != config_digest
                or type(observed_us) is not int
                or type(row_count) is not int
                or row_count <= 0
                or row_count > MAX_OBSERVATIONS
                or now_us < observed_us
                or now_us - observed_us > _FRESHNESS_US
            ):
                return None
            row = connection.execute(
                """
                SELECT content_revision, external_id_sha256, colors_json
                  FROM observations
                 WHERE supplier_id = ? AND supplier_product_id = ?
                """,
                (supplier_id, supplier_product_id),
            ).fetchone()
            if (
                row is None
                or row[0] != content_revision
                or row[1] != external_id_sha256
                or not isinstance(row[2], str)
                or len(row[2].encode("utf-8")) > MAX_COLOR_LENGTH * MAX_COLORS_PER_PRODUCT * 4
            ):
                return None
            colors = json.loads(row[2])
            if type(colors) is not list or not 1 <= len(colors) <= MAX_COLORS_PER_PRODUCT:
                return None
            if any(
                not isinstance(color, str)
                or not color
                or len(color) > MAX_COLOR_LENGTH
                or color != color.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in color)
                for color in colors
            ):
                return None
            if len({color.casefold() for color in colors}) != len(colors):
                return None
            return colors
        except Exception:
            return None
        finally:
            if connection is not None:
                connection.close()

    def _connect(self, *, write: bool, create: bool) -> Optional[sqlite3.Connection]:
        if not self._safe_parent():
            return None
        descriptor = None
        connection = None
        try:
            flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if not getattr(os, "O_NOFOLLOW", 0):
                return None
            if create:
                flags |= os.O_RDWR | os.O_CREAT
            elif write:
                flags |= os.O_RDWR
            else:
                flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self.path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_STORE_BYTES
            ):
                os.close(descriptor)
                return None
            named = os.stat(self.path, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino):
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = None
            mode = "rw" if write else "ro"
            uri = f"{self.path.as_uri()}?mode={mode}"
            connection = sqlite3.connect(
                uri, uri=True, timeout=0.1, isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout = 100")
            if write:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = DELETE")
            return connection
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            return None

    def _safe_parent(self) -> bool:
        try:
            parent = self.path.parent
            info = os.stat(parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                return False
            # Reject symlinked parent components so the configured sibling
            # cannot silently be redirected away from the persistent volume.
            return parent.resolve(strict=True) == parent
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _schema_is_current(connection: sqlite3.Connection) -> bool:
        try:
            version = connection.execute("PRAGMA user_version").fetchone()
            if version != (_SCHEMA_VERSION,):
                return False
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'",
                )
            }
            return {
                "reservations", "observation_batches", "observations",
            }.issubset(tables)
        except sqlite3.Error:
            return False

    @classmethod
    def _ensure_schema(cls, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()
        if version == (_SCHEMA_VERSION,):
            if not cls._schema_is_current(connection):
                raise sqlite3.DatabaseError("invalid_store_schema")
            return
        if version != (0,):
            raise sqlite3.DatabaseError("unsupported_store_schema")
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
        if tables:
            raise sqlite3.DatabaseError("unversioned_store_schema")
        statements = (
            """
            CREATE TABLE reservations (
                supplier_id INTEGER PRIMARY KEY CHECK(supplier_id > 0),
                token TEXT NOT NULL CHECK(length(token) = 32),
                config_digest TEXT NOT NULL CHECK(length(config_digest) = 64),
                reserved_at_us INTEGER NOT NULL CHECK(reserved_at_us >= 0),
                status TEXT NOT NULL CHECK(status IN ('reserved', 'finished'))
            )
            """,
            """
            CREATE TABLE observation_batches (
                supplier_id INTEGER PRIMARY KEY CHECK(supplier_id > 0),
                token TEXT NOT NULL CHECK(length(token) = 32),
                config_digest TEXT NOT NULL CHECK(length(config_digest) = 64),
                observed_at_us INTEGER NOT NULL CHECK(observed_at_us >= 0),
                row_count INTEGER NOT NULL CHECK(row_count >= 0)
            )
            """,
            """
            CREATE TABLE observations (
                supplier_id INTEGER NOT NULL CHECK(supplier_id > 0),
                supplier_product_id INTEGER NOT NULL CHECK(supplier_product_id > 0),
                content_revision INTEGER NOT NULL CHECK(content_revision > 0),
                external_id_sha256 TEXT NOT NULL CHECK(length(external_id_sha256) = 64),
                colors_json TEXT NOT NULL,
                PRIMARY KEY (supplier_id, supplier_product_id),
                FOREIGN KEY (supplier_id)
                    REFERENCES observation_batches(supplier_id) ON DELETE CASCADE
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        if not cls._schema_is_current(connection):
            raise sqlite3.DatabaseError("store_schema_create_failed")


def configured_supplier_color_observation_store() -> Optional[SupplierColorObservationStore]:
    """Return a sibling store only for an absolute file-backed SQLite URI."""
    try:
        from flask import current_app, has_app_context
        from sqlalchemy.engine import make_url

        if not has_app_context():
            return None
        value = current_app.config.get("SQLALCHEMY_DATABASE_URI")
        if not isinstance(value, str) or not value:
            return None
        url = make_url(value)
        if (
            url.get_backend_name() != "sqlite"
            or url.query
            or not isinstance(url.database, str)
            or not os.path.isabs(url.database)
            or url.database == ":memory:"
        ):
            return None
        database_path = Path(os.path.abspath(url.database))
        if database_path.is_symlink():
            return None
        store_path = database_path.with_name(database_path.name + STORE_SUFFIX)
        store = SupplierColorObservationStore(store_path)
        return store if store._safe_parent() else None
    except Exception:
        return None
