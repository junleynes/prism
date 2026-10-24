"""Config > Backup (database dump, restore, configuration export) and changing one's own password."""
import io
import os
import sys
import unittest.mock as mock
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with mock.patch('requests.post'), mock.patch('requests.get'):
    import auth
    import backup
    import main


def _admin(c):
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return {'X-CSRF-Token': 't'}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setattr(backup, '_RESTORE', {'state': 'idle', 'message': '', 'started': 0.0, 'safety': ''})
    return tmp_path / 'backups'


def test_a_backup_is_made_listed_downloaded_and_deleted(folder):
    c = main.app.test_client(); h = _admin(c)
    d = c.post('/admin/api/backup/db/create', headers=h).get_json()
    assert d['ok'] and d['name'].startswith('prism-') and d['name'].endswith('.dump')
    assert open(folder / d['name'], 'rb').read(5) == b'PGDMP'
    st = c.get('/admin/api/backup/status').get_json()
    assert st['pg_dump'] and [b['name'] for b in st['backups']] == [d['name']] and '@' in st['database'] and 'password' not in st['database'].lower()
    r = c.get('/admin/api/backup/db/' + d['name'])
    assert r.status_code == 200 and r.data[:5] == b'PGDMP'
    assert c.get('/admin/api/backup/db/..%2Fetc.dump').status_code == 404
    assert c.post(f"/admin/api/backup/db/{d['name']}/delete", headers=h).get_json()['backups'] == []


def test_only_an_admin_can_reach_any_of_it(folder):
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=2, username='ana', role='user', csrf_token='t')
    for path in ('/admin/api/backup/status', '/admin/api/backup/config'):
        r = c.get(path)
        assert r.status_code in (302, 401, 403) and b'PGDMP' not in r.data and b'DATABASE_URL' not in r.data
    assert c.post('/admin/api/backup/db/create', headers={'X-CSRF-Token': 't'}).status_code in (302, 401, 403)
    assert not os.path.isdir(backup.BACKUP_DIR) or not os.listdir(backup.BACKUP_DIR)


def test_restore_needs_confirmation_a_real_dump_and_takes_a_safety_copy_first(folder, monkeypatch):
    c = main.app.test_client(); h = _admin(c)
    ran = []
    monkeypatch.setattr(backup, '_restore_job', lambda path, safety, who: ran.append((os.path.basename(path), safety)))
    name = c.post('/admin/api/backup/db/create', headers=h).get_json()['name']
    assert c.post('/admin/api/backup/db/restore', data={'name': name}, headers=h).status_code == 400, 'no confirmation'
    r = c.post('/admin/api/backup/db/restore', data={'confirm': 'RESTORE', 'file': (io.BytesIO(b'not a dump'), 'x.dump')},
               headers=h, content_type='multipart/form-data')
    assert r.status_code == 400 and 'not a PRISM database backup' in r.get_json()['error']
    assert not [n for n in os.listdir(folder) if n.startswith('upload-')], 'a rejected upload is not kept'
    r = c.post('/admin/api/backup/db/restore', data={'confirm': 'RESTORE', 'name': name}, headers=h).get_json()
    assert r['ok'] and r['safety'].startswith('before-restore-')
    assert os.path.exists(folder / r['safety']), 'the current database was copied before anything was replaced'
    for _ in range(50):
        if ran:
            break
        import time; time.sleep(0.05)
    assert ran == [(name, r['safety'])]


def test_restore_job_runs_pg_restore_clean_and_applies_the_schema_again(folder, monkeypatch):
    calls = []
    monkeypatch.setattr(backup.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or mock.Mock(returncode=1, stderr='warning: x'))
    monkeypatch.setattr(backup, '_tool', lambda n: '/usr/bin/' + n)
    schema = []
    monkeypatch.setattr(backup.database, 'init_schema', lambda: schema.append(1))
    backup._restore_job(str(folder / 'x.dump'), 'before-restore-1.dump', 'admin')
    assert '--clean' in calls[0] and '--if-exists' in calls[0] and calls[0][0].endswith('pg_restore') and schema == [1]
    assert backup._RESTORE['state'] == 'done' and 'before-restore-1.dump' in backup._RESTORE['message']
    monkeypatch.setattr(backup.subprocess, 'run', lambda cmd, **kw: mock.Mock(returncode=2, stderr='boom'))
    backup._restore_job(str(folder / 'x.dump'), 'before-restore-1.dump', 'admin')
    assert backup._RESTORE['state'] == 'failed' and 'boom' in backup._RESTORE['message']


