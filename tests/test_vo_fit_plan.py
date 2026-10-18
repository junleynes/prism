import cutcraft as cc


def _words(spec):
    return [{'word': w, 'start': a, 'end': b} for w, a, b in spec]


LINES = [{'at': None, 'text': 'one two three', 'optional': False},
         {'at': None, 'text': 'four five six', 'optional': False}]


def _run(words, sound, window_end, **kw):
    return cc.plan_vo(LINES, words, start_at=0.0, window_end=window_end, sound=sound, **kw)


def test_whole_line_times_are_snapped_onto_the_sound():
    # the transcript says line 1 ends at 2.4, in the pause; the sound stops at 2.0
    words = _words([('one', 0.0, 0.8), ('two', 0.8, 1.6), ('three', 1.6, 2.4),
                    ('four', 3.0, 3.8), ('five', 3.8, 4.6), ('six', 4.6, 5.4)])
    sound = [(0.1, 2.0), (3.1, 5.0)]
    plan = _run(words, sound, None)
    p1, p2 = plan['pieces'][0], plan['pieces'][-1]
    assert p1['b'] <= 2.0 + cc.TRAIL_MAX + 0.01                                      # not the 2.4 of the transcript
    assert p2['b'] <= 5.0 + cc.TRAIL_MAX + 0.01


def test_hard_speedup_keeps_every_word_before_any_trim():
    words = _words([('one', 0.0, 0.8), ('two', 0.8, 1.6), ('three', 1.6, 2.4),
                    ('four', 3.0, 3.8), ('five', 3.8, 4.6), ('six', 4.6, 5.4)])
    sound = [(0.1, 2.3), (3.1, 5.3)]
    # the speech is about 4.6 s with its pauses tightened; 4.4 s asks for ~15 %: more than the usual 8 %, less than the hard cap
    plan = _run(words, sound, 4.4)
    assert plan['fits'] and all(r['status'] != 'cut off' for r in plan['lines'])
    assert any('every word is kept' in n for n in plan['notes'])
    assert max(p['tempo'] for p in plan['pieces']) <= cc.HARD_TEMPO + 1e-6


def test_trim_happens_only_in_a_pause_and_never_through_a_word():
    words = _words([('one', 0.0, 0.8), ('two', 0.8, 1.6), ('three', 1.6, 2.4),
                    ('four', 3.0, 3.8), ('five', 3.8, 4.6), ('six', 4.6, 5.4)])
    sound = [(0.1, 2.3), (3.1, 3.9), (4.2, 5.3)]           # 'four' | 'five six' with a real pause after 'four'
    plan = _run(words, sound, 3.6)                          # not even the hard speed-up fits all three words
    last = plan['pieces'][-1]
    # it ends where the recording is quiet: at the end of a stretch of speech (+ the small tail), not inside one
    in_sound = any(a + 0.02 < last['b'] < b - 0.02 for a, b in sound)
    assert not in_sound


def test_a_line_with_no_pause_to_cut_in_is_dropped_whole_not_cut_through():
    words = _words([('one', 0.0, 0.8), ('two', 0.8, 1.6), ('three', 1.6, 2.4),
                    ('four', 3.0, 3.8), ('five', 3.8, 4.6), ('six', 4.6, 5.4)])
    sound = [(0.1, 2.3), (3.1, 5.3)]                        # line 2 is one unbroken run
    plan = _run(words, sound, 3.5)
    assert plan['lines'][1]['status'] == 'cut off'
    assert all(not (3.1 + 0.02 < p['b'] < 5.3 - 0.02) for p in plan['pieces'])
