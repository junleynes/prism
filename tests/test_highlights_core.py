"""Highlights scoring signals: frames, sound and movement, windows, look-alikes, tags, ranking, pins."""
import re
import numpy as np
import pytest

import highlights_core as hc


def test_a_short_scene_is_judged_on_its_middle_frame_and_a_longer_one_on_three():
    assert hc.sample_fractions(0.8) == [0.5]
    assert hc.sample_fractions(4.0) == [0.25, 0.5, 0.75]
    assert hc.sample_fractions(4.0, n=1) == [0.5]


def test_motion_is_zero_for_one_frame_and_grows_with_change():
    still = np.full((36, 64), 90, np.uint8)
    moved = np.full((36, 64), 200, np.uint8)
    assert hc.motion_amount([still]) == 0.0
    assert hc.motion_amount([still, still]) == 0.0
    assert hc.motion_amount([still, moved]) == pytest.approx(110 / 255, abs=0.01)


def test_tags_are_read_from_a_structured_reply_and_weighted_by_genre():
    assert hc.parse_tags('{"score": 4, "desc": "x", "tags": ["Conflict", "emotion", "nonsense", "conflict"]}') == ['conflict', 'emotion']
    assert hc.parse_tags('{"score": 4, "desc": "x", "tags": ["reveal"') == ['reveal']      # cut off mid-list
    assert hc.parse_tags('SCORE: 4') == []
    assert hc.tag_bonus(['conflict'], 'drama') > 0 > hc.tag_bonus(['filler'], 'drama') + 0.5
    assert hc.tag_bonus(['action'], 'action') > hc.tag_bonus(['action'], 'romance')
    assert hc.tag_bonus(['conflict', 'emotion', 'reveal', 'reaction'], 'drama') == hc.TAG_CLAMP     # clamped
    assert hc.tag_bonus(['filler'], 'no-such-genre') < 0


def _db(n, base=-50.0):
    return np.full(n, base, dtype=np.float32)


def test_scene_audio_stats_find_the_peak_and_a_held_beat_before_the_words():
    hop = hc.ENVELOPE_STEP
    db = _db(int(20 / hop))
    db[int(7.0 / hop):int(7.2 / hop)] = -10.0        # a shout 7.0-7.2 s
    db[int(12.0 / hop):int(14.0 / hop)] = -20.0      # speech from 12 s, after quiet from 10 s
    a = hc.scene_audio_stats(db, 5.0, 8.0, speech_db=-30.0)
    assert 7.0 <= a['peak_t'] <= 7.2 and a['peak_db'] == pytest.approx(-10.0)
    assert a['pre_pause'] is True                     # quiet 5.0 -> 7.0 before the first loud
    b = hc.scene_audio_stats(db, 12.0, 14.0, speech_db=-30.0)
    assert b['pre_pause'] is False                    # speaking from the first moment
    assert hc.scene_audio_stats(None, 0, 1) is None


def _scene(start, dur, peak_db=None, mean_db=None, motion=0.0, pre=False, peak_t=None, material=1):
    s = {'start': start, 'duration': dur, 'end': start + dur, 'motion': motion, 'material': material}
    if peak_db is not None:
        s['audio'] = {'peak_db': peak_db, 'mean_db': mean_db if mean_db is not None else peak_db - 12,
                      'peak_t': peak_t if peak_t is not None else start + dur / 2, 'pre_pause': pre}
    return s


def test_energy_rewards_the_loud_and_busy_scenes_against_the_rest():
    scenes = [_scene(i * 5, 4, peak_db=-30 + i, motion=0.02) for i in range(10)]
    scenes.append(_scene(60, 4, peak_db=-5, mean_db=-12, motion=0.4, pre=True))
    hc.energy_scores(scenes)
    loud = scenes[-1]['energy_score']
    quiet = scenes[0]['energy_score']
    assert loud == 2.0 and quiet < loud
    assert all(0 <= s['energy_score'] <= 2 for s in scenes)


def test_scenes_with_no_sound_facts_are_scored_on_movement_alone():
    scenes = [_scene(i, 3, motion=0.01 * (i + 1)) for i in range(8)]
    hc.energy_scores(scenes)
    assert scenes[-1]['energy_score'] == 0.5 and scenes[0]['energy_score'] == 0.0


def test_the_clip_window_takes_in_the_peak_not_the_first_seconds():
    s = _scene(100, 12, peak_db=-5, peak_t=108.0)
    t = hc.pick_window(s, 4.0)
    assert 100 <= t and t + 4.0 <= 112 and t < 108 < t + 4.0
    assert hc.pick_window(s, 11.9) == 100                       # (nearly) the whole scene
    assert hc.pick_window(_scene(100, 12), 4.0) == 100          # nothing to go on
    late = _scene(100, 12, peak_db=-5, peak_t=111.9)
    assert hc.pick_window(late, 4.0) == 108                     # never past the end of the scene


