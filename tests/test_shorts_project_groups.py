"""
Vertical Shorts projects belong to a group: other groups must not see them,
the shorts filed under them, or what is running in them. An account can be in
several groups and then sees the projects of all of them.

Requested by JUN: "in vertical shorts, other groups should not be able to see
other groups project".
"""
import json
import os
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import shorts
    import auth
from tests.dbutil import reset_tables, USER_TABLES

PASSWORD = 'a-genuinely-long-password-1'


def _client(uid, role, username):
    client = main.app.test_client()
    tok = f'csrf-groups-{uid}'
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=uid, username=username, role=role, csrf_token=tok)
    return client, {'X-CSRF-Token': tok}


@pytest.fixture
def world(tmp_path, monkeypatch):
    reset_tables(*USER_TABLES)
    monkeypatch.setattr(shorts, 'SHORTS_DIR', str(tmp_path / 'shorts'))
    monkeypatch.setattr(shorts, 'SHORTS_PROJECTS_DIR', str(tmp_path / 'projects'))
    os.makedirs(shorts.SHORTS_DIR)
    people = {}
    groups = {}
    for name in ('Drama', 'News'):
        ok, _err, gid = auth.group_create(name)
        auth.group_set_permissions(gid, ['vertical_shorts'])
        groups[name] = gid
    for username, mine in (('anna', ['Drama']), ('benny', ['Drama']), ('cyrus', ['News']), ('deena', []),
                           ('erin', ['Drama', 'News'])):
        auth.user_create(username, PASSWORD)
        uid = auth.user_get_by_username(username)['id']
        auth.user_set_groups(uid, [groups[g] for g in mine])
        people[username] = (uid,) + _client(uid, 'user', username)
    auth.user_create('boss', PASSWORD, role='admin')
    uid = auth.user_get_by_username('boss')['id']
    people['boss'] = (uid,) + _client(uid, 'admin', 'boss')
    return people, groups


def _create(person, title, **extra):
    _uid, client, headers = person
    r = client.post('/api/shorts/projects', data=dict(title=title, **extra), headers=headers)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['project']


def _titles(person):
    _uid, client, _h = person
    return {p['title'] for p in client.get('/api/shorts/projects').get_json()['items']}


def _batch(owner_uid, pid, bid='1792000000_abc123'):
    bdir = os.path.join(shorts.SHORTS_DIR, bid)
    os.makedirs(bdir)
    with open(os.path.join(bdir, 'short_01.mp4'), 'wb') as f:
        f.write(b'not really a video')
    shorts._write_manifest(bdir, {'batch_id': bid, 'project_id': pid, 'user_id': owner_uid, 'username': 'x',
                                  'created': 1, 'status': 'done',
                                  'shorts': [{'index': 1, 'title': 'One', 'file': 'short_01.mp4', 'start': 0, 'end': 5}]})
    return bid


def test_each_group_sees_only_its_own_projects(world):
    p, groups = world
    drama = _create(p['anna'], 'Tadhana')
    news = _create(p['cyrus'], 'Balita')
    loose = _create(p['boss'], 'Unfiled', group_id=str(groups['News']))   # a group_id sent is ignored
    assert drama['group_name'] == 'Drama' and news['group_name'] == 'News' and loose['group_name'] is None

    assert _titles(p['anna']) == _titles(p['benny']) == {'Tadhana'}, 'the whole group sees it'
    assert _titles(p['cyrus']) == {'Balita'}
    assert _titles(p['erin']) == {'Tadhana', 'Balita'}, 'in two groups: both groups\' projects'
    assert _titles(p['boss']) == {'Tadhana', 'Balita', 'Unfiled'}, 'admins see all'
    # In no group: no Vertical Shorts at all.
    assert p['deena'][1].get('/api/shorts/projects').status_code == 403

    # Another group's project answers exactly like one that does not exist.
    _uid, cy, ch = p['cyrus']
    for url in (f"/api/shorts/projects/{drama['project_id']}", f"/api/shorts/projects/{drama['project_id']}/selections"):
        r = cy.get(url)
        assert r.status_code == 404 and r.get_json()['error'] == 'That project no longer exists.'
    assert cy.post(f"/api/shorts/projects/{drama['project_id']}", data={'title': 'Hijacked'}, headers=ch).status_code == 404
    assert cy.delete(f"/api/shorts/projects/{drama['project_id']}", headers=ch).status_code == 404
    assert cy.post(f"/api/shorts/projects/{drama['project_id']}/selections", json={}, headers=ch).status_code == 404
    _uid, ben, bh = p['benny']
    assert ben.get(f"/api/shorts/projects/{drama['project_id']}").status_code == 200


