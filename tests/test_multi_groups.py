"""
An account can be in several groups, and admins manage membership from either
side: the account's groups, or a group's members. Requested by JUN: "user can
be part of multiple groups" (with users not choosing their own groups).
"""
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import auth
    import database
from tests.dbutil import reset_tables, USER_TABLES

PASSWORD = 'a-genuinely-long-password-1'


@pytest.fixture
def setup():
    reset_tables(*USER_TABLES)
    gids = {}
    for name, perms in (('Promos', ['promo_generation', 'docs']), ('Shorts', ['vertical_shorts']),
                        ('Players', ['player'])):
        ok, _err, gid = auth.group_create(name)
        auth.group_set_permissions(gid, perms)
        gids[name] = gid
    uids = {}
    for name in ('anna', 'benny', 'cyrus'):
        auth.user_create(name, PASSWORD)
        uids[name] = auth.user_get_by_username(name)['id']
    return gids, uids


def _admin():
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return client, {'X-CSRF-Token': 't'}


def _page(uid, username):
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=uid, username=username, role='user', csrf_token='t')
    return client.get('/').get_data(as_text=True)


def test_tabs_are_everything_any_of_their_groups_grant(setup):
    gids, uids = setup
    assert auth.user_set_groups(uids['anna'], [gids['Promos'], gids['Shorts']]) == (True, None)
    assert auth.user_groups(uids['anna']) == {gids['Promos'], gids['Shorts']}
    assert auth.user_permissions(uids['anna'], 'user') == {'promo_generation', 'docs', 'vertical_shorts'}
    assert auth.user_can(uids['anna'], 'user', 'vertical_shorts') and not auth.user_can(uids['anna'], 'user', 'player')
    html = _page(uids['anna'], 'anna')
    assert "switchTab('p-trailer',this)" in html and "switchTab('p-shorts',this)" in html
    assert "switchTab('p-tools',this)" not in html


def test_no_group_means_dashboard_and_docs_only(setup):
    _gids, uids = setup
    assert auth.user_permissions(uids['benny'], 'user') == {'docs'}
    html = _page(uids['benny'], 'benny')
    for tab in ('p-trailer', 'p-shorts', 'p-schedule', 'p-tools'):
        assert f"switchTab('{tab}',this)" not in html, tab
    assert "switchTab('p-docs',this)" in html and "switchTab('p-dashboard',this)" in html
    # Nor does an account that no longer exists (a session outliving a deleted user) get anything gated.
    assert auth.user_permissions(987654, 'user') == {'docs'}
    assert auth.user_permissions(None, 'user') == set()
    assert auth.user_can(1, 'admin', 'player'), 'admins are not limited by groups'


def test_admins_set_an_accounts_groups_or_a_groups_members(setup):
    gids, uids = setup
    admin, h = _admin()
    r = admin.post(f"/admin/api/users/{uids['anna']}/groups", json={'group_ids': [gids['Promos'], gids['Players']]}, headers=h)
    assert r.get_json()['ok']
    r = admin.post(f"/admin/api/groups/{gids['Promos']}/members", json={'user_ids': [uids['benny'], uids['cyrus']]}, headers=h)
    assert r.get_json()['ok']
    # anna left Promos (members are the group's whole list) but kept Players.
    assert auth.user_groups(uids['anna']) == {gids['Players']}
    assert auth.user_groups(uids['benny']) == {gids['Promos']}

    listing = admin.get('/admin/api/users').get_json()
    users = {u['username']: u for u in listing['users']}
    assert users['anna']['group_names'] == ['Players'] and users['benny']['group_ids'] == [gids['Promos']]
    groups = {g['name']: g for g in listing['groups']}
    assert groups['Promos']['member_ids'] == sorted([uids['benny'], uids['cyrus']]) and groups['Promos']['member_count'] == 2

    assert not admin.post(f"/admin/api/users/{uids['anna']}/groups", json={'group_ids': [999999]}, headers=h).get_json()['ok']
    assert not admin.post(f"/admin/api/groups/{gids['Promos']}/members", json={'user_ids': [999999]}, headers=h).get_json()['ok']
    # Users cannot change memberships, their own or anyone's.
    user = main.app.test_client()
    with user.session_transaction() as sess:
        sess.update(authed=True, user_id=uids['anna'], username='anna', role='user', csrf_token='u')
    r = user.post(f"/admin/api/users/{uids['anna']}/groups", json={'group_ids': [gids['Shorts']]}, headers={'X-CSRF-Token': 'u'})
    assert r.status_code in (302, 403) and auth.user_groups(uids['anna']) == {gids['Players']}


def test_deleting_a_group_or_an_account_removes_its_memberships(setup):
    gids, uids = setup
    auth.user_set_groups(uids['anna'], [gids['Promos'], gids['Shorts']])
    auth.user_set_groups(uids['benny'], [gids['Shorts']])
    assert auth.group_delete(gids['Shorts']) == (True, None)
    assert auth.user_groups(uids['anna']) == {gids['Promos']}, 'keeps her other group'
    assert auth.user_permissions(uids['benny'], 'user') == {'docs'}, 'left in no group'
    auth.user_delete(uids['anna'])
    conn = database.connect()
    left = conn.execute('SELECT COUNT(*) AS c FROM user_groups WHERE user_id=%s', (uids['anna'],)).fetchone()['c']
    conn.close()
    assert left == 0


def test_the_old_single_group_becomes_a_membership_once(setup):
    gids, uids = setup
    conn = database.connect()
    conn.execute('UPDATE users SET group_id=%s WHERE id=%s', (gids['Promos'], uids['cyrus']))
    conn.execute('UPDATE users SET group_id=%s WHERE id=%s', (424242, uids['benny']))   # a group long gone
    conn.commit()
    conn.close()
    database._schema_done = False
    database.init_schema()                       # what every start does
    assert auth.user_groups(uids['cyrus']) == {gids['Promos']}
    assert auth.user_groups(uids['benny']) == set()
    # Removed by an admin afterwards: a restart does not bring it back.
    auth.user_set_groups(uids['cyrus'], [])
    database._schema_done = False
    database.init_schema()
    assert auth.user_groups(uids['cyrus']) == set()
