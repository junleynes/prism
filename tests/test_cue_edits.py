import json

import cue_locate as cl
import pipeline


def test_tc_secs_reads_what_people_type():
    assert pipeline._tc_secs('100') == 100.0 and pipeline._tc_secs('1:40') == 100.0
    assert pipeline._tc_secs('') is None and pipeline._tc_secs('abc') is None


def test_cue_overrides_mark_edited_rows_and_validate():
    rows = [{'material': 2, 'start': '1:40', 'end': '1:45', 'desc': 'NINA', 'kind': 'sot', 'speech': 'NINA ANAK', 'edited': True},
            {'material': None, 'start': '0:00', 'end': '0:01', 'desc': 'MOM', 'kind': 'sot'}]
    cues, err = pipeline._parse_cue_overrides(json.dumps(rows), 2)
    assert not err and cues[0]['time'] == 100.0 and cues[0]['out'] == 105.0 and cues[0]['material'] == 2 and cues[0]['user_set']
    assert 'material' not in cues[1] and 'user_set' not in cues[1] and cues[1]['order'] == 1
    assert pipeline._parse_cue_overrides(json.dumps([{'start': '5', 'end': '3'}]), 2)[1]
    assert pipeline._parse_cue_overrides(json.dumps([{'material': 3, 'start': '5'}]), 2)[1]
    assert pipeline._parse_cue_overrides(json.dumps([{'start': 'x'}]), 2)[1]


def test_a_cue_the_person_typed_is_never_moved_by_what_is_heard():
    c = {'time': 100.0, 'out': 105.0, 'material': 1, 'kind': 'sot', 'desc': 'NINA ANAK WHAT IS OUR PASSWORD', 'user_set': True}
    words = [{'word': w, 'start': 10 + i * 0.5, 'end': 10.4 + i * 0.5} for i, w in enumerate('nina anak what is our password'.split())]
    out, _ = cl.locate_cues([c], {1: words}, 1)
    assert out[0]['time'] == 100.0 and out[0]['out'] == 105.0
    assert not cl.is_spoken(c)
    merged = cl.merge_overlaps([c, {'time': 101.0, 'out': 103.0, 'kind': 'shot', 'desc': 'x'}])
    assert len(merged) == 2
    kept, note = cl.drop_sot_when_clips_fill([c, {'time': 1.0, 'out': 13.0, 'material': 1, 'kind': 'shot'}], 15)
    assert c in kept


def test_edited_scene_is_built_from_the_material_and_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'probe_duration', lambda p: 300.0)
    old = {'script_desc': 'WIDE REACTS', 'cue_order': 2, 'selected_dur': 5.0}
    new, err = pipeline._edited_scene(old, {'material': 2, 'start': '1:40', 'end': '1:45'}, ['/a.mp4', '/b.mp4'])
    assert not err and new['material'] == 2 and new['source_path'] == '/b.mp4' and new['start'] == 100.0
    assert new['selected_dur'] == 5.0 and new['cue_fixed'] and new['cue_order'] == 2 and 'edited' in new['script_desc']
    assert pipeline._edited_scene(old, {'material': 5, 'start': '1'}, ['/a.mp4'])[1]
