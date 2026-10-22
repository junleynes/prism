"""Config > Backup: the database as a dump file (pg_dump), restoring one, and the configuration and keys as a zip.
Admin only (everything here is under /admin/). A dump is the same thing docs/POSTGRESQL.md section 7 describes."""
import glob
import io
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

from flask import request, jsonify, session, send_file

import database
from core import app, _client_ip
from library_db import LIBRARY_DIR, audit_log

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.abspath(os.environ.get('BACKUP_DIR') or os.path.join(LIBRARY_DIR, 'backups'))
_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,120}\.dump$')
DUMP_TIMEOUT = 3600
_RESTORE = {'state': 'idle', 'message': '', 'started': 0.0, 'safety': ''}
_RESTORE_LOCK = threading.Lock()


def _tool(name):
    """pg_dump / pg_restore: PG_BIN_DIR, then the PATH, then where the Windows installer puts them."""
    exe = name + ('.exe' if os.name == 'nt' else '')
    d = os.environ.get('PG_BIN_DIR', '').strip()
    if d and os.path.isfile(os.path.join(d, exe)):
        return os.path.join(d, exe)
    found = shutil.which(name)
    if found:
        return found
    if os.name == 'nt':
        hits = sorted(glob.glob(os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'PostgreSQL', '*', 'bin', exe)),
                      reverse=True)
        if hits:
            return hits[0]
    return None


def _pg_env():
    """libpq settings for the tools, from DATABASE_URL: the password goes in the environment, never on a command line."""
    env = dict(os.environ)
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:
        from psycopg.conninfo import conninfo_to_dict
        d = conninfo_to_dict(url)
        for key, var in (('host', 'PGHOST'), ('port', 'PGPORT'), ('user', 'PGUSER'), ('password', 'PGPASSWORD'),
                         ('dbname', 'PGDATABASE'), ('sslmode', 'PGSSLMODE')):
            if d.get(key):
                env[var] = str(d[key])
    return env


def _describe():
    env = _pg_env()
    return f"{env.get('PGUSER', '?')}@{env.get('PGHOST', 'localhost')}:{env.get('PGPORT', '5432')}/{env.get('PGDATABASE', '?')}"


def _listing():
    out = []
    if os.path.isdir(BACKUP_DIR):
        for n in os.listdir(BACKUP_DIR):
            p = os.path.join(BACKUP_DIR, n)
            if _NAME_RE.match(n) and os.path.isfile(p):
                out.append({'name': n, 'size': os.path.getsize(p), 'time': os.path.getmtime(p)})
    return sorted(out, key=lambda x: -x['time'])


