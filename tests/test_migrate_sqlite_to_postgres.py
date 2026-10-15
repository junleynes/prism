"""
migrate_sqlite_to_postgres.py: the one-time copy from the old SQLite files.

The source databases are built with the schemas the SQLite version of the app
actually created -- including the older shapes (no totp/lockout columns, no
trailer owner, no show_templates.settings_json) that sites upgraded through --
so this exercises the real inputs, not a tidied-up copy of them.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import migrate_sqlite_to_postgres as mig
from tests.dbutil import reset_tables, USER_TABLES, LIBRARY_TABLES

ALL = USER_TABLES + LIBRARY_TABLES + ('chat_messages', 'show_templates')


@pytest.fixture
def old_install(tmp_path):
    lib = tmp_path / 'trailer_library'
    tpl = tmp_path / 'show_templates'
    lib.mkdir()
    tpl.mkdir()

    c = sqlite3.connect(lib / 'users.db')
    # the ORIGINAL users table: none of group_id / totp_* / lockout columns yet
    c.execute('CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, '
              'password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT \'user\', is_active INTEGER NOT NULL DEFAULT 1, '
              'created_at REAL NOT NULL, last_login REAL)')
    c.execute('CREATE TABLE groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)')
    c.execute('CREATE TABLE group_permissions (group_id INTEGER NOT NULL, permission TEXT NOT NULL, PRIMARY KEY (group_id, permission))')
    c.executemany('INSERT INTO users (id, username, password_hash, role, is_active, created_at, last_login) VALUES (?,?,?,?,?,?,?)',
                  [(1, 'admin', 'pbkdf2:x', 'admin', 1, 1760000000.25, 1760000500.5),
                   (7, 'ana', 'pbkdf2:y', 'user', 1, 1760000100.0, None)])
    c.execute("INSERT INTO groups VALUES (3, 'Editors', 1760000200.0)")
    c.executemany('INSERT INTO group_permissions VALUES (?,?)', [(3, 'promo_generation'), (3, 'player')])
    c.commit(); c.close()

    c = sqlite3.connect(lib / 'library.db')
    c.execute('CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, user_id INTEGER, '
              'username TEXT, action TEXT NOT NULL, target TEXT, detail TEXT, ip TEXT)')
    c.execute('CREATE TABLE network_favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, '
              'category TEXT NOT NULL, path TEXT NOT NULL, label TEXT, created_at REAL NOT NULL, UNIQUE(user_id, category, path))')
    c.execute('CREATE TABLE network_destinations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, path TEXT NOT NULL, '
              'username TEXT, password TEXT, delivery_kind TEXT NOT NULL DEFAULT \'video\', created_at REAL NOT NULL, updated_at REAL NOT NULL)')
    # the ORIGINAL trailers table: no user_id / username (those came later)
    c.execute('CREATE TABLE trailers (id INTEGER PRIMARY KEY AUTOINCREMENT, orig_name TEXT, filename TEXT NOT NULL, created_at REAL NOT NULL, '
              'trailer_duration REAL, video_duration REAL, trailer_length TEXT, bgm_source TEXT, sfx_source TEXT, vo_source TEXT, result_json TEXT)')
    c.execute("INSERT INTO audit_log VALUES (10, 1760000300.5, 1, 'admin', 'user_create', 'ana', 'role=user', '10.0.0.5')")
    c.execute("INSERT INTO network_favorites VALUES (2, 7, 'hires', '24Oras\\\\2026', 'Oras', 1760000400.0)")
    c.execute("INSERT INTO network_destinations VALUES (5, 'MAM', '\\\\\\\\vantage\\\\in', 'svc', 'pw', 'csv_video', 1.0, 2.0)")
    c.execute("INSERT INTO trailers VALUES (30, 'EP101.mov', 'abc.mp4', 1760000600.75, 30.5, 2700.0, '30s', 'ai', 'ai', NULL, '{\"k\": 1}')")
    c.commit(); c.close()

    c = sqlite3.connect(lib / 'chat_memory.db')
    c.execute('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, role TEXT NOT NULL, '
              'content TEXT NOT NULL, images TEXT, created_at REAL NOT NULL)')
    c.execute("INSERT INTO chat_messages VALUES (4, 7, 'user', ?, NULL, 1760000700.0)", ('hello\x00there',))
    c.commit(); c.close()

    c = sqlite3.connect(tpl / 'templates.db')
    # created before settings_json existed
    c.execute('CREATE TABLE show_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, notes TEXT, '
              'created_at REAL NOT NULL, updated_at REAL NOT NULL, genre TEXT, transition TEXT, xfade_dur REAL, '
              'bgm_file TEXT, bgm_name TEXT)')
    c.execute("INSERT INTO show_templates VALUES (12, '24 Oras', NULL, 1.0, 2.0, 'drama', 'fade', 0.5, 'bgm_x.wav', 'theme.wav')")
    c.commit(); c.close()

    # jobs.db must be ignored
    c = sqlite3.connect(lib / 'jobs.db')
    c.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY)')
    c.execute("INSERT INTO jobs VALUES ('x')")
    c.commit(); c.close()
    return str(lib), str(tpl)


def _quiet(*_a, **_k):
    pass


def _fetch(sql, params=None):
    conn = database.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_migration_keeps_ids_values_and_advances_sequences(old_install):
    lib, tpl = old_install
    reset_tables(*ALL)
    copied = mig.migrate(lib, tpl, out=_quiet)
    assert copied == {'users': 2, 'groups': 1, 'group_permissions': 2, 'audit_log': 1, 'network_favorites': 1,
                      'network_destinations': 1, 'trailers': 1, 'chat_messages': 1, 'show_templates': 1}

    users = {r['id']: r for r in _fetch('SELECT * FROM users')}
    assert users[7]['username'] == 'ana' and users[1]['role'] == 'admin'
    assert users[1]['created_at'] == 1760000000.25 and users[1]['last_login'] == 1760000500.5   # no float truncation
    # columns the old table never had come out as the new defaults
    assert users[7]['group_id'] is None and users[7]['totp_enabled'] == 0 and users[7]['failed_logins'] == 0
    assert {r['permission'] for r in _fetch('SELECT * FROM group_permissions WHERE group_id=3')} == {'promo_generation', 'player'}

    t = _fetch('SELECT * FROM trailers')[0]
    assert t['id'] == 30 and t['user_id'] is None and t['result_json'] == '{"k": 1}'      # pre-ownership rows stay admin-only
    assert _fetch('SELECT * FROM show_templates')[0]['bgm_name'] == 'theme.wav'
    assert _fetch('SELECT * FROM show_templates')[0]['settings_json'] is None
    assert _fetch('SELECT content FROM chat_messages')[0]['content'] == 'hellothere'      # NUL stripped, rest kept

    # sequences moved past the copied ids, so new rows don't collide with them
    conn = database.connect()
    try:
        new_user = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('new','h',1.0) RETURNING id").fetchone()['id']
        new_trailer = conn.execute("INSERT INTO trailers (filename, created_at) VALUES ('n.mp4', 1.0) RETURNING id").fetchone()['id']
        conn.commit()
    finally:
        conn.close()
    assert new_user == 8 and new_trailer == 31


def test_jobs_are_not_migrated(old_install):
    lib, tpl = old_install
    reset_tables(*ALL, 'jobs')
    mig.migrate(lib, tpl, out=_quiet)
    assert _fetch('SELECT COUNT(*) AS c FROM jobs')[0]['c'] == 0


def test_dry_run_reports_but_writes_nothing(old_install):
    lib, tpl = old_install
    reset_tables(*ALL)
    copied = mig.migrate(lib, tpl, dry_run=True, out=_quiet)
    assert copied['users'] == 2
    assert _fetch('SELECT COUNT(*) AS c FROM users')[0]['c'] == 0


def test_it_refuses_to_overwrite_existing_rows_unless_told_to(old_install):
    lib, tpl = old_install
    reset_tables(*ALL)
    conn = database.connect()
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('bootstrap','h',1.0)")
    conn.commit(); conn.close()
    with pytest.raises(mig.MigrationError, match='already contain data'):
        mig.migrate(lib, tpl, out=_quiet)
    assert [r['username'] for r in _fetch('SELECT username FROM users')] == ['bootstrap']   # untouched

    mig.migrate(lib, tpl, replace=True, out=_quiet)
    assert {r['username'] for r in _fetch('SELECT username FROM users')} == {'admin', 'ana'}


def test_a_failure_part_way_leaves_nothing_behind(old_install, tmp_path):
    lib, tpl = old_install
    reset_tables(*ALL)
    # A value PostgreSQL rejects in a later table: all-or-nothing means the
    # users copied before it must not remain.
    c = sqlite3.connect(os.path.join(lib, 'library.db'))
    c.execute("INSERT INTO trailers (id, filename, created_at) VALUES (31, 'bad.mp4', 'not-a-number')")
    c.commit(); c.close()
    with pytest.raises(mig.MigrationError):
        mig.migrate(lib, tpl, out=_quiet)
    for table in ('users', 'trailers'):
        assert _fetch(f'SELECT COUNT(*) AS c FROM {table}')[0]['c'] == 0


def test_a_folder_with_no_databases_is_an_error(tmp_path):
    reset_tables(*ALL)
    with pytest.raises(mig.MigrationError, match='No SQLite databases found'):
        mig.migrate(str(tmp_path), str(tmp_path), out=_quiet)


def test_the_source_files_are_never_modified(old_install):
    lib, tpl = old_install
    reset_tables(*ALL)
    before = {f: os.path.getmtime(os.path.join(lib, f)) for f in os.listdir(lib)}
    sizes = {f: os.path.getsize(os.path.join(lib, f)) for f in os.listdir(lib)}
    mig.migrate(lib, tpl, out=_quiet)
    assert {f: os.path.getmtime(os.path.join(lib, f)) for f in os.listdir(lib)} == before
    assert {f: os.path.getsize(os.path.join(lib, f)) for f in os.listdir(lib)} == sizes
