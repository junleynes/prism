"""
The landing page covers the whole suite, and an admin can switch it off
(Config > Branding) so signed-out visitors get only the sign-in card.
Requested by JUN: "lets update the landing page, its no longer about episodic
plug. also allow to enable disable landing page."
"""
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import library_db


@pytest.fixture(autouse=True)
def own_branding_file(tmp_path, monkeypatch):
    monkeypatch.setattr(library_db, 'BRANDING_FILE', str(tmp_path / 'branding_config.json'))


def _admin():
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return client, {'X-CSRF-Token': 't'}


def test_the_landing_page_is_generic_about_ai_media_processing():
    html = main.app.test_client().get('/').get_data(as_text=True)
    for name in ('Understands your media', 'Automated editing', 'Hands-off automation', 'Self-hosted, team-ready'):
        assert name in html, name
    for tool in ('Episodic Plug', 'Vertical Shorts', 'Schedule Plug'):
        assert tool not in html, 'the landing page talks about what it does, not which tools are inside'
    assert 'AI media processing' in html and 'episodic promo</span> production' not in html
    assert 'id="how"' in html and 'id="features"' in html
    assert 'class="no-landing"' not in html


def test_switched_off_it_is_only_the_sign_in_card():
    library_db.save_branding_landing(False)
    html = main.app.test_client().get('/').get_data(as_text=True)
    assert 'class="no-landing"' in html
    assert 'id="how"' not in html and 'id="features"' not in html and 'landing-hero.jpg' not in html
    assert 'action="/login"' in html and 'action="/register"' in html, 'signing in and requesting access still work'
    assert 'var landing = false' in html
    # A failed sign-in re-renders the same way.
    r = main.app.test_client().post('/login', data={'username': 'nobody-here', 'password': 'wrong-password-123'})
    page = r.get_data(as_text=True)
    assert 'class="no-landing"' in page and 'id="features"' not in page


def test_only_an_admin_turns_it_on_and_off():
    user = main.app.test_client()
    with user.session_transaction() as sess:
        sess.update(authed=True, user_id=2, username='someone', role='user', csrf_token='u')
    assert user.post('/api/branding', data={'landing_enabled': '0'}, headers={'X-CSRF-Token': 'u'}).status_code == 403
    assert library_db.load_branding()['landing_enabled'] is True

    admin, headers = _admin()
    r = admin.post('/api/branding', data={'landing_enabled': '0'}, headers=headers).get_json()
    assert r['ok'] and r['landing_enabled'] is False
    assert admin.get('/api/branding').get_json()['landing_enabled'] is False
    # Saving other branding fields leaves it alone.
    admin.post('/api/branding', data={'name': 'PRISM'}, headers=headers)
    assert library_db.load_branding()['landing_enabled'] is False
    r = admin.post('/api/branding', data={'landing_enabled': '1'}, headers=headers).get_json()
    assert r['landing_enabled'] is True


def test_the_switch_is_on_the_branding_page():
    admin, _ = _admin()
    html = admin.get('/').get_data(as_text=True)
    assert 'id=brand-landing-input' in html and 'saveBrandLanding()' in html
