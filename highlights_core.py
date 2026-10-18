"""Scene-scoring helpers for the Highlights Video pipeline.

Pure functions, so each signal can be tested on its own rather than only through a full render:
multi-frame sampling, audio and motion energy, the best window inside a scene, near-duplicate
shots, vision tags weighted by genre, and the ranking pass over the top candidates.
"""
import os
import re
import json
import numpy as np

# ---- settings (env so a server can tune or switch each off) ----
FRAMES_PER_SCENE = max(1, int(os.environ.get('HIGHLIGHT_FRAMES', 3)))          # 1 = old: middle frame only
MULTI_FRAME_MIN = 1.2         # seconds: a shorter scene is judged on its middle frame alone
THUMB_WIDTH = 640             # frames sent to the vision model are shrunk to this width
ENERGY_WEIGHT = float(os.environ.get('HIGHLIGHT_ENERGY_WEIGHT', 1.0))          # 0 = off
SMART_WINDOW = os.environ.get('HIGHLIGHT_SMART_WINDOW', '1') not in ('0', 'false', 'no')
DEDUPE_SIM = float(os.environ.get('HIGHLIGHT_DEDUPE_SIM', 0.95))               # 0 = off
DEDUPE_PENALTY = float(os.environ.get('HIGHLIGHT_DEDUPE_PENALTY', 1.5))
RANK_TOP = int(os.environ.get('HIGHLIGHT_RANK_TOP', 8))                        # 0 = off
RANK_MAX_BONUS = 1.0
TAG_CLAMP = 1.0
ENVELOPE_STEP = 0.05          # seconds between loudness readings of the source sound
PRE_PAUSE = 0.4               # quiet this long before the first words = a held beat

TAGS = ('conflict', 'reveal', 'emotion', 'action', 'dialogue', 'reaction', 'comedy', 'romance',
        'establishing', 'filler')

# What each kind of promo wants more or less of. Added to the score, clamped to +-TAG_CLAMP.
_DEFAULT_WEIGHTS = {'conflict': .4, 'reveal': .4, 'emotion': .3, 'reaction': .2, 'establishing': -.3, 'filler': -1.0}
GENRE_TAG_WEIGHTS = {
    'drama': {'conflict': .5, 'emotion': .5, 'reveal': .5, 'reaction': .3, 'establishing': -.3, 'filler': -1.0},
    'romance': {'romance': .6, 'emotion': .5, 'reaction': .3, 'action': -.2, 'establishing': -.3, 'filler': -1.0},
    'action': {'action': .6, 'conflict': .4, 'reveal': .2, 'dialogue': -.2, 'establishing': -.2, 'filler': -1.0},
    'thriller': {'conflict': .5, 'reveal': .5, 'action': .3, 'establishing': -.2, 'filler': -1.0},
    'mystery': {'reveal': .6, 'conflict': .3, 'reaction': .3, 'establishing': -.1, 'filler': -1.0},
    'horror': {'reveal': .5, 'reaction': .4, 'conflict': .4, 'establishing': 0, 'filler': -1.0},
    'comedy': {'comedy': .6, 'reaction': .5, 'dialogue': .2, 'establishing': -.3, 'filler': -1.0},
    'documentary': {'dialogue': .3, 'reveal': .3, 'establishing': .1, 'filler': -1.0},
}
for _g in ('scifi', 'fantasy', 'adventure', 'western', 'sports', 'war', 'noir'):
    GENRE_TAG_WEIGHTS[_g] = dict(GENRE_TAG_WEIGHTS['action'] if _g in ('adventure', 'sports', 'war', 'western')
                                 else GENRE_TAG_WEIGHTS['thriller'])


def tag_prompt_suffix():
    return ('\n\nAlso list up to three tags for the shot, chosen only from: ' + ', '.join(TAGS) +
            '. Use "filler" for a shot that carries no story (people walking, a wide empty room, a transition).')


def parse_tags(txt):
    """Tags out of a structured vision reply; [] when there are none or the reply is not JSON."""
    if not txt or not txt.lstrip().startswith('{'):
        return []
    tags = None
    try:
        tags = json.loads(txt).get('tags')
    except (ValueError, AttributeError):
        m = re.search(r'"tags"\s*:\s*\[([^\]]*)', txt)
        if m:
            tags = re.findall(r'"([a-z]+)"', m.group(1))
    if not isinstance(tags, list):
        return []
    out = []
    for t in tags:
        t = str(t).strip().lower()
        if t in TAGS and t not in out:
            out.append(t)
    return out[:3]


def tag_bonus(tags, genre):
    """How much the tags move a scene's score for this genre: the sum of their weights, clamped."""
    w = GENRE_TAG_WEIGHTS.get((genre or '').lower(), _DEFAULT_WEIGHTS)
    return max(-TAG_CLAMP, min(TAG_CLAMP, sum(w.get(t, 0.0) for t in tags or [])))


# ---- frames ----
def sample_fractions(duration, n=None):
    """Where in a scene (0..1) frames are taken: the middle for a short one, spread out for a longer."""
    n = FRAMES_PER_SCENE if n is None else n
    if n <= 1 or duration < MULTI_FRAME_MIN:
        return [0.5]
    if n == 2:
        return [0.33, 0.67]
    return [0.25, 0.5, 0.75][:n] if n <= 3 else [(i + 1) / (n + 1) for i in range(n)]


def motion_amount(grays):
    """0..1: how much the picture changes between the sampled frames (mean absolute difference of
    small grayscale copies). 0 for a single frame."""
    if len(grays) < 2:
        return 0.0
    d = [float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32)))) / 255.0
         for a, b in zip(grays, grays[1:])]
    return float(np.mean(d))