def test_config_export_is_a_zip_with_the_keys_unless_left_out(folder, tmp_path, monkeypatch):
    app_dir = tmp_path / 'app'; app_dir.mkdir()
    (app_dir / '.env').write_text('DATABASE_URL=postgresql://x\n')
    (app_dir / '.data_key').write_text('k1')
    (app_dir / 'ai_services_config.json').write_text('{}')
    lib = tmp_path / 'lib'; lib.mkdir()
    (lib / 'network_folders.json').write_text('[]')
    monkeypatch.setattr(backup, 'APP_DIR', str(app_dir))
    monkeypatch.setattr('library_db.LIBRARY_DIR', str(lib))
    monkeypatch.delenv('DATA_KEY_FILE', raising=False)
    monkeypatch.delenv('SECRET_KEY_FILE', raising=False)
    c = main.app.test_client(); _admin(c)
    z = zipfile.ZipFile(io.BytesIO(c.get('/admin/api/backup/config').data))
    assert {'.env', '.data_key', 'ai_services_config.json', 'library/network_folders.json', 'README.txt'} <= set(z.namelist())
    z = zipfile.ZipFile(io.BytesIO(c.get('/admin/api/backup/config?keys=0').data))
    assert '.data_key' not in z.namelist() and '.env' in z.namelist()


@pytest.mark.real_sessions
def test_a_user_changes_their_own_password(users_db):
    ok, _ = auth.user_create('ana', 'the-first-long-password-1', role='user')
    assert ok
    uid = next(u['id'] for u in auth.user_list() if u['username'] == 'ana')
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=uid, username='ana', role='user', csrf_token='t', sv=auth.user_get(uid)['session_version'] or 0)
    h = {'X-CSRF-Token': 't'}
    post = lambda **kw: c.post('/api/account/password', json=kw, headers=h)
    assert post(current='wrong-password-123', password='the-second-long-password-2').status_code == 400
    assert 'different' in post(current='the-first-long-password-1', password='the-first-long-password-1').get_json()['error']
    assert post(current='the-first-long-password-1', password='short').status_code == 400, 'the usual policy applies'
    d = post(current='the-first-long-password-1', password='the-second-long-password-2').get_json()
    assert d['ok']
    row = auth.user_get(uid)
    assert auth.check_password_hash(row['password_hash'], 'the-second-long-password-2')
    assert c.get('/api/dashboard/presence').status_code == 200, 'this session carries on'
    other = main.app.test_client()
    with other.session_transaction() as s:
        s.update(authed=True, user_id=uid, username='ana', role='user', csrf_token='t', sv=0)
    assert other.get('/api/dashboard/presence').status_code in (401, 302), 'the other sessions ended'


def test_every_user_not_only_admins_can_reach_the_password_change():
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=2, username='ana', role='user', csrf_token='t')
    html = c.get('/').get_data(as_text=True)
    assert 'openPasswordDialog()' in html and 'id=pw-dialog' in html and "changeOwnPassword('pwd')" in html
    # In the account menu at the foot of the left menu, with two-factor beside it, for every account.
    assert 'id=acct-menu' in html and 'Profile &amp; 2FA' in html and 'function pfTwofaBegin()' in html and 'id=pf-2fa' in html
    # Not on the dashboard any more: no theme switch, no Security card.
    dash = html[html.index('id="p-dashboard"'):]
    dash = dash[:dash.index('id=dash-presence')]
    assert 'theme-toggle' not in dash and 'dash-security' not in html
    assert "onclick=\"switchTab('p-config',this)\"" not in html and 'id="p-config-backup"' not in html, 'Config and Backup stay admin-only'
