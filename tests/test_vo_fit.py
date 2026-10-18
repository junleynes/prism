"""Laying the script's lines over a narration recording by its own sound (vo_fit)."""
import numpy as np
import pytest

import vo_fit


def _env(spans, total=40.0, hop=0.01, floor=-70.0):
    db = np.full(int(total / hop), floor)
    for a, b, lvl in spans:
        db[int(a / hop):int(b / hop)] = lvl
    return db


# The shape of a real read: phrases with pauses between, two of them (D, E) only a breath apart.
PHRASES = [(14.62, 16.92), (17.93, 19.94), (20.79, 22.67), (23.70, 24.99), (25.27, 27.16), (27.95, 28.29)]


def _segs():
    return vo_fit.speech_segments(_env([(a, b, -20.0) for a, b in PHRASES]))


def test_stretches_of_speech_are_found_with_a_pause_ending_each():
    segs = _segs()
    assert len(segs) == 6
    assert segs[0] == pytest.approx((14.62, 16.92), abs=0.03)
    # a dip of under MIN_GAP is the gap between two words, not a pause
    words = vo_fit.speech_segments(_env([(1.0, 1.5, -20.0), (1.56, 2.2, -20.0)]))
    assert len(words) == 1
    # a click is not speech
    assert vo_fit.speech_segments(_env([(1.0, 1.01, -10.0), (3.0, 4.0, -20.0)])) == [pytest.approx((3.0, 4.0), abs=0.03)]
    assert vo_fit.speech_segments(_env([])) == []


def _lines(*times):
    return [{'in': a, 'out': b} for a, b in times]


def test_a_line_ends_on_its_last_sound_even_when_the_transcript_ran_it_on_into_the_pause():
    # The transcript ends each line where the NEXT one begins (the common run-on).
    out = vo_fit.place_lines(_segs(), _lines((14.62, 17.93), (17.93, 23.70), (23.70, 25.27), (25.27, 27.9)))
    ends = [round(e - vo_fit.TAIL, 2) for _s, e in out]
    assert ends == [16.92, 22.67, 24.99, 27.16]            # each on its last sound, never into a pause
    starts = [round(s + vo_fit.LEAD, 2) for s, _e in out]
    assert starts == [14.62, 17.93, 23.70, 25.27]


def test_a_line_that_is_two_phrases_keeps_both_and_a_line_that_is_one_keeps_only_one():
    # the second line is phrases 2 and 3 (a pause inside it), the third is phrase 4 alone
    out = vo_fit.place_lines(_segs(), _lines((14.6, 16.9), (17.9, 22.6), (23.7, 24.9), (25.3, 27.1)))
    assert round(out[1][1] - vo_fit.TAIL, 2) == 22.67 and round(out[2][1] - vo_fit.TAIL, 2) == 24.99
    # ...and the same sound read as one line per phrase
    out = vo_fit.place_lines(_segs(), _lines((14.6, 16.9), (17.9, 19.9), (20.8, 22.6), (23.7, 27.1)))
    assert [round(e - vo_fit.TAIL, 2) for _s, e in out] == [16.92, 19.94, 22.67, 27.16]


def test_the_last_line_stops_at_the_last_sound_the_transcript_heard():
    out = vo_fit.place_lines(_segs(), _lines((25.27, 29.9)))
    assert round(out[0][1] - vo_fit.TAIL, 2) == 28.29      # heard through the next phrase, so it takes it
    out = vo_fit.place_lines(_segs(), _lines((25.27, 27.6)))
    assert round(out[0][1] - vo_fit.TAIL, 2) == 27.16      # the pause after it, and the next phrase, are left out


def test_starts_a_little_off_still_land_on_the_sound():
    for shift in (-0.3, -0.15, 0.15, 0.3):
        out = vo_fit.place_lines(_segs(), _lines((14.62 + shift, 17.0), (17.93 + shift, 22.7), (23.70 + shift, 25.0), (25.27 + shift, 27.2)))
        assert [round(s + vo_fit.LEAD, 2) for s, _e in out] == [14.62, 17.93, 23.70, 25.27], shift


def test_nothing_to_go_on_gives_none_and_the_caller_falls_back():
    assert vo_fit.place_lines([], _lines((1, 2))) is None
    # two lines the transcript puts on the same sound cannot be told apart
    assert vo_fit.place_lines(_segs(), _lines((14.6, 15.0), (14.7, 16.9))) is None


def test_the_narration_range_uses_the_sound_not_the_transcripts_run_on_ends(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import pipeline
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    f = tmp_path / 'v.wav'
    # 3 phrases of voice with 1 s pauses (a quiet breath in each): 0-2, 3-5, 6-8 s
    parts = []
    for i in range(3):
        parts += ['-f', 'lavfi', '-i', 'sine=f=300:d=2,volume=4', '-f', 'lavfi', '-i', 'anoisesrc=d=1:a=0.004']
    chain = ''.join(f'[{k}]' for k in range(6)) + 'concat=n=6:v=0:a=1,aformat=sample_rates=44100:channel_layouts=mono'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error'] + parts + ['-filter_complex', chain, str(f)], check=True, timeout=60)
    words = []
    for i, text in enumerate(['kakanta na ba', 'ako ang nagtimbre', 'ngayong intense']):
        for j, w in enumerate(text.split()):
            t0 = i * 3.0 + j * 0.5
            # run on: the last word of each line is timed to the start of the next line
            words.append({'word': w, 'start': t0, 'end': (i + 1) * 3.0 if j == len(text.split()) - 1 else t0 + 0.45})
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda *a, **k: (words, []))
    brief = {'vo': [{'text': 'KAKANTA NA BA', 'anchor': {'kind': 'clip'}},
                    {'text': 'AKO ANG NAGTIMBRE', 'anchor': {'kind': 'clip'}},
                    {'text': 'NGAYONG INTENSE', 'anchor': {'kind': 'clip'}}]}
    r = pipeline._narration_range_from_brief(brief, str(f))
    outs = [p['out'] for p in r['parts']]
    assert len(outs) == 3
    for k, o in enumerate(outs):
        assert abs(o - (k * 3.0 + 2.0 + vo_fit.TAIL)) < 0.06, outs     # 0.12 s after the voice stops, not 1 s of pause
    ins = [p['in'] for p in r['parts']]
    assert all(abs(i - (k * 3.0 - vo_fit.LEAD)) < 0.06 or (k == 0 and i == 0.0) for k, i in enumerate(ins)), ins
