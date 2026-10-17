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


def test_the_shorts_overlay_option_is_offered_and_validated():
    html = _page()
    assert '<option value=overlay>Transition overlay (from library)</option>' in html
    assert "openNetworkBrowser('shorts_overlay','transitions','Transition overlay')" in html
    opts = shorts._render_options({'cut_join': 'overlay', 'transition_overlay': 'net_1_sting.mov'})
    assert opts['cut_join'] == 'overlay'
    ov, err = shorts._resolve_transition_overlay(opts)
    assert ov is None and 'no longer available' in err
    staged = os.path.join(main.app.config['UPLOAD_FOLDER'], 'net_1_sting.mov')
    open(staged, 'wb').write(b'x')
    ov, err = shorts._resolve_transition_overlay(opts)
    assert err is None and ov == {'path': staged, 'name': 'sting.mov'}
    assert shorts._resolve_transition_overlay(dict(opts, transition_overlay_name=''))[1].startswith('Pick the transition')
    assert shorts._resolve_transition_overlay(dict(opts, cut_join='fade')) == (None, None)
    assert shorts._render_options({'cut_join': 'bogus'})['cut_join'] == 'fade'


@pytest.mark.parametrize('with_sound', [False, True])
def test_the_overlay_plays_across_each_join_and_nowhere_else(tmp_path, with_sound):
    d = str(tmp_path)
    for name, colour, freq in (('a', 'blue', 440), ('b', 'green', 660)):
        subprocess.run(FF + ['-f', 'lavfi', '-i', f'color=c={colour}:s=360x640:r=25:d=2', '-f', 'lavfi',
                             '-i', f'sine=f={freq}:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                             '-shortest', f'{d}/{name}.mp4'], check=True)
    ov = [*FF, '-f', 'lavfi', '-i', 'color=c=red:s=200x200:r=25:d=0.8,format=rgba,colorchannelmixer=aa=0.6']
    if with_sound:
        ov += ['-f', 'lavfi', '-i', 'sine=f=2000:d=0.8', '-c:a', 'pcm_s16le']
    subprocess.run(ov + ['-c:v', 'qtrle', '-shortest', f'{d}/ov.mov'], check=True)

    ok, err = shorts._join_scenes([f'{d}/a.mp4', f'{d}/b.mp4'], f'{d}/out.mp4', 20, 'veryfast', join=0.0,
                                  overlay=f'{d}/ov.mov')
    assert ok, err
    assert abs(pipeline.probe_duration(f'{d}/out.mp4') - 4.0) < 0.15, 'hard cuts: nothing is lost to a fade'
    cap = cv2.VideoCapture(f'{d}/out.mp4')

    def px(t, x, y):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _r, f = cap.read()
        return [int(v) for v in f[y, x]]
    b, g, r = px(1.8, 180, 320)
    assert r > 100 and b > 60, 'red laid over blue just before the cut'
    b, g, r = px(2.15, 180, 320)
    assert r > 100 and g > 30, 'red laid over green just after it'
    assert px(1.8, 10, 10)[0] > 200, 'transparent around the overlay: the corner is still blue'
    assert px(0.3, 180, 320)[2] < 40 and px(3.7, 180, 320)[2] < 40, 'no overlay away from the join'
