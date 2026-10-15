"""PostgreSQL access for PRISM.

Accounts, the trailer library, the audit log, background jobs, AI chat memory
and show templates all live in one PostgreSQL database. This module is the one
place that knows how to reach it: a lazily-created connection pool, a small
sqlite-shaped handle (connect()), and the schema bootstrap (init_schema()).

Configuration (environment / .env):

    DATABASE_URL          postgresql://user:password@host:5432/dbname
                          (or the libpq variables PGHOST, PGPORT, PGDATABASE,
                          PGUSER, PGPASSWORD, PGSSLMODE, ... if you prefer)
    DB_POOL_MAX           max pooled connections            (default 20)
    DB_POOL_MIN           connections kept open when idle   (default 1)
    DB_CONNECT_TIMEOUT    seconds to reach the server       (default 10)
    DB_POOL_TIMEOUT       seconds to wait for a free conn   (default 30)
    DB_AUTO_SCHEMA        0 = never run schema.sql at startup (default 1)

Check the settings without starting the app:    python database.py

Usage -- same shape the code had with sqlite3, so call sites stay simple:

    conn = database.connect()
    row = conn.execute('SELECT * FROM users WHERE id=%s', (uid,)).fetchone()
    conn.commit()      # only after writes
    conn.close()       # returns the connection to the pool; always call it

Rows come back as dicts (row['col'], dict(row)). Placeholders are %s, and a
literal % in a query that also has parameters must be written %%.
"""
import atexit
import os
import sys
import threading

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

try:                       # same .env the app reads (core.py); never overrides a set variable
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Re-exported so callers catch database errors without importing psycopg.
Error = psycopg.Error
IntegrityError = psycopg.IntegrityError          # includes UniqueViolation


class DatabaseError(RuntimeError):
    """The database isn't configured or can't be reached. The message says
    what to fix, and never contains the password."""


def _env_int(name, default):
    try:
        return int(os.environ.get(name, '').strip() or default)
    except ValueError:
        return default


def _conninfo():
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:
        return url
    # No URL: libpq reads PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD itself.
    # Insist on at least a database name, because libpq's own defaults (the OS
    # user name as the database) fail later with a baffling error.
    if os.environ.get('PGDATABASE', '').strip() or os.environ.get('PGSERVICE', '').strip():
        return ''
    raise DatabaseError(
        'No database configured. Set DATABASE_URL in .env, for example:\n'
        '    DATABASE_URL=postgresql://prism:YOUR_PASSWORD@localhost:5432/prism\n'
        '(or set PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD). '
        'See docs/POSTGRESQL.md.')


def _describe(conninfo):
    """user@host:port/dbname for error messages -- never the password."""
    try:
        d = conninfo_to_dict(conninfo) if conninfo else {}
    except psycopg.ProgrammingError:
        return 'the server in DATABASE_URL (which could not be parsed -- check its format)'
    host = d.get('host') or os.environ.get('PGHOST') or 'localhost'
    port = d.get('port') or os.environ.get('PGPORT') or '5432'
    name = d.get('dbname') or os.environ.get('PGDATABASE') or '?'
    user = d.get('user') or os.environ.get('PGUSER') or '?'
    return f'{user}@{host}:{port}/{name}'


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        conninfo = _conninfo()
        connect_timeout = _env_int('DB_CONNECT_TIMEOUT', 10)
        kwargs = {'row_factory': dict_row, 'connect_timeout': connect_timeout,
                  'application_name': 'prism'}
        # Probe with a plain connection first: the pool retries failures in the
        # background and would only ever report "timed out", hiding the real
        # reason (wrong password, no pg_hba.conf entry, database missing...).
        try:
            psycopg.connect(conninfo, **kwargs).close()
        except psycopg.Error as e:
            raise DatabaseError(
                f'Cannot connect to PostgreSQL as {_describe(conninfo)}:\n'
                f'    {str(e).strip()}\n'
                'Check DATABASE_URL in .env, that the server is running, and '
                'that pg_hba.conf allows this host. See docs/POSTGRESQL.md.') from e
        pool = ConnectionPool(
            conninfo, kwargs=kwargs, open=False, name='prism',
            min_size=max(1, _env_int('DB_POOL_MIN', 1)),
            max_size=max(1, _env_int('DB_POOL_MAX', 20)),
            timeout=_env_int('DB_POOL_TIMEOUT', 30),
            # A server restart or dropped idle connection is detected at
            # checkout and replaced, instead of surfacing as a failed request.
            check=ConnectionPool.check_connection)
        pool.open()
        _pool = pool
        return _pool


def close_pool():
    """Close every pooled connection. Registered to run at exit."""
    global _pool, _schema_done
    with _pool_lock:
        pool, _pool = _pool, None
        _schema_done = False
    if pool is not None:
        pool.close()


atexit.register(close_pool)


class _Conn:
    """One pooled connection with the small surface the app uses:
    execute / executemany / commit / rollback / close.

    Transactions are explicit: nothing is saved until commit(). close() rolls
    back anything uncommitted (which is also what clears a transaction left
    aborted by a constraint error) and returns the connection to the pool."""
    __slots__ = ('_pool', '_conn')

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn

    def execute(self, sql, params=None):
        return self._conn.execute(sql, params)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        conn, self._conn = self._conn, None
        if conn is None:                      # closing twice is harmless
            return
        try:
            conn.rollback()
        finally:
            self._pool.putconn(conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect():
    """A connection from the pool. Always close() it (or use `with`)."""
    pool = _get_pool()
    try:
        return _Conn(pool, pool.getconn())
    except PoolTimeout as e:
        raise DatabaseError(
            'No free PostgreSQL connection after '
            f'{_env_int("DB_POOL_TIMEOUT", 30)}s -- the pool (DB_POOL_MAX) is '
            'exhausted or the server stopped answering.') from e


# ---- Schema ----
_SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')
_SCHEMA_LOCK_KEY = 0x5052534D          # "PRSM": serialises concurrent first starts
_schema_done = False
_schema_lock = threading.Lock()


def init_schema():
    """Create any missing tables (schema.sql), once per process. Safe to call
    from every module's init. With DB_AUTO_SCHEMA=0 only checks that the
    database is reachable, for sites where a DBA applies schema.sql."""
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        auto = os.environ.get('DB_AUTO_SCHEMA', '1').strip().lower() not in ('0', 'false', 'no')
        with connect() as conn:
            if auto:
                with open(_SCHEMA_FILE, encoding='utf-8') as f:
                    ddl = f.read()
                # The advisory lock makes two processes starting together take
                # turns; unguarded CREATE TABLE IF NOT EXISTS can race.
                conn.execute('SELECT pg_advisory_xact_lock(%s)', (_SCHEMA_LOCK_KEY,))
                conn.execute(ddl)
                conn.commit()
        _schema_done = True


if __name__ == '__main__':
    # `python database.py`: is DATABASE_URL right, and can this machine reach it?
    try:
        with connect() as _c:
            _row = _c.execute('SELECT current_database() AS db, current_user AS usr, '
                              'version() AS v').fetchone()
    except DatabaseError as _e:
        print(f'FAILED: {_e}', file=sys.stderr)
        sys.exit(1)
    print(f'OK: connected to database "{_row["db"]}" as "{_row["usr"]}"\n{_row["v"]}')
    init_schema()
    print('Schema is in place.' if os.environ.get('DB_AUTO_SCHEMA', '1').strip().lower() not in ('0', 'false', 'no')
          else 'DB_AUTO_SCHEMA=0: schema.sql was not applied by this check.')
