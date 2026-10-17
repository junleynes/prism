"""
Transition media from the library. Requested by JUN: the Highlights Video
"Custom (upload matte)" transition gets Browse library, and Vertical Shorts
gets a custom transition overlay (a video with an alpha channel) picked from
the library.
"""
import os
import shutil
import subprocess
import unittest.mock as mock

import cv2
import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import pipeline
    import shorts
    import library_db

pytestmark = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not available')
FF = ['ffmpeg', '-y', '-loglevel', 'error']


def _page():
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return c.get('/').get_data(as_text=True)


def test_there_is_a_transitions_library_folder():
    assert 'transitions' in library_db.NETWORK_CATEGORY_KEYS
    cat = pipeline._network_categories()['transitions']
    assert cat['fallback'] == 'tcard' and {'mov', 'webm', 'png'} <= set(cat['exts'])


def test_the_highlights_matte_can_be_picked_from_the_library(tmp_path):
    html = _page()
    assert "openNetworkBrowser('transition_matte','transitions','Transitions')" in html
    assert 'name=transition_matte_network' in html
    staged = 'net_1_wipe.mov'
    open(os.path.join(main.app.config['UPLOAD_FOLDER'], staged), 'wb').write(b'x')
    with main.app.test_request_context('/api/trailer/generate', method='POST',
                                       data={'transition_matte_network': staged}):
        assert pipeline._resolve_upload('transition_matte', pipeline.TRANSITION_EXTENSIONS).endswith(staged)
    with main.app.test_request_context('/api/trailer/generate', method='POST',
                                       data={'transition_matte_network': 'net_1_notes.txt'}):
        open(os.path.join(main.app.config['UPLOAD_FOLDER'], 'net_1_notes.txt'), 'wb').write(b'x')
        assert pipeline._resolve_upload('transition_matte', pipeline.TRANSITION_EXTENSIONS) is None, 'wrong kind of file'
    src = open(pipeline.__file__, encoding='utf-8').read()
    assert "transition_matte_path = _resolve_upload('transition_matte', TRANSITION_EXTENSIONS)" in src


def test_the_overlay_is_a_cliffhanger_setting_not_a_join():
    html = _page()
    assert '<option value=overlay>' not in html, 'not under Where pieces join'
    i = html.index('id=sh-ending-more')
    assert html.index("openNetworkBrowser('shorts_overlay','transitions'", i) < html.index('id=sh-ending-pv', i), \
        'with the cliffhanger\'s own settings'
    assert shorts._render_options({'cut_join': 'overlay'})['cut_join'] == 'fade'
    opts = shorts._render_options({'ending': 'cliffhanger', 'ending_overlay': 'net_1_sting.mov'})
    ov, err = shorts._resolve_ending_overlay(opts)
    assert ov is None and 'no longer available' in err
    staged = os.path.join(main.app.config['UPLOAD_FOLDER'], 'net_1_sting.mov')
    subprocess.run(FF + ['-f', 'lavfi', '-i', 'color=c=red:s=64x64:r=25:d=0.6', '-c:v', 'qtrle', staged], check=True)
    ov, err = shorts._resolve_ending_overlay(opts)
    assert err is None and ov['path'] == staged and ov['name'] == 'sting.mov'
    assert abs(ov['dur'] - 0.6) < 0.1 and ov['audio'] is False
    assert shorts._resolve_ending_overlay(dict(opts, ending_overlay_name='')) == (None, None), 'optional'
    assert shorts._resolve_ending_overlay(dict(opts, ending='none')) == (None, None), 'only with the cliffhanger'
    assert shorts._resolve_ending_overlay(dict(opts, ending_overlay_name='net_1_notes.txt'))[0] is None


