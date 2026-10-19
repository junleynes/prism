import shorts_core as sc
import shorts


def _split_seg():
    return {'a': 0, 'b': 49, 'layout': 'split', 'size': (320, 360), 'people': [(160, 180), (480, 180)],
            'panes': [(0, 0), (320, 0)], 'keys': None}


def test_split_windows_can_be_moved_and_stay_inside_the_picture():
    segs = [_split_seg()]
    out = sc.apply_framing(segs, [], 50, [(10, 'split', [[40, 20], [10000, -50]])], 1000.0,
                           splits=[_split_seg()], disp=(640, 720))
    sp = [s for s in out if s['layout'] == 'split'][0]
    (x1, y1), (x2, y2) = sp['panes']
    w, h = sp['size']
    assert 0 <= x1 <= 640 - w and 0 <= y1 <= 720 - h
    base = sc.apply_framing([_split_seg()], [], 50, [(10, 'split', None)], 1000.0,
                            splits=[_split_seg()], disp=(640, 720))[0]['panes']
    assert x2 == 640 - w                                  # pushed past the right edge: held at it
    assert y2 == max(0, base[1][1] - 50)


def test_split_without_a_shift_is_left_as_planned():
    out = sc.apply_framing([_split_seg()], [], 50, [(10, 'split', None)], 1000.0,
                           splits=[_split_seg()], disp=(640, 720))
    base = sc.apply_framing([_split_seg()], [], 50, [(10, 'split', [[0, 0], [0, 0]])], 1000.0,
                            splits=[_split_seg()], disp=(640, 720))
    assert out[0]['panes'] == base[0]['panes']


def test_framing_request_keeps_a_split_shift_and_rejects_nonsense():
    ok, err = shorts._parse_framing({'shots': [{'at': 1.0, 'layout': 'split', 'shift': [[5, 6], [7, 8]]}]}, 'm', 10.0)
    assert err is None and ok['shots'][0]['shift'] == [[5.0, 6.0], [7.0, 8.0]]
    ov = shorts._framing_overrides({'framing': ok}, 0, 25.0)
    assert ov == [(25, 'split', [[5.0, 6.0], [7.0, 8.0]])]
    _, err = shorts._parse_framing({'shots': [{'at': 1.0, 'layout': 'split', 'shift': [[5]]}]}, 'm', 10.0)
    assert err


def test_title_cards_and_interviews_are_kinds_that_can_be_left_out():
    assert {'tcard', 'interview'} <= set(sc.EXCLUDE_KINDS)
    assert {'tcard', 'interview'} <= set(sc.VISION_KINDS)
    assert sc.parse_vision_kind('{"score":1,"desc":"x","kind":"tcard"}') == 'tcard'
    assert sc.parse_vision_kind('{"score":1,"desc":"x","kind":"title card"}') == 'tcard'
    assert sc.parse_vision_kind('{"score":2,"desc":"x","kind":"interview"}') == 'interview'
    assert sc.parse_exclude_kinds(None) >= {'tcard', 'interview'}
    visual = [{'t': 100.0, 'kind': 'interview', 'score': 2}, {'t': 300.0, 'kind': 'tcard', 'score': 1},
              {'t': 500.0, 'kind': 'story', 'score': 4}]
    blocked = sc.not_story_ranges([], [], visual, [], None, kinds=sc.parse_exclude_kinds(None))
    assert [k for _, _, ks in blocked for k in ks] == ['interview', 'tcard']
    # Unticked: that kind stays in.
    kept = sc.not_story_ranges([], [], visual, [], None, kinds=sc.parse_exclude_kinds('credits'))
    assert kept == []


def test_faces_are_shared_between_overlapping_looks(monkeypatch):
    calls = []

    def fake(path, start_f, n_frames, fps, detector, **kw):
        calls.append((start_f, n_frames))
        return [(i, [(1.0, 1.0, 1.0, 1.0)]) for i in range(0, n_frames, 5) if (start_f + i) % 5 == 0]

    class D:
        kind = 'x'
    monkeypatch.setattr(sc, 'sample_faces', fake)
    a = {}
    info = {'sar': 1.0}
    s1 = shorts._face_samples(a, 'p', info, 100, 100, 25.0, D(), 300, False, [], [])
    s2 = shorts._face_samples(a, 'p', info, 150, 100, 25.0, D(), 300, False, [], [])
    assert calls == [(100, 100), (200, 50)], 'the second look reads only what the first had not'
    assert [i for i, _ in s2][0] == 0 and len(s2) == 20
