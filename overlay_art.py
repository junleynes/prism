"""Artwork laid over a finished promo: a still (PNG with transparency, JPEG) or a video with an alpha channel
(ProRes 4444 / QuickTime Animation .mov, VP9 .webm). It appears with a chosen animation, holds, and leaves.
"""
import os
import subprocess

ANIMS = ('none', 'fade', 'slide_left', 'slide_right', 'slide_up', 'slide_down', 'pop')
OUTS = ('none', 'fade')
POSITIONS = ('full', 'center', 'top_left', 'top_right', 'bottom_left', 'bottom_right')
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'}
MARGIN = 0.04               # of the frame's width, kept between the artwork and the edge


def prepare(art, workdir):
    """(path ffmpeg can read, temp file to delete or None). A Photoshop file is flattened to a PNG with its
    transparency kept: every visible layer composed as it shows in Photoshop. Raises ValueError when it cannot be read."""
    if os.path.splitext(art)[1].lower() not in ('.psd', '.psb'):
        return art, None
    try:
        from psd_tools import PSDImage
    except ImportError as e:
        raise ValueError('Photoshop files need the psd-tools package on the server (pip install psd-tools); '
                         'a PNG of the same artwork works now.') from e
    try:
        img = PSDImage.open(art).composite(force=True, alpha=0.0)
        dst = os.path.join(workdir, f'overlay_psd_{os.getpid()}_{int(os.path.getmtime(art) * 1000)}.png')
        img.convert('RGBA').save(dst)
    except Exception as e:
        raise ValueError(f'This Photoshop file could not be flattened ({e}).') from e
    return dst, dst


def clean_options(form):
    """The artwork settings from a form (or any .get-able), clamped to what makes sense."""
    def f(k, d, lo, hi):
        try:
            v = float(form.get(k) if form.get(k) not in (None, '') else d)
        except (TypeError, ValueError):
            v = d
        return max(lo, min(hi, v))
    anim = form.get('overlay_anim') or 'fade'
    out = form.get('overlay_out') or 'fade'
    pos = form.get('overlay_pos') or 'full'
    return {'start': f('overlay_start', 1.0, 0.0, 600.0), 'hold': f('overlay_hold', 0.0, 0.0, 600.0),
            'anim': anim if anim in ANIMS else 'fade', 'out': out if out in OUTS else 'fade',
            'dur': f('overlay_anim_dur', 0.5, 0.1, 3.0), 'pos': pos if pos in POSITIONS else 'full',
            'scale': f('overlay_scale', 100.0, 5.0, 100.0)}


def _ease(p):
    """Eased progress 0..1 as an ffmpeg expression of the plain progress `p`: fast start, soft landing."""
    return f'(1-pow(1-({p}),3))'


def filter_graph(o, total, frame_w):
    """filter_complex turning [0:v] (the promo) and [1:v] (the artwork) into [v]. `total` is the promo's length."""
    s = float(o['start'])
    e = min(float(total), s + o['hold']) if o['hold'] > 0 else float(total)
    d = min(o['dur'], max(0.1, (e - s) / 2.0))
    scale = o['scale'] / 100.0
    art = ['format=rgba']
    w_expr = f'{int(frame_w * scale) // 2 * 2}'
    if o['anim'] == 'pop':
        # grows from 60% to full size (with a small overshoot) while it fades in; sized per frame, centred below
        p = f'min(1,t/{d:.3f})'
        k = f'(0.6+0.4*{_ease(p)}+0.06*sin(3.14159*min(1,t/{d:.3f})))'
        art.append(f"scale=w='trunc({frame_w}*{scale:.4f}*{k}/2)*2':h=-2:eval=frame")
    else:
        art.append(f'scale={w_expr}:-2')
    if o['anim'] in ('fade', 'pop'):
        art.append(f'fade=t=in:st=0:d={d:.3f}:alpha=1')
    if o['out'] == 'fade':
        art.append(f'fade=t=out:st={max(0.0, e - s - d):.3f}:d={d:.3f}:alpha=1')
    art.append(f'setpts=PTS+{s:.3f}/TB')
    m = f'main_w*{MARGIN}'
    X = {'full': '(main_w-overlay_w)/2', 'center': '(main_w-overlay_w)/2', 'top_left': m, 'bottom_left': m,
         'top_right': f'main_w-overlay_w-{m}', 'bottom_right': f'main_w-overlay_w-{m}'}[o['pos']]
    Y = {'full': '(main_h-overlay_h)/2', 'center': '(main_h-overlay_h)/2', 'top_left': m, 'top_right': m,
         'bottom_left': f'main_h-overlay_h-{m}', 'bottom_right': f'main_h-overlay_h-{m}'}[o['pos']]
    p = f'min(1,max(0,(t-{s:.3f})/{d:.3f}))'
    q = f'min(1,max(0,({e:.3f}-t)/{d:.3f}))'
    ease_in = _ease(p)
    if o['anim'] == 'slide_left':
        X = f'({X})-(overlay_w+({X}))*(1-{ease_in})'
    elif o['anim'] == 'slide_right':
        X = f'({X})+(main_w-({X}))*(1-{ease_in})'
    elif o['anim'] == 'slide_up':
        Y = f'({Y})+(main_h-({Y}))*(1-{ease_in})'
    elif o['anim'] == 'slide_down':
        Y = f'({Y})-(overlay_h+({Y}))*(1-{ease_in})'
    if o['out'] == 'fade' and o['anim'].startswith('slide'):
        pass                                   # a slid-in piece leaves by fading (the alpha fade above)
    return (f"[1:v]{','.join(art)}[ov];"
            f"[0:v][ov]overlay=x='{X}':y='{Y}':eof_action=repeat:enable='between(t,{s:.3f},{e:.3f})':format=auto,"
            f"format=yuv420p[v]")


def probe_video(ffmpeg, src):
    """(width, fps) of the promo."""
    import shutil
    probe = ffmpeg.replace('ffmpeg', 'ffprobe') if 'ffmpeg' in os.path.basename(ffmpeg) else None
    probe = probe if probe and (os.path.exists(probe) or shutil.which(probe)) else shutil.which('ffprobe') or 'ffprobe'
    r = subprocess.run([probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,r_frame_rate',
                        '-of', 'csv=p=0', src], capture_output=True, text=True, timeout=30)
    try:
        w, rate = r.stdout.strip().split(',')[:2]
        n, d = rate.split('/')
        return int(w), (float(n) / float(d) if float(d) else 25.0)
    except (ValueError, ZeroDivisionError):
        return 1920, 25.0


def apply(ffmpeg, src, art, dst, o, total, timeout=600):
    """Writes `dst`: `src` with the artwork over it. Audio is copied. True when it worked."""
    is_image = os.path.splitext(art)[1].lower() in IMAGE_EXTS
    frame_w, fps = probe_video(ffmpeg, src)
    pre = ['-loop', '1', '-framerate', f'{fps:g}', '-t', f'{total:.3f}'] if is_image else []
    cmd = [ffmpeg, '-y', '-loglevel', 'error', '-i', src] + pre + ['-i', art,
           '-filter_complex', filter_graph(o, total, frame_w), '-map', '[v]', '-map', '0:a?',
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p',
           '-c:a', 'copy', '-movflags', '+faststart', '-t', f'{total:.3f}', dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0, r.stderr[-400:]
