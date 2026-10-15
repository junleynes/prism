"""Helpers for tests that touch PostgreSQL.

The suite runs against a real database (TEST_DATABASE_URL, see conftest.py)
rather than mocks, because the code under test is mostly SQL. Each test that
needs a clean slate empties the tables it uses; the schema itself is left in
place."""
import database

LIBRARY_TABLES = ('audit_log', 'network_favorites', 'network_destinations', 'trailers')
USER_TABLES = ('users', 'groups', 'group_permissions')


def reset_tables(*tables):
    """Create the schema if needed, then empty `tables` and restart their id
    counters so ids start at 1 again."""
    database.init_schema()
    conn = database.connect()
    try:
        conn.execute('TRUNCATE ' + ', '.join(tables) + ' RESTART IDENTITY')
        conn.commit()
    finally:
        conn.close()
