"""Finding where a script cue's dialogue is in the footage, by listening.

A script cue is a timecode and what is said there ("00:40-45 NINA, ANAK, WHAT'S OUR PASSWORD?"). When
the cue names no material (no M1 / M2), the written time alone cannot say which file it is on, and a
time typed from a different cut of the episode is a little off anyway. So the cue's words are looked
for in what each uploaded material says (Whisper), and the cue is moved to where they are spoken.
A cue with no words to look for, or none found, keeps its written time on the first material.
"""
import json
import re
import cutcraft

MIN_TOKENS = 2          # fewer words than this cannot be told apart from chatter
MIN_SCORE = 0.6         # how much of the cue's words must be heard, in order
LEAD = 0.15             # room kept before the first word
TAIL = 0.35             # room kept after the last word
NEAR = 90.0             # seconds: between equal matches, the one this near the written time wins


def is_complete(c):
    """A cue that says everything: its material, where it starts and where it ends. It is cut exactly as written."""
    return bool(c.get('material')) and c.get('out') is not None and c.get('time') is not None


def locate_cues(cues, transcripts, n_materials=None):
    """`cues` as parse_script_cues made them; `transcripts` {material_number: [word dicts]}.
    Returns (cues, notes): every cue has a 'material'; a cue found by its words has its time (and out) moved
    there and 'located' = True."""
    notes, out = [], []
    n_materials = n_materials or (max(transcripts) if transcripts else 1)
    for c in cues:
        c = dict(c)
        if is_complete(c):
            if is_spoken(c) and (transcripts or {}).get(c['material']):
                c = verify_spoken(c, transcripts[c['material']], notes)
            out.append(c)
            continue
        said = _spoken_words(c)
        want = cutcraft.norm_tokens(said or '')
        best = None
        if len(want) >= MIN_TOKENS:
            for mat, words in transcripts.items():
                if c.get('material') and mat != c['material']:
                    continue                                   # a labelled cue is only looked for on its own material
                if not words:
                    continue
                r = align_best(said, words)
                if r['w0'] is None:
                    continue
                w0, w1 = words[r['w0']], words[r['w1']]
                near = abs(float(w0['start']) - float(c['time']))
                key = (round(r['score'], 1), -min(near, NEAR))
                if best is None or key > best[0]:
                    best = (key, mat, float(w0['start']), float(w1['end']), r['score'])
        if best:
            _k, mat, a, b, score = best
            if not c.get('material') and c.get('out') is not None and n_materials > 1 and inside(a, b, c['time'], c['out']):
                # Written in and out, no material: the speech of every loaded material was searched for these words,
                # and they are said there, at the written time. That identifies the material; the cut stays as written.
                c['material'], c['material_by'] = mat, 'heard'
                notes.append(f'"{c["desc"][:40]}" is said in material {mat} at {a:.1f}s (score {score:.2f}): cut as written.')
                c = snap_edges(c, transcripts.get(mat) or [], notes)
                out.append(c)
                continue
            span = (c['out'] - c['time']) if c.get('out') else 0.0
            c['material'] = mat
            c['written_time'] = c['time']
            c['time'] = round(max(0.0, a - LEAD), 3)
            c['out'] = round(max(b + TAIL, c['time'] + span), 3)
            c['located'] = True
            notes.append(f'"{c["desc"][:40]}" found in material {mat} at {a:.1f}s (written {c["written_time"]:.1f}s).')
            c = snap_edges(c, transcripts.get(mat) or [], notes)
        else:
            if not c.get('material') and not (c.get('out') is not None and (n_materials or 1) > 1):
                c['material'] = 1                              # (written in/out with several materials: looked for next)
            if len(want) >= MIN_TOKENS and transcripts:
                notes.append(f'"{c["desc"][:40]}" was not heard in the footage; kept the written time.')
        out.append(c)
    out.sort(key=lambda c: (c.get('order', 0), c.get('material') or 1, c['time']))
    return out, notes