def test_look_alikes_rank_below_the_best_of_their_kind_but_stay_selectable():
    a = np.zeros(128, np.float32); a[3] = 1.0
    b = np.zeros(128, np.float32); b[90] = 1.0
    scenes = [{'start': 0, 'total_score': 8, 'hist': a, 'material': 1},
              {'start': 30, 'total_score': 7, 'hist': a.copy(), 'material': 1},
              {'start': 60, 'total_score': 6, 'hist': b, 'material': 1},
              {'start': 90, 'total_score': 5, 'hist': a.copy(), 'material': 2}]
    n = hc.dedupe_penalties(scenes, sim=0.95, penalty=1.5)
    assert n == 1
    assert scenes[1]['total_score'] == 5.5 and scenes[1]['dup']
    assert scenes[0]['total_score'] == 8 and scenes[2]['total_score'] == 6 and scenes[3]['total_score'] == 5   # other material is not a copy
    assert hc.dedupe_penalties(scenes, sim=0) == 0


def test_the_ranking_reply_must_name_every_image_once():
    assert hc.parse_order('{"order": [3, 1, 2]}', 3) == [2, 0, 1]
    assert hc.parse_order('{"order": [3, 1]}', 3) is None
    assert hc.parse_order('{"order": [1, 1, 2]}', 3) is None
    assert hc.parse_order('best is [2, 3, 1]', 3) == [1, 2, 0]
    assert hc.parse_order('', 3) is None
    assert hc.rank_bonus(0, 8) == 1.0 and hc.rank_bonus(7, 8) == 0.0 and 0 < hc.rank_bonus(3, 8) < 1


def test_the_rank_prompt_carries_the_genre_and_the_steering_text():
    p = hc.build_rank_prompt(5, 'action', 'a fight', 'crowds')
    assert '5 images' in p and 'action' in p and 'a fight' in p and 'crowds' in p


def test_the_pipeline_uses_each_signal():
    src = open('pipeline.py', encoding='utf-8').read()
    for needle in ('hc.sample_fractions', 'hc.energy_scores', 'hc.pick_window', 'hc.dedupe_penalties',
                   'hc.parse_tags', 'hc.parse_order', "'tags': {'type': 'array'", 'pinned_scenes'):
        assert needle in src, needle
    # the clip window is taken from the part of the scene, never past its end
    assert "slack = (last['start'] + last['duration']) - (last.get('trim_start', last['start']) + last['selected_dur'])" in src


def test_pin_and_swap_buttons_work_in_the_review_grid():
    sync = pytest.importorskip('playwright.sync_api')
    html = open('templates/index.html', encoding='utf-8').read()
    i = html.index('window._pins = window._pins || []')
    j = html.index('\nfunction toggleAlternates')
    js = html[i:j]
    page_html = ('<div id=card><input type=checkbox class=tr-preview-pick data-scene=1 checked></div>'
                 '<input type=checkbox class=tr-alt-pick data-scene=1><input type=checkbox class=tr-alt-pick data-scene=2>'
                 '<input id=tr-file-input><input id=tr-network-file value="x.mxf"><input id=tr-materials-network>')
    stub = ('function altPicks(){return Array.prototype.slice.call(document.querySelectorAll(".tr-alt-pick"))};'
            'window.kept=0;function updatePreviewKept(){window.kept++};')
    with sync.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(page_html)
        pg.add_script_tag(content=stub + js)
        pg.evaluate("window.s0={material:1,start:12.3,pinned:false}; addSceneActions(document.getElementById('card'), window.s0)")
        pg.click('.scene-swap')
        assert pg.evaluate("document.querySelectorAll('.tr-alt-pick')[0].checked") is True
        assert pg.evaluate("document.querySelector('.tr-preview-pick').checked") is False
        assert 'Alt 1' in pg.inner_text('.scene-note')
        pg.evaluate("document.querySelector('.tr-preview-pick').checked = true")
        pg.click('.scene-pin')
        assert pg.evaluate("window._pins.length") == 1 and pg.inner_text('.scene-pin') == 'Pinned'
        assert pg.evaluate("document.querySelector('.scene-swap').disabled") is True
        assert pg.evaluate("document.querySelector('.tr-preview-pick').disabled") is True
        # a pin made on other footage is not one for this
        pg.evaluate("document.getElementById('tr-network-file').value = 'other.mxf'")
        assert pg.evaluate("_pinIndex(window.s0)") == -1
        pg.evaluate("document.getElementById('tr-network-file').value = 'x.mxf'")
        pg.click('.scene-pin')
        assert pg.evaluate("window._pins.length") == 0 and pg.inner_text('.scene-pin') == 'Pin'
        b.close()
