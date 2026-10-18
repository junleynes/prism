"""Saved settings (Config tab, production defaults, branding, network folders, storage) kept in PostgreSQL.

They used to live only in JSON files next to the code, so replacing or re-deploying the app folder (a fresh clone,
a container rebuild, a different working directory) brought every setting back to its default. The database
survives that. A file that sits in the app folder is the same setting as before: it is read once, copied into the
database, and from then on the database is the source. A path somewhere else (a test's temp file, a file named by
an environment variable) is kept as a plain file, as before.
"""
import json
import os
import threading
import time

import database

APP_DIR = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
_ready = False


def _key(path):
    if os.path.dirname(os.path.abspath(path)) != APP_DIR or os.environ.get('SETTINGS_IN_FILES') == '1':
        return None
    return os.path.basename(path)


def _ensure(conn):
    global _ready
    if _ready:
        return
    conn.execute('CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)')
    conn.commit()
    _ready = True


def _read_file(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load(path):
    """The saved settings (parsed JSON) for the file `path`, or None when nothing has been saved."""
    key = _key(path)
    if key is None:
        return _read_file(path)
    try:
        conn = database.connect()
        try:
            _ensure(conn)
            row = conn.execute('SELECT value FROM app_settings WHERE name=%s', (key,)).fetchone()
            if row:
                return json.loads(row['value'])
            legacy = _read_file(path)                       # first run after this change: take the file's settings in
            if legacy is not None:
                conn.execute('INSERT INTO app_settings (name, value, updated_at) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING',
                             (key, json.dumps(legacy), time.time()))
                conn.commit()
            return legacy
        finally:
            conn.close()
    except Exception as e:
        print(f'Settings "{key}" not read from the database ({e}); using the file.')
        return _read_file(path)


def save(path, obj):
    """Saves the settings. In the database when the file is in the app folder (and the file too, as a copy)."""
    key = _key(path)
    stored = False
    if key is not None:
        try:
            conn = database.connect()
            try:
                _ensure(conn)
                conn.execute('INSERT INTO app_settings (name, value, updated_at) VALUES (%s,%s,%s) '
                             'ON CONFLICT (name) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at',
                             (key, json.dumps(obj), time.time()))
                conn.commit()
                stored = True
            finally:
                conn.close()
        except Exception as e:
            print(f'Settings "{key}" not saved to the database ({e}); writing the file only.')
    try:
        with _lock:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(obj, f, indent=2)
            os.replace(tmp, path)
    except OSError as e:
        if not stored:
            raise
        print(f'Settings file copy "{path}" not written ({e}).')
