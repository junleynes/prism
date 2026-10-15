"""
The shared job queue starts waiting jobs by priority: episodic plugs and
Schedule Plugs ahead of Vertical Shorts by default, and an admin can move a
waiting job up or down by hand. Requested by JUN: "Concurrency is at 2. Can we
set priority of process? like for example, episodic and schedule plug always on
top priority over vertical shorts. But also we can manually prioritize job".
"""
import threading
import time
import unittest.mock as mock

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import pipeline
from tests.dbutil import reset_tables


@pytest.fixture
def one_busy_slot(monkeypatch):
    """A limit of 1, held by someone else's job until the test lets it go."""
    reset_tables('jobs')
    monkeypatch.setattr(pipeline, 'JOB_QUEUE', [])
    monkeypatch.setattr(pipeline, 'JOB_QUEUE_META', {})
    monkeypatch.setattr(pipeline.GATE, 'limit', 1)
    with pipeline.GATE.cond:
        pipeline.GATE.running = 1
    started = []
    threads = []

    def submit(kind, name):
        jid = pipeline.job_new(kind=kind)
        pipeline.job_set_orig_name(jid, name)

        def runner(j, params):
            started.append(name)
        t = threading.Thread(target=pipeline.run_trailer_job_gated, args=(jid, {}), kwargs={'runner': runner})
        t.start()
        threads.append(t)
        _wait(lambda: jid in pipeline.JOB_QUEUE)
        return jid

    def release():
        with pipeline.GATE.cond:
            pipeline.GATE.running -= 1
            pipeline.GATE.cond.notify_all()
        for t in threads:
            t.join(timeout=20)
        assert not any(t.is_alive() for t in threads)

    yield submit, release, started
    with pipeline.GATE.cond:
        pipeline.GATE.running = 0


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError('timed out')


def _names(jids):
    return [pipeline.job_get(j)['orig_name'] for j in pipeline.JOB_QUEUE if j in jids]


def test_plugs_go_ahead_of_shorts_and_keep_their_order_within_a_level(one_busy_slot):
    submit, release, started = one_busy_slot
    jids = [submit('shorts', 'shorts 1'), submit('promo', 'promo 1'), submit('shorts', 'shorts 2'),
            submit('schedule', 'schedule 1'), submit('promo', 'promo 2')]
    assert _names(jids) == ['promo 1', 'schedule 1', 'promo 2', 'shorts 1', 'shorts 2']
    release()
    assert started == ['promo 1', 'schedule 1', 'promo 2', 'shorts 1', 'shorts 2']


def test_an_admin_can_move_a_waiting_job(one_busy_slot):
    submit, release, started = one_busy_slot
    s1 = submit('shorts', 'shorts 1')
    p1 = submit('promo', 'promo 1')
    s2 = submit('shorts', 'shorts 2')

    admin = main.app.test_client()
    with admin.session_transaction() as sess:
        sess.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    h = {'X-CSRF-Token': 't'}
    r = admin.post(f'/api/admin/jobs/{s2}/priority', json={'priority': 'urgent'}, headers=h)
    assert r.status_code == 200 and r.get_json()['position'] == 0
    assert admin.post(f'/api/admin/jobs/{p1}/priority', json={'priority': 'low'}, headers=h).status_code == 200

    listing = admin.get('/api/admin/jobs').get_json()
    assert [(q['orig_name'], q['priority'], q['priority_manual']) for q in listing['queued']] == [
        ('shorts 2', 'urgent', True), ('shorts 1', 'normal', False), ('promo 1', 'low', True)]
    assert [p['value'] for p in listing['priorities']] == ['urgent', 'high', 'normal', 'low']

    assert admin.post(f'/api/admin/jobs/{s1}/priority', json={'priority': 'top'}, headers=h).status_code == 400
    user = main.app.test_client()
    with user.session_transaction() as sess:
        sess.update(authed=True, user_id=5, username='someone', role='user', csrf_token='u')
    assert user.post(f'/api/admin/jobs/{s1}/priority', json={'priority': 'urgent'},
                     headers={'X-CSRF-Token': 'u'}).status_code == 403

    release()
    assert started == ['shorts 2', 'shorts 1', 'promo 1']
    # Once it has started, there is nothing left to reorder.
    r = admin.post(f'/api/admin/jobs/{s1}/priority', json={'priority': 'high'}, headers=h)
    assert r.status_code == 409


def test_a_free_slot_goes_to_the_front_of_the_line_not_the_first_thread_awake(one_busy_slot):
    submit, release, started = one_busy_slot
    submit('shorts', 'shorts 1')
    submit('shorts', 'shorts 2')
    submit('promo', 'promo 1')
    release()
    assert started[0] == 'promo 1'


def test_cancelling_a_waiting_job_lets_the_next_one_through(one_busy_slot):
    submit, release, started = one_busy_slot
    p1 = submit('promo', 'promo 1')
    submit('shorts', 'shorts 1')
    assert pipeline.job_cancel(p1)
    assert p1 not in pipeline.JOB_QUEUE and p1 not in pipeline.JOB_QUEUE_META
    release()
    assert started == ['shorts 1']


def test_default_levels_can_be_changed_per_feature(monkeypatch):
    monkeypatch.setenv('JOB_KIND_PRIORITY', 'shorts:high, promo:normal, bogus:urgent, schedule:sideways')
    assert pipeline._kind_priorities() == {'promo': 'normal', 'schedule': 'high', 'shorts': 'high'}