def color_hist(frame_bgr):
    """Normalised 8x4x4 HSV histogram, flattened -- what 'looks the same' is judged on."""
    import cv2
    hsv = cv2.cvtColor(cv2.resize(frame_bgr, (96, 54)), cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).flatten().astype(np.float32)
    s = float(h.sum())
    return h / s if s else h


def hist_similarity(a, b):
    """Correlation of two histograms, -1..1 (1 = same look)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den else 0.0


# ---- sound ----
def scene_audio_stats(db, start, end, hop=ENVELOPE_STEP, speech_db=None):
    """Loudness facts about one scene from the source's level readings (dB, one per `hop` s):
    peak level and when, mean level, and whether the first words come after a held beat of quiet."""
    if db is None or len(db) == 0 or end <= start:
        return None
    i0 = max(0, int(start / hop))
    i1 = min(len(db), max(i0 + 1, int(np.ceil(end / hop))))
    seg = np.asarray(db[i0:i1], dtype=np.float64)
    if not len(seg):
        return None
    k = int(np.argmax(seg))
    stats = {'peak_db': float(seg[k]), 'peak_t': (i0 + k) * hop, 'mean_db': float(np.mean(seg)), 'pre_pause': False}
    if speech_db is not None:
        loud = np.nonzero(seg > speech_db)[0]
        if len(loud):
            quiet_before = loud[0] * hop
            stats['pre_pause'] = bool(quiet_before >= PRE_PAUSE and (end - start) - quiet_before >= 0.3)
    return stats


def energy_scores(scenes):
    """Sets s['energy_score'] (0..2) on every scene: a loud peak and a loud scene overall (against the
    rest of the episode), a held beat before the words, and movement. Scenes without sound facts are
    judged on movement alone."""
    peaks = [s['audio']['peak_db'] for s in scenes if s.get('audio')]
    means = [s['audio']['mean_db'] for s in scenes if s.get('audio')]
    motion = [s.get('motion', 0.0) for s in scenes if s.get('motion', 0.0) > 0]
    p_hi = float(np.percentile(peaks, 85)) if peaks else None
    p_mid = float(np.percentile(peaks, 60)) if peaks else None
    m_hi = float(np.percentile(means, 75)) if means else None
    mo_hi = float(np.percentile(motion, 75)) if motion else None
    for s in scenes:
        e = 0.0
        a = s.get('audio')
        if a and p_hi is not None:
            if a['peak_db'] >= p_hi:
                e += 1.0
            if a['mean_db'] >= m_hi:
                e += 0.5
            if a.get('pre_pause') and a['peak_db'] >= p_mid:
                e += 0.5
        if mo_hi is not None and s.get('motion', 0.0) >= mo_hi:
            e += 0.5
        s['energy_score'] = round(min(2.0, e), 2)
    return scenes


def pick_window(scene, seg_dur, lead=0.6):
    """Where to start a clip of `seg_dur` seconds inside this scene so it takes in the peak: the loudest
    moment, else the most movement, a little way in. The scene's own start when it has no better idea or
    the clip is (nearly) the whole scene."""
    start = float(scene['start'])
    end = start + float(scene['duration'])
    if not SMART_WINDOW or end - start - seg_dur < 0.3:
        return start
    peak = (scene.get('audio') or {}).get('peak_t')
    if peak is None:
        peak = scene.get('motion_t')
    if peak is None:
        return start
    t = peak - seg_dur * lead
    return round(max(start, min(t, end - seg_dur)), 3)


# ---- near-duplicate shots ----
def dedupe_penalties(scenes, sim=None, penalty=None):
    """Takes `penalty` off the score of a scene that looks like a better-scored scene from the same material
    (so it stays selectable, but only when nothing different is left). Returns the number penalised."""
    sim = DEDUPE_SIM if sim is None else sim
    penalty = DEDUPE_PENALTY if penalty is None else penalty
    if sim <= 0 or penalty <= 0:
        return 0
    kept, n = [], 0
    for s in sorted(scenes, key=lambda x: x.get('total_score', 0), reverse=True):
        h = s.get('hist')
        if h is None:
            continue
        if any(k.get('material') == s.get('material') and hist_similarity(h, k['hist']) >= sim for k in kept):
            s['total_score'] = s.get('total_score', 0) - penalty
            s['dup'] = True
            n += 1
        else:
            kept.append(s)
    return n


# ---- ranking pass ----
def build_rank_prompt(n, genre=None, priority=None, negative=None):
    p = (f'These {n} images are candidate shots for a {genre or "drama"} promo, numbered 1 to {n} in order. '
         'Rank them from the most striking, story-carrying shot to the weakest. Prefer clear emotion, conflict '
         'or a reveal; avoid empty or transitional shots. Return the numbers in ranked order.')
    if priority:
        p += f' The promo should especially feature: {priority}.'
    if negative:
        p += f' It should NOT feature: {negative}.'
    return p


def parse_order(txt, n):
    """The 0-based ranking from a reply like {"order": [3, 1, 2]}; None unless it names every image once."""
    if not txt:
        return None
    nums = None
    try:
        nums = json.loads(txt).get('order')
    except (ValueError, AttributeError):
        m = re.search(r'\[([\d,\s]+)\]', txt)
        if m:
            nums = [int(x) for x in re.findall(r'\d+', m.group(1))]
    if not isinstance(nums, list):
        return None
    try:
        order = [int(x) - 1 for x in nums]
    except (TypeError, ValueError):
        return None
    return order if sorted(order) == list(range(n)) else None


def rank_bonus(position, n, top=RANK_MAX_BONUS):
    """Score added for coming `position`th (0 = best) of n in the ranking pass: `top` for first, none for last."""
    return round(top * (1 - position / (n - 1)), 2) if n > 1 else 0.0
