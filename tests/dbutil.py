"""Helpers for tests that touch PostgreSQL.

The suite runs against a real database (TEST_DATABASE_URL, see conftest.py)
rather than mocks, because the code under test is mostly SQL. Each test that
needs a clean slate empties the tables it uses; the schema itself is left in
place."""
import database

LIBRARY_TABLES = ('audit_log', 'network_favorites', 'network_destinations', 'trailers')
USER_TABLES = ('users', 'groups', 'group_permissions', 'user_groups')


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


def ungrouped_user_gets_every_tab(user_id, username):
    """Tests sign in as made-up non-admin accounts (user 7 'ana', ...). An
    account in no group may only open the Dashboard and Docs, so such an
    account is created if missing and, when it is in no group, put in a group
    granting every tab -- what these tests assumed before accounts needed a
    group. Accounts a test already put in a group are left alone, so tests
    about restricted access still see the restriction."""
    import auth
    database.init_schema()
    conn = database.connect()
    try:
        conn.execute("INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                     "VALUES (%s, %s, 'x', 'user', 1, 0) ON CONFLICT DO NOTHING", (user_id, username))
        conn.execute("SELECT setval(pg_get_serial_sequence('users', 'id'), "
                     "GREATEST((SELECT MAX(id) FROM users), 1))")
        if conn.execute('SELECT 1 FROM user_groups WHERE user_id=%s', (user_id,)).fetchone():
            conn.commit()
            return
        row = conn.execute("SELECT id FROM groups WHERE name='All tabs (tests)'").fetchone()
        gid = row['id'] if row else conn.execute(
            "INSERT INTO groups (name, created_at) VALUES ('All tabs (tests)', 0) RETURNING id").fetchone()['id']
        conn.executemany('INSERT INTO group_permissions (group_id, permission) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                         [(gid, k) for k in sorted(auth._PERMISSION_KEYS)])
        conn.execute('INSERT INTO user_groups (user_id, group_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user_id, gid))
        conn.commit()
    finally:
        conn.close()
