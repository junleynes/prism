"""Retiming captions by matching the sound of the cut video against the uncut part videos."""
import io
import os
import subprocess

import numpy as np
import pytest

import audioalign
import captionretime as rt

SR = 48000


def _sound(sec, seed):
    r = np.random.default_rng(seed)
    n = int(sec * SR)
    out, pos, t = np.zeros(n, np.float32), 0, np.arange(n) / SR
    while pos < n:
        L = min(int(r.uniform(0.08, 0.4) * SR), n - pos)
        seg = np.sin(2 * np.pi * r.uniform(120, 900) * t[:L]) * r.uniform(0.05, 0.5) + r.normal(0, 0.02, L) * (r.random() < 0.4)
        out[pos:pos + L] = seg * np.hanning(L)
        pos += L + int(r.uniform(0, 0.15) * SR)
    return out


def _write(path, a):
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '-', '-c:a', 'aac',
                    '-b:a', '128k', path], input=a.astype(np.float32).tobytes(), check=True)


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp('aa')
    p1, p2 = _sound(60, 1), _sound(50, 2)
    sl = lambda a, x, y: a[int(x * SR):int(y * SR)]
    # Cut out 20.0-22.5 and 40.0-41.2 (a cut shorter than two seconds) of part 1, and 5-15 of part 2; a quieter copy.
    cut = np.concatenate([sl(p1, 0, 20), sl(p1, 22.5, 40), sl(p1, 41.2, 60), sl(p2, 0, 5), sl(p2, 15, 50)]) * 0.6
    paths = {k: str(d / f'{k}.m4a') for k in ('p1', 'p2', 'cut')}
    _write(paths['p1'], p1)
    _write(paths['p2'], p2)
    _write(paths['cut'], cut)
    return paths


def _expect(part, t):
    """Where time t of a part lands in the cut video, or None when it was cut out."""
    if part == 0:
        if t < 20: return t
        if 20 <= t < 22.5 or 40 <= t < 41.2: return None
        return t - 2.5 if t < 40 else t - 3.7
    base = 60 - 3.7
    if t < 5: return base + t
    if t < 15: return None
    return base + t - 10


def test_the_cuts_are_found_from_the_sound(media):
    m = audioalign.align_files('ffmpeg', media['cut'], [media['p1'], media['p2']])
    st = m['stretches']
    assert len(st) == 4, st
    # The end of part 1 runs straight into part 2 (nothing cut between them), so that is one stretch.
    for (v0, v1, s0), (ev0, es0) in zip(st, [(0, 0), (20, 22.5), (37.5, 41.2), (61.3, 75)]):
        assert abs((s0 - v0) - (es0 - ev0)) < 0.04, st          # the offset: to a frame
        assert abs(v0 - ev0) < 0.35, st                         # the cut point: within a pause, at worst
    assert abs(m['part_offsets'][1] - 60) < 0.05


def test_captions_follow_the_sound_and_those_in_cuts_go(media):
    m = audioalign.align_files('ffmpeg', media['cut'], [media['p1'], media['p2']])
    parts = [[{'start': s, 'end': s + 1.5, 'text': f'P1 line {i}'} for i, s in enumerate((2, 12, 20.5, 25, 39.0, 40.3, 50))],
             [{'start': s, 'end': s + 1.5, 'text': f'P2 line {i}'} for i, s in enumerate((1, 8, 20, 40))]]
    res = rt.align_audio(parts, m['stretches'], m['part_offsets'], m['part_durations'])
    for c in res['cues']:
        want = _expect(c['part'], c['start'])
        if c['start'] == 39.0:
            # runs into the 40.0 cut: kept, trimmed to end at the cut
            assert not c['drop'] and abs(c['new_start'] - 36.5) < 0.06 and abs(c['new_end'] - 37.5) < 0.35
        elif c['start'] == 40.3:
            # starts inside the 40.0-41.2 cut and runs on after it: what is left of it starts at the cut
            assert not c['drop'] and c['status'] == 'placed' and abs(c['new_start'] - 37.5) < 0.35
        elif want is None:
            assert c['drop'], c
        else:
            assert not c['drop'] and abs(c['new_start'] - want) < 0.06, (c['part'], c['start'], c.get('new_start'), want)
    assert [round(x['seconds'], 1) for x in res['cuts']] == [2.5, 1.2, 10.0]


