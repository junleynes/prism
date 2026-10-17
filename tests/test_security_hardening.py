"""
The low-severity items from the security review (October 2026): access
requests can be switched off and are rate-limited, signed-out requests are
small, the session cookie is Secure over HTTPS, two-factor codes work once,
saved secrets are encrypted at rest, and signing out needs a POST.
"""
import json
import os
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import auth
    import core
    import database
    import library_db
    import pipeline
    import secretbox
from tests.dbutil import reset_tables, USER_TABLES, LIBRARY_TABLES

PW = 'a-genuinely-long-password-1'
pytestmark = pytest.mark.real_sessions


@pytest.fixture
def site(tmp_path, monkeypatch):
    reset_tables(*USER_TABLES)
    core._login_limiter.buckets.clear()
    core._register_limiter.buckets.clear()
    monkeypatch.setattr(library_db, 'BRANDING_FILE', str(tmp_path / 'branding_config.json'))
    assert auth.user_create('boss', PW, role='admin') == (True, None)
    return tmp_path


def _login(name, password=PW):
    core._login_limiter.buckets.clear()
    c = main.app.test_client()
    r = c.post('/login', data={'username': name, 'password': password})
    with c.session_transaction() as s:
        tok = s.get('csrf_token')
    return c, {'X-CSRF-Token': tok}, r


# ---- access requests ----

def test_access_requests_can_be_switched_off(site):
    c = main.app.test_client()
    assert 'Request access' in c.get('/').get_data(as_text=True)
    library_db.save_branding_registration(False)
    html = c.get('/').get_data(as_text=True)
    assert 'id="navBtnRegister"' not in html and 'action="/register"' not in html and 'Ask an administrator' in html
    r = c.post('/register', data={'username': 'sneaky', 'password': PW, 'password2': PW})
    assert r.status_code == 302 and auth.user_get_by_username('sneaky') is None
    admin, h, _ = _login('boss')
    assert admin.post('/api/branding', data={'registration_enabled': '1'}, headers=h).get_json()['registration_enabled']


def test_access_requests_are_rate_limited(site):
    c = main.app.test_client()
    made = 0
    for i in range(8):
        c.post('/register', data={'username': f'asker{i}', 'password': PW, 'password2': PW})
        made += auth.user_get_by_username(f'asker{i}') is not None
    assert made == 5


def test_signed_out_visitors_cannot_send_big_bodies(site):
    c = main.app.test_client()
    r = c.post('/login', data={'username': 'x' * 200_000, 'password': 'y'})
    assert r.status_code == 413
    admin, h, _ = _login('boss')
    r = admin.post('/api/branding', data={'footer': 'z' * 100_000}, headers=h)
    assert r.status_code == 200, 'signed in, the normal limit applies'


# ---- cookie ----

def test_the_session_cookie_is_secure_over_https(site):
    c = main.app.test_client()
    r = c.post('/login', data={'username': 'boss', 'password': PW}, base_url='https://prism.example')
    cookie = r.headers.get('Set-Cookie', '')
    assert 'Secure' in cookie and 'HttpOnly' in cookie
    core._login_limiter.buckets.clear()
    r = main.app.test_client().post('/login', data={'username': 'boss', 'password': PW})
    assert 'Secure' not in r.headers.get('Set-Cookie', ''), 'plain-HTTP installs still work'


# ---- two-factor codes work once ----

def test_a_two_factor_code_cannot_be_used_twice(site):
    import pyotp
    uid = auth.user_get_by_username('boss')['id']
    secret, _uri = auth.user_totp_begin_enrolment(uid)
    code = pyotp.TOTP(secret).now()
    codes, err = auth.user_totp_confirm(uid, code)
    assert err is None and codes
    stored = auth.user_get(uid)['totp_secret']
    assert stored.startswith('enc:v1:') and secret not in stored, 'kept encrypted'
    assert not auth.user_totp_verify(auth.user_get(uid), code), 'the code used to confirm is spent'
    # The next step's code (clock drift allowance) works once, then not again.
    nxt = pyotp.TOTP(secret).at(__import__('time').time() + 30)
    assert auth.user_totp_verify(auth.user_get(uid), nxt)
    assert not auth.user_totp_verify(auth.user_get(uid), nxt)
    assert not auth.user_totp_verify(auth.user_get(uid), code), 'nor an older one'


