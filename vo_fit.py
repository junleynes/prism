"""Fitting the script's lines to a narration recording by its own sound.

A speech-to-text service gives where each word was heard, and for some languages (or servers that
return only line timings) those times are rough: a line's end may sit in the pause after it, or a
whole phrase early. The sound is exact about where speech starts and stops. So the recording is cut
into its spoken stretches and the pauses between them, and the script's lines -- in order -- are laid
over those stretches, using the transcript only as a hint of where each one is and how long each line
should take to say. Every line then starts on a sound and ends on a sound, and two lines are never
separated by anything but a pause.
"""
import re
import numpy as np

HOP = 0.01
MIN_GAP = 0.12          # a quiet this long is a pause between stretches of speech
MIN_RUN = 0.06          # a sound this short is a click, not speech
LEAD = 0.08             # room kept before the first sound of a line
TAIL = 0.12             # room kept after its last sound (the word's own fade-out)
REGION_PAD = 1.2        # stretches this far beyond the transcript's first and last line are considered
MAX_SEGS_PER_LINE = 8
SKIP = 0.5              # what leaving out a stretch (another take, a stray sound) at either end of the lines costs
RATES = (9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 20.0)   # letters a second


def speech_segments(db, hop=HOP, lo=0.0, hi=None):
    """[(start, end)] seconds of the stretches of speech in `db` (dB readings every `hop` s), a quiet of
    MIN_GAP or more ending one. 'Quiet' is judged against the recording itself: well under its loud speech."""
    db = np.asarray(db, dtype=np.float64)
    if not len(db):
        return []
    floor, top = float(np.percentile(db, 5)), float(np.percentile(db, 99))
    if top - floor < 12.0:
        return []
    thr = max(floor + 12.0, top - 30.0)
    loud = db >= thr
    i0 = int(max(0.0, lo) / hop)
    i1 = len(db) if hi is None else min(len(db), int(hi / hop) + 1)
    runs, i = [], i0
    while i < i1:
        if loud[i]:
            j = i
            while j < i1 and loud[j]:
                j += 1
            if (j - i) * hop >= MIN_RUN:
                runs.append([i * hop, j * hop])
            i = j
        else:
            i += 1
    segs = []
    for a, b in runs:
        if segs and a - segs[-1][1] < MIN_GAP:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    return [(a, b) for a, b in segs]


COARSE_GAP = 0.3        # a pause this long is a break between phrases, not between two words
START_REACH = 0.8       # a line starts on a sound this near the time the transcript heard it start


def place_lines(segs, lines):
    """One (start, end) per line, laid over the recording's stretches of speech `segs`. `lines` are in recording
    order, each {'in', 'out'} as the transcript heard them. None when the sound gives nothing to go on.

    The transcript's START of a line is the part to trust: a service that times words, or only whole lines,
    tends to run a line's END on into the pause after it (even up to the next line's first sound). So a line
    begins on the sound nearest where it was heard to begin -- a break between phrases preferred to a gap between
    two words -- and ends on the last sound before the next line begins. The last line ends on the last sound
    the transcript heard before its own end, not on whatever it ran on into."""
    if not segs or not lines:
        return None
    onsets = [(a, (k == 0 or a - segs[k - 1][1] >= COARSE_GAP)) for k, (a, _b) in enumerate(segs)]
    starts = []
    for l in lines:
        w = float(l['in'])
        best = None
        for k, (a, coarse) in enumerate(onsets):
            d = abs(a - w)
            if d > START_REACH:
                continue
            score = d - (0.3 if coarse else 0.0)
            if best is None or score < best[0]:
                best = (score, k)
        starts.append(best[1] if best else None)
    # Two lines on one sound, or out of order: the transcript cannot tell them apart here.
    for i in range(1, len(starts)):
        if starts[i] is not None and starts[i - 1] is not None and starts[i] <= starts[i - 1]:
            return None
    out = []
    for i, l in enumerate(lines):
        k = starts[i]
        if k is None:
            out.append(None)
            continue
        nxt = next((starts[j] for j in range(i + 1, len(lines)) if starts[j] is not None), None)
        if nxt is not None:
            e = nxt - 1                                       # the last stretch before the next line begins
        else:
            e = k
            w_out = float(l['out'])
            while e + 1 < len(segs) and segs[e + 1][0] <= w_out - 0.1:
                e += 1
        out.append((max(0.0, segs[k][0] - LEAD), segs[e][1] + TAIL))
    return out
