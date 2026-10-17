"""Card narration with the dead air taken out."""
import os
import subprocess

import numpy as np
import pytest

import pipeline

FF = ['ffmpeg', '-y', '-loglevel', 'error']


def _speech(path, pieces):
    """pieces: [('tone', s) | ('gap', s)] -> a wav of 1 kHz tone bursts and silences."""
    ins, labels = [], []
    for i, (kind, s) in enumerate(pieces):
        src = 'sine=frequency=1000:sample_rate=48000' if kind == 'tone' else 'anullsrc=r=48000:cl=mono'
        ins += ['-t', str(s), '-f', 'lavfi', '-i', src]
        labels.append(f'[{i}:a]')
    subprocess.run(FF + ins + ['-filter_complex', ''.join(labels) + f'concat=n={len(pieces)}:v=0:a=1',
                               '-ar', '48000', '-ac', '1', str(path)], check=True)
    return str(path)


def _dur(path):
    return pipeline.probe_duration(str(path))


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setitem(pipeline.app.config, 'UPLOAD_FOLDER', str(tmp_path))
    return tmp_path


def test_long_pauses_between_words_and_around_them_are_taken_out(work):
    src = _speech(work / 'vo.wav', [('gap', 1.2), ('tone', 0.6), ('gap', 1.5), ('tone', 0.6), ('gap', 0.1),
                                    ('tone', 0.6), ('gap', 2.0)])
    assert abs(_dur(src) - 6.6) < 0.05
    path, a, b, saved = pipeline.tighten_card_vo(src, 0.0, None)
    assert path != src and (a, b) == (0.0, None)
    # lead 0.08 + 0.6 + 0.18 + 0.6 + 0.1 (a short pause is kept as it is) + 0.6 + tail 0.25
    assert abs(_dur(path) - 2.41) < 0.2, _dur(path)
    assert abs(saved - (6.6 - _dur(path))) < 0.15 and saved > 3.5
    raw = subprocess.run(FF + ['-i', path, '-f', 's16le', '-ac', '1', '-ar', '48000', '-'],
                         capture_output=True, check=True).stdout
    pcm = np.abs(np.frombuffer(raw, np.int16))
    assert pcm[:int(0.04 * 48000)].max() < 300, 'starts on silence only for the short lead'
    assert (pcm > 2000).sum() > 0.55 * 1.8 * 48000, 'every word kept'


def test_a_clip_with_nothing_to_take_out_is_left_alone(work):
    src = _speech(work / 'vo.wav', [('tone', 1.0), ('gap', 0.1), ('tone', 1.0)])
    assert pipeline.tighten_card_vo(src, 0.0, None) == (src, 0.0, None, 0.0)


def test_the_in_and_out_chosen_are_respected(work):
    src = _speech(work / 'vo.wav', [('tone', 0.5), ('gap', 3.0), ('tone', 0.5), ('gap', 1.0), ('tone', 3.0)])
    path, _, _, saved = pipeline.tighten_card_vo(src, 0.0, 4.5)        # the last tone is outside the window
    assert abs(_dur(path) - (0.08 + 0.5 + 0.18 + 0.5 + 0.25)) < 0.2, _dur(path)


def test_a_missing_file_is_a_no_op(work):
    assert pipeline.tighten_card_vo(str(work / 'nope.wav'), 0.0, None)[3] == 0.0


def test_the_page_paints_and_drags_every_part_of_a_multi_part_recording(users_db):
    from tests.test_shorts import _client
    client, _ = _client()
    html = client.get('/').get_data(as_text=True)
    for needle in ('_extraHandles', "dragMode = 'extra'", 'extraSel', "'.extra-ranges[data-prefix=\"' + prefix + '\"] .er-row'"):
        assert needle in html, needle


def _card(path, secs):
    subprocess.run(FF + ['-f', 'lavfi', '-i', f'color=c=black:s=320x180:r=25:d={secs}', '-f', 'lavfi',
                         '-i', f'anullsrc=r=48000:cl=stereo:d={secs}', '-shortest', '-c:v', 'libx264',
                         '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    return str(path)


def test_silence_is_only_taken_out_of_card_narration_that_does_not_fit_its_card(work):
    card = _card(work / 'card.mp4', 4.0)
    fits = _speech(work / 'fits.wav', [('gap', 0.6), ('tone', 0.8), ('gap', 1.0), ('tone', 0.8)])       # 3.2 s
    long_ = _speech(work / 'long.wav', [('gap', 1.2), ('tone', 0.8), ('gap', 1.5), ('tone', 0.8), ('gap', 1.5)])  # 5.8 s
    assert pipeline.card_vo_overruns(card, fits, 0.0, None) is False, 'fits: left as recorded'
    assert pipeline.card_vo_overruns(card, long_, 0.0, None) is True
    assert pipeline.card_vo_overruns(card, long_, 0.0, 3.0) is False, 'its Out is what counts'
    assert pipeline.card_vo_overruns(card, long_, 1.0, None) is True
    assert pipeline.card_vo_overruns(str(work / 'nope.mp4'), long_, 0.0, None) is False
    path, _, _, saved = pipeline.tighten_card_vo(long_, 0.0, None)
    assert saved > 1.5 and _dur(path) < 4.0, 'taken out, it now fits the card'
