import cue_locate as cl


def W(txt, t0, step=0.4):
    return [{'word': w, 'start': t0 + i * step, 'end': t0 + i * step + 0.3} for i, w in enumerate(txt.split())]


WORDS = (W('hello there everyone', 2) + W('nina anak what is our password', 40.0)
         + W('can you put your three fingers in front of your face', 75.0))


def test_dialog_cue_moves_to_where_it_is_spoken():
    cues = [{'time': 43.0, 'out': 45.0, 'desc': "NINA, ANAK, WHAT'S OUR PASSWORD PASSWORD?"}]
    out, notes = cl.locate_cues(cues, {1: WORDS}, 1)
    c = out[0]
    assert c['material'] == 1 and c['located']
    assert abs(c['time'] - (40.0 - cl.LEAD)) < 0.01
    assert c['out'] >= 45.0 - 3 and notes


def test_picks_the_material_that_says_it():
    cues = [{'time': 5.0, 'desc': 'CAN YOU PUT YOUR 3 FINGERS IN FRONT OF YOUR FACE'}]
    out, _ = cl.locate_cues(cues, {1: W('nothing at all here', 3), 2: WORDS}, 2)
    assert out[0]['material'] == 2 and abs(out[0]['time'] - 74.85) < 0.01


def test_no_words_found_keeps_written_time_on_material_one():
    cues = [{'time': 40.0, 'out': 43.0, 'desc': 'WIDE REACTS KUHA CP'}]
    out, _ = cl.locate_cues(cues, {1: WORDS}, 1)
    assert out[0]['material'] == 1 and out[0]['time'] == 40.0 and not out[0].get('located')


def test_labelled_cue_untouched_and_no_transcript_ok():
    out, _ = cl.locate_cues([{'time': 9.0, 'material': 2, 'desc': 'x'}, {'time': 1.0, 'desc': 'MOM I NEED MONEY'}], {}, 2)
    assert [c['material'] for c in out] == [1, 2] and out[0]['time'] == 1.0


def test_picture_match_moves_cue_only_when_clearly_better():
    c = {'time': 40.0, 'out': 43.0, 'desc': 'WIDE REACTS', 'material': 1}
    out = cl.pick_by_look(c, {40.0: 2, 41.5: 8, 38.5: 3})
    assert out['located'] == 'look' and abs(out['time'] - 41.2) < 0.01 and abs(out['out'] - 44.2) < 0.01
    assert cl.pick_by_look(c, {40.0: 7, 41.5: 8}) is c       # not clearly better
    assert cl.pick_by_look(c, {40.0: 1, 41.5: 4}) is c       # no real match
    assert cl.pick_by_look(c, {}) is c


def test_model_reading_splits_spoken_from_shots():
    cues = [{'time': 40.0, 'desc': 'NINA SAYS WHAT IS OUR PASSWORD'}, {'time': 50.0, 'desc': 'WIDE REACTS KUHA CP'}]
    reply = '{"cues":[{"n":1,"dialog":"what is our password","visual":null,"material":null},{"n":2,"dialog":null,"visual":"wide shot, people react","material":null}]}'
    a = cl.parse_analysis(reply, cues)
    assert a[0]['dialog'] == 'what is our password' and a[1]['dialog'] is None and a[1]['visual'].startswith('wide')
    out, _ = cl.locate_cues(a, {1: WORDS}, 1)
    assert out[0]['located'] is True and not out[1].get('located')
    assert cl.parse_analysis('not json', cues) == cues


# ---- the two real scripts ----
import os
import scriptbrief as sb

FIX = os.path.join(os.path.dirname(__file__), 'fixtures')


def _cues(name, avail):
    b = sb.read_layout(sb.pdf_pages(open(os.path.join(FIX, name), 'rb').read()))
    return b, sb.cues_from_brief(b, avail)


def test_single_material_script_keeps_every_spoken_line_and_shot():
    b, cues = _cues('script_single_material.pdf', {1})
    said = [c['desc'] for c in cues if c['kind'] == 'sot']
    assert said[0] == 'MOM I NEED MONEY' and len(said) == 4
    assert any('WIDE REACTS KUHA CP' == c['desc'] for c in cues if c['kind'] == 'shot')   # "CP" stays on the clip's line
    assert all(not c.get('material') for c in cues)                                      # nothing is labelled
    assert [v['text'] for v in b['vo']][0] == "MATUTO PARA ‘DI MABUDOL!"


def test_two_material_script_keeps_the_lines_under_each_timecode():
    _b, cues = _cues('script_two_materials.pdf', {1, 2})
    by = {(c['material'], c['time']): c for c in cues}
    assert 'TRAYDOR KA' in by[(1, 218.0)]['desc'] and by[(1, 218.0)]['kind'] == 'sot'
    assert by[(2, 35.0)]['desc'] == 'BUNOT GALO! TAWA MARK'
    assert cl.is_complete(by[(1, 218.0)]) and cl.is_complete(by[(2, 35.0)])   # labelled with in and out: followed as written


def test_labelled_spoken_line_is_looked_for_only_on_its_own_material():
    c = {'time': 5.0, 'material': 2, 'kind': 'sot', 'desc': 'NASAN KAPATID KO'}      # a start only: incomplete
    other = W('nasan kapatid ko', 30)
    mine = W('nasan kapatid ko', 44)
    out, _ = cl.locate_cues([c], {1: other, 2: mine}, 2)
    assert out[0]['material'] == 2 and abs(out[0]['time'] - 43.85) < 0.01


def test_a_complete_cue_is_cut_exactly_as_written():
    c = {'time': 218.0, 'out': 223.0, 'material': 1, 'kind': 'sot', 'desc': 'TRAYDOR KA'}
    assert cl.is_complete(c)
    out, notes = cl.locate_cues([c], {1: W('traydor ka', 230)}, 2)
    assert out[0]['time'] == 218.0 and out[0]['out'] == 223.0 and not notes
    assert not cl.is_complete({'time': 1.0, 'material': 1}) and not cl.is_complete({'time': 1.0, 'out': 4.0})
