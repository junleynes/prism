import pipeline as P


def _clip(start, dur, fixed):
    return {'start': start, 'end': start + 60, 'duration': 60, 'selected_dur': dur, 'trim_start': start, 'cue_fixed': fixed}


def test_written_cuts_are_not_stretched_or_shrunk():
    sel = [_clip(213, 4.0, True), _clip(35, 7.0, True)]
    for target in (6.0, 30.0):
        out = P._rebalance_selected_durations(sel, target)
        assert [s['selected_dur'] for s in out] == [4.0, 7.0]


def test_only_start_only_cues_absorb_the_difference():
    sel = [_clip(213, 4.0, True), _clip(40, 4.0, False)]
    out = P._rebalance_selected_durations(sel, 10.0)
    assert out[0]['selected_dur'] == 4.0 and abs(out[1]['selected_dur'] - 6.0) < 0.2


def test_cue_clip_marks_complete_cues(monkeypatch):
    monkeypatch.setattr(P, 'probe_duration', lambda p: 400.0)
    cues = [{'time': 213.0, 'out': 217.0, 'material': 1, 'desc': 'HABOL TACKLE'},
            {'time': 40.0, 'material': 1, 'desc': 'start only'}]
    clips = P.build_cue_clips(cues, ['/x.mp4'])
    by = {c['start']: c for c in clips}
    assert by[213.0]['cue_fixed'] and by[213.0]['selected_dur'] == 4.0 and not by[40.0]['cue_fixed']
