"""End to end on a real (synthetic) video: the clip window follows the loud moment, and a pinned scene is kept."""
import subprocess
import time
import unittest.mock as mock
from io import BytesIO

import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main


@pytest.fixture(scope='module')
def video(tmp_path_factory):
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    tmp = tmp_path_factory.mktemp('hl_e2e')

    def clip(name, colour, vol):
        out = tmp / name
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', f'color=c={colour}:s=320x240:d=25:r=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=400:duration=25', '-af', vol,
                        '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(out)], check=True, timeout=60)
        return out
    a = clip('a.mp4', 'red', 'volume=0.05')
    b = clip('b.mp4', 'blue', "volume='if(gt(t,19),1,0.03)':eval=frame")     # quiet, then loud for its last 6 s
    lst = tmp / 'l.txt'
    lst.write_text(f"file '{a}'\nfile '{b}'\n")
    out = tmp / 'two.mp4'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(lst), '-c', 'copy', str(out)],
                   check=True, timeout=60)
    return out.read_bytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    up = tmp_path / 'uploads'
    up.mkdir()
    monkeypatch.setitem(main.app.config, 'UPLOAD_FOLDER', str(up))
    monkeypatch.setattr(main.pipeline, 'ALLOW_LOCAL_MEDIA_UPLOAD', True)
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return c, {'X-CSRF-Token': 't'}


def _preview(client, video, **extra):
    c, h = client
    data = {'file': (BytesIO(video), 'two.mp4'), 'genre': '', 'trailer_length': '8', 'scoring_mode': 'none',
            'sfx_mode': 'none', 'vo_mode': 'none', 'transition': 'cut', 'preview_only': '1', 'sync_beats': '',
            'max_scene_dur': '8'}
    data.update(extra)
    r = c.post('/api/trailer/generate', data=data, headers=h, content_type='multipart/form-data')
    assert r.status_code == 200, r.get_data(as_text=True)
    jid = r.get_json()['job_id']
    end = time.time() + 120
    while time.time() < end:
        d = c.get(f'/api/trailer/progress/{jid}', headers=h).get_json()
        if d.get('done'):
            assert d.get('error') is None, d.get('error')
            return d['result']
        time.sleep(1)
    pytest.fail('preview did not finish')


def test_the_clip_is_taken_from_the_loud_part_of_a_scene(client, video):
    res = _preview(client, video, pinned_scenes='[{"material": 1, "start": 25.0}]')
    scenes = res['scenes']
    if not any(s['start'] >= 20 for s in scenes):
        pytest.skip('scene detection did not split the two scenes here')
    store = main.pipeline.preview_get(res['preview_id'])
    loud = [s for s in store['selected'] if s['start'] >= 20][0]
    # the scene runs 25-50 s and is only loud from 44 s: an 8 s clip from its start would miss that entirely
    assert loud['trim_start'] > loud['start'] + 5, loud
    assert loud['trim_start'] + loud['selected_dur'] <= loud['start'] + loud['duration'] + 1e-6


def test_a_pinned_scene_is_in_the_cut_even_when_the_other_scores_higher(client, video):
    free = _preview(client, video, trailer_length='15', max_scene_dur='20')
    if len(free['scenes']) != 1:
        pytest.skip('this length selected more than one scene here')
    chosen_start = free['scenes'][0]['start']
    other = 25.0 if chosen_start < 20 else 0.0
    pinned = _preview(client, video, trailer_length='15', max_scene_dur='20', pinned_scenes='[{"material": 1, "start": %s}]' % other)
    starts = [s['start'] for s in pinned['scenes']]
    assert any(abs(s - other) < 1.0 for s in starts), (chosen_start, starts)
    assert any(s.get('pinned') for s in pinned['scenes'])
