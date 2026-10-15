"""
database.py + schema.sql: the PostgreSQL plumbing every other DB test builds on.
"""
import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

with mock.patch('requests.post'), mock.patch('requests.get'):
    import pipeline

from tests.dbutil import reset_tables

EXPECTED_TABLES = {'users', 'groups', 'group_permissions', 'audit_log', 'network_favorites',
                   'network_destinations', 'trailers', 'jobs', 'chat_messages', 'show_templates'}


def _columns(table):
    conn = database.connect()
    try:
        rows = conn.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = %s', (table,)).fetchall()
    finally:
        conn.close()
    return {r['column_name'] for r in rows}


def test_schema_creates_every_table_and_is_safe_to_run_twice():
    database.init_schema()
    ddl = open(database._SCHEMA_FILE, encoding='utf-8').read()
    conn = database.connect()
    try:
        conn.execute(ddl)          # a second run over existing tables must not fail
        conn.commit()
        have = {r['table_name'] for r in conn.execute(
            'SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()').fetchall()}
    finally:
        conn.close()
    assert EXPECTED_TABLES <= have


def test_show_templates_has_a_file_and_name_column_for_every_slot():
    """The slot columns are written out in schema.sql, so adding a slot in
    pipeline.py without adding them there must fail here, not at runtime."""
    cols = _columns('show_templates')
    for slot in pipeline.TEMPLATE_SLOT_KEYS:
        assert f'{slot}_file' in cols and f'{slot}_name' in cols, slot
    for field in pipeline.TEMPLATE_SLOT_KEYS:
        assert field  # slots are never blank
    # and nothing the save route writes is missing
    for c in ('name', 'notes', 'genre', 'transition', 'xfade_dur', 'settings_json', 'vo_start', 'vo_volume',
              'vo_trim_start', 'vo_trim_end', 'title_card_vo_start', 'title_card_vo_end',
              'end_card_vo_start', 'end_card_vo_end', 'created_at', 'updated_at'):
        assert c in cols, c


def test_rows_are_dicts_and_a_unique_violation_is_an_integrity_error():
    reset_tables('groups')
    conn = database.connect()
    try:
        row = conn.execute('INSERT INTO groups (name, created_at) VALUES (%s, %s) RETURNING id, name',
                           ('ops', 1.0)).fetchone()
        assert row['name'] == 'ops' and dict(row)['id'] == row['id']
        conn.commit()
        with pytest.raises(database.IntegrityError):
            conn.execute('INSERT INTO groups (name, created_at) VALUES (%s, %s)', ('ops', 2.0))
    finally:
        conn.close()
    # The failed statement poisoned that transaction; close() must have rolled
    # it back so the pooled connection is usable again.
    conn = database.connect()
    try:
        assert conn.execute('SELECT COUNT(*) AS c FROM groups').fetchone()['c'] == 1
    finally:
        conn.close()


def test_an_uncommitted_write_is_discarded_on_close():
    reset_tables('groups')
    conn = database.connect()
    conn.execute('INSERT INTO groups (name, created_at) VALUES (%s, %s)', ('ghost', 1.0))
    conn.close()
    conn = database.connect()
    try:
        assert conn.execute('SELECT COUNT(*) AS c FROM groups').fetchone()['c'] == 0
    finally:
        conn.close()


def test_epoch_timestamps_keep_sub_second_precision():
    """REAL would round a 2026 epoch to ~2 minutes; the schema must use
    DOUBLE PRECISION."""
    reset_tables('audit_log')
    import time, library_db
    t0 = time.time()
    library_db.audit_log('precision_check')
    stored = library_db.audit_log_list()[0]['created_at']
    assert abs(stored - t0) < 1.0


def test_ids_can_be_inserted_explicitly_and_the_sequence_still_works():
    """What lets the SQLite migration keep original ids."""
    reset_tables('groups')
    conn = database.connect()
    try:
        conn.execute('INSERT INTO groups (id, name, created_at) VALUES (41, %s, 1.0)', ('old',))
        conn.execute("SELECT setval(pg_get_serial_sequence('groups', 'id'), (SELECT MAX(id) FROM groups))")
        new_id = conn.execute('INSERT INTO groups (name, created_at) VALUES (%s, 2.0) RETURNING id', ('new',)).fetchone()['id']
        conn.commit()
    finally:
        conn.close()
    assert new_id == 42


def test_an_unreachable_database_gives_a_clear_error_that_never_leaks_the_password(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://prism:hunter2secret@127.0.0.1:1/prism')
    monkeypatch.setenv('DB_CONNECT_TIMEOUT', '2')
    database.close_pool()
    try:
        with pytest.raises(database.DatabaseError) as exc:
            database.connect()
    finally:
        database.close_pool()          # let the next test build a pool from the restored URL
    msg = str(exc.value)
    assert 'Cannot connect to PostgreSQL' in msg and '127.0.0.1:1/prism' in msg
    assert 'hunter2secret' not in msg


def test_no_configuration_at_all_says_what_to_set(monkeypatch):
    for k in ('DATABASE_URL', 'PGDATABASE', 'PGSERVICE'):
        monkeypatch.delenv(k, raising=False)
    database.close_pool()
    try:
        with pytest.raises(database.DatabaseError, match='DATABASE_URL'):
            database.connect()
    finally:
        database.close_pool()
