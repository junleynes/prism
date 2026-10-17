"""
Episodic Plug and Schedule Plug projects: the same idea as Vertical Shorts
projects. A project is its group's -- other groups must not see it, what is
filed in it, or what is running in it -- and plugs are filed under one.

Requested by JUN: "similar to vertical shorts, add project feature to
episodic and schedule".
"""
import json
import os
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import pipeline
    import schedule
    import auth
    import library_db
    import project_routes
from tests.dbutil import reset_tables, USER_TABLES

PASSWORD = 'a-genuinely-long-password-1'
FEATURES = (('episodic', 'promo_generation'), ('schedule', 'schedule_plug'))


def _client(uid, role, username):
    client = main.app.test_client()
    tok = f'csrf-proj-{uid}'
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=uid, username=username, role=role, csrf_token=tok)
    return client, {'X-CSRF-Token': tok}


@pytest.fixture
def world(tmp_path, monkeypatch):
    reset_tables(*USER_TABLES, 'trailers', 'audit_log')
    for kind, _perm in FEATURES:
        monkeypatch.setattr(project_routes._FEATURES[kind], 'root', str(tmp_path / f'{kind}_projects'))
    monkeypatch.setattr(schedule, 'SCHEDULE_DIR', str(tmp_path / 'plugs'))
    monkeypatch.setattr(library_db, 'LIBRARY_DIR', str(tmp_path / 'library'))
    monkeypatch.setattr(pipeline, 'LIBRARY_DIR', str(tmp_path / 'library'))
    os.makedirs(schedule.SCHEDULE_DIR)
    os.makedirs(library_db.LIBRARY_DIR)
    people, groups = {}, {}
    for name in ('Drama', 'News'):
        ok, _err, gid = auth.group_create(name)
        auth.group_set_permissions(gid, [perm for _k, perm in FEATURES])
        groups[name] = gid
    for username, mine in (('anna', ['Drama']), ('benny', ['Drama']), ('cyrus', ['News']), ('deena', [])):
        auth.user_create(username, PASSWORD)
        uid = auth.user_get_by_username(username)['id']
        auth.user_set_groups(uid, [groups[g] for g in mine])
        people[username] = (uid,) + _client(uid, 'user', username)
    auth.user_create('boss', PASSWORD, role='admin')
    uid = auth.user_get_by_username('boss')['id']
    people['boss'] = (uid,) + _client(uid, 'admin', 'boss')
    return people, groups


def _create(person, kind, title, **extra):
    _uid, client, headers = person
    r = client.post(f'/api/{kind}/projects', data=dict(title=title, **extra), headers=headers)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['project']


def _titles(person, kind):
    _uid, client, _h = person
    return {p['title'] for p in client.get(f'/api/{kind}/projects').get_json()['items']}


def _trailer(owner_uid, pid, name='Promo'):
    """A saved Episodic plug, filed under `pid`, through the same function a finished job uses."""
    src = os.path.join(main.app.config['UPLOAD_FOLDER'], f'{name}.mp4')
    with open(src, 'wb') as f:
        f.write(b'not really a video')
    return library_db.library_add(f'{name}.mp4', {'orig_name': name, 'trailer_duration': 15}, user_id=owner_uid,
                                  username='x', project_id=pid)


def _plug(owner_uid, pid, plug_id='1792000000_abc123'):
    """A saved Schedule plug on disk, filed under `pid`."""
    pdir = os.path.join(schedule.SCHEDULE_DIR, plug_id)
    os.makedirs(pdir)
    with open(os.path.join(pdir, 'plug.mp4'), 'wb') as f:
        f.write(b'not really a video')
    schedule._write_manifest(pdir, {'plug_id': plug_id, 'project_id': pid, 'user_id': owner_uid, 'username': 'x',
                                    'created': 1, 'title': 'Schedule', 'file': 'plug.mp4', 'duration': 15})
    return plug_id


