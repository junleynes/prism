"""Where each stretch of a cut video comes from in its uncut parts, found by matching the sound.

The cut video is the parts laid end to end with pieces taken out. Its audio is compared with the parts'
audio on a spectral fingerprint (log energy in a few frequency bands, 100 frames a second), which does not
care what language is spoken, whether there is any speech at all, or how loud the copy is. The result is a
list of kept stretches: (video start, video end, source start) in seconds, the source being the parts end to
end. Cut points are placed to the hundredth of a second.

Pure numpy; ffmpeg decodes the audio."""
import subprocess

import numpy as np

SR = 8000           # audio rate the fingerprint is taken at
HOP = 80            # 10 ms per fingerprint frame
WIN = 256
FPS = SR // HOP     # 100 fingerprint frames a second
BANDS = 16
W = 300             # frames in a matching window (3 s)
STEP = 100          # windows start every second
GOOD = 0.80         # similarity at which a window is taken as found where expected
FOUND = 0.65        # similarity a searched-for window needs
MIN_KEEP = 1.0      # seconds: a stretch shorter than this is not believed on its own


def load_audio(ffmpeg, path, timeout=3600):
    """Mono float32 at SR, the first audio track, from any file ffmpeg reads."""
    cmd = [ffmpeg, '-v', 'error', '-nostdin', '-i', path, '-map', '0:a:0', '-ac', '1', '-ar', str(SR),
           '-f', 'f32le', '-']
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        raise ValueError((p.stderr or b'').decode('utf-8', 'replace').strip()[-300:] or 'no audio could be read')
    a = np.frombuffer(p.stdout, dtype=np.float32)
    if not len(a):
        raise ValueError('the file has no audio')
    return a


def _edges():
    f = np.fft.rfftfreq(WIN, 1.0 / SR)
    e = np.geomspace(100.0, SR / 2 * 0.95, BANDS + 1)
    return [(int(np.searchsorted(f, e[i])), max(int(np.searchsorted(f, e[i + 1])), int(np.searchsorted(f, e[i])) + 1))
            for i in range(BANDS)]


_BANDS = None


