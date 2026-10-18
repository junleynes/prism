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
    c = {'time': 218.0, 'out': 223.0, 'material': 1, 'kind': 'shot', 'desc': 'TRAYDOR KA'}      # a shot: its words are not speech
    assert cl.is_complete(c)
    out, notes = cl.locate_cues([c], {1: W('traydor ka', 230)}, 2)
    assert out[0]['time'] == 218.0 and out[0]['out'] == 223.0 and not notes
    assert not cl.is_complete({'time': 1.0, 'material': 1}) and not cl.is_complete({'time': 1.0, 'out': 4.0})


def test_spoken_lines_are_not_extra_clips_when_the_written_shots_fill_the_plug():
    shots = [{'time': 213.0, 'out': 217.0, 'material': 1, 'kind': 'shot', 'desc': 'HABOL'},
             {'time': 35.0, 'out': 42.0, 'material': 2, 'kind': 'shot', 'desc': 'BUNOT'}]
    sots = [{'time': 218.0, 'out': 223.0, 'material': 1, 'kind': 'sot', 'desc': 'TRAYDOR'},
            {'time': 12.0, 'out': 20.0, 'material': 2, 'kind': 'sot', 'desc': 'AKO'}]
    out, note = cl.drop_sot_when_clips_fill(shots + sots, 15)
    assert out == shots and note
    one = [{'time': 40.0, 'out': 43.0, 'kind': 'shot', 'desc': 'WIDE'}]          # 3 s of a 15 s plug: the SOTs are needed
    out, note = cl.drop_sot_when_clips_fill(one + sots, 15)
    assert len(out) == 3 and note is None


def test_analysis_prompt_lists_materials_and_script():
    pr = cl.analysis_prompt([{'desc': 'WIDE REACTS'}], ['FIREWA_A.mov', 'FIREWA_B.mov'], 'whole script text')
    assert '1. FIREWA_A.mov' in pr and '2. FIREWA_B.mov' in pr and 'whole script text' in pr


def test_parse_analysis_maps_material_and_ignores_out_of_range():
    cues = [{'desc': 'a'}, {'desc': 'b'}, {'desc': 'c', 'material': 1}]
    reply = '{"cues":[{"n":1,"material":2},{"n":2,"material":"M9"},{"n":3,"material":2}]}'
    out = cl.parse_analysis(reply, cues, 2)
    assert out[0]['material'] == 2 and 'material' not in out[1] and out[2]['material'] == 1


def test_locate_keeps_script_order():
    cues = [{'desc': 'x', 'time': 50.0, 'order': 0, 'material': 2}, {'desc': 'y', 'time': 5.0, 'order': 1, 'material': 1}]
    out, _ = cl.locate_cues(cues, {}, 2)
    assert [c['order'] for c in out] == [0, 1]


def test_roles_drop_narrator_and_keep_complete():
    cues = [{'desc': 'narrator line', 'time': 5.0, 'role': 'vo'},
            {'desc': 'written', 'time': 10.0, 'out': 14.0, 'material': 1, 'role': 'vo'},
            {'desc': 'speech', 'time': 20.0, 'role': 'sot', 'kind': 'shot'}]
    out, notes = cl.apply_roles(cues)
    assert [c['desc'] for c in out] == ['written', 'speech'] and out[1]['kind'] == 'sot' and len(notes) == 1


def test_flow_applies_only_without_script_order_and_summary():
    cues = [{'desc': 'a', 'time': 1.0}, {'desc': 'b', 'time': 2.0}]
    reply = '{"cues":[{"n":1,"role":"shot"},{"n":2,"role":"shot"}],"flow":[2,1],"summary":"A fire."}'
    out = cl.parse_analysis(reply, cues)
    assert [c['desc'] for c in out] == ['b', 'a'] and out[0]['ai_summary'] == 'A fire.'
    ordered = [dict(c, order=i) for i, c in enumerate(cues)]
    assert [c['desc'] for c in cl.parse_analysis(reply, ordered)] == ['a', 'b']


