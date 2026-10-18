"""Config > Storage: retention, pinning, the low-disk guard and the admin API."""
import json
import os
import time
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import pipeline
    import shorts
    import schedule
    import storage
    import library_db

DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'CONFIG_FILE', str(tmp_path / 'storage_config.json'))
    monkeypatch.setattr(shorts, 'SHORTS_DIR', str(tmp_path / 'shorts'))
    monkeypatch.setattr(schedule, 'SCHEDULE_DIR', str(tmp_path / 'sched'))
    os.makedirs(shorts.SHORTS_DIR); os.makedirs(schedule.SCHEDULE_DIR)
    return tmp_path


def _batch(bid, age_days, status='done', size=2000):
    d = os.path.join(shorts.SHORTS_DIR, bid)
    os.makedirs(d)
    open(os.path.join(d, 's1.mp4'), 'wb').write(b'v' * size)
    json.dump({'batch_id': bid, 'orig_name': f'show {bid}', 'created': time.time() - age_days * DAY, 'status': status},
              open(os.path.join(d, 'batch.json'), 'w'))
    return d


def _plug(pid, age_days):
    d = os.path.join(schedule.SCHEDULE_DIR, pid)
    os.makedirs(d)
    open(os.path.join(d, 'plug.mp4'), 'wb').write(b'p' * 500)
    json.dump({'plug_id': pid, 'title': 'anim', 'created': time.time() - age_days * DAY}, open(os.path.join(d, 'plug.json'), 'w'))
    return d


def test_nothing_is_removed_until_a_retention_is_set(env):
    d = _batch('1700000000_aaaaaa', 400)
    assert storage.clean()['items'] == 0 and os.path.isdir(d)


def test_retention_removes_old_unpinned_idle_items_only(env):
    old, fresh = _batch('1700000000_aaaaaa', 40), _batch('1700000001_bbbbbb', 3)
    pinned, busy = _batch('1700000002_cccccc', 90), _batch('1700000003_dddddd', 90, status='rendering')
    plug_old, plug_new = _plug('1700000000_abc123', 60), _plug('1700000001_abc124', 1)
    cfg = storage.load_settings()
    cfg['retention_days'].update(shorts=30, schedule=30)
    cfg['pinned'] = ['shorts:1700000002_cccccc']
    storage.save_settings(cfg)
    preview = storage.clean(dry_run=True)
    assert preview['items'] == 2 and os.path.isdir(old), 'a dry run deletes nothing'
    r = storage.clean()
    assert r['items'] == 2 and r['bytes'] >= 2500
    assert not os.path.exists(old) and not os.path.exists(plug_old)
    for kept in (fresh, pinned, busy, plug_new):
        assert os.path.isdir(kept)


def test_old_saved_plugs_are_deleted_with_their_converted_copies(env):
    conn = library_db.database.connect()
    fname = f'ret_{int(time.time()*1000)}.mp4'
    open(os.path.join(library_db.LIBRARY_DIR, fname), 'wb').write(b'm' * 100)
    base = os.path.splitext(fname)[0]
    open(os.path.join(library_db.LIBRARY_DIR, base + '_mov.mov'), 'wb').write(b'c' * 100)
    row = conn.execute('INSERT INTO trailers (orig_name, filename, created_at) VALUES (%s,%s,%s) RETURNING id',
                       ('old plug', fname, time.time() - 100 * DAY)).fetchone()
    conn.commit(); conn.close()
    tid = row['id']
    items = [i for i in storage.inventory() if i['kind'] == 'trailers' and i['id'] == str(tid)]
    assert items and items[0]['bytes'] == 200
    cfg = storage.load_settings(); cfg['retention_days']['trailers'] = 30; storage.save_settings(cfg)
    storage.clean()
    assert library_db.library_get_row(tid) is None
    assert not os.path.exists(os.path.join(library_db.LIBRARY_DIR, fname))
    assert not os.path.exists(os.path.join(library_db.LIBRARY_DIR, base + '_mov.mov'))


def test_a_render_is_refused_on_a_nearly_full_drive(env, monkeypatch):
    assert storage.low_space_error() is None
    cfg = storage.load_settings(); cfg['min_free_gb'] = 10 ** 9; storage.save_settings(cfg)
    assert 'Not enough disk space' in storage.low_space_error()
    cfg['min_free_gb'] = 0; storage.save_settings(cfg)
    assert storage.low_space_error() is None, '0 turns the guard off'
    cfg['min_free_gb'] = 10 ** 9; storage.save_settings(cfg)
    jid = pipeline.job_new(user_id=1, username='admin', kind='promo')
    ran = []
    pipeline.run_trailer_job_gated(jid, {}, runner=lambda j, p: ran.append(j))
    assert not ran and 'Not enough disk space' in (pipeline.job_get(jid).get('error') or '')


def _admin_client(role='admin'):
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role=role, csrf_token='t')
    return c, {'X-CSRF-Token': 't'}


def test_the_storage_api_is_admin_only_and_validates(env):
    user, h = _admin_client('user')
    assert user.get('/api/admin/storage').status_code == 403
    assert user.post('/api/admin/storage/clean', headers=h).status_code == 403
    c, h = _admin_client()
    _batch('1700000000_aaaaaa', 5)
    d = c.get('/api/admin/storage').get_json()
    assert d['ok'] and d['kinds']['shorts']['count'] == 1 and d['volumes'] and 'ttl_hours' in d['temp']
    assert c.post('/api/admin/storage/settings', json={'retention_days': {'shorts': -1}}, headers=h).status_code == 400
    assert c.post('/api/admin/storage/settings', json={'retention_days': {'shorts': 14}, 'min_free_gb': 3}, headers=h).get_json()['ok']
    assert storage.load_settings()['retention_days']['shorts'] == 14
    assert c.post('/api/admin/storage/pin', json={'key': '../etc', 'pinned': True}, headers=h).status_code == 400
    assert c.post('/api/admin/storage/pin', json={'key': 'shorts:1700000000_aaaaaa', 'pinned': True}, headers=h).get_json()['ok']
    assert storage.load_settings()['pinned'] == ['shorts:1700000000_aaaaaa']
    assert 'id=p-config-storage' in c.get('/').get_data(as_text=True).replace('"', '')
