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