def fingerprint(audio):
    """(frames, BANDS) float32: log band energy, each band's mean taken off and scaled to unit spread, so a
    copy at another level or with another EQ still lines up."""
    global _BANDS
    if _BANDS is None:
        _BANDS = _edges()
    # Padded so there is one frame per HOP samples: the frames of parts laid end to end then add up to their time.
    audio = np.concatenate([audio, np.zeros(WIN, dtype=np.float32)])
    n = max(0, (len(audio) - WIN) // HOP)
    out = np.empty((n, BANDS), dtype=np.float32)
    win = np.hanning(WIN).astype(np.float32)
    chunk = 20000
    for s in range(0, n, chunk):
        k = min(chunk, n - s)
        idx = (np.arange(k)[:, None] + s) * HOP + np.arange(WIN)[None, :]
        spec = np.abs(np.fft.rfft(audio[idx] * win, axis=1)) ** 2
        for b, (lo, hi) in enumerate(_BANDS):
            out[s:s + k, b] = np.log(spec[:, lo:hi].sum(axis=1) + 1e-9)
    out -= out.mean(axis=0, keepdims=True)
    out /= out.std(axis=0, keepdims=True) + 1e-6
    return out


def _sim_at(V, S, v, s, w=W):
    """Cosine similarity of V[v:v+w] and S[s:s+w] (both already centred), or -1 off the end."""
    if s < 0 or v < 0 or s + w > len(S) or v + w > len(V):
        return -1.0
    a, b = V[v:v + w].ravel(), S[s:s + w].ravel()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / d if d > 0 else -1.0


class _Searcher:
    """Finds a window anywhere in the source: cross-correlation by FFT, per band, normalised by the
    source's own energy under the window."""

    def __init__(self, S):
        self.S = S
        n = len(S) + W
        self.N = 1 << int(np.ceil(np.log2(n)))
        self.FS = np.fft.rfft(S, n=self.N, axis=0)
        sq = np.concatenate([[0.0], np.cumsum((S.astype(np.float64) ** 2).sum(axis=1))])
        self.energy = np.sqrt(np.maximum(sq[W:] - sq[:-W], 1e-9))      # for each start s: |S[s:s+W]|

    def best(self, win):
        fw = np.fft.rfft(win[::-1], n=self.N, axis=0)
        corr = np.fft.irfft((self.FS * fw).sum(axis=1), n=self.N)
        # corr[s + W - 1] = sum S[s:s+W] * win
        c = corr[W - 1:W - 1 + len(self.energy)] / (self.energy * (np.linalg.norm(win) + 1e-9))
        s = int(np.argmax(c))
        return s, float(c[s])


def match(V, S, report=None):
    """Kept stretches [(v0, v1, s0)] in fingerprint frames: V[v0:v1] is S[s0:s0 + v1 - v0]."""
    if len(V) < W or len(S) < W:
        return []
    srch = _Searcher(S)
    found = []                # (v, s) of each window placed
    d = None
    starts = list(range(0, len(V) - W + 1, STEP))
    for n, v in enumerate(starts):
        if report and n % 200 == 0:
            report(n / max(len(starts), 1))
        if d is not None:
            # Where it would be if nothing was cut here; a frame or two either side for drift.
            best = max(((_sim_at(V, S, v, v + d + e), e) for e in (0, -1, 1, -2, 2)), key=lambda x: x[0])
            if best[0] >= GOOD:
                found.append((v, v + d + best[1]))
                d += best[1]
                continue
        s, sim = srch.best(V[v:v + W])
        if sim >= FOUND:
            d = s - v
            found.append((v, s))
        else:
            d = None
    # Runs of windows that agree on the offset.
    runs = []
    for v, s in found:
        if runs and abs((s - v) - runs[-1]['d']) <= 2 and v - runs[-1]['v1'] <= STEP:
            runs[-1]['v1'] = v + W
            continue
        runs.append({'v0': v, 'v1': v + W, 'd': s - v})
    runs = [r for r in runs if (r['v1'] - r['v0']) >= max(W, MIN_KEEP * FPS)]
    if not runs:
        return []
    # Each run reaches out frame by frame as far as the sound still matches, and two runs that overlap
    # (a cut between two windows) meet where switching from one to the other fits best.
    fsim = lambda v, s: _frame_sim(V, S, v, s)
    for i, r in enumerate(runs):
        lo = runs[i - 1]['v1'] if i else 0
        while r['v0'] > lo and fsim(r['v0'] - 1, r['v0'] - 1 + r['d']) > 0.5:
            r['v0'] -= 1
        hi = runs[i + 1]['v0'] if i + 1 < len(runs) else len(V)
        while r['v1'] < hi and fsim(r['v1'], r['v1'] + r['d']) > 0.5:
            r['v1'] += 1
    for a, b in zip(runs, runs[1:]):
        if a['v1'] <= b['v0']:
            continue
        lo, hi = b['v0'], a['v1']
        ga = np.array([fsim(v, v + a['d']) for v in range(lo, hi)])
        gb = np.array([fsim(v, v + b['d']) for v in range(lo, hi)])
        # switch at k: frames lo..lo+k-1 from a, the rest from b
        score = np.concatenate([[0.0], np.cumsum(ga)]) + np.concatenate([np.cumsum(gb[::-1])[::-1], [0.0]])
        k = int(np.argmax(score))
        a['v1'] = b['v0'] = lo + k
    return [(r['v0'], r['v1'], r['v0'] + r['d']) for r in runs if r['v1'] - r['v0'] >= MIN_KEEP * FPS]


def _frame_sim(V, S, v, s, half=5):
    """Similarity of a few frames around v and s: what says whether one frame still belongs to a stretch."""
    a0, a1 = max(0, v - half), min(len(V), v + half + 1)
    o = s - v
    b0, b1 = a0 + o, a1 + o
    if b0 < 0 or b1 > len(S) or a1 <= a0:
        return -1.0
    a, b = V[a0:a1].ravel(), S[b0:b1].ravel()
    dn = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / dn if dn > 0 else -1.0


def align_files(ffmpeg, video, parts, report=None):
    """{'stretches': [(v0, v1, s0) seconds], 'part_durations': [s], 'video_duration': s} for a cut video
    and its uncut parts (in order)."""
    durs, fps_ = [], []
    for k, p in enumerate(parts):
        if report:
            report(f'Reading part {k + 1} of {len(parts)}')
        a = load_audio(ffmpeg, p)
        durs.append(len(a) / SR)
        fps_.append(fingerprint(a))
    if report:
        report('Reading the cut video')
    va = load_audio(ffmpeg, video)
    V = fingerprint(va)
    S = np.concatenate(fps_) if len(fps_) > 1 else fps_[0]
    if report:
        report('Matching the sound')
    st = match(V, S)
    # The parts' fingerprints are laid end to end, so a part starts where the frames before it end.
    offs = [sum(len(f) for f in fps_[:k]) / FPS for k in range(len(fps_))]
    return {'stretches': [(v0 / FPS, v1 / FPS, s0 / FPS) for v0, v1, s0 in st],
            'part_durations': durs, 'video_duration': len(va) / SR, 'part_offsets': offs}