def test_route_uses_the_part_videos_when_given(media, monkeypatch):
    import main
    import pipeline
    monkeypatch.setattr(pipeline, 'ALLOW_LOCAL_MEDIA_UPLOAD', True)
    monkeypatch.setattr(pipeline, 'transcribe_video_detailed', lambda *a, **k: 1 / 0)    # never asked
    srt = lambda cues: '\n'.join(f"{i + 1}\n00:00:{int(s):02d},000 --> 00:00:{int(s) + 1:02d},500\n{t}\n" for i, (s, t) in enumerate(cues))
    client = main.app.test_client()
    with client.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    rb = lambda p: io.BytesIO(open(p, 'rb').read())
    data = {'rt_video': (rb(media['cut']), 'cut.m4a'),
            'captions': [(io.BytesIO(srt([(2, 'one'), (21, 'gone'), (30, 'three')]).encode()), 'part1.srt'),
                         (io.BytesIO(srt([(1, 'four'), (20, 'five')]).encode()), 'part2.srt')],
            'rt_parts': [(rb(media['p2']), 'part2.m4a'), (rb(media['p1']), 'part1.m4a')]}
    r = client.post('/api/stt/retime', data=data, headers={'X-CSRF-Token': 't'}, content_type='multipart/form-data')
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] and d['by'] == 'audio', d
    assert d['part_videos'] == ['part1.m4a', 'part2.m4a']
    assert 'gone' not in d['srt'] and '00:00:27,5' in d['srt'], d['srt']
    data['rt_parts'] = [(rb(media['p1']), 'part1.m4a')]
    data['rt_video'] = (rb(media['cut']), 'cut.m4a')
    data['captions'] = [(io.BytesIO(b'1\n00:00:01,000 --> 00:00:02,000\nx\n'), 'a.srt'), (io.BytesIO(b'1\n00:00:01,000 --> 00:00:02,000\ny\n'), 'b.srt')]
    r = client.post('/api/stt/retime', data=data, headers={'X-CSRF-Token': 't'}, content_type='multipart/form-data')
    assert r.status_code == 400 and 'one part video per caption file' in r.get_json()['error']


def test_captions_in_a_hole_of_the_matching_are_kept_not_dropped():
    """The sound was not matched between 40 and 50 s of the cut video, but the video there is source: only the
    middle of the source the cut removed is dropped, and a hole with no shift across it is no cut at all."""
    cues = lambda *spans: [{'start': a, 'end': b, 'text': f'line {i}'} for i, (a, b) in enumerate(spans)]
    # cut video: 0-40 is source 0-40; 50-100 is source 70-120 (30 s of source gone, 10 s of video unmatched)
    parts = [cues((10, 12), (41, 43), (55, 57), (66, 68), (80, 82))]
    res = rt.align_audio(parts, [(0, 40, 0), (50, 100, 70)], [0.0], [200.0], video_duration=100.0)
    by = {c['idx']: c for c in res['cues']}
    assert not by[0]['drop'] and by[0]['status'] == 'matched' and abs(by[0]['new_start'] - 10) < 0.01
    assert not by[1]['drop'] and by[1]['status'] == 'check' and 40 <= by[1]['new_start'] < 45     # from the earlier side
    assert by[2]['drop'], 'the middle is what the cut removed'
    assert not by[3]['drop'] and by[3]['status'] == 'check' and 45 <= by[3]['new_start'] < 50    # from the later side
    assert not by[4]['drop'] and by[4]['status'] == 'matched' and abs(by[4]['new_start'] - 60) < 0.01
    # The same hole with the same offset on both sides is not a cut: nothing there is dropped.
    res = rt.align_audio(parts, [(0, 40, 0), (50, 100, 10)], [0.0], [200.0], video_duration=100.0)
    assert not res['cuts'] and not any(c['drop'] for c in res['cues'][:2])