def _dump(prefix='prism'):
    """pg_dump -Fc into BACKUP_DIR -> (name, error). Written under a private name, renamed when complete."""
    exe = _tool('pg_dump')
    if not exe:
        return None, 'pg_dump was not found on this server. Install the PostgreSQL client tools, or set PG_BIN_DIR in .env.'
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"{prefix}-{time.strftime('%Y-%m-%d-%H%M%S')}.dump"
    part = os.path.join(BACKUP_DIR, name + '.part')
    try:
        r = subprocess.run([exe, '-Fc', '--no-password', '-f', part], env=_pg_env(), capture_output=True, text=True,
                           timeout=DUMP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f'Could not run pg_dump: {e}'
    if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
        if os.path.exists(part):
            os.remove(part)
        return None, 'pg_dump failed: ' + (r.stderr or '').strip()[-400:]
    os.replace(part, os.path.join(BACKUP_DIR, name))
    return name, None


def _is_dump(path):
    try:
        with open(path, 'rb') as f:
            return f.read(5) == b'PGDMP'
    except OSError:
        return False


@app.route('/admin/api/backup/status')
def api_backup_status():
    with _RESTORE_LOCK:
        restore = dict(_RESTORE)
    return jsonify(ok=True, database=_describe(), pg_dump=bool(_tool('pg_dump')), pg_restore=bool(_tool('pg_restore')),
                   folder=BACKUP_DIR, backups=_listing(), restore=restore)


@app.route('/admin/api/backup/db/create', methods=['POST'])
def api_backup_create():
    name, err = _dump()
    if err:
        return jsonify(ok=False, error=err), 500
    audit_log('backup_create', target=name, user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return jsonify(ok=True, name=name, backups=_listing())


@app.route('/admin/api/backup/db/<name>')
def api_backup_download(name):
    if not _NAME_RE.match(name) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        return jsonify(ok=False, error='No such backup.'), 404
    audit_log('backup_download', target=name, user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return send_file(os.path.join(BACKUP_DIR, name), as_attachment=True, download_name=name, mimetype='application/octet-stream')


@app.route('/admin/api/backup/db/<name>/delete', methods=['POST'])
def api_backup_delete(name):
    if not _NAME_RE.match(name) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        return jsonify(ok=False, error='No such backup.'), 404
    os.remove(os.path.join(BACKUP_DIR, name))
    audit_log('backup_delete', target=name, user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return jsonify(ok=True, backups=_listing())


def _restore_job(path, safety, who):
    def say(state, message):
        with _RESTORE_LOCK:
            _RESTORE.update(state=state, message=message)
    exe = _tool('pg_restore')
    try:
        say('running', 'Restoring the database…')
        r = subprocess.run([exe, '--clean', '--if-exists', '--no-owner', '--no-privileges', '--no-password',
                            '-d', _pg_env().get('PGDATABASE', ''), path],
                           env=_pg_env(), capture_output=True, text=True, timeout=DUMP_TIMEOUT)
        # pg_restore exits 1 for warnings (an object that did not exist to drop, say); a real failure leaves nothing usable.
        if r.returncode not in (0, 1):
            say('failed', f'pg_restore failed: {(r.stderr or "").strip()[-400:]} The copy taken before is {safety}.')
            return
        # An older backup may predate columns this version needs: apply the schema again, as at start-up.
        database.init_schema()
        audit_log('backup_restore', target=os.path.basename(path), detail=f'safety copy {safety}', username=who)
        say('done', f'Restored. Everyone may need to sign in again. The copy taken just before is {safety}.')
    except Exception as e:      # noqa: BLE001 -- shown to the admin
        say('failed', f'Restore failed: {e}. The copy taken before is {safety}.')
    finally:
        if path.startswith(os.path.join(BACKUP_DIR, 'upload-')) and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@app.route('/admin/api/backup/db/restore', methods=['POST'])
def api_backup_restore():
    """Replaces the whole database with a backup: from a file uploaded here or one of the server's own. A copy of
    the database as it is now is taken first, so a wrong choice can be undone from the list."""
    if (request.form.get('confirm') or '').strip() != 'RESTORE':
        return jsonify(ok=False, error='Type RESTORE to confirm.'), 400
    if not _tool('pg_restore'):
        return jsonify(ok=False, error='pg_restore was not found on this server. Install the PostgreSQL client tools, or set PG_BIN_DIR in .env.'), 500
    os.makedirs(BACKUP_DIR, exist_ok=True)
    f = request.files.get('file')
    name = (request.form.get('name') or '').strip()
    if f and f.filename:
        path = os.path.join(BACKUP_DIR, f"upload-{time.strftime('%Y%m%d%H%M%S')}.dump")
        f.save(path)
    elif _NAME_RE.match(name) and os.path.isfile(os.path.join(BACKUP_DIR, name)):
        path = os.path.join(BACKUP_DIR, name)
    else:
        return jsonify(ok=False, error='Choose a backup file, or one from the list.'), 400
    if not _is_dump(path):
        if os.path.basename(path).startswith('upload-'):
            os.remove(path)
        return jsonify(ok=False, error='That is not a PRISM database backup (a pg_dump -Fc file).'), 400
    with _RESTORE_LOCK:
        if _RESTORE['state'] == 'running':
            return jsonify(ok=False, error='A restore is already running.'), 409
        _RESTORE.update(state='running', message='Taking a copy of the current database…', started=time.time(), safety='')
    safety, err = _dump('before-restore')
    if err:
        with _RESTORE_LOCK:
            _RESTORE.update(state='failed', message='Not restored: the copy of the current database could not be taken. ' + err)
        return jsonify(ok=False, error='Not restored: the copy of the current database could not be taken. ' + err), 500
    with _RESTORE_LOCK:
        _RESTORE['safety'] = safety
    threading.Thread(target=_restore_job, args=(path, safety, session.get('username')), daemon=True).start()
    return jsonify(ok=True, safety=safety)


_CONFIG_NOTE = """PRISM configuration export
==========================
Taken {when} from {db}.

Contents (restore by copying each file back to the same place on the new server, then start PRISM):
  .env                      settings and the database address (edit paths and passwords for the new machine)
  .data_key                 encrypts saved share passwords, API keys and 2FA secrets: without it they must be entered again
  .secret_key               signs sessions (optional: without it everyone signs in again)
  ai_services_config.json   where each AI service is
  library/*.json            network folders, destinations and other settings kept next to the library

The database is separate: use Config > Backup > Back up the database.
THIS FILE CONTAINS SECRETS. Keep it somewhere only administrators can read, and delete copies you no longer need.
"""


def _config_files(include_keys):
    from library_db import LIBRARY_DIR as lib
    env_keys = {'.data_key': os.environ.get('DATA_KEY_FILE') or os.path.join(APP_DIR, '.data_key'),
                '.secret_key': os.environ.get('SECRET_KEY_FILE') or os.path.join(APP_DIR, '.secret_key')}
    files = [('.env', os.path.join(APP_DIR, '.env')), ('ai_services_config.json', os.path.join(APP_DIR, 'ai_services_config.json'))]
    if include_keys:
        files += list(env_keys.items())
    if os.path.isdir(lib):
        for n in sorted(os.listdir(lib)):
            p = os.path.join(lib, n)
            if n.lower().endswith('.json') and os.path.isfile(p) and os.path.getsize(p) < 5_000_000:
                files.append(('library/' + n, p))
    return [(arc, p) for arc, p in files if os.path.isfile(p)]


@app.route('/admin/api/backup/config')
def api_backup_config():
    include_keys = request.args.get('keys', '1') != '0'
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, p in _config_files(include_keys):
            z.write(p, arc)
            names.append(arc)
        z.writestr('README.txt', _CONFIG_NOTE.format(when=time.strftime('%Y-%m-%d %H:%M'), db=_describe()))
    buf.seek(0)
    audit_log('config_export', detail=', '.join(names) + ('' if include_keys else ' (without keys)'),
              user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return send_file(buf, as_attachment=True, download_name=f"prism-config-{time.strftime('%Y-%m-%d')}.zip", mimetype='application/zip')
