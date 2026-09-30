"""Same per-step semantics and full FK gates under a shared SQLite connection."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import pytest
from migrations import run_scoped_batch as batch
from migrations import _foreign_key_safety as safety

SCRIPTS=['migrations/migrate_add_marketplace_accounts.py','migrations/migrate_add_ozon_references.py']


def test_steps_run_once_in_order_and_share_only_unchanged_fk_evidence(tmp_path,monkeypatch):
    path=tmp_path/'migration.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE data(id INTEGER PRIMARY KEY)')
    calls=[];traces=[]
    def first(c,**kwargs):
        calls.append(('first',id(c),c.execute('PRAGMA foreign_keys').fetchone()[0]));c.set_trace_callback(traces.append)
        safety.foreign_key_snapshot(c);c.row_factory=sqlite3.Row;c.text_factory=bytes
    def second(c,**kwargs):
        assert c.row_factory is None and c.text_factory is str
        calls.append(('second',id(c),c.execute('PRAGMA foreign_keys').fetchone()[0]));safety.foreign_key_snapshot(c)
    methods=iter([first,second]);monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=next(methods)))
    batch.run_batch(path,SCRIPTS,verbose=False)
    assert [row[0] for row in calls]==['first','second'] and calls[0][1]==calls[1][1]
    assert traces.count('PRAGMA foreign_key_check')==1
    assert safety._scan_scope.get() is None


def test_each_step_commits_and_failure_rolls_back_only_current_dml(tmp_path,monkeypatch):
    path=tmp_path/'migration.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE data(id INTEGER PRIMARY KEY)')
    called=[]
    def first(c,**kwargs):c.execute('INSERT INTO data VALUES(1)')
    def failed(c,**kwargs):
        c.execute('INSERT INTO data VALUES(2)');raise sqlite3.IntegrityError('synthetic invalid data')
    def never(c,**kwargs):called.append(True)
    methods=iter([first,failed,never]);monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=next(methods)))
    with pytest.raises(sqlite3.IntegrityError):batch.run_batch(path,SCRIPTS+['migrations/migrate_add_marketplace_finance.py'],verbose=False)
    with sqlite3.connect(path) as c:assert c.execute('SELECT * FROM data').fetchall()==[(1,)]
    assert not called and safety._scan_scope.get() is None


def test_profiles_preserve_foreign_keys_and_bounded_listing_backfill(tmp_path,monkeypatch):
    path=tmp_path/'migration.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE data(id INTEGER PRIMARY KEY)')
    seen=[]
    def step(c,**kw):seen.append((c.execute('PRAGMA foreign_keys').fetchone()[0],kw))
    monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=step))
    batch.run_batch(path,[SCRIPTS[0],'migrations/migrate_add_marketplace_finance.py',batch.LISTINGS],verbose=False)
    assert seen==[(0,{'verbose':False}),(1,{'verbose':False}),(0,{'verbose':False,'backfill_limit':200})]


def test_rejects_unsupported_steps_and_missing_database_before_any_execution(tmp_path,monkeypatch):
    path=tmp_path/'missing.db';calls=[]
    monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=lambda *a,**kw:calls.append(True)))
    for scripts in [[],SCRIPTS*2,['migrations/migrate_add_enrichment_inference.py'],['../services/secret.py']]:
        with pytest.raises(ValueError):batch.run_batch(path,scripts,verbose=False)
    with pytest.raises(sqlite3.OperationalError):batch.run_batch(path,SCRIPTS,verbose=False)
    assert not path.exists() and not calls


def test_connection_closed_and_scope_reset_after_failure(tmp_path,monkeypatch):
    path=tmp_path/'migration.db';sqlite3.connect(path).close();connections=[]
    def step(c,**kwargs):connections.append(c);raise RuntimeError('interrupted step')
    monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=step))
    with pytest.raises(RuntimeError):batch.run_batch(path,SCRIPTS,verbose=False)
    with pytest.raises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')
    assert safety._scan_scope.get() is None


@pytest.mark.parametrize('domain',['references','observations'])
def test_real_fresh_and_repeated_batch_matches_standalone_schema_and_keeps_history(tmp_path,domain):
    from importlib import import_module
    from tests import test_marketplace_quality_analytics_migration as analytics_schema
    if domain=='references':
        scripts=SCRIPTS+['migrations/migrate_add_ozon_product_type_visibility.py','migrations/migrate_add_ozon_reference_reviews.py']
    else:
        scripts=['migrations/migrate_add_marketplace_'+name+'.py' for name in ['quality_analytics','fulfillment','finance','inbox']]
    paths=[tmp_path/(name+'.db') for name in ['standalone','batch']]
    for path in paths:
        with sqlite3.connect(path) as c:
            if domain=='references':c.executescript('CREATE TABLE sellers(id INTEGER PRIMARY KEY);CREATE TABLE users(id INTEGER PRIMARY KEY);')
            else:
                analytics_schema._prerequisite_schema(c)
                c.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
            c.executescript("CREATE TABLE legacy_parent(id INTEGER PRIMARY KEY); CREATE TABLE legacy_history(id INTEGER PRIMARY KEY,parent_id REFERENCES legacy_parent(id), payload TEXT); INSERT INTO legacy_history VALUES(1,404,'retain historical evidence');")
    for script in scripts:
        module=import_module(script[:-3].replace('/','.'));module.migrate(str(paths[0]))
    batch.run_batch(paths[1],scripts,verbose=False)
    for path in paths:
        with sqlite3.connect(path) as c:c.execute("INSERT INTO legacy_history VALUES(2,405,'new unrelated history')")
    for script in scripts:import_module(script[:-3].replace('/','.')).migrate(str(paths[0]))
    batch.run_batch(paths[1],scripts,verbose=False)
    def state(path):
        with sqlite3.connect(path) as c:
            return (c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),c.execute('PRAGMA foreign_key_check').fetchall(),c.execute('SELECT * FROM legacy_history ORDER BY id').fetchall())
    assert state(paths[0])==state(paths[1])


def test_standalone_command_keeps_its_connection_and_invalidates_shared_scan(tmp_path,monkeypatch):
    path=tmp_path/'mixed.db'
    with sqlite3.connect(path) as c:c.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY);CREATE TABLE child(parent_id REFERENCES parent(id));INSERT INTO child VALUES(404);')
    handles=[];snapshots=[];commands=[]
    def first(c,**kw):handles.append(c);snapshots.append(safety.foreign_key_snapshot(c))
    def last(c,**kw):snapshots.append(safety.foreign_key_snapshot(c))
    def own_command(command,**kw):
        assert not handles[0].in_transaction
        assert command[0]==batch.sys.executable and Path(command[-1])==path
        assert Path(command[1]).name=='migrate_add_marketplace_auto_publish.py' and kw['check'] is True
        with sqlite3.connect(path,timeout=.1) as writer:writer.execute('INSERT INTO parent VALUES(404)')
        commands.append(command)
    steps=iter([first,last]);monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=next(steps)))
    monkeypatch.setattr(batch.subprocess,'run',own_command)
    batch.run_batch(path,[SCRIPTS[0],'migrations/migrate_add_marketplace_auto_publish.py',SCRIPTS[1]],verbose=False)
    assert len(commands)==1 and snapshots[0] and not snapshots[1] and snapshots[0] is not snapshots[1]


def test_failed_standalone_command_stops_sequence_and_preserves_previous_commit(tmp_path,monkeypatch):
    path=tmp_path/'mixed-failure.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE data(id INTEGER PRIMARY KEY)')
    calls=[]
    def first(c,**kw):c.execute('INSERT INTO data VALUES(1)')
    def last(c,**kw):calls.append(True)
    steps=iter([first,last]);monkeypatch.setattr(batch,'import_module',lambda name:SimpleNamespace(apply_migration=next(steps)))
    def failed(*a,**kw):raise batch.subprocess.CalledProcessError(2,['synthetic migration'])
    monkeypatch.setattr(batch.subprocess,'run',failed)
    with pytest.raises(batch.subprocess.CalledProcessError):batch.run_batch(path,[SCRIPTS[0],'migrations/migrate_add_marketplace_auto_publish.py',SCRIPTS[1]],verbose=False)
    with sqlite3.connect(path) as c:assert c.execute('SELECT * FROM data').fetchall()==[(1,)]
    assert not calls and safety._scan_scope.get() is None