@pytest.mark.parametrize('kind,_perm', FEATURES)
def test_each_group_sees_only_its_own_projects(world, kind, _perm):
    p, groups = world
    drama = _create(p['anna'], kind, 'Tadhana')
    news = _create(p['cyrus'], kind, 'Balita')
    loose = _create(p['boss'], kind, 'Unfiled', group_id=str(groups['News']))   # a group_id sent is ignored
    assert drama['group_name'] == 'Drama' and news['group_name'] == 'News' and loose['group_name'] is None

    assert _titles(p['anna'], kind) == _titles(p['benny'], kind) == {'Tadhana'}, 'the whole group sees it'
    assert _titles(p['cyrus'], kind) == {'Balita'}
    assert _titles(p['boss'], kind) == {'Tadhana', 'Balita', 'Unfiled'}, 'admins see all'
    assert p['deena'][1].get(f'/api/{kind}/projects').status_code == 403, 'in no group: no access to the tab'

    # Another group's project answers exactly like one that does not exist.
    _uid, cy, ch = p['cyrus']
    gone = cy.get(f'/api/{kind}/projects/p0000000000')
    assert gone.status_code == 404
    other = cy.get(f"/api/{kind}/projects/{drama['project_id']}")
    assert other.status_code == 404 and other.get_json() == gone.get_json(), 'indistinguishable from a missing one'
    assert cy.get(f"/api/{kind}/projects/{drama['project_id']}/thumb").status_code == 404
    r = cy.post(f"/api/{kind}/projects/{drama['project_id']}", data={'title': 'Hijacked'}, headers=ch)
    assert r.status_code == 404
    assert cy.delete(f"/api/{kind}/projects/{drama['project_id']}", headers=ch).status_code == 404


@pytest.mark.parametrize('kind,_perm', FEATURES)
def test_edit_keeps_status_and_delete_is_for_its_maker(world, kind, _perm):
    p, _g = world
    proj = _create(p['anna'], kind, 'Tadhana', episode='Ep. 101', status='in_progress', assigned_to='benny')
    pid = proj['project_id']
    assert proj['status'] == 'in_progress' and proj['assigned_to'] == 'benny' and proj['name'] == 'Tadhana — Ep. 101'

    _u, bc, bh = p['benny']
    r = bc.post(f'/api/{kind}/projects/{pid}', data={'title': 'Tadhana', 'description': 'Edited'}, headers=bh)
    assert r.status_code == 200 and r.get_json()['project']['description'] == 'Edited'
    assert r.get_json()['project']['status'] == 'in_progress', 'editing without a status leaves it alone'
    assert bc.post(f'/api/{kind}/projects/{pid}', data={'title': ''}, headers=bh).status_code == 400

    # A group-mate may use the project but not delete it; its maker may.
    r = bc.delete(f'/api/{kind}/projects/{pid}', headers=bh)
    assert r.status_code == 403
    _u, ac, ah = p['anna']
    assert ac.delete(f'/api/{kind}/projects/{pid}', headers=ah).status_code == 200
    assert ac.get(f'/api/{kind}/projects/{pid}').status_code == 404


@pytest.mark.parametrize('kind,_perm', FEATURES)
def test_a_project_holding_plugs_cannot_be_deleted(world, kind, _perm):
    p, _g = world
    pid = _create(p['anna'], kind, 'Tadhana')['project_id']
    _plug(p['anna'][0], pid) if kind == 'schedule' else _trailer(p['anna'][0], pid)
    _u, ac, ah = p['anna']
    r = ac.delete(f'/api/{kind}/projects/{pid}', headers=ah)
    assert r.status_code == 409 and 'plug' in r.get_json()['error']
    assert ac.get(f'/api/{kind}/projects/{pid}').get_json()['project']['count'] == 1