def test_old_plaintext_two_factor_secrets_are_encrypted_on_start(site):
    import pyotp
    uid = auth.user_get_by_username('boss')['id']
    secret = pyotp.random_base32()
    conn = database.connect()
    conn.execute('UPDATE users SET totp_secret=%s, totp_enabled=1 WHERE id=%s', (secret, uid))
    conn.commit()
    conn.close()
    auth.users_db_init()
    assert auth.user_get(uid)['totp_secret'].startswith('enc:v1:')
    assert auth.user_totp_verify(auth.user_get(uid), pyotp.TOTP(secret).now()), 'and still works'


# ---- saved secrets encrypted at rest ----

def test_destination_passwords_are_encrypted_at_rest(site):
    reset_tables(*LIBRARY_TABLES, 'network_destinations')
    did = library_db.network_destination_add('Playout', r'\\fs\ingest', 'svc', 'hunter2-secret')
    conn = database.connect()
    raw = conn.execute('SELECT password FROM network_destinations WHERE id=%s', (did,)).fetchone()['password']
    conn.execute("INSERT INTO network_destinations (name, path, password, delivery_kind, include_fcpxml, "
                 "fcpxml_audio_tracks, created_at, updated_at) VALUES ('Old', 'x', 'plain-old', 'video', 0, 0, 1, 1)")
    conn.commit()
    conn.close()
    assert raw.startswith('enc:v1:') and 'hunter2' not in raw
    assert library_db.network_destination_get(did)['password'] == 'hunter2-secret'
    library_db.network_destination_update(did, name='Playout 2')
    assert library_db.network_destination_get(did)['password'] == 'hunter2-secret', 'an edit keeps it'
    library_db.library_db_init()
    conn = database.connect()
    old = conn.execute("SELECT password FROM network_destinations WHERE name='Old'").fetchone()['password']
    conn.close()
    assert old.startswith('enc:v1:')
    assert {d['name']: d['password'] for d in library_db.network_destinations_list()}['Old'] == 'plain-old'


def test_share_passwords_and_api_keys_are_encrypted_at_rest(site, monkeypatch):
    folders = site / 'network_folders.json'
    folders.write_text(json.dumps({'hires': {'path': r'\\fs\hires', 'username': 'u', 'password': 'legacy-pass'}}))
    monkeypatch.setattr(library_db, 'NETWORK_FOLDERS_FILE', str(folders))
    assert library_db.load_network_folders()['hires']['password'] == 'legacy-pass'
    assert 'legacy-pass' not in folders.read_text(), 'a plaintext file is re-saved encrypted on first read'
    library_db.save_network_folder('music', {'path': r'\\fs\music', 'password': 'music-pass'})
    assert 'music-pass' not in folders.read_text()
    assert library_db.load_network_folders()['music']['password'] == 'music-pass'

    cfg = site / 'ai_services_config.json'
    monkeypatch.setattr(pipeline, 'CONFIG_FILE', str(cfg))
    before = (pipeline.FISH_AUDIO_API_KEY, pipeline.ACE_STEP_API_KEY)
    try:
        pipeline.save_config_overrides({'FISH_AUDIO_API_KEY': 'fish-secret-key'})
        assert 'fish-secret-key' not in cfg.read_text() and pipeline.FISH_AUDIO_API_KEY == 'fish-secret-key'
        pipeline.FISH_AUDIO_API_KEY = None
        pipeline.load_config_overrides()
        assert pipeline.FISH_AUDIO_API_KEY == 'fish-secret-key'
    finally:
        pipeline.FISH_AUDIO_API_KEY, pipeline.ACE_STEP_API_KEY = before


def test_a_replaced_data_key_is_reported_not_crashed_on(site):
    token = secretbox.encrypt('x')
    tampered = token[:-6] + ('A' if token[-6] != 'A' else 'B') + token[-5:]
    with pytest.raises(secretbox.SecretError):
        secretbox.decrypt(tampered)
    assert secretbox.decrypt_or_blank(tampered) == ''
    assert secretbox.decrypt('not-encrypted') == 'not-encrypted' and secretbox.encrypt('') == ''


# ---- signing out ----

def test_signing_out_needs_a_post_with_the_token(site):
    c, h, _ = _login('boss')
    assert c.get('/logout').status_code == 302 and c.get('/admin/api/users').status_code == 200, 'a link does not sign out'
    assert c.post('/logout').status_code == 403, 'nor a cross-site form without the token'
    assert c.get('/admin/api/users').status_code == 200
    assert c.post('/logout', data={'_csrf': h['X-CSRF-Token']}).status_code == 302
    assert c.get('/admin/api/users').status_code == 401
    html = _login('boss')[0].get('/').get_data(as_text=True)
    assert 'action="/logout"' in html and 'href="/logout"' not in html
