"""
Fixes from the security review (October 2026). Each test is the attack that
worked before the fix.
"""
import os
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import auth
    import core
    import pipeline
from tests.dbutil import reset_tables, USER_TABLES

PW = 'a-genuinely-long-password-1'
pytestmark = pytest.mark.real_sessions


@pytest.fixture
def accounts():
    reset_tables(*USER_TABLES)
    core._login_limiter.buckets.clear()
    ok, _e, promos = auth.group_create('Promos')
    auth.group_set_permissions(promos, ['promo_generation'])
    ok, _e, docs = auth.group_create('Docs only')
    auth.group_set_permissions(docs, ['docs'])
    ids = {}
    for name, role, groups in (('admin1', 'admin', []), ('admin2', 'admin', []), ('editor', 'user', [promos]),
                               ('reader', 'user', [docs])):
        assert auth.user_create(name, PW, role=role) == (True, None)
        ids[name] = auth.user_get_by_username(name)['id']
        auth.user_set_groups(ids[name], groups)
    return ids


def _login(name):
    core._login_limiter.buckets.clear()
    c = main.app.test_client()
    r = c.post('/login', data={'username': name, 'password': PW})
    assert r.status_code == 302, name
    with c.session_transaction() as s:
        tok = s['csrf_token']
    return c, {'X-CSRF-Token': tok}


# ---- 1. Stored XSS: show template names on the Dashboard ----

def test_the_dashboard_escapes_show_template_names(accounts):
    c, _h = _login('admin1')
    html = c.get('/').get_data(as_text=True)
    start = html.index('No saved shows yet')
    block = html[start:start + 1500]
    assert "escapeHtmlLite(t.name)" in block and "' + t.name + '" not in block


# ---- 2. Stored XSS via a self-registered username on the old admin page ----

def test_usernames_cannot_carry_markup(accounts):
    c = main.app.test_client()
    c.post('/register', data={'username': '<img src=x onerror=alert(1)>', 'password': PW, 'password2': PW})
    assert auth.user_get_by_username('<img src=x onerror=alert(1)>') is None
    ok, err = auth.user_create('bad"name', PW)
    assert not ok and 'letters' in err
    assert auth.user_create('jun.leynes@gma-7', PW) == (True, None)


def test_the_old_server_rendered_users_page_is_gone(accounts):
    c, h = _login('admin1')
    for url in ('/admin/users', '/admin/users?embed=1'):
        r = c.get(url)
        assert r.status_code == 302 and r.headers['Location'].endswith('/')
    assert c.post(f"/admin/users/{accounts['editor']}/role", data={'role': 'admin', '_csrf': h['X-CSRF-Token']}).status_code == 404


# ---- 3. Sessions are checked against the account on every request ----

def test_a_disabled_account_is_signed_out(accounts):
    c, _h = _login('editor')
    assert c.get('/api/templates').status_code == 200
    auth.user_set_active(accounts['editor'], False)
    assert c.get('/api/templates').status_code == 401
    auth.user_set_active(accounts['editor'], True)
    assert c.get('/api/templates').status_code == 401, 'and re-enabling does not bring the old session back'


def test_a_deleted_admin_loses_admin_at_once(accounts):
    c, _h = _login('admin2')
    assert c.get('/admin/api/users').status_code == 200
    auth.user_delete(accounts['admin2'])
    assert c.get('/admin/api/users').status_code == 401


def test_a_demoted_admin_stops_being_admin_on_the_next_request(accounts):
    c, _h = _login('admin2')
    auth.user_set_role(accounts['admin2'], 'user')
    r = c.get('/admin/api/users')
    assert r.status_code in (302, 403)
    with c.session_transaction() as s:
        assert s['role'] == 'user'


def test_setting_a_password_or_resetting_2fa_ends_existing_sessions(accounts):
    c, _h = _login('editor')
    auth.user_set_password(accounts['editor'], 'another-long-password-2')
    assert c.get('/api/templates').status_code == 401
    c2 = main.app.test_client()
    core._login_limiter.buckets.clear()
    assert c2.post('/login', data={'username': 'editor', 'password': 'another-long-password-2'}).status_code == 302
    assert c2.get('/api/templates').status_code == 200, 'signing in again works'
    auth.user_totp_disable(accounts['editor'])
    assert c2.get('/api/templates').status_code == 401


def test_a_forged_session_for_an_account_that_does_not_exist_gets_nothing(accounts):
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=999999, username='ghost', role='admin', csrf_token='t')
    assert c.get('/admin/api/users').status_code == 401


# ---- 4. Server-wide queue settings are admin-only ----

def test_only_admins_change_the_concurrency_limit_or_see_the_whole_queue(accounts):
    before = pipeline.GATE.status()['limit']
    c, h = _login('reader')
    assert c.post('/api/queue/limit', json={'limit': 500}, headers=h).status_code == 403
    assert c.get('/api/queue/status').status_code == 403
    assert pipeline.GATE.status()['limit'] == before
    a, ah = _login('admin1')
    try:
        assert a.post('/api/queue/limit', json={'limit': before + 1}, headers=ah).get_json()['limit'] == before + 1
    finally:
        pipeline.GATE.set_limit(before)


# ---- 5. X-Forwarded-For is not trusted unless a proxy is configured ----

def test_a_made_up_forwarded_address_does_not_dodge_the_login_limit(accounts):
    core._login_limiter.buckets.clear()
    c = main.app.test_client()
    limited = 0
    for i in range(12):
        r = c.post('/login', data={'username': f'nobody{i}', 'password': 'wrong'},
                   headers={'X-Forwarded-For': f'10.9.9.{i}'})
        limited += 'Too many attempts' in r.get_data(as_text=True)
    assert limited >= 4
    core._login_limiter.buckets.clear()


# ---- 6. Network shares, staged files and media helpers need a media tab ----

@pytest.mark.parametrize('method,url', [
    ('get', '/api/network/list?category=hires'), ('get', '/api/network/search?category=hires&q=x'),
    ('get', '/api/network/favorites'), ('post', '/api/network/fetch'), ('post', '/api/network/combine'),
    ('post', '/api/media/probe'), ('post', '/api/media/playable'), ('post', '/api/scene/clip'),
    ('post', '/api/opencv/info'), ('get', '/uploads/anything.mp4'),
])
def test_media_helpers_are_closed_to_accounts_without_a_media_tab(accounts, method, url):
    c, h = _login('reader')
    r = getattr(c, method)(url, headers=h)
    blocked = (302, 403) if url.startswith('/uploads/') else (403,)    # a page, not the API: sent back to the app
    assert r.status_code in blocked, (url, r.status_code)
    e, eh = _login('editor')
    assert getattr(e, method)(url, headers=eh).status_code not in blocked, 'open to someone with a media tab'


# ---- 7. Dependencies ----

def test_patched_werkzeug_and_pypdf_are_required():
    req = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'requirements.txt')).read()
    assert 'Werkzeug==3.1.9' in req and 'pypdf==6.20.1' in req
