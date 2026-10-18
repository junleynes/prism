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


def _layered_psd(tmp_path, size=(640, 360)):
    from PIL import Image
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    psd = PSDImage.new('RGBA', size)
    psd.append(PixelLayer.frompil(Image.new('RGBA', size, (10, 10, 60, 255)), psd, 'bg', top=0, left=0))
    psd.append(PixelLayer.frompil(Image.new('RGBA', (200, 60), (255, 0, 0, 255)), psd, 'logo', top=40, left=40))
    psd.append(PixelLayer.frompil(Image.new('RGBA', (240, 50), (255, 0, 0, 255)), psd, 'title', top=270, left=200))
    p = str(tmp_path / 'layers.psd')
    psd.save(p)
    return p


@pytest.mark.parametrize('style', ['fade_still', 'slide_float', 'pop_pulse', 'wipe_mix'])
def test_layers_arrive_on_nothing_and_the_promo_shows_through(tmp_path, style):
    base, _ = _fixtures(tmp_path)
    psd = _layered_psd(tmp_path)
    lo = oa.clean_layer_options({'overlay_mode': 'layers', 'overlay_style': style})
    mov = str(tmp_path / 'layers.mov')
    ok, note = oa.render_layers(FF, psd, lo, 4.0, (640, 360), 25.0, mov, str(tmp_path))
    assert ok, note
    o = oa.clean_options({'overlay_anim': 'none', 'overlay_start': '1', 'overlay_hold': '0', 'overlay_out': 'none'})
    dst = str(tmp_path / 'out.mp4')
    assert oa.apply(FF, base, mov, dst, o, 6.0)[0]
    assert _red_at(dst, 0.5) == 0                          # not yet
    assert _red_at(dst, 5.0) > 3000                        # arrived and held (red logo + title, backdrop left out)
    r = subprocess.run([FF, '-loglevel', 'error', '-ss', '5', '-i', dst, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                       capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8).reshape(360, 640, 3).astype(int)
    assert abs(a[180, 320, 0] - 0x30) < 12                 # the promo's own grey is still there in the middle: no backdrop


def test_keep_background_covers_the_promo(tmp_path):
    base, _ = _fixtures(tmp_path)
    psd = _layered_psd(tmp_path)
    lo = oa.clean_layer_options({'overlay_mode': 'layers', 'overlay_style': 'fade_still', 'overlay_keep_bg': 'on'})
    mov = str(tmp_path / 'l.mov')
    assert oa.render_layers(FF, psd, lo, 4.0, (640, 360), 25.0, mov, str(tmp_path))[0]
    o = oa.clean_options({'overlay_anim': 'none', 'overlay_start': '0', 'overlay_out': 'none'})
    dst = str(tmp_path / 'k.mp4')
    assert oa.apply(FF, base, mov, dst, o, 6.0)[0]
    r = subprocess.run([FF, '-loglevel', 'error', '-ss', '5', '-i', dst, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                       capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8).reshape(360, 640, 3).astype(int)
    assert a[180, 320, 2] > 40 and a[180, 320, 0] < 30      # the PSD's dark-blue backdrop