def _words(text, t0):
    out = []
    for i, w in enumerate(text.split()):
        out.append({'word': w, 'start': t0 + i * 0.4, 'end': t0 + i * 0.4 + 0.3})
    return out


def test_shot_and_spoken_line_of_the_same_moment_are_one_clip():
    cues = [{'order': 1, 'time': 40.0, 'out': 45.0, 'kind': 'sot', 'desc': 'NINA ANAK WHAT IS OUR PASSWORD'},
            {'order': 2, 'time': 40.0, 'out': 43.0, 'kind': 'shot', 'desc': 'WIDE REACTS'},
            {'order': 3, 'time': 71.0, 'out': 74.0, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}]
    out = cl.merge_overlaps(cues)
    assert len(out) == 2 and out[0]['time'] == 40.0 and out[0]['out'] == 45.0
    assert out[0]['kind'] == 'shot' and out[0]['speech'] == 'NINA ANAK WHAT IS OUR PASSWORD' and out[0]['desc'] == 'WIDE REACTS'


def test_written_cut_without_material_takes_the_material_it_is_said_in_and_stays_as_written():
    cue = {'order': 0, 'time': 71.0, 'out': 74.0, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}
    tr = {1: _words('hello there nothing relevant here at all', 70.0),
          2: _words('can you do something for me', 71.5)}
    out, notes = cl.locate_cues([cue], tr, 2)
    assert out[0]['material'] == 2 and out[0]['time'] == 71.0 and out[0]['out'] == 74.0 and cl.is_complete(out[0])


def test_unresolved_cues_take_the_previous_cues_material():
    out = cl.fallback_materials([{'order': 0, 'time': 1.0, 'material': 2}, {'order': 1, 'time': 5.0}, {'order': 2, 'time': 9.0}])
    assert [c['material'] for c in out] == [2, 2, 2]
    assert cl.fallback_materials([{'order': 0, 'time': 1.0}])[0]['material'] == 1


def test_scene_captions_come_from_the_vision_model(monkeypatch):
    import pipeline
    class R:
        stdout = b'\xff\xd8jpeg'
    class P:
        def json(self):
            return {'response': ' A woman looks shocked at a phone. Close-up. '}
    monkeypatch.setattr(pipeline, '_check_service', lambda *a, **k: {'status': 'up'})
    monkeypatch.setattr(pipeline.subprocess, 'run', lambda *a, **k: R())
    monkeypatch.setattr(pipeline.requests, 'post', lambda *a, **k: P())
    s = [{'source_path': '/x.mp4', 'start': 40.0, 'selected_dur': 4.0}, {'ai_desc': 'kept', 'start': 1.0}]
    pipeline._caption_selected(s)
    assert s[0]['ai_desc'] == 'A woman looks shocked at a phone. Close-up.' and s[1]['ai_desc'] == 'kept'


def test_material_is_the_one_whose_whole_recording_says_the_line_best():
    cue = {'order': 0, 'time': 71.0, 'out': 74.0, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}
    tr = {1: _words('can you do something', 71.2) + _words('totally different words follow now', 80.0),
          2: _words('hello can you do something for me please', 71.0)}
    out, _ = cl.locate_cues([cue], tr, 2)
    assert out[0]['material'] == 2 and out[0]['time'] == 71.0 and 74.0 <= out[0]['out'] < 75.0


def test_line_said_far_from_its_written_time_moves_to_where_it_is_said():
    cue = {'order': 0, 'time': 71.0, 'out': 74.0, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}
    tr = {1: _words('nothing here', 5.0), 2: _words('can you do something for me', 140.0)}
    out, _ = cl.locate_cues([cue], tr, 2)
    assert out[0]['material'] == 2 and out[0]['located'] and out[0]['time'] > 130