def test_episodic_plugs_are_the_projects_groups_not_their_makers(world):
    p, _g = world
    pid = _create(p['anna'], 'episodic', 'Tadhana')['project_id']
    tid = _trailer(p['anna'][0], pid)

    # A group-mate sees it listed under the project, and can open and play it...
    _u, bc, bh = p['benny']
    items = bc.get(f'/api/trailer/library?project_id={pid}').get_json()['items']
    assert [i['id'] for i in items] == [tid] and items[0]['project_id'] == pid
    assert bc.get(f'/api/trailer/library/{tid}').status_code == 200
    assert bc.get(f'/library/{tid}/file').status_code == 200
    # ... but without a project filter the library is still each person's own.
    assert bc.get('/api/trailer/library').get_json()['items'] == []
    # Another group sees nothing: not the list, not the file.
    _u, cc, ch = p['cyrus']
    assert cc.get(f'/api/trailer/library?project_id={pid}').status_code == 404
    assert cc.get(f'/api/trailer/library/{tid}').status_code == 404
    assert cc.get(f'/library/{tid}/file').status_code == 404
    # Deleting a plug stays with its maker and admins.
    assert bc.post(f'/api/trailer/library/{tid}/delete', headers=bh).status_code == 404
    _u, boss, bossh = p['boss']
    assert boss.get(f'/library/{tid}/file').status_code == 200


def test_episodic_unfiled_plugs_and_moving_them(world):
    p, _g = world
    pid = _create(p['anna'], 'episodic', 'Tadhana')['project_id']
    loose = _trailer(p['anna'][0], None, 'Old')
    orphan = _trailer(p['anna'][0], 'p0123456789', 'Orphan')       # its project has since been deleted
    filed = _trailer(p['anna'][0], pid, 'Filed')
    theirs = _trailer(p['cyrus'][0], None, 'Cyrus old')

    _u, ac, ah = p['anna']
    ids = {i['id'] for i in ac.get('/api/trailer/library?unfiled=1').get_json()['items']}
    assert ids == {loose, orphan}, 'her own plugs that are in no project, and not another person\'s'

    assert ac.post(f'/api/trailer/library/{loose}/project', json={'project_id': pid}, headers=ah).status_code == 200
    assert ac.post(f'/api/trailer/library/{orphan}/project', json={'project_id': pid}, headers=ah).status_code == 200
    assert ac.get('/api/trailer/library?unfiled=1').get_json()['items'] == []
    assert {i['id'] for i in ac.get(f'/api/trailer/library?project_id={pid}').get_json()['items']} == {loose, orphan, filed}

    # Only a plug's maker or an admin may move it, and only into a project they may see.
    _u, bc, bh = p['benny']
    assert bc.post(f'/api/trailer/library/{theirs}/project', json={'project_id': pid}, headers=bh).status_code == 404
    _u, cc, ch = p['cyrus']
    assert cc.post(f'/api/trailer/library/{theirs}/project', json={'project_id': pid}, headers=ch).status_code == 404
    assert cc.post(f'/api/trailer/library/{theirs}/project', json={'project_id': 'nope'}, headers=ch).status_code == 404


def test_schedule_plugs_are_the_projects_groups_not_their_makers(world):
    p, _g = world
    pid = _create(p['anna'], 'schedule', 'Tadhana')['project_id']
    plug = _plug(p['anna'][0], pid)

    _u, bc, bh = p['benny']
    items = bc.get(f'/api/schedule/items?project_id={pid}').get_json()['items']
    assert [i['plug_id'] for i in items] == [plug] and items[0]['project_id'] == pid
    assert bc.get(f'/api/schedule/file/{plug}/plug.mp4').status_code == 200
    assert bc.get('/api/schedule/items').get_json()['items'] == [], 'unfiltered, still each person\'s own'
    assert bc.delete(f'/api/schedule/items/{plug}', headers=bh).status_code == 404, 'deleting stays with its maker'

    _u, cc, ch = p['cyrus']
    assert cc.get(f'/api/schedule/items?project_id={pid}').status_code == 404
    assert cc.get(f'/api/schedule/file/{plug}/plug.mp4').status_code == 404

    _u, ac, ah = p['anna']
    assert ac.delete(f'/api/schedule/items/{plug}', headers=ah).status_code == 200