def test_another_groups_shorts_cannot_be_listed_played_or_filed_into(world):
    p, _groups = world
    drama = _create(p['anna'], 'Tadhana')
    news = _create(p['cyrus'], 'Balita')
    bid = _batch(p['anna'][0], drama['project_id'])

    _uid, ben, _bh = p['benny']
    assert [b['batch_id'] for b in ben.get(f"/api/shorts/batches?project_id={drama['project_id']}").get_json()['items']] == [bid]
    assert ben.get(f'/api/shorts/file/{bid}/short_01.mp4').status_code == 200

    _uid, cy, ch = p['cyrus']
    assert cy.get(f"/api/shorts/batches?project_id={drama['project_id']}").get_json()['items'] == []
    assert cy.get('/api/shorts/batches').get_json()['items'] == []
    assert cy.get(f'/api/shorts/file/{bid}/short_01.mp4').status_code == 404
    assert cy.get(f'/api/shorts/batches/{bid}/zip').status_code == 404

    # Shorts cannot be started in, or moved into, a project of another group.
    r = cy.post('/api/shorts/analyze', data={'project_id': drama['project_id']}, headers=ch)
    assert r.status_code == 400 and 'Choose the project' in r.get_json()['error']
    _uid, ana, ah = p['anna']
    assert ana.post(f'/api/shorts/batches/{bid}/project', json={'project_id': news['project_id']},
                    headers=ah).status_code == 400

    # What is running in a project is shown to its group only.
    shorts._JOB_PROJECTS['job-drama'] = drama['project_id']
    try:
        with mock.patch.object(shorts.pipeline, 'job_list_all',
                               return_value={'job-drama': {'done': 0, 'status': 'running', 'percent': 10}}):
            assert drama['project_id'] in ben.get('/api/shorts/projects/activity').get_json()['jobs']
            assert cy.get('/api/shorts/projects/activity').get_json()['jobs'] == {}
    finally:
        shorts._JOB_PROJECTS.pop('job-drama', None)


def test_assignees_are_the_projects_group(world):
    p, _groups = world
    drama = _create(p['anna'], 'Tadhana')
    _uid, ana, _ah = p['anna']
    assert set(ana.get('/api/shorts/assignees').get_json()['items']) == {'anna', 'benny', 'erin', 'boss'}
    assert set(ana.get(f"/api/shorts/assignees?project_id={drama['project_id']}").get_json()['items']) == {
        'anna', 'benny', 'erin', 'boss'}
    _uid, boss, _h = p['boss']
    # deena, in no group, cannot use Vertical Shorts, so she is nobody's assignee.
    assert set(boss.get('/api/shorts/assignees').get_json()['items']) == {'anna', 'benny', 'cyrus', 'erin', 'boss'}
    assert set(boss.get(f"/api/shorts/assignees?project_id={drama['project_id']}").get_json()['items']) == {
        'anna', 'benny', 'erin', 'boss'}


def test_a_projects_group_is_always_its_creators_current_group(world):
    """Requested by JUN: "current group should be always the current group of user created". Nobody picks
    it -- a group_id sent is ignored -- and it follows the creator when they change group."""
    p, groups = world
    _uid, ana, ah = p['anna']
    sneaky = _create(p['anna'], 'Sneaky', group_id=str(groups['News']))
    assert sneaky['group_name'] == 'Drama' and 'Sneaky' not in _titles(p['cyrus'])
    edited = ana.post(f"/api/shorts/projects/{sneaky['project_id']}", data={'title': 'Sneaky', 'group_id': str(groups['News'])},
                      headers=ah).get_json()['project']
    assert edited['group_name'] == 'Drama', 'an edit cannot move it either'
    assert 'groups' not in ana.get('/api/shorts/projects').get_json(), 'nothing to choose from'

    # Someone in two groups: both groups see what they make.
    both = _create(p['erin'], 'Both')
    assert both['group_name'] == 'Drama, News'
    assert 'Both' in _titles(p['benny']) and 'Both' in _titles(p['cyrus'])

    # The creator moves to News: their projects go with them.
    auth.user_set_groups(p['anna'][0], [groups['News']])
    assert 'Sneaky' in _titles(p['cyrus']) and 'Sneaky' not in _titles(p['benny'])
    # Their creator in no group: only admins see it.
    auth.user_set_groups(p['anna'][0], [])
    assert 'Sneaky' not in _titles(p['cyrus']) and 'Sneaky' in _titles(p['boss'])


def test_a_group_saved_by_an_earlier_version_is_ignored(world):
    p, groups = world
    old = _create(p['anna'], 'Old show')
    path = os.path.join(shorts.SHORTS_PROJECTS_DIR, old['project_id'], 'project.json')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    data['group_id'] = groups['News']                      # filed under News by the old group picker
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    assert 'Old show' in _titles(p['benny']) and 'Old show' not in _titles(p['cyrus'])
