"""A layer can be told to come in at a time of its own, or after another layer."""
import schedule_core as sk


def _timeline(tune, names=('Background', 'Logo', 'Title', 'Date'), duration=12.0):
    recipe = dict(sk.default_recipe(), layer_tune=tune)
    return sk.build_timeline(recipe, list(names), duration, roles=['background', 'content', 'content', 'content'])


def test_clean_item_keeps_at_and_after():
    it = sk._clean_item({'at': 3.5, 'after': '2', 'after_mode': 'start', 'delay': 0.5})
    assert it['at'] == 3.5 and it['after'] == '2' and it['after_mode'] == 'start' and it['delay'] == 0.5
    assert sk._clean_item({'after': 'x/y'}).get('after') is None
    assert sk._clean_item({'after': '1', 'after_mode': 'bogus'})['after_mode'] == 'end'
    assert sk._clean_item({'at': 999})['at'] == 60.0


def test_at_puts_the_layer_at_that_time():
    tl = _timeline({'title': {'at': 5.0}})
    assert abs(tl[2]['start'] - 5.0) < 1e-6


def test_after_waits_for_the_other_layer_to_finish_or_start():
    tl = _timeline({'date': {'after': ['title'], 'after_mode': 'end', 'delay': 0.5}})
    assert abs(tl[3]['start'] - (tl[2]['start'] + tl[2]['dur'] + 0.5)) < 1e-6
    tl = _timeline({'date': {'after': ['title'], 'after_mode': 'start'}})
    assert abs(tl[3]['start'] - tl[2]['start']) < 1e-6


def test_chain_and_circle():
    tl = _timeline({'title': {'at': 4.0}, 'date': {'after': ['title']}})
    assert tl[3]['start'] >= tl[2]['start'] + tl[2]['dur'] - 1e-6
    base = _timeline({})
    tl = _timeline({'title': {'after': ['date']}, 'date': {'after': ['title']}})
    assert [round(t['start'], 3) for t in tl] == [round(t['start'], 3) for t in base]


def test_never_starts_after_the_end():
    tl = _timeline({'title': {'at': 60.0}}, duration=8.0)
    assert tl[2]['start'] <= 8.0 - 0.3 + 1e-6


def test_a_wait_by_place_in_the_file_becomes_a_wait_by_name(monkeypatch, tmp_path):
    import numpy as np
    px = np.zeros((4, 4, 4), np.uint8)
    def lay(name, path, tune=None):
        return {'name': name, 'px': px, 'x': 0, 'y': 0, '_path': path, 'tune': tune or {}}
    layers = [lay('Back', '0'), lay('Title', '1'), lay('Date', '2', {'after': '1', 'after_mode': 'end'}),
              lay('Lost', '3', {'after': '9'}), lay('Self', '4', {'after': '4'})]
    monkeypatch.setattr(sk, '_load_psd', lambda *a, **k: (layers, (1920, 1080), [], []))
    f = tmp_path / 'a.psd'
    f.write_bytes(b'x')
    art = sk.load_artwork(str(f))
    by = {ly['name']: ly['tune'] for ly in art['layers']}
    assert by['Date']['after'] == ['title'] and by['Date']['after_mode'] == 'end'
    assert 'after' not in by['Lost'] and 'after' not in by['Self']


def test_a_layer_can_be_told_when_to_go():
    tl = _timeline({'title': {'leave_at': 6.0, 'leave_dur': 0.5}})
    assert tl[2]['out'] == (6.0, 0.5)
    # Never before it has arrived.
    tl = _timeline({'title': {'leave_at': 0.0}})
    assert tl[2]['out'][0] >= tl[2]['start'] + tl[2]['dur'] - 1e-6
    # Past the end: it stays.
    assert 'out' not in _timeline({'title': {'leave_at': 60.0}}, duration=8.0)[2]


def test_a_layer_goes_after_another_starts_arrives_or_goes():
    tl = _timeline({'logo': {'leave_after': ['date'], 'leave_mode': 'end', 'leave_delay': 0.5}})
    assert abs(tl[1]['out'][0] - max(tl[3]['start'] + tl[3]['dur'] + 0.5, tl[1]['start'] + tl[1]['dur'])) < 1e-6
    tl = _timeline({'title': {'leave_at': 7.0, 'leave_dur': 0.4}, 'logo': {'leave_after': ['title'], 'leave_mode': 'out'}})
    assert abs(tl[1]['out'][0] - 7.4) < 1e-6
    tl = _timeline({'logo': {'leave_after': ['title'], 'leave_mode': 'start'}})
    assert tl[1]['out'][0] >= tl[2]['start']


def test_leave_and_arrive_together_and_circles():
    tl = _timeline({'date': {'after': ['title']}, 'title': {'leave_after': ['date'], 'leave_mode': 'out'},
                    'logo': {'leave_after': ['logo']}})
    assert 'out' not in tl[1]
    base = _timeline({})
    tl = _timeline({'logo': {'leave_after': ['title'], 'leave_mode': 'out'}, 'title': {'leave_after': ['logo'], 'leave_mode': 'out'}})
    assert 'out' not in tl[1] and 'out' not in tl[2] and tl[3]['start'] == base[3]['start']


def test_clean_item_leave():
    it = sk._clean_item({'leave_at': 4, 'leave_dur': 9, 'leave_after': '3', 'leave_mode': 'out', 'leave_delay': 1})
    assert it['leave_at'] == 4.0 and it['leave_dur'] == 3.0 and it['leave_after'] == '3' and it['leave_mode'] == 'out'
    assert sk._clean_item({'leave_after': '3', 'leave_mode': 'x'})['leave_mode'] == 'end'
