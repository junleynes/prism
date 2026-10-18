"""Finding where a script cue's dialogue is in the footage, by listening.

A script cue is a timecode and what is said there ("00:40-45 NINA, ANAK, WHAT'S OUR PASSWORD?"). When
the cue names no material (no M1 / M2), the written time alone cannot say which file it is on, and a
time typed from a different cut of the episode is a little off anyway. So the cue's words are looked
for in what each uploaded material says (Whisper), and the cue is moved to where they are spoken.
A cue with no words to look for, or none found, keeps its written time on the first material.
"""
import cutcraft

MIN_TOKENS = 2          # fewer words than this cannot be told apart from chatter
MIN_SCORE = 0.6         # how much of the cue's words must be heard, in order
LEAD = 0.15             # room kept before the first word
TAIL = 0.35             # room kept after the last word
NEAR = 90.0             # seconds: between equal matches, the one this near the written time wins


def locate_cues(cues, transcripts, n_materials=None):
    """`cues` as parse_script_cues made them; `transcripts` {material_number: [word dicts]}.
    Returns (cues, notes): every cue has a 'material'; a cue found by its words has its time (and out) moved
    there and 'located' = True."""
    notes, out = [], []
    n_materials = n_materials or (max(transcripts) if transcripts else 1)
    for c in cues:
        c = dict(c)
        if c.get('material'):
            out.append(c)
            continue
        want = cutcraft.norm_tokens(c.get('desc') or '')
        best = None
        if len(want) >= MIN_TOKENS:
            for mat, words in transcripts.items():
                if not words:
                    continue
                r = cutcraft.align_script([{'text': c['desc']}], words, min_score=MIN_SCORE)[0]
                if r['w0'] is None:
                    continue
                w0, w1 = words[r['w0']], words[r['w1']]
                near = abs(float(w0['start']) - float(c['time']))
                key = (round(r['score'], 1), -min(near, NEAR))
                if best is None or key > best[0]:
                    best = (key, mat, float(w0['start']), float(w1['end']), r['score'])
        if best:
            _k, mat, a, b, score = best
            span = (c['out'] - c['time']) if c.get('out') else 0.0
            c['material'] = mat
            c['written_time'] = c['time']
            c['time'] = round(max(0.0, a - LEAD), 3)
            c['out'] = round(max(b + TAIL, c['time'] + span), 3)
            c['located'] = True
            notes.append(f'"{c["desc"][:40]}" found in material {mat} at {a:.1f}s (written {c["written_time"]:.1f}s).')
        else:
            c['material'] = 1
            if len(want) >= MIN_TOKENS and transcripts:
                notes.append(f'"{c["desc"][:40]}" was not heard in the footage; kept the written time.')
        out.append(c)
    out.sort(key=lambda c: (c.get('material') or 1, c['time']))
    return out, notes


# ---- by looking: cues whose words were not heard ("WIDE REACTS KUHA CP") ----
OFFSETS = (-6.0, -4.5, -3.0, -1.5, 0.0, 1.5, 3.0, 4.5, 6.0)   # seconds around the written time that are looked at
VISION_MIN = 6              # out of 10: the picture must really match the description
VISION_MARGIN = 2           # ... and beat the picture at the written time by this much


def describe_prompt(desc):
    return ('A script cue for a promo says: "' + (desc or '').strip() + '". Does this frame show what the cue describes '
            '(the shot size, who or what is on screen, the reaction)? Rate the match from 0 (not at all) to 10 (exactly).')


def pick_by_look(cue, scores, duration=None):
    """`scores` {time: 0..10} from looking at frames around the cue's written time. The cue moved to the best-matching
    moment when it clearly beats the written time; else unchanged. Keeps the cue's own length."""
    if not scores:
        return cue
    t0 = float(cue['time'])
    here = scores.get(t0, 0)
    t, s = max(scores.items(), key=lambda kv: (kv[1], -abs(kv[0] - t0)))
    if s < VISION_MIN or s < here + VISION_MARGIN or t == t0:
        return cue
    c = dict(cue)
    span = (c['out'] - t0) if c.get('out') else 0.0
    new = max(0.0, t - 0.3)
    if duration:
        new = min(new, max(0.0, duration - max(span, 1.0)))
    c['written_time'] = t0
    c['time'] = round(new, 3)
    if c.get('out'):
        c['out'] = round(new + span, 3)
    c['located'] = 'look'
    return c