def test_schedule_unfiled_plugs_and_moving_them(world):
    p, _g = world
    pid = _create(p['anna'], 'schedule', 'Tadhana')['project_id']
    loose = _plug(p['anna'][0], None, '1792000000_aaaaaa')
    orphan = _plug(p['anna'][0], 'p0123456789', '1792000001_bbbbbb')
    theirs = _plug(p['cyrus'][0], None, '1792000002_cccccc')

    _u, ac, ah = p['anna']
    got = {i['plug_id'] for i in ac.get('/api/schedule/items?unfiled=1').get_json()['items']}
    assert got == {loose, orphan}
    assert ac.post(f'/api/schedule/items/{loose}/project', json={'project_id': pid}, headers=ah).status_code == 200
    assert ac.post(f'/api/schedule/items/{theirs}/project', json={'project_id': pid}, headers=ah).status_code == 404
    assert ac.post(f'/api/schedule/items/{orphan}/project', json={'project_id': 'nope'}, headers=ah).status_code == 404
    assert {i['plug_id'] for i in ac.get(f'/api/schedule/items?project_id={pid}').get_json()['items']} == {loose}
    assert ac.get(f'/api/schedule/projects/{pid}').get_json()['project']['count'] == 1


@pytest.mark.parametrize('kind,url', (('episodic', '/api/trailer/generate'), ('schedule', '/api/schedule/render')))
def test_a_job_for_a_project_that_is_gone_is_refused_before_anything_is_saved(world, kind, url):
    p, _g = world
    _u, ac, ah = p['anna']
    r = ac.post(url, data={'project_id': 'p0123456789', 'duration': '15'}, headers=ah)
    assert r.status_code == 404 and 'no longer exists' in r.get_json()['error']
    # Another group's project is refused in the same words.
    other = _create(p['cyrus'], kind, 'Balita')['project_id']
    r = ac.post(url, data={'project_id': other, 'duration': '15'}, headers=ah)
    assert r.status_code == 404 and 'no longer exists' in r.get_json()['error']


@pytest.mark.parametrize('kind,_perm', FEATURES)
def test_activity_shows_what_is_running_to_the_projects_group_only(world, kind, _perm, monkeypatch):
    p, _g = world
    pid = _create(p['anna'], kind, 'Tadhana')['project_id']
    jkind = 'promo' if kind == 'episodic' else 'schedule'
    project_routes.track_job(kind, 'job-1', pid, jkind)
    project_routes.track_job(kind, 'job-2', pid, jkind)
    monkeypatch.setattr(pipeline, 'job_list_all', lambda: {
        'job-1': {'done': False, 'status': 'running', 'percent': 40, 'step': 'Drawing frames', 'created': 1},
        'job-2': {'done': False, 'status': 'queued', 'percent': 0, 'step': '', 'created': 2}})

    _u, bc, _h = p['benny']
    jobs = bc.get(f'/api/{kind}/projects/activity').get_json()['jobs']
    assert [j['state'] for j in jobs[pid]] == ['running', 'waiting'] and jobs[pid][0]['percent'] == 40
    assert bc.get(f'/api/{kind}/projects').get_json()['items'][0]['jobs'][0]['job_id'] == 'job-1'
    _u, cc, _h = p['cyrus']
    assert cc.get(f'/api/{kind}/projects/activity').get_json()['jobs'] == {}

    # Once a job is done it is no longer shown.
    monkeypatch.setattr(pipeline, 'job_list_all', lambda: {'job-1': {'done': True}, 'job-2': {'done': True}})
    assert bc.get(f'/api/{kind}/projects/activity').get_json()['jobs'] == {}


@pytest.mark.parametrize('kind,_perm', FEATURES)
def test_projects_of_one_feature_are_not_the_other_features(world, kind, _perm):
    p, _g = world
    _create(p['anna'], kind, 'Tadhana')
    other = 'schedule' if kind == 'episodic' else 'episodic'
    assert _titles(p['anna'], other) == set()
