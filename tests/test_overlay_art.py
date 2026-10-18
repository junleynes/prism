import os
import shutil
import subprocess

import numpy as np
import pytest

import overlay_art as oa

FF = shutil.which('ffmpeg')
pytestmark = pytest.mark.skipif(not FF, reason='ffmpeg needed')


def _fixtures(tmp_path):
    from PIL import Image
    base = str(tmp_path / 'base.mp4')
    subprocess.run([FF, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=0x303030:s=640x360:r=25:d=6',
                    '-f', 'lavfi', '-i', 'sine=f=300:d=6', '-shortest', '-c:v', 'libx264', '-c:a', 'aac', base], check=True)
    im = Image.new('RGBA', (640, 360), (0, 0, 0, 0))
    for x in range(40, 300):
        for y in range(260, 330):
            im.putpixel((x, y), (255, 0, 0, 255))
    art = str(tmp_path / 'art.png')
    im.save(art)
    return base, art


def _red_at(path, t):
    r = subprocess.run([FF, '-loglevel', 'error', '-ss', f'{t}', '-i', path, '-frames:v', '1', '-f', 'rawvideo',
                        '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8).reshape(360, 640, 3).astype(int)
    return int(((a[:, :, 0] > 180) & (a[:, :, 1] < 80)).sum())


def test_options_are_clamped():
    o = oa.clean_options({'overlay_anim': 'spin', 'overlay_start': '-4', 'overlay_scale': '900', 'overlay_pos': 'nope'})
    assert o['anim'] == 'fade' and o['start'] == 0.0 and o['scale'] == 100.0 and o['pos'] == 'full'


@pytest.mark.parametrize('anim', ['none', 'fade', 'slide_left', 'slide_up', 'pop'])
def test_appears_holds_and_leaves(tmp_path, anim):
    base, art = _fixtures(tmp_path)
    dst = str(tmp_path / f'{anim}.mp4')
    o = oa.clean_options({'overlay_anim': anim, 'overlay_start': '1', 'overlay_hold': '3', 'overlay_anim_dur': '0.5'})
    ok, err = oa.apply(FF, base, art, dst, o, 6.0)
    assert ok, err
    assert _red_at(dst, 0.5) == 0                  # not yet
    assert _red_at(dst, 2.5) > 5000                # held (full-frame artwork: its red bar is there)
    assert _red_at(dst, 5.5) == 0                  # gone after start + hold
    # audio kept
    r = subprocess.run([FF, '-i', dst], capture_output=True, text=True)
    assert 'Audio' in r.stderr


def test_slide_is_partway_during_the_move(tmp_path):
    base, art = _fixtures(tmp_path)
    dst = str(tmp_path / 's.mp4')
    o = oa.clean_options({'overlay_anim': 'slide_left', 'overlay_start': '1', 'overlay_hold': '4', 'overlay_anim_dur': '1'})
    assert oa.apply(FF, base, art, dst, o, 6.0)[0]
    mid, held = _red_at(dst, 1.25), _red_at(dst, 3.0)
    assert 0 < mid < held


def test_corner_artwork_is_scaled_and_placed(tmp_path):
    base, art = _fixtures(tmp_path)
    dst = str(tmp_path / 'c.mp4')
    o = oa.clean_options({'overlay_anim': 'none', 'overlay_start': '0', 'overlay_pos': 'top_right', 'overlay_scale': '50'})
    assert oa.apply(FF, base, art, dst, o, 6.0)[0]
    r = subprocess.run([FF, '-loglevel', 'error', '-ss', '2', '-i', dst, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                       capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8).reshape(360, 640, 3).astype(int)
    red = (a[:, :, 0] > 180) & (a[:, :, 1] < 80)
    ys, xs = np.nonzero(red)
    assert xs.min() > 300 and ys.max() < 190          # the half-size artwork sits in the top-right quarter, margin kept


def test_photoshop_file_is_flattened_with_transparency(tmp_path):
    from PIL import Image
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    psd = PSDImage.new('RGBA', (640, 360))
    psd.append(PixelLayer.frompil(Image.new('RGBA', (260, 70), (255, 0, 0, 255)), psd, 'bar', top=260, left=40))
    src = str(tmp_path / 'art.psd')
    psd.save(src)
    png, tmp = oa.prepare(src, str(tmp_path))
    im = Image.open(png)
    assert tmp == png and im.mode == 'RGBA' and im.getpixel((100, 290))[:1] == (255,) and im.getpixel((5, 5))[3] == 0
    base, _ = _fixtures(tmp_path)
    dst = str(tmp_path / 'p.mp4')
    o = oa.clean_options({'overlay_anim': 'fade', 'overlay_start': '1', 'overlay_hold': '3'})
    assert oa.apply(FF, base, png, dst, o, 6.0)[0]
    assert _red_at(dst, 0.5) == 0 and _red_at(dst, 2.5) > 5000 and _red_at(dst, 5.5) == 0


def test_plain_images_pass_through_and_a_bad_psd_says_so(tmp_path):
    base, art = _fixtures(tmp_path)
    assert oa.prepare(art, str(tmp_path)) == (art, None)
    bad = tmp_path / 'bad.psd'
    bad.write_bytes(b'not a psd')
    with pytest.raises(ValueError):
        oa.prepare(str(bad), str(tmp_path))
