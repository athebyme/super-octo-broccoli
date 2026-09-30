import sqlite3
import pytest
from migrations import migrate_add_marketplace_credential_notices as migration


@pytest.fixture
def connection():
    c=sqlite3.connect(':memory:');c.execute('PRAGMA foreign_keys=ON')
    c.executescript('''CREATE TABLE sellers(id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces(id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts(id INTEGER PRIMARY KEY,seller_id INTEGER,marketplace_id INTEGER,is_active BOOLEAN,credential_expires_at DATETIME);
        INSERT INTO sellers VALUES(1); INSERT INTO marketplaces VALUES(1);
        INSERT INTO seller_marketplace_accounts VALUES(1,1,1,1,'2026-10-01');''')
    yield c
    c.close()


def test_additive_repeat_preserves_other_data_and_dedup(connection):
    assert migration.apply_migration(connection,verbose=False)==3
    connection.execute("INSERT INTO marketplace_credential_notices VALUES(1,1,1,2,'2026-10-01',2,'2026-09-26')")
    connection.commit()
    before=connection.execute('SELECT * FROM marketplace_credential_notices').fetchall()
    assert migration.apply_migration(connection,verbose=False)==0
    assert connection.execute('SELECT * FROM marketplace_credential_notices').fetchall()==before
    assert connection.execute('SELECT credential_expires_at FROM seller_marketplace_accounts').fetchone()==('2026-10-01',)
    assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]


@pytest.mark.parametrize('version,stage,account', [(0,1,1),(1,0,1),(1,5,1),(1,1,99)])
def test_database_constraints(connection,version,stage,account):
    migration.apply_migration(connection,verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('INSERT INTO marketplace_credential_notices VALUES(?,1,1,?,\'2026-10-01\',?,\'2026-09-26\')',(account,version,stage))


def test_orm_schema_matches_migration(connection):
    from models import MarketplaceCredentialNotice
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects.sqlite import dialect
    connection.execute(str(CreateTable(MarketplaceCredentialNotice.__table__).compile(dialect=dialect())))
    assert migration.apply_migration(connection,verbose=False)==2
    assert migration.apply_migration(connection,verbose=False)==0


def test_unknown_existing_schema_not_rebuilt_and_created_indexes_rolled_back(connection):
    connection.execute('CREATE TABLE marketplace_credential_notices(account_id INTEGER PRIMARY KEY)')
    before=connection.execute('SELECT * FROM sqlite_master').fetchall()
    with pytest.raises(sqlite3.OperationalError):migration.apply_migration(connection,verbose=False)
    assert connection.execute('SELECT * FROM sqlite_master').fetchall()==before


def test_prerequisites_missing_creates_nothing():
    with sqlite3.connect(':memory:') as c:
        with pytest.raises(sqlite3.OperationalError,match='prerequisites'):migration.apply_migration(c,verbose=False)
        assert c.execute('SELECT * FROM sqlite_master').fetchall()==[]


def test_legacy_unrelated_orphan_preserved_but_managed_orphan_rejected(connection):
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE old(id INTEGER REFERENCES parent(id)); INSERT INTO old VALUES(42);')
    assert migration.apply_migration(connection,verbose=False)==3
    connection.execute("INSERT INTO marketplace_credential_notices VALUES(99,1,1,1,'2026-10-01',1,'2026-09-26')")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError,match='foreign-key safety'):migration.apply_migration(connection,verbose=False)
    assert connection.execute('SELECT * FROM old').fetchall()==[(42,)]
    assert connection.execute('SELECT account_id FROM marketplace_credential_notices').fetchall()==[(99,)]


def test_interruption_at_safety_check_rolls_back_all_new_schema(connection,monkeypatch):
    before=connection.execute('SELECT * FROM sqlite_master').fetchall()
    def fail(*a,**kw):raise sqlite3.IntegrityError('synthetic')
    monkeypatch.setattr(migration,'assert_foreign_key_safety',fail)
    with pytest.raises(sqlite3.IntegrityError):migration.apply_migration(connection,verbose=False)
    assert connection.execute('SELECT * FROM sqlite_master').fetchall()==before
