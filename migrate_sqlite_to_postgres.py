#!/usr/bin/env python3
"""Copy an existing SQLite-based PRISM installation into PostgreSQL.

Run once, on the machine that holds the old data, AFTER setting DATABASE_URL
(see docs/POSTGRESQL.md) and BEFORE the first start of the PostgreSQL build:

    python migrate_sqlite_to_postgres.py --dry-run      # count only, change nothing
    python migrate_sqlite_to_postgres.py                # copy

It reads these files, read-only (they are never modified or deleted):

    <LIBRARY_DIR>/users.db          users, groups, group_permissions
    <LIBRARY_DIR>/library.db        audit_log, network_favorites,
                                    network_destinations, trailers
    <LIBRARY_DIR>/chat_memory.db    chat_messages
    <TEMPLATES_DIR>/templates.db    show_templates

Row ids are preserved (so trailer links, ownership and group membership keep
pointing at the right rows) and each id sequence is moved past the highest
copied id. The copy is one transaction: it all lands or none of it does.

jobs.db is deliberately not migrated -- jobs are transient (finished ones are
purged after an hour, unfinished ones are marked interrupted at startup anyway).

LIBRARY_DIR / TEMPLATES_DIR come from the environment or .env, exactly as the
app reads them; --library-dir / --templates-dir override them.

If the target tables already hold rows (for example the app was started once
and created its default admin), the script stops. Pass --replace to empty the
tables it is about to load first.
"""
import argparse
import os
import pathlib
import sqlite3
import sys

import database

HERE = os.path.dirname(os.path.abspath(__file__))

# (sqlite file, which directory it lives in, tables to copy from it)
SOURCES = [
    ('users.db',       'library',   ['users', 'groups', 'group_permissions']),
    ('library.db',     'library',   ['audit_log', 'network_favorites', 'network_destinations', 'trailers']),
    ('chat_memory.db', 'library',   ['chat_messages']),
    ('templates.db',   'templates', ['show_templates']),
]
# Tables whose id comes from an identity sequence that must be advanced.
IDENTITY_TABLES = {'users', 'groups', 'audit_log', 'network_favorites',
                   'network_destinations', 'trailers', 'chat_messages', 'show_templates'}
BATCH = 500


class MigrationError(RuntimeError):
    pass


def _sqlite_readonly(path):
    uri = pathlib.Path(path).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


def _pg_columns(conn, table):
    rows = conn.execute(
        'SELECT column_name FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position',
        (table,)).fetchall()
    return [r['column_name'] for r in rows]


def _clean(value, counter):
    # PostgreSQL text cannot hold NUL; SQLite could. Drop it rather than fail.
    if isinstance(value, str) and '\x00' in value:
        counter[0] += 1
        return value.replace('\x00', '')
    return value