def align_best(said, words):
    """cutcraft.align_script's answer for the line, trying the whole line and then its beginning (a transcript often loses
    the tail of a line, or hears it differently): the best-scoring result, the whole line winning a tie."""
    toks = str(said or '').split()
    variants = [' '.join(toks)]
    if len(toks) > 6:
        variants.append(' '.join(toks[:max(4, len(toks) // 2 + 1)]))
    best = None
    for v in variants:
        r = cutcraft.align_script([{'text': v}], words, min_score=MIN_SCORE)[0]
        if r['w0'] is not None and (best is None or r['score'] > best['score'] + 1e-9):
            best = r
    return best or cutcraft.align_script([{'text': variants[0]}], words, min_score=MIN_SCORE)[0]


def inside(a, b, t, o, slack=0.4):
    """True when the spoken span a..b lies inside the written cut t..o (give or take `slack`)."""
    return a >= float(t) - slack and b <= float(o) + slack


def snap_to_silence(c, silences, slack=1.5):
    """A spoken cue's edges moved into the pauses of the real sound: an edge that falls in speech moves to the nearest
    pause (the start back, the end forward, within `slack` seconds). `silences` are (start, end) in the footage's time."""
    if c.get('time') is None or c.get('out') is None or not silences:
        return c
    sil = sorted((float(a), float(b)) for a, b in silences if b > a)
    t, o = float(c['time']), float(c['out'])

    def in_pause(x):
        return any(a - 0.01 <= x <= b + 0.01 for a, b in sil)
    if not in_pause(t):
        before = [(a, b) for a, b in sil if b <= t and t - b <= slack]
        if before:
            a, b = before[-1]
            t = max(a, b - 0.06)
    if not in_pause(o):
        after = [(a, b) for a, b in sil if a >= o and a - o <= slack]
        if after:
            a, b = after[0]
            o = a + min(0.25, (b - a) / 2.0)
    if (t, o) == (float(c['time']), float(c['out'])) or o <= t:
        return c
    return dict(c, time=round(t, 3), out=round(o, 3), snapped='sound')


def is_spoken(c):
    """A cue whose sound matters: a spoken line (SOT), alone or joined to a shot."""
    return c.get('kind') == 'sot' or bool(c.get('speech'))


def _spoken_words(c):
    """The words to listen for. The script's own spoken line comes first (it is what the writer meant, and is clean in a
    two-column script); the language model's reading of a mixed line is the fallback."""
    own = c.get('speech') or (c.get('desc') if c.get('kind') == 'sot' else None)
    if own:
        return own
    return (c.get('dialog') or c.get('desc')) if 'dialog' in c else c.get('desc')


def snap_edges(c, words, notes=None):
    """A spoken cue's in and out never fall inside a word: an edge that does moves out to the gap beside that word
    (the word's start less LEAD, its end plus TAIL), never into the next or the previous word."""
    if c.get('time') is None or c.get('out') is None or not words:
        return c
    ws = sorted(words, key=lambda w: float(w['start']))
    t, o = float(c['time']), float(c['out'])
    t0, o0 = t, o
    for i, w in enumerate(ws):
        a, b = float(w['start']), float(w['end'])
        if a + 0.03 < t < b - 0.03:
            prev_end = max((float(x['end']) for x in ws[:i]), default=0.0)
            t = max(a - LEAD, min(prev_end + 0.01, a), 0.0)
            break
    for i, w in enumerate(ws):
        a, b = float(w['start']), float(w['end'])
        if a + 0.03 < o < b - 0.03:
            nxt = min((float(x['start']) for x in ws[i + 1:]), default=b + TAIL)
            o = min(b + TAIL, max(nxt - 0.01, b))
            break
    if (t, o) != (t0, o0) and o > t:
        c = dict(c, time=round(t, 3), out=round(o, 3))
        if notes is not None:
            notes.append(f'"{(c.get("desc") or "")[:40]}" cut moved from {t0:.2f}-{o0:.2f}s to {t:.2f}-{o:.2f}s so no word is cut in half.')
    return c


def verify_spoken(c, words, notes):
    """A spoken cue with material, in and out written. Its words are looked for around the written time: found there, the
    written cut stands; not there but said elsewhere in the same material, the cut moves to where it is said; not
    heard at all, the written cut stands. Either way no word is cut in half."""
    said = _spoken_words(c)
    if len(cutcraft.norm_tokens(said or '')) < MIN_TOKENS or not words:
        return snap_edges(c, words, notes)
    lo, hi = float(c['time']) - WINDOW, float(c['out']) + WINDOW
    near = [w for w in words if lo <= float(w['start']) <= hi]
    if near:
        r = align_best(said, near)
        if r['w0'] is not None:
            a, b = float(near[r['w0']]['start']), float(near[r['w1']]['end'])
            if inside(a, b, c['time'], c['out']):
                return snap_edges(c, words, notes)             # said inside the written cut: as written
    r = align_best(said, words)
    if r['w0'] is None:
        return snap_edges(c, words, notes)
    a, b = float(words[r['w0']]['start']), float(words[r['w1']]['end'])
    span = float(c['out']) - float(c['time'])
    c = dict(c)
    c['written_time'] = c['time']
    c['time'] = round(max(0.0, a - LEAD), 3)
    c['out'] = round(max(b + TAIL, c['time'] + span), 3)
    c['located'] = True
    notes.append(f'"{(c.get("desc") or "")[:40]}" is not said at its written time ({c["written_time"]:.1f}s); '
                 f'it is said in material {c["material"]} at {a:.1f}s: cut there.')
    return snap_edges(c, words, notes)


WINDOW = 4.0            # seconds around a written cut in which its words are listened for


def merge_overlaps(cues):
    """One cue where the script wrote the same moment twice: a shot (left column) and the spoken line (right column) whose
    times overlap are one clip, the span of both. The shot's words describe the picture, the spoken line's are what is said."""
    out = []
    for c in cues:
        hit = None
        for o in out:
            if o.get('out') is None or c.get('out') is None or o.get('time') is None or c.get('time') is None:
                continue
            if o.get('material') and c.get('material') and o['material'] != c['material']:
                continue
            if {o.get('kind'), c.get('kind')} == {'shot', 'sot'} and c['time'] < o['out'] and o['time'] < c['out']:
                hit = o
                break
        if not hit:
            out.append(dict(c))
            continue
        sot, shot = (c, hit) if c.get('kind') == 'sot' else (hit, c)
        hit['speech'] = sot.get('desc')
        hit['desc'] = shot.get('desc') or sot.get('desc')
        hit['kind'] = 'shot'
        hit['time'] = min(hit['time'], c['time'])
        hit['out'] = max(hit['out'], c['out'])
        hit['material'] = hit.get('material') or c.get('material')
        if not hit['material']:
            hit.pop('material', None)
        hit['order'] = min(hit.get('order', 0), c.get('order', 0))
    return out


def fallback_materials(cues):
    """Cues still without a material take the one the cue before them (in script order) has, else material 1."""
    prev = None
    out = []
    for c in sorted(cues, key=lambda x: x.get('order', 0)):
        c = dict(c)
        if not c.get('material'):
            c['material'] = prev or 1
            c['material_by'] = 'previous cue'
        prev = c['material']
        out.append(c)
    return out


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


# ---- the language model reads the script's cue lines first ----
def _stamp(c):
    t = c.get('time')
    if t is None:
        return ''
    s = f'{int(t // 60)}:{t % 60:04.1f}' + (f'-{c["out"] % 60:04.1f}' if c.get('out') is not None else '')
    return f' [{"M%d " % c["material"] if c.get("material") else ""}{s}]'


def analysis_prompt(cues, materials=None, script_text=None):
    lines = '\n'.join(f'{i + 1}. {c.get("desc") or ""}{_stamp(c)}' for i, c in enumerate(cues))
    mats = ''
    if materials:
        mats = 'Source files the editor has loaded:\n' + '\n'.join(f'  {i}. {n}' for i, n in enumerate(materials, 1)) + '\n'
    ctx = f'\nThe whole script, for context:\n{script_text.strip()[:3500]}\n' if script_text and script_text.strip() else ''
    return ('You are a TV promo editor reading a script. Below are its cue lines (timecodes in brackets where written). For each, say:\n'
            '"role": "shot" (a picture to cut to), "sot" (words a person SPEAKS on camera, a sound bite) or "vo" (read by the narrator, not footage);\n'
            '"dialog": the exact spoken words for a sot, else null; "visual": a short description of the picture wanted (shot size, who, '
            'what they do), else null;\n'
            '"material": the number of the source file it comes from, when the line names one (M1, "material 2") or its words clearly '
            'match a file name below, else null.\n'
            'Also give "flow": the cue numbers in the order the story plays, normally the order they are written.\n' + mats +
            'Reply as JSON: {"cues":[{"n":1,"role":"shot","dialog":null,"visual":null,"material":null}, ...],"flow":[1,2,...],'
            '"summary":"one sentence on what the promo is about"}\n' + ctx + '\nCue lines:\n' + lines)


def parse_analysis(reply, cues, n_materials=None):
    """The cues with role / dialog / visual / material filled from the model's reply (written cuts are never changed);
    unchanged when the reply is unusable. Sets c['ai_summary'] on the first cue when the model gave one."""
    try:
        obj = json.loads(reply)
    except (ValueError, TypeError):
        m = re.search(r'\{.*\}', reply or '', re.S)
        try:
            obj = json.loads(m.group(0)) if m else None
        except ValueError:
            obj = None
    rows = obj.get('cues') if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        return cues
    by_n = {}
    for r in rows:
        try:
            by_n[int(r.get('n'))] = r
        except (TypeError, ValueError, AttributeError):
            continue
    out = []
    for i, c in enumerate(cues):
        r = by_n.get(i + 1)
        if not r:
            out.append(c)
            continue
        c = dict(c)
        for k in ('dialog', 'visual'):
            v = r.get(k)
            c[k] = v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() != 'null' else None
        role = str(r.get('role') or '').strip().lower()
        if role in ('shot', 'sot', 'vo'):
            c['role'] = role
        mat = r.get('material')
        if isinstance(mat, str) and mat.strip().lstrip('Mm').isdigit():
            mat = int(mat.strip().lstrip('Mm'))
        if not c.get('material') and isinstance(mat, int) and mat >= 1 and (n_materials is None or mat <= n_materials):
            c['material'] = mat
        out.append(c)
    # the story's order, when the script's own reading order is not known and the model's is a true permutation
    flow = obj.get('flow') if isinstance(obj, dict) else None
    if isinstance(flow, list) and sorted(flow) == list(range(1, len(cues) + 1)) and not any('order' in c for c in out):
        for pos, n in enumerate(flow):
            out[n - 1]['order'] = pos
        out.sort(key=lambda c: c['order'])
    summ = obj.get('summary') if isinstance(obj, dict) else None
    if out and isinstance(summ, str) and summ.strip():
        out[0]['ai_summary'] = summ.strip()[:200]
    return out


def apply_roles(cues):
    """(cues, notes). For cues the script left incomplete, the model's role decides: a narrator line is no footage (dropped),
    a spoken line is searched by its words, a shot by its picture. Complete cues are never touched."""
    keep, notes = [], []
    for c in cues:
        if is_complete(c) or not c.get('role'):
            keep.append(c)
            continue
        if c['role'] == 'vo':
            notes.append(f'"{(c.get("desc") or "")[:40]}" is a narrator line, not footage; left out of the cuts.')
            continue
        c = dict(c)
        c['kind'] = c['role']
        keep.append(c)
    return keep, notes


FILL = 0.6              # written clips that make up this much of the plug already fill it


def drop_sot_when_clips_fill(cues, plug_length):
    """(cues, note). The spoken lines (SOT) are the footage's own sound: they become clips of their own only when the
    written shots (material, in and out) cover less than FILL of the plug. Otherwise they are left out of the cut."""
    sots = [c for c in cues if c.get('kind') == 'sot']
    if not sots:
        return cues, None
    shots = [c for c in cues if c.get('kind') != 'sot']
    span = sum(max(0.0, c['out'] - c['time']) for c in shots if c.get('out') is not None and c.get('time') is not None)
    if span >= FILL * plug_length:
        return shots, (f'The script\'s written clips ({span:.0f}s) fill the {plug_length:g}s plug; its spoken lines (SOT) '
                       'are the sound of those shots, not extra clips.')
    return cues, None
