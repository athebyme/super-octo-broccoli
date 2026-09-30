"""Full FK evidence can be reused only across verified no-op migration steps."""
import sqlite3
from unittest.mock import patch
import pytest
from migrations import _foreign_key_safety as safety


def connection():
    c=sqlite3.connect(':memory:')
    c.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id REFERENCES parent(id)); INSERT INTO child VALUES(1,404);')
    return c


def scans(statements):return statements.count('PRAGMA foreign_key_check')


def test_explicit_scope_reuses_noop_but_each_domain_still_rejects_its_orphans():
    c=connection();sql=[];c.set_trace_callback(sql.append)
    try:
        with safety.reuse_foreign_key_scans(c):
            first=safety.foreign_key_snapshot(c)
            c.execute('CREATE TABLE IF NOT EXISTS parent(id INTEGER PRIMARY KEY)');c.commit()
            second=safety.foreign_key_snapshot(c)
            assert second is first and scans(sql)==1
            safety.assert_foreign_key_safety(c,baseline=second,managed_tables={'unrelated'},label='test')
            with pytest.raises(sqlite3.IntegrityError,match='managed=1'):
                safety.assert_foreign_key_safety(c,baseline=second,managed_tables={'parent'},label='test')
        safety.foreign_key_snapshot(c);assert scans(sql)==2
        with safety.reuse_foreign_key_scans(c):
            safety.foreign_key_snapshot(c);assert scans(sql)==3
    finally:c.close()


@pytest.mark.parametrize('change',[
    'INSERT INTO child VALUES(2,405)',
    'CREATE TABLE extra(id INTEGER)',
    'BEGIN',
])
def test_local_dml_ddl_or_transaction_requires_new_scan(change):
    c=connection();sql=[];c.set_trace_callback(sql.append)
    try:
        with safety.reuse_foreign_key_scans(c):
            safety.foreign_key_snapshot(c);c.execute(change)
            safety.foreign_key_snapshot(c);assert scans(sql)==2
    finally:c.close()


def test_external_commit_rechecks_same_connection_and_finds_new_violation(tmp_path):
    path=tmp_path/'foreign.db';c=sqlite3.connect(path);writer=sqlite3.connect(path)
    try:
        c.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY);CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id REFERENCES parent(id));')
        with safety.reuse_foreign_key_scans(c):
            baseline=safety.foreign_key_snapshot(c)
            writer.execute('INSERT INTO child VALUES(1,404)');writer.commit()
            fresh=safety.foreign_key_snapshot(c);assert fresh!=baseline
            with pytest.raises(sqlite3.IntegrityError,match='introduced=1'):
                safety.assert_foreign_key_safety(c,baseline=baseline,managed_tables={'unrelated'},label='test')
    finally:c.close();writer.close()


def test_other_connection_and_nested_scope_cannot_share_cached_evidence():
    c=connection();other=connection();sql=[];other.set_trace_callback(sql.append)
    try:
        with safety.reuse_foreign_key_scans(c):
            first=safety.foreign_key_snapshot(c)
            safety.foreign_key_snapshot(other);safety.foreign_key_snapshot(other);assert scans(sql)==2
            with safety.reuse_foreign_key_scans(other):
                a=safety.foreign_key_snapshot(other);assert safety.foreign_key_snapshot(other) is a
            assert safety.foreign_key_snapshot(c) is first
    finally:c.close();other.close()


def test_failed_scope_drops_evidence_and_unstable_scan_cannot_be_cached():
    c=connection();sql=[];c.set_trace_callback(sql.append)
    try:
        with pytest.raises(RuntimeError):
            with safety.reuse_foreign_key_scans(c):
                safety.foreign_key_snapshot(c);raise RuntimeError('failed step')
        assert safety._scan_scope.get() is None
        with safety.reuse_foreign_key_scans(c):
            original=safety._database_revision
            with patch.object(safety,'_database_revision',side_effect=[('before',),('after',)]):
                unstable=safety.foreign_key_snapshot(c);assert unstable.revision is None
            fresh=safety.foreign_key_snapshot(c);assert fresh is not unstable
            assert fresh.revision==original(c)
        assert scans(sql)==3
    finally:c.close()
