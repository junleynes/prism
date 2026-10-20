"""Retiming zero-based, uncut caption files onto a combined video with cuts."""
import random

import captionfile
import captionretime as rt

SYL = ['ka', 'ta', 'ma', 'na', 'sa', 'ba', 'la', 'pa', 'ri', 'to', 'mi', 'lo', 'gu', 'han', 'ang', 'ko', 'mo']


def _sentences(n, seed=3):
    r = random.Random(seed)
    return [' '.join(''.join(r.choice(SYL) for _ in range(r.randint(1, 3))) for _ in range(r.randint(4, 8)))
            for _ in range(n)]


def _scc(cues, drop=True):
    """Pop-on SCC with one load line and one erase line per caption."""
    def hexw(text):
        b = text.encode('latin-1')
        if len(b) % 2:
            b += b'\x80'
        return ' '.join(f'{b[i]:02x}{b[i + 1]:02x}' for i in range(0, len(b), 2))
    out = ['Scenarist_SCC V1.0', '']
    for c in cues:
        out += [f"{rt._frames_tc(int(round((c['start'] - 0.5) / rt.FRAME)), drop)}\t9420 9420 94ae 94ae 9470 9470 "
                f"{hexw(c['text'])} 942f 942f", '',
                f"{rt._frames_tc(int(round(c['end'] / rt.FRAME)), drop)}\t942c 942c", '']
    return '\n'.join(out)


def _case():
    S = _sentences(40)
    cut = set(range(8, 12))
    prog = [(i * 4.0 + 1.0, i * 4.0 + 3.5) for i in range(40)]

    def part(lo, hi):
        base = prog[lo][0] - 1.0
        return [{'start': prog[i][0] - base, 'end': prog[i][1] - base,
                 'text': ('>> MYLA: ' if i % 5 == 0 else '') + S[i] + (' [LAUGHS]' if i % 7 == 0 else '')}
                for i in range(lo, hi)]
    vt, t = {}, 1.0
    for i in range(40):
        if i in cut:
            continue
        if i == 20:
            t += 6.0
        vt[i] = t
        t += 4.0
    r = random.Random(9)
    words = []
    for i, t0 in vt.items():
        for k, w in enumerate(S[i].split()):
            w = ''.join(c if r.random() > 0.12 else r.choice('aeiou') for c in w)
            words.append({'word': w, 'start': t0 + k * 0.3, 'end': t0 + k * 0.3 + 0.28})
    return S, cut, vt, part(0, 20), part(20, 40), words


def test_clean_text_for_matching():
    assert rt.strip_labels('>> MYLA: Hindi ko alam') == 'Hindi ko alam'
    assert rt.strip_labels('JUN: Ano? >> MYLA: Wala.') == 'Ano? Wala.'
    assert rt.strip_descriptors('Sige [LAUGHS] na ♪ music ♪ (door slams)') == 'Sige na music'
    assert rt.norm('Hindi, Ko álam!') == 'hindi ko alam'


def test_cuts_are_found_and_cut_captions_dropped():
    S, cut, vt, p1, p2, words = _case()
    res = rt.align([p1, p2], words, [])
    assert [c['seconds'] for c in res['cuts']] == [16.0]
    for c in res['cues']:
        i = c['idx'] + (20 if c['part'] else 0)
        if i in cut:
            assert c['drop'], i
        else:
            assert not c['drop'] and abs(c['new_start'] - vt[i]) < 1.0, (i, c['new_start'], vt[i])
    assert rt.summary(res)['dropped'] == 4


def test_srt_keeps_or_removes_labels_and_descriptors():
    S, cut, vt, p1, p2, words = _case()
    res = rt.align([p1, p2], words, [])
    full = rt.write_srt(res)
    assert '>> MYLA:' in full and '[LAUGHS]' in full
    plain = rt.write_srt(res, labels=True, descriptors=True)
    assert 'MYLA' not in plain and 'LAUGHS' not in plain and S[1] in plain
    assert S[9] not in full and S[10] not in full, 'captions inside the cut are gone'


def test_scc_is_retimed_line_by_line_and_reads_back():
    S, cut, vt, p1, p2, words = _case()
    t1, t2 = _scc(p1), _scc(p2)
    parts = [captionfile.parse_scc(t1), captionfile.parse_scc(t2)]
    assert len(parts[0]) == 20 and parts[0][0]['lines'] and parts[0][0]['erase'] is not None
    res = rt.align(parts, words, [])
    out = rt.write_scc([t1, t2], parts, res)
    back = captionfile.parse_scc(out)
    assert len(back) == 36
    texts = [c['text'] for c in back]
    assert all(S[i] in ' '.join(texts) for i in (0, 7, 12, 39)) and S[9] not in ' '.join(texts)
    # a caption's start is where the video says its words are (the flip comes a little after the load line)
    by_text = {c['text']: c for c in back}
    for i in (0, 7, 12, 20, 39):
        hit = next(c for c in back if S[i] in c['text'])
        assert abs(hit['start'] - vt[i]) < 1.2, (i, hit['start'], vt[i])
    assert out.startswith('Scenarist_SCC V1.0') and ';' in out


def test_drop_frame_timecode_round_trip():
    for fr in (0, 1, 1799, 1800, 17981, 17982, 18000, 107892, 200000):
        for drop in (True, False):
            back, d = rt._tc_frames(rt._frames_tc(fr, drop))
            assert back == fr and d == drop, (fr, drop)


def test_a_file_without_any_match_is_flagged_and_placed_after_the_one_before():
    S, cut, vt, p1, p2, words = _case()
    junk = [{'start': i * 3.0, 'end': i * 3.0 + 2, 'text': f'zzzz qqqq xxxx {i}'} for i in range(5)]
    res = rt.align([p1, junk], [w for w in words if w['start'] < 80], [])
    assert all(c['status'] == 'check' for c in res['cues'] if c['part'] == 1)


def test_route_retimes_several_scc_files_onto_a_cut_video(monkeypatch):
    import io
    import main
    import pipeline
    S, cut, vt, p1, p2, words = _case()
    t1, t2 = _scc(p1), _scc(p2)
    seen = {}

    def fake(path, language=None, prompt=None):
        seen['prompt'], seen['language'] = prompt, language
        return words, [], {'ok': True, 'reason': None}
    monkeypatch.setattr(pipeline, 'transcribe_video_detailed', fake)
    monkeypatch.setattr(pipeline, 'ALLOW_LOCAL_MEDIA_UPLOAD', True)
    client = main.app.test_client()
    with client.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    data = {'rt_video': (io.BytesIO(b'not really a video'), 'episode.mp4'), 'language': 'tl', 'strip_labels': '1',
            'captions': [(io.BytesIO(t2.encode()), 'part2.scc'), (io.BytesIO(t1.encode()), 'part1.scc')]}
    r = client.post('/api/stt/retime', data=data, headers={'X-CSRF-Token': 't'}, content_type='multipart/form-data')
    d = r.get_json()
    assert r.status_code == 200 and d['ok'], d
    assert d['files'] == ['part1.scc', 'part2.scc'], 'taken in file-name order, not the order sent'
    assert d['summary']['dropped'] == 4 and len(d['summary']['cuts']) == 1 and abs(d['summary']['cuts'][0]['seconds'] - 16.0) < 1.0
    assert 'MYLA' not in d['srt'] and d['scc'].startswith('Scenarist_SCC')
    assert seen['language'] == 'tl' and seen['prompt'], 'the captions\' own words are given to Whisper'
