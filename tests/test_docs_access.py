"""
The Docs tab is a permission like the others: a group sees it only when an
admin ticks "Docs" for it. Requested by JUN: "DOCS sections should also be not
can anyone can access and should be added in groups selected access."
"""
import unittest.mock as mock

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import auth

PASSWORD = 'a-genuinely-long-password-1'
TAB = "switchTab('p-docs',this)"
PANEL = 'id="p-docs"'


def _page(uid, role, username):
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=uid, username=username, role=role, csrf_token='t')
    return client.get('/').get_data(as_text=True)


def _member(username, permissions):
    ok, _err, gid = auth.group_create(username + ' group')
    auth.group_set_permissions(gid, permissions)
    auth.user_create(username, PASSWORD)
    uid = auth.user_get_by_username(username)['id']
    auth.user_set_group(uid, gid)
    return uid


def test_docs_is_a_permission_admins_can_grant():
    assert ('docs', 'Docs') in auth.AVAILABLE_PERMISSIONS


def test_a_group_without_docs_gets_neither_the_tab_nor_its_content(users_db):
    uid = _member('shorts-only', ['vertical_shorts'])
    html = _page(uid, 'user', 'shorts-only')
    assert TAB not in html and PANEL not in html
    assert 'The Highlights Video page' not in html, 'the Docs text is not sent at all, not just hidden'
    assert "switchTab('p-shorts',this)" in html


def test_a_group_with_docs_sees_it(users_db):
    uid = _member('readers', ['vertical_shorts', 'docs'])
    html = _page(uid, 'user', 'readers')
    assert TAB in html and PANEL in html


def test_a_group_with_only_docs_gets_the_dashboard_and_docs(users_db):
    uid = _member('docs-only', ['docs'])
    html = _page(uid, 'user', 'docs-only')
    assert TAB in html and "switchTab('p-shorts',this)" not in html


def test_admins_and_accounts_without_a_group_still_see_docs(users_db):
    assert PANEL in _page(1, 'admin', 'admin')
    auth.user_create('nogroup', PASSWORD)
    uid = auth.user_get_by_username('nogroup')['id']
    assert PANEL in _page(uid, 'user', 'nogroup')
