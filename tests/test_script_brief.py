"""What a promo script says besides its timecodes: voice-over lines (highlighted in the script), who they play
over, the delivery note, the tune-in card; and what the job does with it (VO placed at its clip's time on the
plug, the title card cut around its title, the cards kept long enough for the tune-in line).

The PDF here is drawn to look like the editors' scripts: a two-column table, the video column on the left, the
audio column on the right, voice-over lines highlighted yellow on the row of the clip they play over."""
import io
import json
import shutil
import subprocess
import unittest.mock as mock

import cv2
import numpy as np
import pytest

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import pipeline

import cutcraft
import scriptbrief as sb

reportlab = pytest.importorskip('reportlab')
pytest.importorskip('pdfplumber')
from reportlab.lib.colors import Color, black  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402

PAGE_H = 792
YELLOW, CYAN = Color(1, 1, 0), Color(0, 1, 1)


def _script_pdf(rows):
    """rows: (side 'L'|'R', top_pt, text, highlight|None, underline) -> PDF bytes."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(612, PAGE_H))
    c.setFont('Times-Roman', 10)
    # the table's borders
    c.setStrokeColor(black)
    c.line(58, PAGE_H - 7, 551, PAGE_H - 7)
    c.line(307, PAGE_H - 7, 307, PAGE_H - 529)
    c.line(58, PAGE_H - 137, 551, PAGE_H - 137)
    for side, top, text, hl, ul in rows:
        x = 63 if side == 'L' else 313
        y = PAGE_H - top - 9
        w = c.stringWidth(text, 'Times-Roman', 10)
        if hl:
            c.setFillColor(hl)
            c.rect(x, y - 2, w, 11.5, stroke=0, fill=1)
        c.setFillColor(black)
        c.drawString(x, y, text)
        if ul:
            c.setLineWidth(0.5)
            c.line(x, y - 1.2, x + w, y - 1.2)
    c.showPage()
    c.save()
    return buf.getvalue()


EDITORS_SCRIPT = [
    ('L', 9, 'FINAL CUT/VGFX/AUDIO', None, False), ('R', 9, 'PVPD_FIREWA_070226_LN015', None, False),
    ('L', 21, 'EDITOR/ JAIMEE /JARED/NEIL', None, False), ('R', 21, 'VO SENT JUNE 30 PM SHIFT', None, False),
    ('R', 33, '/PMC_PROMO_GRAPHICS/00_TEMPLATES/TCARD', None, False), ('R', 45, 'ENDCARD LIFT FROM FIRWK6', None, False),
    ('R', 90, '/PMC_PROMO_AUDIO_MUSIC/G_PROMO_MUSIC/THEMES', None, False),
    ('R', 102, 'FIREWALL_DEVIL DANCE_NAC_ACT-SUS', None, False),
    ('R', 139, '(VO TONE: MAANGAS', CYAN, False),
    ('R', 197, "KAKANTA NA BA 'PAG NAHULI NA?", YELLOW, False),
    ('L', 208, 'M1 3:33—37 HABOL TACKLE', None, False),
    ('R', 220, 'M1 3:38—43', None, False), ('R', 231, 'TRAYDOR KA', None, False),
    ('R', 243, 'SINO LEADER NIYO', None, False),
    ('R', 289, 'M2 00:12—20', None, False), ('R', 300, 'AKO ANG NAGTIMBRE SA SINDIKATO', None, False),
    ('L', 358, 'M2 00:35—42 BUNOT', None, False), ('R', 358, 'NGAYONG INTENSE NA HUWEBES!', YELLOW, False),
    ('L', 369, 'GALO!', None, False), ('L', 381, 'TAWA MARK', None, False),
    ('R', 381, 'M2 00:43—46', None, False), ('R', 392, 'NASAN KAPATID KO', None, False),
    ('L', 427, 'TASKFORCE FIREWALL', None, True), ('L', 438, 'WEEKDAYS', None, True), ('L', 450, 'GMA 8:50 PM', None, True),
    ('L', 461, 'SIMULCAST ON IHEART MOVIES', None, True), ('L', 473, 'HEART OF ASIA CHANNEL', None, True),
    ('L', 484, 'AFTER THE MASTER CUTTER', YELLOW, True),
    ('R', 450, 'TASKFORCE FIREWALL', YELLOW, False), ('R', 461, '8:50PM ON GMA PRIME', YELLOW, False),
]


@pytest.fixture(scope='module')
def brief():
    b = sb.read_script_brief(_script_pdf(EDITORS_SCRIPT), 'script.pdf', '', [])
    assert b, 'the layout should be readable'
    return b


# ---------------------------------------------------------------- reading the layout

def test_the_highlighted_lines_are_the_voice_over_and_sit_over_the_clip_on_their_row(brief):
    assert [v['text'] for v in brief['vo']] == ["KAKANTA NA BA 'PAG NAHULI NA?", 'NGAYONG INTENSE NA HUWEBES!',
                                               'TASKFORCE FIREWALL', '8:50PM ON GMA PRIME']
    a, b, c, d = (v['anchor'] for v in brief['vo'])
    assert (a['kind'], a['material'], a['time'], a['out']) == ('cue', 1, 213.0, 217.0)
    assert (b['kind'], b['material'], b['time'], b['out']) == ('cue', 2, 35.0, 42.0)
    assert c['kind'] == 'card' and d['kind'] == 'card'
    assert [v['how'] for v in brief['vo']] == ['row'] * 4
    assert brief['read_by'] == 'layout'


def test_tone_title_card_music_and_footage_dialogue_are_picked_out(brief):
    assert brief['tone'] == 'MAANGAS'
    assert brief['tcard']['title'] == 'TASKFORCE FIREWALL'
    assert brief['tcard']['lines'][:3] == ['TASKFORCE FIREWALL', 'WEEKDAYS', 'GMA 8:50 PM']
    assert brief['tcard']['highlighted'] == ['AFTER THE MASTER CUTTER']
    assert brief['music'] == 'FIREWALL_DEVIL DANCE_NAC_ACT-SUS'
    assert brief['script_id'] == 'PVPD_FIREWA_070226_LN015'
    assert 'ENDCARD LIFT FROM FIRWK6' in brief['notes']
    # the dialogue that comes with the footage is not voice-over
    sot = brief['sot']
    assert [(s['material'], s['time']) for s in sot] == [(1, 218.0), (2, 12.0), (2, 43.0)]
    assert sot[0]['lines'][0] == 'TRAYDOR KA'
    assert all('TRAYDOR' not in v['text'] and 'NASAN' not in v['text'] for v in brief['vo'])


def test_the_script_without_highlights_has_no_layout_voice_over_and_asks_the_model():
    rows = [(s, t, x, None if h is not YELLOW else None, u) for s, t, x, h, u in EDITORS_SCRIPT]
    asked = []

    def model(prompt):
        asked.append(prompt)
        return json.dumps({'vo': [{'text': "KAKANTA NA BA 'PAG NAHULI NA?", 'covers': 'M1 3:33-3:37'},
                                  {'text': 'TASKFORCE FIREWALL 8:50PM ON GMA PRIME', 'covers': 'card'}],
                           'tone': 'maangas', 'title': 'TASKFORCE FIREWALL', 'music': None, 'card_lines': ['WEEKDAYS']})
    cues = [{'material': 1, 'time': 213.0, 'out': 217.0}, {'material': 2, 'time': 35.0, 'out': 42.0}]
    plain = [(s, t, x, (CYAN if h is CYAN else None), u) for s, t, x, h, u in rows]
    b = sb.read_script_brief(_script_pdf(plain), 'script.pdf', 'M1 3:33', cues, ask_llm=model)
    assert asked and 'M1 3:33-3:37' in asked[0] and 'KAKANTA' in asked[0], 'the model reads both columns of the script'
    assert [v['anchor']['kind'] for v in b['vo']] == ['cue', 'card'] and b['vo'][0]['anchor']['time'] == 213.0
    assert b['read_by'] == 'layout and AI model' and b['tone'] == 'MAANGAS' and b['tcard']['title'] == 'TASKFORCE FIREWALL'


def test_a_model_that_is_down_or_talks_nonsense_never_breaks_the_script():
    def down(_):
        raise RuntimeError('Ollama is not reachable')
    assert sb.read_script_brief(b'%PDF-not really', 'x.pdf', 'M1 0:05', [], ask_llm=down) is None
    assert sb.read_script_brief(b'', 'x.txt', 'M1 0:05', [], ask_llm=lambda p: 'I think it should fade') is None
    assert sb.parse_llm_reply('{"vo": [{"text": "x", "covers": "M9 9:99"}]}', [{'material': 1, 'time': 5.0, 'out': None}])['vo'][0]['anchor']['kind'] == 'order'


def test_a_plain_text_script_with_vo_lines_needs_no_model():
    b = sb.read_script_brief(b'', 'script.txt', 'M1 0:05-0:09\nVO: Abangan ngayong gabi\nVO: Only on GMA, 8:50 PM', [])
    assert [v['anchor']['kind'] for v in b['vo']] == ['order', 'card']


def test_highlight_colours_are_told_apart_from_black_and_grey():
    assert sb.colour_name((1.0, 1.0, 0.0)) == 'yellow' and sb.colour_name((0.0, 1.0, 1.0)) == 'cyan'
    assert sb.colour_name((0, 0, 0)) is None and sb.colour_name(0.5) is None and sb.colour_name((0.8, 0.8, 0.8)) is None
    assert sb.colour_name((0.0, 0.0, 0.0, 0.0)) is None, 'CMYK white'
    assert sb.colour_name((0.0, 0.0, 1.0, 0.0)) == 'yellow', 'CMYK yellow'


# ---------------------------------------------------------------- placing the lines on the plug

CLIPS = [{'material': 1, 'trim_start': 213.0, 'selected_dur': 5.0}, {'material': 2, 'trim_start': 35.0, 'selected_dur': 7.0}]


def _ats(lines):
    return [None if l['at'] is None else round(l['at'], 2) for l in lines]


def test_each_line_starts_with_its_clip_and_the_tune_in_line_with_the_cards(brief):
    clip_starts, card_starts = sb.timeline_starts([5.0, 7.0], [3.0, 3.0], 0.3)
    assert clip_starts == pytest.approx([0.0, 4.7]) and card_starts == pytest.approx([11.4, 14.1])
    lines, notes = sb.place_lines(brief['vo'], CLIPS, clip_starts, card_starts)
    # The title card's line starts with the title card, the end card's (the schedule) with the end card.
    assert _ats(lines) == [0.12, 4.82, 11.6, 14.3] and len(notes) == 1 and 'end card' in notes[0]
    text = sb.vo_script_text(lines)
    assert text.splitlines()[0].startswith('@0.12s KAKANTA') and '@11.60s TASKFORCE FIREWALL' in text
    parsed = cutcraft.parse_vo_script(text)
    assert [None if p['at'] is None else round(p['at'], 2) for p in parsed] == [0.12, 4.82, 11.6, 14.3]
    assert parsed[3]['text'] == '8:50PM ON GMA PRIME'


def test_the_title_cards_line_goes_to_the_title_card_and_the_end_cards_line_to_the_end_card():
    assert sb.card_targets(['TASKFORCE FIREWALL', '8:50PM ON GMA PRIME'], 2) == [0, 1]
    assert sb.card_targets(['8:50PM ON GMA PRIME', 'TASKFORCE FIREWALL'], 2) == [1, 0], 'by what it says, not by order'
    assert sb.card_targets(['Abangan ngayong gabi', 'Huwag palampasin'], 2) == [0, 1], 'alike: first then second'
    assert sb.card_targets(['8:50PM ON GMA PRIME'], 2) == [0], 'one line stays on the first card and plays on'
    assert sb.card_targets(['TASKFORCE FIREWALL', '8:50PM ON GMA PRIME'], 1) == [0, 0], 'one card: all on it'
    vo = [{'text': 'TASKFORCE FIREWALL', 'anchor': {'kind': 'card'}}, {'text': 'WEEKNIGHTS 8:50PM ON GMA PRIME', 'anchor': {'kind': 'card'}}]
    lines, _ = sb.place_lines(vo, [], [], [10.0, 13.0], xfade=0.3, card_jcut=0.5)
    assert _ats(lines) == [9.7, None], 'a led title line is followed by the end card line'
    lines, _ = sb.place_lines(vo, [], [], [10.0])
    assert _ats(lines) == [10.2, None], 'with one card the second line follows the first'


def test_a_clip_that_was_dropped_makes_its_line_follow_the_line_before(brief):
    clip_starts, card_starts = sb.timeline_starts([7.0], [3.0], 0.3)
    lines, notes = sb.place_lines(brief['vo'], CLIPS[1:], clip_starts, card_starts)
    assert lines[0]['at'] is None and lines[1]['at'] == pytest.approx(0.12) and notes and 'not in the plug' in notes[0]


def test_two_lines_on_one_clip_are_said_one_after_the_other():
    vo = [{'text': 'one', 'anchor': {'kind': 'cue', 'material': 1, 'time': 213.0, 'label': 'M1'}},
          {'text': 'two', 'anchor': {'kind': 'cue', 'material': 1, 'time': 214.0, 'label': 'M1'}}]
    lines, _ = sb.place_lines(vo, CLIPS[:1], [0.0], [5.0])
    assert lines[0]['at'] == pytest.approx(0.12) and lines[1]['at'] is None


# ---------------------------------------------------------------- the card

def test_a_card_is_not_cut_before_its_title_has_been_on_screen_long_enough():
    assert cutcraft.card_floor(8.0, still_from=1.0, title_at=3.0) == pytest.approx(3.0 + cutcraft.TITLE_HOLD)
    assert cutcraft.card_floor(8.0, still_from=3.5, title_at=1.0) == pytest.approx(3.5 + cutcraft.CARD_STILL_PAD)
    assert cutcraft.card_floor(4.0, title_at=3.9) == 4.0, 'never longer than the card itself'


def test_a_title_is_found_in_what_a_frame_reads_as_even_with_a_wrong_letter():
    assert cutcraft.title_in_text('TASKFORCE FIREWALL', 'rasa TASKF0RCE\nFIREWALL ')
    assert not cutcraft.title_in_text('TASKFORCE FIREWALL', 'FIREWALL')
    assert not cutcraft.title_in_text('TASKFORCE FIREWALL', '')


def test_each_card_is_kept_as_long_as_its_own_line_needs():
    floors, short = cutcraft.raise_floors_per_card([1.5, 1.5], [4.0, 3.0], [3.0, 2.0])
    assert floors == [3.0, 2.0] and short == 0.0
    floors, short = cutcraft.raise_floors_per_card([1.5, 1.5], [2.0, 3.0], [3.0, 0.0])
    assert floors == [2.0, 1.5] and short == pytest.approx(1.0), 'never past the card, and it says by how much it is short'
    assert cutcraft.raise_floors_per_card([1.5, 1.5], [4.0, 3.0], [None, None]) == ([1.5, 1.5], 0.0)


def test_the_cards_are_kept_as_long_as_the_tune_in_line_needs():
    floors, short = cutcraft.raise_floors_for_voice([1.5, 1.5], [4.0, 3.0], 5.0, xfade=0.3)
    assert sum(floors) - 0.3 == pytest.approx(5.0) and floors == pytest.approx([3.8, 1.5]) and short == 0
    floors, short = cutcraft.raise_floors_for_voice([1.5, 1.5], [2.0, 2.0], 9.0, xfade=0.3)
    assert floors == [2.0, 2.0] and short == pytest.approx(5.3)


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('tesseract') is None, reason='needs ffmpeg and tesseract')
def test_the_card_is_measured_for_where_its_title_appears(tmp_path):
    """A 6 s card, black until 2 s, then the title appears in large white letters and stays."""
    w, h, fps = 640, 360, 25
    out = tmp_path / 'card.mp4'
    proc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
                             '-r', str(fps), '-i', 'pipe:0', '-pix_fmt', 'yuv420p', '-an', str(out)], stdin=subprocess.PIPE)
    for n in range(6 * fps):
        img = np.zeros((h, w, 3), np.uint8)
        if n >= 2 * fps:
            cv2.putText(img, 'TASKFORCE', (60, 160), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
            cv2.putText(img, 'FIREWALL', (90, 250), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
        else:
            cv2.circle(img, (60 + n * 8, 180), 20, (0, 0, 255), -1)     # something moving before the title
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()
    floor, det = pipeline._card_measure(str(out), 6.0, title='TASKFORCE FIREWALL')
    assert det['title_at'] is not None and 1.9 <= det['title_at'] <= 2.6 and det['title_by'] == 'text reading'
    assert floor >= det['title_at'] + cutcraft.TITLE_HOLD - 0.01
    assert floor < 6.0, 'a picture that keeps no motion after the title can be cut after the hold'
    floor_without, det2 = pipeline._card_measure(str(out), 6.0)
    assert det2['title_at'] is None and floor_without >= 1.0


def test_the_job_turns_the_script_brief_into_timed_narration_text(brief):
    selected = [{'material': 1, 'trim_start': 213.0, 'selected_dur': 5.0}, {'material': 2, 'trim_start': 35.0, 'selected_dur': 7.0}]
    text, notes = pipeline._script_vo_text(brief, selected, [3.0, 3.0], 0.3)
    assert text.splitlines()[1].startswith('@4.82s NGAYONG') and '@11.60s TASKFORCE' in text and '@14.30s 8:50PM' in text
    assert len(notes) == 1 and 'end card' in notes[0]
    assert pipeline._script_vo_text(None, selected, [3.0], 0.3) == (None, [])
    summary = pipeline._brief_summary(brief)
    assert summary['title'] == 'TASKFORCE FIREWALL' and summary['tone'] == 'MAANGAS' and len(summary['vo']) == 4


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('tesseract') is None, reason='needs ffmpeg and tesseract')
def test_a_card_with_a_music_bed_and_a_moving_picture_is_still_cut_after_its_title(tmp_path):
    w, h, fps = 640, 360, 25
    out = tmp_path / 'bed.mp4'
    silent = tmp_path / 's.mp4'
    proc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
                             '-r', str(fps), '-i', 'pipe:0', '-pix_fmt', 'yuv420p', '-an', str(silent)], stdin=subprocess.PIPE)
    for n in range(6 * fps):
        img = np.zeros((h, w, 3), np.uint8)
        if n >= 2 * fps:
            cv2.putText(img, 'TASKFORCE', (60, 160), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
            cv2.putText(img, 'FIREWALL', (90, 250), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
        cv2.circle(img, (20 + n * 4, 330), 8, (0, 0, 255), -1)          # keeps moving to the very end
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(silent), '-f', 'lavfi', '-i', 'sine=frequency=220:duration=6',
                    '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(out)], check=True)
    floor, det = pipeline._card_measure(str(out), 6.0, title='TASKFORCE FIREWALL')
    assert det['title_at'] is not None and floor == pytest.approx(det['title_at'] + cutcraft.TITLE_HOLD) and floor < 6.0
    floor2, det2 = pipeline._card_measure(str(out), 6.0)
    assert floor2 == 6.0, 'without the script a card with a sound bed to the end keeps its length'


def test_the_scripts_clips_are_kept_whole_and_the_card_gets_what_is_left():
    plan = cutcraft.plan_card_fit(15, [8.0], [1.0], min_scenes=11.0)
    assert plan['targets'] == pytest.approx([4.0]) and plan['short_by'] == 0
    # with floors from the title (3.6 s) the card would stay too long
    plan = cutcraft.plan_card_fit(15, [8.0], [3.6], min_scenes=11.0)
    assert plan['targets'] == pytest.approx([4.0]) and plan['scene_budget'] == pytest.approx(11.0)
    plan = cutcraft.plan_card_fit(15, [8.0], [5.0], min_scenes=11.0)
    assert plan['targets'][0] == pytest.approx(5.0) and plan['short_by'] == pytest.approx(1.0)


def test_a_j_cut_starts_a_line_under_the_clip_before_but_never_before_the_start(brief):
    clip_starts, card_starts = sb.timeline_starts([5.0, 7.0], [3.0, 3.0], 0.3)
    lines, _ = sb.place_lines(brief['vo'], CLIPS, clip_starts, card_starts, jcut=0.5)
    assert _ats(lines) == [0.12, 4.32, 11.1, 13.8], 'the first clip has nothing before it'
    lines, _ = sb.place_lines(brief['vo'], CLIPS, clip_starts, card_starts, jcut=0.0)
    assert _ats(lines) == [0.12, 4.82, 11.6, 14.3]
    lines, _ = sb.place_lines(brief['vo'], CLIPS, [0.0, 0.2], [0.3], jcut=5.0)
    assert all(l['at'] is None or l['at'] >= 0 for l in lines)


def test_the_plan_view_tells_each_selector_what_the_plug_uses(brief):
    plan = {'lines': [{'text': 'a', 'at': 0.1, 'end': 3.0, 'status': 'ok'}, {'text': 'b', 'at': 4.0, 'end': 6.0, 'status': 'ok'}],
            'pieces': [{'line': 0, 'a': 12.0, 'b': 13.5}, {'line': 0, 'a': 13.6, 'b': 14.0}, {'line': 1, 'a': 20.0, 'b': 22.0}]}
    fit = {'cards': [{'label': 'Title card', 'secs': 4.0, 'was': 8.0, 'details': {'title': 'X', 'title_at': 2.1, 'title_by': 'text reading'}}]}
    v = pipeline._plan_view({'vo_jcut': 0.3, 'scoring_audio_trim_start': 10.0, 'sync_beats': True,
                             'script_brief': brief}, plan, fit, 15.0, 0.3)
    assert v['narration'][0]['src_in'] == 12.0 and v['narration'][0]['src_out'] == 14.0 and v['narration'][1]['src_in'] == 20.0
    assert v['cards'][0]['was'] == 8.0 and v['cards'][0]['title_at'] == 2.1
    assert v['music'] == {'in': 10.0, 'out': 25.0, 'beat_synced': True, 'plug': 15.0, 'named_by_script': 'FIREWALL_DEVIL DANCE_NAC_ACT-SUS'}
    assert pipeline._plan_view({}, None, None, 12.0, 0.3)['narration'] if False else 'narration' not in pipeline._plan_view({}, None, None, 12.0, 0.3)


def test_a_tune_in_card_is_cut_from_its_end_or_from_just_before_its_title():
    assert cutcraft.card_window(8.0, 4.0) == 4.0, 'no title known: the last seconds, where the card has built up'
    assert cutcraft.card_window(8.0, 4.0, title_at=6.0) == 4.0
    assert cutcraft.card_window(8.0, 4.0, title_at=2.0) == pytest.approx(1.5), 'title early: start just before it'
    assert cutcraft.card_window(8.0, 4.0, title_at=0.2) == 0.0
    assert cutcraft.card_window(3.0, 4.0) == 0.0


def test_the_recording_is_matched_when_it_splits_or_spells_out_what_the_script_writes():
    assert cutcraft.norm_tokens('GMA 8:50PM') == ['gma', '8', '50', 'pm']
    assert cutcraft.norm_tokens('eight fifty p.m.') == ['8', '50', 'pm']
    words = [{'word': w, 'start': i * .3, 'end': i * .3 + .25} for i, w in enumerate(
        'sino leader niyo task force firewall eight fifty p m on gma prime'.split())]
    a = cutcraft.align_script([{'text': 'TASKFORCE FIREWALL 8:50PM ON GMA PRIME'}], words)[0]
    assert (a['w0'], a['w1']) == (3, 12), 'neither the start (task|force) nor the end is cut short'


def test_the_tune_in_line_can_lead_the_cards_by_more_than_the_other_lines(brief):
    clip_starts, card_starts = sb.timeline_starts([5.0, 7.0], [4.0], 0.3)
    lines, _ = sb.place_lines(brief['vo'], CLIPS, clip_starts, card_starts, jcut=0.0, card_jcut=0.8)
    assert _ats(lines) == [0.12, 4.82, 10.8, None]


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('tesseract') is None, reason='needs ffmpeg and tesseract')
def test_the_piece_cut_from_a_card_is_the_one_with_the_title_not_the_head(tmp_path):
    w, h, fps = 640, 360, 25
    src = tmp_path / 'card.mp4'
    proc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
                             '-r', str(fps), '-i', 'pipe:0', '-pix_fmt', 'yuv420p', '-an', str(src)], stdin=subprocess.PIPE)
    for n in range(8 * fps):                    # dark and moving for 5 s, then the title
        img = np.zeros((h, w, 3), np.uint8)
        if n >= 5 * fps:
            cv2.putText(img, 'TASKFORCE', (60, 160), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
            cv2.putText(img, 'FIREWALL', (90, 250), cv2.FONT_HERSHEY_DUPLEX, 2.2, (255, 255, 255), 5)
        else:
            cv2.circle(img, (30 + n * 4, 180), 14, (0, 0, 255), -1)
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()
    floor, det = pipeline._card_measure(str(src), 8.0, title='TASKFORCE FIREWALL', window=True)
    assert 4.9 <= det['title_at'] <= 5.6 and floor == pytest.approx(cutcraft.TITLE_HOLD + cutcraft.TITLE_LEAD)
    start = cutcraft.card_window(8.0, 3.0, det['title_at'])
    assert start == pytest.approx(det['title_at'] - cutcraft.TITLE_LEAD, abs=0.01), 'just before the title'
    out = tmp_path / 'cut.mp4'
    assert pipeline._adjust_card_duration(str(src), -5.0, str(out), start=start)
    assert pipeline.probe_duration(str(out)) == pytest.approx(3.0, abs=0.1)
    cap = cv2.VideoCapture(str(out))
    cap.set(cv2.CAP_PROP_POS_MSEC, 1000)
    ok, frame = cap.read()
    assert ok and (frame > 200).sum() > 2000, 'the title is on screen a second into the piece'


def test_the_plan_says_which_words_it_heard_for_each_line_and_which_it_left_out():
    words = [{'word': w, 'start': i * .3, 'end': i * .3 + .25} for i, w in enumerate(
        'ngayong intense na huwebes um task force firewall eight fifty p m on gma prime'.split())]
    plan = cutcraft.plan_vo([{'text': 'NGAYONG INTENSE NA HUWEBES!', 'at': 0.0, 'optional': False},
                             {'text': 'FIREWALL 8:50PM ON GMA PRIME', 'at': 3.0, 'optional': False}], words, window_end=9.0)
    l0, l1 = plan['lines']
    assert l0['heard'].startswith('ngayong@0.0') and 'huwebes' in l0['heard']
    assert l0['strays'] == 'um@1.2 task@1.5 force@1.8', 'what lies between the lines is reported, not silently played'
    assert l1['heard'].startswith('firewall@2.1') and l1['heard'].endswith('prime@4.2')
    assert any('belong to no script line' in n for n in plan['notes'])


def _words(t0, text):
    return [{'word': w, 'start': t0 + i * .3, 'end': t0 + i * .3 + .25} for i, w in enumerate(text.split())]


TAKES = (_words(0, 'kakanta na ba pag nahuli na') + _words(5, 'sino leader niyo') + _words(9, 'task force firewall weekdays')
         + _words(14, 'ngayong intense na huwebes') + _words(20, 'task force firewall eight fifty p m on gma prime')
         + _words(30, 'ngayong intense na huwebes'))
SCRIPT_LINES = [{'text': "KAKANTA NA BA 'PAG NAHULI NA?"}, {'text': 'NGAYONG INTENSE NA HUWEBES!'},
                {'text': 'TASKFORCE FIREWALL 8:50PM ON GMA PRIME'}]


def test_a_line_is_one_stretch_of_speech_even_when_the_recording_holds_several_takes():
    al = cutcraft.align_script(SCRIPT_LINES, TAKES)
    spans = [(TAKES[a['w0']]['start'], TAKES[a['w1']]['end']) for a in al]
    assert spans[1] == (14.0, pytest.approx(15.15)), 'the first complete take'
    # the tune-in line is the take at 20 s, whole (task force firewall ... prime), not "task" from 9 s joined to "prime"
    assert spans[2][0] == 20.0 and spans[2][1] == pytest.approx(22.95)


def test_a_line_recorded_out_of_script_order_is_still_found_and_cut_between_its_neighbours():
    out_of_order = _words(0, 'task force firewall eight fifty p m on gma prime') + _words(6, 'ngayong intense na huwebes')
    al = cutcraft.align_script(SCRIPT_LINES[1:], out_of_order)
    assert out_of_order[al[0]['w0']]['start'] == 6.0 and al[1]['w0'] == 0
    plan = cutcraft.plan_vo([dict(l, at=a, optional=False) for l, a in zip(SCRIPT_LINES[1:], (0.0, 5.0))], out_of_order, window_end=12.0)
    assert [r['status'] for r in plan['lines']] == ['ok', 'ok'] and plan['fits']


def test_the_parts_of_a_recording_are_joined_in_the_order_given(tmp_path):
    if shutil.which('ffmpeg') is None:
        pytest.skip('needs ffmpeg')
    src = tmp_path / 's.wav'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=10', str(src)], check=True)
    assert pipeline._parse_ranges('[[1,2],[3,"x"],[5,4],[6,8.5]]') == [(1.0, 2.0), (6.0, 8.5)]
    assert pipeline._parse_ranges('nonsense') == []
    path, a, b = pipeline._apply_extra_ranges(str(src), 1.0, 3.0, '[[6,7.5]]', upload_dir=str(tmp_path), tag='t')
    assert (a, b) == (0.0, None) and pipeline.probe_duration(path) == pytest.approx(3.5, abs=0.1)
    same = pipeline._apply_extra_ranges(str(src), 1.0, 3.0, '[]', upload_dir=str(tmp_path))
    assert same == (str(src), 1.0, 3.0), 'no extra ranges: the recording is used as before'


def test_the_cards_own_narration_is_read_for_where_the_script_words_are(tmp_path, monkeypatch, brief):
    f = tmp_path / 'tc.wav'
    f.write_bytes(b'x')
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda p, a, b: (_words(2, 'sino leader task force firewall eight fifty p m on gma prime'), []))
    r = pipeline._suggest_card_vo_ranges({'title_card_vo_path': str(f)}, brief)
    assert r['title']['in'] == pytest.approx(2.35, abs=0.05) and r['title']['out'] > r['title']['in'] + 2.5
    assert 'end' not in r, 'no end card narration file was given'


def test_the_script_and_the_recording_give_the_in_and_out_before_generate(brief, tmp_path, monkeypatch):
    f = tmp_path / 'nar.wav'
    f.write_bytes(b'x')
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda p, a, b: (TAKES, []))
    r = pipeline._narration_range_from_brief(brief, str(f))
    assert r['in'] == pytest.approx(0.0, abs=0.01) and len(r['lines']) == 4 and r['missing'] == 0
    assert len(r['parts']) == 4 and r['parts'][-1]['out'] > 22.9 and r['out'] == r['parts'][0]['out']
    assert pipeline._narration_range_from_brief({'vo': []}, str(f)) is None
    assert pipeline._narration_range_from_brief(brief, str(tmp_path / 'missing.wav')) is None


def test_the_route_reads_a_picked_recording_against_the_posted_script(brief, tmp_path, monkeypatch):
    f = tmp_path / 'net_1_nar.wav'
    f.write_bytes(b'x')
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda p, a, b: (TAKES, []))
    monkeypatch.setattr(pipeline, 'staged_path', lambda name: str(tmp_path / name))
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    hdr = {'X-CSRF-Token': 't'}
    resp = client.post('/api/trailer/script_vo_ranges', headers=hdr,
                       data={'script_brief': json.dumps(brief), 'vo_upload_network': 'net_1_nar.wav'})
    data = resp.get_json()
    assert data['ok'] and data['narration']['in'] == pytest.approx(0.0, abs=0.01) and data['narration']['parts'][-1]['out'] > 22.9
    bad = client.post('/api/trailer/script_vo_ranges', headers=hdr, data={'script_brief': '{}'})
    assert bad.get_json()['ok'] is False


def test_a_dramatic_pause_inside_a_line_is_not_placed_on_the_plug():
    """'TASK' ... 1.5 s ... 'FORCE FIREWALL' in the recording: on the plug the two parts sit 0.35 s apart."""
    words = (_words(0, 'ngayong intense na huwebes')
             + [{'word': 'task', 'start': 4.0, 'end': 4.3}, {'word': 'force', 'start': 5.8, 'end': 6.1},
                {'word': 'firewall', 'start': 6.15, 'end': 6.6}, {'word': 'eight', 'start': 6.7, 'end': 6.9},
                {'word': 'fifty', 'start': 6.95, 'end': 7.2}, {'word': 'on', 'start': 7.25, 'end': 7.4},
                {'word': 'gma', 'start': 7.45, 'end': 7.7}, {'word': 'prime', 'start': 7.75, 'end': 8.1}])
    plan = cutcraft.plan_vo([{'text': 'NGAYONG INTENSE NA HUWEBES!', 'at': 0.0, 'optional': False},
                             {'text': 'TASKFORCE FIREWALL 8:50 ON GMA PRIME', 'at': 5.0, 'optional': False}], words, window_end=20.0)
    parts = [p for p in plan['pieces'] if p['line'] == 1]
    assert len(parts) >= 2
    assert parts[1]['at'] - (parts[0]['at'] + (parts[0]['b'] - parts[0]['a'])) <= cutcraft.INLINE_PAUSE_MAX + 0.05
    assert cutcraft.spoken_length(words, 4, 11) < (8.1 - 4.0) - 1.0, 'the line is measured without the long pause'


def test_a_word_the_recording_does_not_contain_does_not_swallow_the_next_lines_first_word():
    """'huwebes' is not heard at all: line 2 must end at 'na', and 'task' must stay with the TASKFORCE line."""
    words = _words(0, 'ngayong intense na') + _words(1.5, 'task force firewall eight fifty p m on gma prime')
    al = cutcraft.align_script(SCRIPT_LINES[1:], words)
    assert words[al[0]['w1']]['word'] == 'na'
    assert words[al[1]['w0']]['word'] == 'task' and words[al[1]['w1']]['word'] == 'prime'


def test_words_after_a_long_pause_are_not_taken_into_the_line_before():
    words = _words(0, 'ngayong intense na') + _words(3.0, 'huwebes') + _words(3.6, 'task force firewall eight fifty p m on gma prime')
    al = cutcraft.align_script(SCRIPT_LINES[1:], words)
    assert words[al[0]['w1']]['word'] == 'huwebes' and words[al[1]['w0']]['word'] == 'task'
    words = _words(0, 'ngayong intense na huebes') + _words(2.0, 'task force firewall eight fifty p m on gma prime')
    al = cutcraft.align_script(SCRIPT_LINES[1:], words)
    assert words[al[0]['w1']]['word'] == 'huebes' and words[al[1]['w0']]['word'] == 'task', 'a misheard word is still the line\'s own'


def test_same_recording_for_narration_and_cards_is_not_played_twice(tmp_path):
    import pipeline
    a = tmp_path / 'a.wav'; b = tmp_path / 'b.wav'; c = tmp_path / 'c.wav'
    a.write_bytes(b'x' * 5000); b.write_bytes(b'x' * 5000); c.write_bytes(b'y' * 5000)
    brief = {'vo': [{'text': 'TASKFORCE FIREWALL 8:50PM ON GMA PRIME', 'anchor': {'kind': 'card'}}]}
    params = {'vo_upload_path': str(a), 'vo_mode': 'upload', 'vo_smart': True, 'script_brief': brief,
              'title_card_vo_path': str(b), 'end_card_vo_path': str(b), 'end_card_vo_start': 0.0, 'end_card_vo_end': None}
    t, e, notes = pipeline._drop_duplicate_card_vo(params)
    assert t is None and e is None and len(notes) == 2
    params.update(title_card_vo_path=str(c), end_card_vo_path=str(b), end_card_vo_start=3.0)
    t, e, notes = pipeline._drop_duplicate_card_vo(params)
    assert t == str(c) and e == str(b) and notes == []


def test_music_skips_leading_silence(tmp_path):
    import subprocess, pipeline
    wav = tmp_path / 'm.wav'
    subprocess.run([pipeline.FFMPEG, '-y', '-loglevel', 'error', '-f', 'lavfi', '-t', '2', '-i', 'anullsrc=r=44100:cl=mono',
                    '-f', 'lavfi', '-t', '3', '-i', 'sine=frequency=440', '-filter_complex', '[0][1]concat=n=2:v=0:a=1', str(wav)], check=True)
    t = pipeline._skip_leading_silence(str(wav), 0.0)
    assert 1.8 < t < 2.1
    assert pipeline._skip_leading_silence(str(wav), 2.5) == 2.5


def test_narration_range_gives_one_part_per_line(tmp_path, monkeypatch):
    import pipeline
    f = tmp_path / 'v.wav'; f.write_bytes(b'x' * 100)
    toks = 'kakanta na ba pag nahuli na ngayong intense na huwebes taskforce firewall'.split()
    words = [{'word': w, 'start': i * 0.5 + (2.0 if i >= 5 else 0) + (2.0 if i >= 9 else 0), 'end': i * 0.5 + 0.4 + (2.0 if i >= 5 else 0) + (2.0 if i >= 9 else 0)}
             for i, w in enumerate(toks)]
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda *a, **k: (words, []))
    brief = {'vo': [{'text': 'KAKANTA NA BA PAG NAHULI NA', 'anchor': {'kind': 'clip'}},
                    {'text': 'NGAYONG INTENSE NA HUWEBES', 'anchor': {'kind': 'clip'}},
                    {'text': 'TASKFORCE FIREWALL', 'anchor': {'kind': 'card'}}]}
    r = pipeline._narration_range_from_brief(brief, str(f))
    assert len(r['parts']) == 3
    assert all(r['parts'][k]['out'] <= r['parts'][k + 1]['in'] + 1e-6 for k in range(2))
    assert (r['in'], r['out']) == (r['parts'][0]['in'], r['parts'][0]['out'])


def test_a_sentence_wrapped_over_two_highlighted_rows_is_one_line_but_a_line_ended_early_is_not():
    rows = [('L', 9, 'FINAL CUT/VGFX/AUDIO', None, False), ('R', 9, 'PVPD_FIREWA_070226_LN015', None, False),
            ('L', 21, 'EDITOR/ JAIMEE /JARED/NEIL', None, False), ('R', 21, 'VO SENT JUNE 30 PM SHIFT', None, False),
            ('L', 100, 'M1 3:33—37 HABOL TACKLE', None, False),
            ('R', 100, 'AKO ANG NAGTIMBRE SA SINDIKATO PARA DI MO MALIGRAS', YELLOW, False),
            ('R', 111, 'KAPATID MO', YELLOW, False),
            ('R', 160, 'TASKFORCE FIREWALL', YELLOW, False), ('R', 171, '8:50PM ON GMA PRIME', YELLOW, False)]
    b = sb.read_script_brief(_script_pdf(rows), 'script.pdf', '', [])
    assert [v['text'] for v in b['vo']] == ['AKO ANG NAGTIMBRE SA SINDIKATO PARA DI MO MALIGRAS KAPATID MO',
                                           'TASKFORCE FIREWALL', '8:50PM ON GMA PRIME']


def _post_generate(**extra):
    import io as _io
    import unittest.mock as mock
    import core
    pipeline.ALLOW_LOCAL_MEDIA_UPLOAD, was = True, pipeline.ALLOW_LOCAL_MEDIA_UPLOAD
    try:
        return _post_generate_inner(_io, mock, core, **extra)
    finally:
        pipeline.ALLOW_LOCAL_MEDIA_UPLOAD = was


def _post_generate_inner(_io, mock, core, **extra):
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t-jcut')
    got = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            got['params'] = args[1] if len(args) > 1 else None

        def start(self):
            pass
    core._job_submit_limiter.buckets.clear()
    data = {'file': (_io.BytesIO(b'\x00' * 10), 'mat1.mp4'), 'genre': '', 'trailer_length': '15',
            'scoring_mode': 'none', 'vo_mode': 'none'}
    data.update(extra)
    with mock.patch('requests.post'), mock.patch('requests.get'), mock.patch('pipeline.threading.Thread', FakeThread):
        r = client.post('/api/trailer/generate', data=data, headers={'X-CSRF-Token': 't-jcut'},
                        content_type='multipart/form-data')
    assert r.status_code == 200, r.get_data(as_text=True)
    return got['params']


def test_the_voice_lead_is_automatic_unless_the_form_says_otherwise():
    p = _post_generate()
    assert p['vo_jcut_auto'] is True and p['vo_jcut'] == 0.0, 'default: only where the narration does not fit'
    p = _post_generate(vo_jcut='auto')
    assert p['vo_jcut_auto'] is True and p['vo_jcut'] == 0.0
    p = _post_generate(vo_jcut='0')
    assert p['vo_jcut_auto'] is False and p['vo_jcut'] == 0.0, 'never'
    p = _post_generate(vo_jcut='0.5')
    assert p['vo_jcut_auto'] is False and p['vo_jcut'] == 0.5, 'always, by that much'
    p = _post_generate(vo_jcut='9')
    assert p['vo_jcut'] == 1.5
    assert _post_generate(vo_jcut='nonsense')['vo_jcut_auto'] is True


def test_the_plan_leads_a_line_only_where_it_does_not_fit_and_the_review_says_so(tmp_path, monkeypatch):
    f = tmp_path / 'nar.wav'
    f.write_bytes(b'x')
    words = [{'word': w, 'start': s, 'end': e} for w, s, e in
             [('abangan', 0.5, 1.0), ('ngayong', 1.05, 1.6), ('gabi', 1.65, 2.1), ('only', 3.0, 3.3), ('on', 3.35, 3.5), ('gma', 3.55, 4.1)]]
    monkeypatch.setattr(pipeline, '_transcribe_vo_cached', lambda p, a, b: (words, []))
    monkeypatch.setattr(pipeline, '_detect_silence_intervals', lambda *a, **k: [])
    monkeypatch.setattr(pipeline, 'probe_duration', lambda p: 5.0)
    text = '@2.00s Abangan ngayong gabi\n@3.00s Only on GMA'
    tight = pipeline._build_vo_plan(str(f), text, 0.0, None, 0.0, 14.85, lead_max=cutcraft.LEAD_AUTO_MAX)
    assert tight['lines'][0].get('lead', 0) > 0.5 and any('J-cut' in n for n in tight['notes'])
    assert pipeline._vo_plan_summary(tight)['lines'][0]['lead'] > 0.5, 'the review gets it'
    never = pipeline._build_vo_plan(str(f), text, 0.0, None, 0.0, 14.85, lead_max=0.0)
    assert 'lead' not in never['lines'][0] and not any('J-cut' in n for n in never['notes'])
    roomy = pipeline._build_vo_plan(str(f), '@2.00s Abangan ngayong gabi\n@6.00s Only on GMA', 0.0, None, 0.0, 14.85,
                                    lead_max=cutcraft.LEAD_AUTO_MAX)
    assert not any('lead' in l for l in roomy['lines']), 'fits: no J-cut'
    assert pipeline._plan_view({'vo_jcut_auto': True}, tight, None, 15.0, 0.3)['jcut_auto'] is True


def _env(spec, total=10.0, hop=0.01, floor=-70.0):
    """dB readings from [(start, end, level)] spans."""
    import numpy as np
    db = np.full(int(total / hop), floor)
    for a, b, lvl in spec:
        db[int(a / hop):int(b / hop)] = lvl
    return db, hop


def test_snap_to_voice_ends_on_the_last_sound_not_the_breath_after_it():
    import pipeline
    # voice at -12 dB from 1.0 to 2.5, then a LOUD breath (-28 dB, only 16 dB under the voice) to 3.0
    env = _env([(1.0, 2.5, -12.0), (2.5, 3.0, -31.0)])
    lo, hi = pipeline._snap_to_voice(env, 1.05, 2.9)      # the transcript's word end runs through the breath
    assert abs(hi - (2.5 + pipeline.VOICE_TAIL)) < 0.02
    assert abs(lo - (1.0 - pipeline.VOICE_LEAD)) < 0.02
    # a line the audio cannot place keeps the transcript padding
    assert pipeline._snap_to_voice(None, 1.0, 2.0) == (0.75, 2.35)


def test_a_hot_recording_is_cut_the_same_as_a_quiet_one():
    import pipeline
    quiet = _env([(1.0, 2.5, -30.0), (2.5, 3.0, -52.0)])
    hot = _env([(1.0, 2.5, -4.0), (2.5, 3.0, -26.0)])     # the same take 26 dB louder: its breath is far above -34 dB
    a = pipeline._snap_to_voice(quiet, 1.05, 2.9)
    b = pipeline._snap_to_voice(hot, 1.05, 2.9)
    assert abs(a[1] - b[1]) < 0.02 and a[1] < 2.7


def test_a_click_is_not_taken_for_the_end_of_the_line():
    import pipeline
    env = _env([(1.0, 2.5, -12.0), (2.8, 2.81, -10.0)])
    assert pipeline._snap_to_voice(env, 1.05, 2.9)[1] < 2.7


def test_snap_to_voice_on_real_audio_leaves_the_breath_out(tmp_path):
    import shutil, subprocess
    import pipeline
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    f = tmp_path / 'v.wav'
    # 1.5 s voice, 0.5 s breath about 20 dB under it (a loud one), then a second line
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=f=300:d=1.5,volume=4',
                    '-f', 'lavfi', '-i', 'anoisesrc=d=0.5:a=0.05', '-f', 'lavfi', '-i', 'anullsrc=d=0.8:r=44100',
                    '-f', 'lavfi', '-i', 'sine=f=300:d=1.2,volume=4', '-filter_complex',
                    '[0][1][2][3]concat=n=4:v=0:a=1,aformat=sample_rates=44100:channel_layouts=mono', str(f)],
                   check=True, timeout=60)
    env = pipeline._voice_envelope(str(f))
    lo, hi = pipeline._snap_to_voice(env, 0.05, 2.2)      # the transcript wrongly ends the first line at 2.2
    assert 1.45 < hi < 1.8, hi


def test_end_card_line_follows_led_title_line():
    import scriptbrief as sb
    vo = [{'text': 'TASKFORCE FIREWALL', 'anchor': {'kind': 'card', 'label': ''}},
          {'text': '8:50PM ON GMA PRIME', 'anchor': {'kind': 'card', 'label': ''}}]
    clips = [{'material': 1, 'start': 0.0, 'selected_dur': 5.0}]
    led, _ = sb.place_lines(vo, clips, [0.0], [10.5, 12.5], 0.3, card_jcut=1.9)
    assert led[0]['at'] < 10.5 and led[1]['at'] is None            # title line led; the end line follows it
    plain, _ = sb.place_lines(vo, clips, [0.0], [10.5, 12.5], 0.3, card_jcut=0.0)
    assert plain[0]['at'] is not None and plain[1]['at'] is not None   # unchanged without a lead