def migrate(library_dir, templates_dir, replace=False, dry_run=False, out=print):
    """Returns {table: rows_copied}. Raises MigrationError for anything the
    user must fix; nothing is committed in that case."""
    dirs = {'library': library_dir, 'templates': templates_dir}
    database.init_schema()
    conn = database.connect()
    opened = []
    copied = {}
    nul_fixes = [0]
    try:
        # ---- plan: which tables exist in which files ----
        plan = []   # (table, sqlite_conn, columns)
        for fname, where, tables in SOURCES:
            path = os.path.join(dirs[where], fname)
            if not os.path.exists(path):
                out(f'  skip   {fname}: not found in {dirs[where]}')
                continue
            src = _sqlite_readonly(path)
            opened.append(src)
            have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in tables:
                if table not in have:
                    out(f'  skip   {fname}: no {table} table')
                    continue
                sqlite_cols = [r[1] for r in src.execute(f'PRAGMA table_info({table})')]
                pg_cols = _pg_columns(conn, table)
                cols = [c for c in sqlite_cols if c in pg_cols]
                dropped = [c for c in sqlite_cols if c not in pg_cols]
                if dropped:
                    out(f'  note   {table}: columns not in the PostgreSQL schema are not copied: {", ".join(dropped)}')
                plan.append((table, src, cols))
        if not plan:
            raise MigrationError(
                f'No SQLite databases found in {dirs["library"]} / {dirs["templates"]}. '
                'Point --library-dir / --templates-dir at the old installation.')

        # ---- target must be empty (or --replace) ----
        non_empty = []
        for table, _, _ in plan:
            n = conn.execute(f'SELECT COUNT(*) AS c FROM {table}').fetchone()['c']
            if n:
                non_empty.append(f'{table} ({n} rows)')
        if non_empty and not replace:
            raise MigrationError(
                'These PostgreSQL tables already contain data: ' + ', '.join(non_empty) + '.\n'
                'Run again with --replace to empty them first, or point DATABASE_URL at an empty database.')
        if non_empty:
            emptied = [t for t, _, _ in plan] + (['user_groups'] if any(t == 'users' for t, _, _ in plan) else [])
            conn.execute('TRUNCATE ' + ', '.join(emptied) + ' RESTART IDENTITY')

        # ---- copy ----
        for table, src, cols in plan:
            col_list = ', '.join(cols)
            insert = f'INSERT INTO {table} ({col_list}) VALUES ({", ".join(["%s"] * len(cols))})'
            try:
                order = ' ORDER BY rowid'
                cur = src.execute(f'SELECT {col_list} FROM {table}{order}')
            except sqlite3.OperationalError:
                cur = src.execute(f'SELECT {col_list} FROM {table}')
            n = 0
            while True:
                batch = cur.fetchmany(BATCH)
                if not batch:
                    break
                rows = [tuple(_clean(v, nul_fixes) for v in r) for r in batch]
                try:
                    conn.executemany(insert, rows)
                except database.Error as e:
                    raise MigrationError(f'{table}: {str(e).strip()}') from e
                n += len(rows)
            copied[table] = n
            if table in IDENTITY_TABLES and n:
                conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f'(SELECT MAX(id) FROM {table}))')

        # Old installs kept one group per account in users.group_id; accounts can
        # now be in several (user_groups). Same move schema.sql makes on start-up.
        if copied.get('users'):
            conn.execute('INSERT INTO user_groups (user_id, group_id) '
                         'SELECT u.id, u.group_id FROM users u JOIN groups g ON g.id = u.group_id '
                         'ON CONFLICT DO NOTHING')
            conn.execute('UPDATE users SET group_id = NULL WHERE group_id IS NOT NULL')

        # ---- verify against the source before committing ----
        for table, src, _ in plan:
            expected = src.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            actual = conn.execute(f'SELECT COUNT(*) AS c FROM {table}').fetchone()['c']
            if expected != actual:
                raise MigrationError(f'{table}: SQLite has {expected} rows but PostgreSQL has {actual} after the copy.')
            out(f'  {"count " if dry_run else "copied"} {table}: {actual} rows')
        if nul_fixes[0]:
            out(f'  note   removed NUL characters from {nul_fixes[0]} text value(s) (PostgreSQL cannot store them)')

        if dry_run:
            conn.rollback()
            out('Dry run: nothing was written.')
        else:
            conn.commit()
            out('Done. The old .db files were left untouched; keep them until you have checked the app.')
        return copied
    finally:
        conn.close()
        for s in opened:
            s.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Copy a SQLite-based PRISM install into PostgreSQL.')
    ap.add_argument('--library-dir', help='folder holding users.db, library.db, chat_memory.db '
                                          '(default: LIBRARY_DIR, else ./trailer_library)')
    ap.add_argument('--templates-dir', help='folder holding templates.db '
                                            '(default: TEMPLATES_DIR, else ./show_templates)')
    ap.add_argument('--replace', action='store_true', help='empty the target tables first if they already hold rows')
    ap.add_argument('--dry-run', action='store_true', help='copy inside a transaction, report counts, then roll back')
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, '.env'))
    except ImportError:
        pass
    library_dir = args.library_dir or os.environ.get('LIBRARY_DIR') or os.path.join(HERE, 'trailer_library')
    templates_dir = args.templates_dir or os.environ.get('TEMPLATES_DIR') or os.path.join(HERE, 'show_templates')

    print(f'Library:   {library_dir}\nTemplates: {templates_dir}')
    try:
        migrate(library_dir, templates_dir, replace=args.replace, dry_run=args.dry_run)
    except (MigrationError, database.DatabaseError) as e:
        print(f'\nERROR: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
