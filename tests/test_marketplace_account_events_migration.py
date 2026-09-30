import sqlite3
import pytest
from migrations import migrate_add_marketplace_account_events as migration


@pytest.fixture
def connection():
    c = sqlite3.connect(':memory:')
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript('''CREATE TABLE sellers(id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces(id INTEGER PRIMARY KEY);
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts(id INTEGER PRIMARY KEY,label TEXT);
        INSERT INTO sellers VALUES(1); INSERT INTO marketplaces VALUES(1);
        INSERT INTO users VALUES(1); INSERT INTO seller_marketplace_accounts VALUES(1,'Unchanged');''')
    yield c
    c.close()


def insert(c, **changes):
    values = dict(id=1, account_id=1, seller_id=1, marketplace_id=1, actor_user_id=1,
        action='settings_changed', account_version_before=1, account_version_after=2,
        credential_version_before=1, credential_version_after=1, changes_json='{}', created_at='2026-09-26')
    values.update(changes)
    c.execute('INSERT INTO marketplace_account_events ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')', tuple(values.values()))


def test_additive_empty_history_then_repeat_preserves_records(connection):
    assert migration.apply_migration(connection, verbose=False) == 3
    assert connection.execute('SELECT * FROM marketplace_account_events').fetchall() == []
    insert(connection)
    connection.commit()
    before = connection.execute('SELECT * FROM marketplace_account_events').fetchall()
    assert migration.apply_migration(connection, verbose=False) == 0
    assert connection.execute('SELECT * FROM marketplace_account_events').fetchall() == before
    assert connection.execute('SELECT label FROM seller_marketplace_accounts').fetchone() == ('Unchanged',)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('change', [dict(action='raw_secret'), dict(account_version_after=1),
    dict(account_version_before=-1), dict(credential_version_before=-1), dict(credential_version_after=0),
    dict(account_id=999), dict(actor_user_id=999)])
def test_database_constraints(connection, change):
    migration.apply_migration(connection, verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        insert(connection, **change)


def test_version_unique_and_deleted_actor_becomes_unknown(connection):
    migration.apply_migration(connection, verbose=False)
    insert(connection)
    with pytest.raises(sqlite3.IntegrityError):
        insert(connection, id=2)
    connection.execute('DELETE FROM users WHERE id=1')
    assert connection.execute('SELECT actor_user_id FROM marketplace_account_events').fetchone() == (None,)


def test_orm_schema_is_accepted(connection):
    from models import MarketplaceAccountEvent
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects.sqlite import dialect
    connection.execute(str(CreateTable(MarketplaceAccountEvent.__table__).compile(dialect=dialect())))
    assert migration.apply_migration(connection, verbose=False) == 1
    assert migration.apply_migration(connection, verbose=False) == 0


def test_unknown_schema_fails_without_rebuild_or_partial_index(connection):
    connection.execute('CREATE TABLE marketplace_account_events(id INTEGER PRIMARY KEY)')
    before = connection.execute('SELECT * FROM sqlite_master').fetchall()
    with pytest.raises(sqlite3.OperationalError):
        migration.apply_migration(connection, verbose=False)
    assert connection.execute('SELECT * FROM sqlite_master').fetchall() == before


def test_prerequisites_missing_creates_nothing():
    with sqlite3.connect(':memory:') as c:
        with pytest.raises(sqlite3.OperationalError, match='prerequisite'):
            migration.apply_migration(c, verbose=False)
        assert c.execute('SELECT * FROM sqlite_master').fetchall() == []


def test_legacy_orphan_preserved_managed_orphan_fails(connection):
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE legacy(id INTEGER REFERENCES parent(id)); INSERT INTO legacy VALUES(99);')
    migration.apply_migration(connection, verbose=False)
    insert(connection, account_id=99)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match='foreign-key safety'):
        migration.apply_migration(connection, verbose=False)
    assert connection.execute('SELECT * FROM legacy').fetchall() == [(99,)]
    assert connection.execute('SELECT account_id FROM marketplace_account_events').fetchall() == [(99,)]


def test_interrupted_safety_check_rolls_back_ddl(connection, monkeypatch):
    before = connection.execute('SELECT * FROM sqlite_master').fetchall()
    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError('synthetic interruption')
    monkeypatch.setattr(migration, 'assert_foreign_key_safety', fail)
    with pytest.raises(sqlite3.IntegrityError):
        migration.apply_migration(connection, verbose=False)
    assert connection.execute('SELECT * FROM sqlite_master').fetchall() == before