def _sentence(t0, text, step=0.5, dur=0.4):
    return [{'word': w, 'start': t0 + i * step, 'end': t0 + i * step + dur} for i, w in enumerate(text.split())]


def test_edges_inside_a_word_move_to_the_gap_beside_it():
    ws = _sentence(10.0, 'one two three four five')            # two: 10.5-10.9, four: 11.5-11.9
    c = {'time': 10.7, 'out': 11.7, 'desc': 'x'}
    out = cl.snap_edges(c, ws)
    assert out['time'] <= 10.5 and out['time'] >= 10.4          # before "two" starts, after "one" ends (10.4)
    assert out['out'] >= 11.9 and out['out'] <= 12.0            # after "four" ends, before "five" starts (12.0)
    same = cl.snap_edges({'time': 10.45, 'out': 11.95, 'desc': 'x'}, ws)
    assert same['time'] == 10.45 and same['out'] == 11.95       # edges in gaps stay


def test_written_sot_cut_into_its_line_is_widened_to_the_whole_line():
    ws = _sentence(70.0, 'can you do something for me') + _sentence(80.0, 'other words here')
    c = {'time': 70.6, 'out': 72.8, 'material': 1, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}
    out, notes = cl.locate_cues([c], {1: ws}, 1)
    assert out[0]['time'] <= 69.9 and out[0]['out'] >= 72.9          # the line began before the written in: the whole line is kept


def test_written_sot_not_said_at_its_time_moves_to_where_it_is_said():
    ws = _sentence(5.0, 'nothing relevant at all') + _sentence(120.0, 'can you do something for me')
    c = {'time': 71.0, 'out': 74.0, 'material': 1, 'kind': 'sot', 'desc': 'CAN YOU DO SOMETHING FOR ME'}
    out, notes = cl.locate_cues([c], {1: ws}, 1)
    assert out[0]['located'] and 119 < out[0]['time'] < 120.5 and out[0]['out'] > 122.5


def test_spoken_line_longer_than_written_cut_moves_out_to_cover_the_whole_line():
    ws = _sentence(0.0, 'mom i need money', step=0.4, dur=0.35)          # runs to 1.55
    c = {'time': 0.0, 'out': 1.0, 'material': 1, 'kind': 'sot', 'desc': 'MOM I NEED MONEY'}
    out, _ = cl.locate_cues([c], {1: ws}, 2)
    assert out[0]['out'] >= 1.55 and out[0]['time'] == 0.0


def test_sot_said_elsewhere_than_its_written_range_uses_where_it_is_said_even_close_by():
    ws = _sentence(47.0, "nina anak what's our password")           # written 40-45, said 47-49.1
    c = {'time': 40.0, 'out': 45.0, 'material': 1, 'kind': 'shot', 'speech': "NINA, ANAK, WHAT'S OUR PASSWORD PASSWORD?", 'desc': 'WIDE REACTS'}
    out, _ = cl.locate_cues([c], {1: ws}, 2)
    assert out[0]['located'] and 46.5 < out[0]['time'] < 47.0 and out[0]['out'] > 49.1


def test_line_heard_only_in_part_still_found_by_its_beginning():
    ws = _sentence(47.0, "nina anak what's our")                    # transcript lost the tail
    r = cl.align_best("NINA ANAK WHAT'S OUR PASSWORD PASSWORD", ws)
    assert r['w0'] == 0


def test_edges_in_speech_move_into_pauses_of_the_sound():
    sil = [(9.0, 9.6), (11.0, 11.5), (12.0, 12.2)]
    c = {'time': 9.9, 'out': 10.9}
    out = cl.snap_to_silence(c, sil)
    assert 9.5 <= out['time'] <= 9.6 and 11.0 <= out["out"] <= 11.3
    assert cl.snap_to_silence({'time': 9.2, 'out': 11.2}, sil) == {'time': 9.2, 'out': 11.2}     # both already in pauses