@pytest.mark.parametrize('with_sound', [False, True])
def test_the_overlay_plays_over_the_freeze_and_nowhere_else(tmp_path, with_sound):
    import shorts_core as sc
    d = str(tmp_path)
    src = f'{d}/src.mp4'
    subprocess.run(FF + ['-f', 'lavfi', '-i', 'color=c=blue:s=640x360:r=25:d=4', '-f', 'lavfi',
                         '-i', 'sine=f=440:sample_rate=48000:d=4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                         '-c:a', 'aac', '-shortest', src], check=True)
    ov = [*FF, '-f', 'lavfi', '-i', 'color=c=red:s=200x200:r=25:d=0.8,format=rgba,colorchannelmixer=aa=0.6']
    if with_sound:
        ov += ['-f', 'lavfi', '-i', 'sine=f=2000:d=0.8', '-c:a', 'pcm_s16le']
    subprocess.run(ov + ['-c:v', 'qtrle', '-shortest', f'{d}/ov.mov'], check=True)
    info = sc.probe_source('ffprobe', src)
    n = 50
    segs = [{'a': 0, 'b': n - 1, 'layout': 'fit', 'x': None, 'keys': None}]
    out = f'{d}/out.mp4'
    ok, err = sc.render_short('ffmpeg', src, out, 0, n, info, segs, work_dir=d, preset='ultrafast', ending=True,
                              ending_room=4, ending_hold=2.0,
                              ending_overlay={'path': f'{d}/ov.mov', 'dur': 0.8, 'audio': with_sound})
    assert ok, err
    stop = sc.cliffhanger_stop(n, 25.0, 4, 0) / 25.0
    cap = cv2.VideoCapture(out)

    def px(t, x, y):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _r, f = cap.read()
        return [int(v) for v in f[y, x]]
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    b, g, r = px(stop + 0.1, w // 2, h // 2)
    assert r > 100, 'red over the freeze'
    b, g, r = px(stop - 0.2, w // 2, h // 2)
    assert r > 100, 'starting just before it (centred on it)'
    assert px(0.4, w // 2, h // 2)[2] < 60, 'not over the action before'
    assert px(stop + 1.0, w // 2, h // 2)[2] < 60, 'gone once it has played'


@pytest.mark.parametrize('with_sound', [False, True])
def test_the_ending_preview_shows_the_overlay_across_the_freeze(tmp_path, with_sound):
    staged = f'net_2_prev{int(with_sound)}.mov'
    ov = [*FF, '-f', 'lavfi', '-i', 'color=c=red:s=200x200:r=25:d=0.8,format=rgba,colorchannelmixer=aa=0.7']
    if with_sound:
        ov += ['-f', 'lavfi', '-i', 'sine=f=2000:d=0.8', '-c:a', 'pcm_s16le']
    subprocess.run(ov + ['-c:v', 'qtrle', '-shortest', os.path.join(main.app.config['UPLOAD_FOLDER'], staged)],
                   check=True)
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    hdr = {'X-CSRF-Token': 't'}
    r = c.post('/api/shorts/ending-preview', json={'ending_hold': 2, 'ending_overlay': staged}, headers=hdr).get_json()
    assert r['ok'], r
    assert r['overlay'] is True and r['sound'] is with_sound
    out = os.path.join(main.app.config['UPLOAD_FOLDER'], os.path.basename(r['url']))
    lead = 0.7                                             # 0.8 s overlay: half of it + 0.3 s before the freeze
    assert abs(pipeline.probe_duration(out) - (lead + 2.0)) < 0.15
    cap = cv2.VideoCapture(out)

    def red(t):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _r, f = cap.read()
        b, g, r_ = [int(v) for v in f[240, 135]]
        return r_ > 150 and r_ > b + 60
    assert red(lead - 0.2) and red(lead + 0.2), 'across the freeze'
    assert not red(0.1) and not red(lead + 1.5), 'and only there'
    plain = c.post('/api/shorts/ending-preview', json={'ending_hold': 2}, headers=hdr).get_json()
    assert plain['ok'] and plain['overlay'] is False
    gone = c.post('/api/shorts/ending-preview', json={'ending_hold': 2, 'ending_overlay': 'net_9_gone.mov'},
                  headers=hdr)
    assert gone.status_code == 400 and 'pick it again' in gone.get_json()['error']


def test_group_members_are_chips_with_a_search_not_a_list_of_every_user():
    html = _page()
    assert 'data-gm-search' in html and 'data-gm-add' in html and 'data-gm-remove' in html
    assert '<input type=checkbox data-group-member' not in html, 'no checkbox per user in each group'
    assert "gmIds(document.querySelector('[data-gm][data-gid=" in html, 'saved from the chips'
