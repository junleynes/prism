"""
Pure helpers for clean cuts, card fitting and script-aware narration.

Nothing here touches ffmpeg, Flask or the network, so every rule can be tested
with plain numbers. pipeline.py does the measuring (Whisper words, silences,
card durations) and the encoding; this module decides where to cut.

Three jobs:

1. Cuts that never land inside a word.
   settle_in / settle_out move a clip's in and out so the speech on either side
   keeps room for the transition, prefer the start of a sentence for the in
   point, and classify_cut reports how clean each edge ended up.

2. Cards that fit the plug length.
   card_floor says how short a title or end card can safely get (its own voice
   and its animation must finish), plan_card_fit shrinks cards before scenes
   are chosen when they would crowd the scenes out, and card_adjust_limit scales
   the last-resort adjustment with the plug length.

3. Narration cut at the script's own points.
   parse_vo_script reads timed lines ("00:03 VO: ..."), align_script finds each
   line in the Whisper words of the recorded VO, and plan_vo cuts the VO at
   word edges, places every line at its in point, and when the result does not
   fit tightens pauses, speeds up slightly, drops optional lines and as a last
   step trims at the end of a word. Never mid-word.
"""
import bisect
import difflib
import re
import unicodedata

# ---------------------------------------------------------------- cuts

LEAD_MAX = 0.25         # room kept before a word that opens a clip
TRAIL_MAX = 0.25        # room kept after a word that closes a clip
EDGE_TOL = 0.02         # a cut this close to a word edge counts as on the edge


def _pairs(starts, ends):
    """(start, end) word pairs, sorted, dropping malformed rows."""
    out = []
    for s, e in zip(starts or [], ends or []):
        try:
            s, e = float(s), float(e)
        except (TypeError, ValueError):
            continue
        if e >= s:
            out.append((s, e))
    out.sort()
    return out


def refine_words(words, quiet, tol=0.15):
    """Pull Whisper's word edges onto measured silence edges.

    Whisper times are usually right to within 50-150 ms, no better. A silence
    detector measures where the sound actually stops and starts, so when a
    silence edge is within `tol` of a word edge the measured edge wins:
      * a word that ends near the START of a silence ends where that silence starts
      * a word that starts near the END of a silence starts where that silence ends
    Order and length stay sane (a word never ends before it starts)."""
    words = [list(w) for w in (words or [])]
    quiet = sorted((float(a), float(b)) for a, b in (quiet or []) if b > a)
    if not words or not quiet:
        return [tuple(w) for w in words]
    q_starts = [q[0] for q in quiet]
    q_ends = [q[1] for q in quiet]
    for w in words:
        s, e = w
        i = bisect.bisect_left(q_starts, e - tol)
        if i < len(quiet) and abs(q_starts[i] - e) <= tol and q_starts[i] > s + 0.05:
            w[1] = q_starts[i]
        j = bisect.bisect_left(q_ends, s - tol)
        if j < len(quiet) and abs(q_ends[j] - s) <= tol and q_ends[j] < w[1] - 0.05:
            w[0] = q_ends[j]
    return [tuple(w) for w in words]


def _word_at(t, words, tol=EDGE_TOL):
    """Index of the word that t falls inside, else None."""
    for i, (s, e) in enumerate(words):
        if s + tol < t < e - tol:
            return i
        if s > t:
            break
    return None


def _room(gap, limit, floor=0.03):
    """How much of a gap between two words to keep as room: most of it, never
    more than `limit`, never so little the cut is flush to a word."""
    if gap is None:
        return limit
    return min(limit, max(floor, gap * 0.6))


BREATH_MIN = 0.08       # a gap shorter than this is not a place to cut
BREATH_OK = 0.12        # a gap at least this long is
BREATH_WIN = 1.0        # how far to look for one


def settle_in(t, words, lo=0.0, hi=None, sentences=(), lead=LEAD_MAX,
              sent_back=0.6, sent_fwd=0.9, breath_win=BREATH_WIN):
    """A clean in point near `t`.

    `words` is [(start, end), ...]; `lo` is the earliest the picture may start
    (the scene's own start); `hi` the latest. `sentences` is [(start, end)] of
    spoken phrases.

    1. Starting mid-sentence is the common "cut in the middle of a thought"
       fault, so if t falls inside a sentence the in point moves to that
       sentence's start (up to `sent_back` earlier) or the next one's (up to
       `sent_fwd` later), whichever is nearer.
    2. If it then falls inside a word, back up to just before the word, leaving
       room for the transition fade; if the scene does not start early enough,
       go forward to the next word instead.
    3. If a word starts right after t with less than the transition's room,
       pull back to leave it.
    4. If the word boundary is only a hairline gap (two words run together),
       look for the nearest real pause within `breath_win` and cut there."""
    if hi is None:
        hi = float('inf')
    words = sorted(words or [])
    t = max(lo, min(t, hi))

    for s, e in sorted(sentences or []):
        if s + 0.05 < t < e - 0.05:
            options = []
            if t - s <= sent_back and s >= lo:
                options.append(s)
            nxt = [s2 for s2, _e2 in sorted(sentences) if s2 > t and s2 - t <= sent_fwd and s2 <= hi]
            if nxt:
                options.append(nxt[0])
            if options:
                t = min(options, key=lambda x: abs(x - t))
            break

    def gap_before(j):
        return None if j == 0 else words[j][0] - words[j - 1][1]

    def cand_for(j):
        prev_end = words[j - 1][1] if j > 0 else None
        c = words[j][0] - _room(gap_before(j), lead)
        return max(c, prev_end + 0.01) if prev_end is not None else c

    k = _word_at(t, words)
    if k is None:
        nxt = next((n for n, (s, _e) in enumerate(words) if s > t), None)
        if nxt is not None and words[nxt][0] - t < _room(gap_before(nxt), lead):
            k = nxt
    if k is None:
        return t

    g = gap_before(k)
    if g is not None and g < BREATH_MIN:
        best = None
        for j in range(len(words)):
            gj = gap_before(j)
            if gj is not None and gj < BREATH_OK:
                continue
            c = cand_for(j)
            d = abs(words[j][0] - t)
            if lo <= c <= hi and d <= breath_win and (best is None or d < best[0]):
                best = (d, c)
        if best:
            return best[1]
    c = cand_for(k)
    if c >= lo:
        return c
    for j in range(k + 1, len(words)):         # the scene starts inside this word
        c = cand_for(j)
        if lo <= c <= hi:
            return c
    return t


def settle_out(t, words, lo=0.0, hi=None, trail=TRAIL_MAX, breath_win=BREATH_WIN):
    """A clean out point near `t`: after the last word that starts before it,
    with room for the transition fade. If that room does not fit inside the
    scene (`hi`), cut before the word instead, in the gap that precedes it.
    If the boundary is only a hairline gap between two words, the nearest real
    pause within `breath_win` is used instead."""
    if hi is None:
        hi = float('inf')
    words = sorted(words or [])
    t = min(max(t, lo), hi)
    idx = None
    for k, (s, _e) in enumerate(words):
        if s < t - 0.005:
            idx = k
        else:
            break
    if idx is None:
        return t

    def gap_after(j):
        return words[j + 1][0] - words[j][1] if j + 1 < len(words) else None

    def cand_for(j):
        c = words[j][1] + _room(gap_after(j), trail)
        nxt = words[j + 1][0] if j + 1 < len(words) else None
        return min(c, nxt - 0.01) if nxt is not None else c

    s, e = words[idx]
    g = gap_after(idx)
    if g is not None and g < BREATH_MIN:
        best = None
        for j in range(len(words)):
            gj = gap_after(j)
            if gj is not None and gj < BREATH_OK:
                continue
            c = cand_for(j)
            d = abs(words[j][1] - t)
            if lo <= c <= hi and d <= breath_win and (best is None or d < best[0]):
                best = (d, c)
        if best:
            return best[1]
    room = _room(g, trail)
    if t >= e + room - 0.005:
        return t                                    # already clear of the word
    cand = cand_for(idx)
    if lo <= cand <= hi:
        return cand
    # No room to finish the word inside the scene: end before it.
    prev_end = words[idx - 1][1] if idx > 0 else None
    back = prev_end + _room(s - prev_end, trail) if prev_end is not None else s - 0.03
    if lo <= back <= hi:
        return back
    return min(max(cand, lo), hi)


def classify_cut(kind, t, words, sentences=(), tight=0.04, near=1.0):
    """How clean is this edge? ('in' or 'out')

    clean    no speech to protect, or the edge sits at a sentence boundary
    word     on a word boundary, but mid-sentence
    tight    on a boundary, but the speech starts/ends within `tight` s of the
             edge, so the transition can bite into it
    clipped  inside a word
    silent   no speech nearby (b-roll)"""
    words = sorted(words or [])
    if not any(abs(s - t) <= near or abs(e - t) <= near or s <= t <= e for s, e in words):
        return 'silent'
    if _word_at(t, words) is not None:
        return 'clipped'
    if kind == 'in':
        nxt = next(((s, e) for s, e in words if s >= t), None)
        gap = (nxt[0] - t) if nxt else None
        if gap is not None and gap < tight:
            return 'tight'
        edge = nxt[0] if nxt else t
        at_sentence = any(abs(s - edge) <= 0.35 for s, _e in (sentences or []))
    else:
        prv = None
        for s, e in words:
            if e <= t + EDGE_TOL:
                prv = (s, e)
        gap = (t - prv[1]) if prv else None
        if gap is not None and gap < tight:
            return 'tight'
        edge = prv[1] if prv else t
        at_sentence = any(abs(e - edge) <= 0.35 for _s, e in (sentences or []))
    if at_sentence:
        return 'clean'
    inside_sentence = any(s + 0.05 < t < e - 0.05 for s, e in (sentences or []))
    if inside_sentence:
        return 'word'
    return 'clean'


CUT_NOTES = {
    'clean': 'clean',
    'word': 'on a word edge, mid-sentence',
    'tight': 'tight to the words',
    'clipped': 'inside a word',
    'silent': 'no speech',
}


def refine_clip(trim_start, dur, bounds, words, sentences=(), xfade=0.3, min_len=0.8):
    """Settle one clip's in and out. Returns (new_start, new_dur, report).

    `bounds` = (scene_start, scene_end): the footage the clip may use. The
    transition's fade is why the room before and after speech scales with
    `xfade` (capped), and the clip is left exactly as it was if settling would
    make it shorter than `min_len`."""
    lo, hi = bounds
    lead = min(LEAD_MAX, max(0.08, float(xfade)))
    end = trim_start + dur
    new_start = settle_in(trim_start, words, lo=lo, hi=max(lo, end - min_len), sentences=sentences, lead=lead)
    new_end = settle_out(end, words, lo=new_start + min_len, hi=hi, trail=lead)
    new_dur = new_end - new_start
    if new_dur < min_len or new_start < lo - 1e-6 or new_end > hi + 1e-6:
        new_start, new_dur = trim_start, dur
    else:
        new_dur = new_end - new_start
    a = classify_cut('in', new_start, words, sentences)
    b = classify_cut('out', new_start + new_dur, words, sentences)
    report = {'in': a, 'out': b, 'moved_in': round(new_start - trim_start, 3),
              'moved_out': round((new_start + new_dur) - end, 3)}
    return new_start, new_dur, report


def cut_summary(reports):
    """Counts for the review header: how many edges are clean, risky, silent."""
    out = {'clean': 0, 'word': 0, 'tight': 0, 'clipped': 0, 'silent': 0}
    for r in reports:
        for k in ('in', 'out'):
            v = (r or {}).get(k)
            if v in out:
                out[v] += 1
    out['edges'] = sum(out[k] for k in ('clean', 'word', 'tight', 'clipped', 'silent'))
    out['risky'] = out['tight'] + out['clipped']
    return out


# ---------------------------------------------------------------- cards

CARD_MIN = 1.0              # a card still has to read as a card
CARD_VOICE_PAD = 0.25       # keep a breath after a card's own voice
CARD_STILL_PAD = 0.4        # keep a beat after the animation settles
TITLE_HOLD = 1.5
TITLE_LEAD = 0.5            # keep this much of the card's build-in before the title when a window is cut from it            # the title stays this long once it is fully on screen


def parse_freeze_start(stderr):
    """First freeze_start from ffmpeg's freezedetect log, else None. The
    picture stops moving there: everything after it is a hold that can be
    shortened without cutting the animation."""
    m = re.search(r'freeze_start:\s*([0-9.]+)', stderr or '')
    return float(m.group(1)) if m else None


def parse_freeze_tail(stderr, duration, tail_tol=0.15):
    """When the picture stops moving for good: the start of the last freeze
    that runs to the end of the card, else None. A freeze in the middle of a
    card that then moves again is not a hold."""
    starts = [float(x) for x in re.findall(r'freeze_start:\s*([0-9.]+)', stderr or '')]
    ends = [float(x) for x in re.findall(r'freeze_end:\s*([0-9.]+)', stderr or '')]
    if not starts:
        return None
    if len(ends) >= len(starts) and ends[-1] < float(duration) - tail_tol:
        return None
    return starts[-1]


def voice_end_from_silences(silences, duration, tail_tol=0.1):
    """Where a card's sound last stops, from its silent intervals. A card whose
    sound runs to the end (music bed) returns its full duration, which makes it
    unshortenable: cutting a bed mid-phrase is worse than a long card."""
    tail = None
    for s, e in sorted(silences or []):
        if e >= float(duration) - tail_tol:
            tail = s
    return float(tail) if tail is not None else float(duration)


def card_floor(duration, voice_end=None, still_from=None, minimum=CARD_MIN, title_at=None):
    """The shortest this card can safely be: past its own voice, past the
    point the picture settles, and long enough after the title is fully on
    screen for it to be read; never under `minimum`, never over its length."""
    floor = minimum
    if voice_end:
        floor = max(floor, voice_end + CARD_VOICE_PAD)
    if still_from is not None:
        floor = max(floor, still_from + CARD_STILL_PAD)
    if title_at is not None:
        floor = max(floor, title_at + TITLE_HOLD)
    return min(floor, duration)


def card_window(duration, target, title_at=None, lead=TITLE_LEAD):
    """Where in a card to cut a `target`-second piece from: the start of the piece, in seconds into the card.
    A tune-in card builds up to its title and holds on it, so the good part is the end, not the start: the piece
    is the card's last `target` seconds, or, when the title shows up earlier than that, it starts just before the
    title so the title is in it."""
    duration, target = float(duration), float(target)
    if target >= duration:
        return 0.0
    tail = duration - target
    if title_at is None:
        return tail
    return max(0.0, min(tail, float(title_at) - lead))


def title_in_text(title, text, min_ratio=0.78):
    """True when every word of `title` is in `text` (what a frame's text reading came to), allowing the
    odd wrong letter an OCR makes. Words of under three letters are ignored."""
    want = [t for t in norm_tokens(title) if len(t) >= 3]
    have = norm_tokens(text)
    if not want or not have:
        return False
    for w in want:
        if not any(difflib.SequenceMatcher(None, w, h).ratio() >= min_ratio for h in have if abs(len(h) - len(w)) <= 3):
            return False
    return True


def raise_floors_for_voice(floors, durations, need, xfade=0.3):
    """The cards play a voice line from the first card on: together they must last `need` seconds (its
    length, plus the lead and the tail kept free). Raises the floors, first card first, never past a
    card's own length. -> (floors, short_by) where short_by > 0 means the cards are too short for the line
    even whole."""
    floors = [min(float(f), float(d)) for f, d in zip(floors, durations)]
    total = lambda xs: sum(xs) - xfade * max(0, len(xs) - 1)          # noqa: E731
    gap = float(need) - total(floors)
    for i in range(len(floors)):
        if gap <= 0.001:
            break
        room = float(durations[i]) - floors[i]
        add = min(room, gap)
        floors[i] += add
        gap -= add
    return floors, max(0.0, round(gap, 3))


def raise_floors_per_card(floors, durations, wants):
    """Each card has its own voice line (the title card's, the end card's), said from where the card starts:
    `wants[i]` seconds (the line, plus the lead and the tail kept free; 0 or None for no line). Raises card i's
    floor to that, never past the card's own length. -> (floors, short_by) where short_by is how much the
    cards fall short of their lines even whole, in total."""
    out, short = [], 0.0
    for f, d, w in zip(floors, durations, wants):
        f = min(float(f), float(d))
        if w and w > f:
            short += max(0.0, float(w) - float(d))
            f = min(float(d), float(w))
        out.append(f)
    return out, round(short, 3)


def card_adjust_limit(length, policy='fit'):
    """How much the cards may stretch or shrink in total, to close the last
    gap. 'off' keeps the old fixed 1.0 s; 'fit' scales with the plug (12 % of
    it, never under 1 s): a 30 s plug can give 3.6 s, a 10 s one 1.2 s."""
    if policy == 'off':
        return 1.0
    return max(1.0, round(0.12 * float(length), 2))


def min_scene_time(length):
    """The least scene time a plug should have. Cards that would push scenes
    under this are shortened first."""
    return max(5.0, 0.45 * float(length))


def plan_card_fit(length, durations, floors, min_scenes=None):
    """Targets for each card before scenes are chosen.

    If cards leave the scenes at least `min_scenes` seconds, nothing changes.
    Otherwise the shortfall is taken from the cards in proportion to how far
    each is above its floor. Returns {'targets': [...], 'scene_budget': x,
    'short_by': y, 'shrunk': bool}; short_by > 0 means even at their floors the
    cards leave the scenes too little."""
    durations = [float(d) for d in durations]
    floors = [min(float(f), d) for f, d in zip(floors, durations)]
    if min_scenes is None:
        min_scenes = min_scene_time(length)
    total = sum(durations)
    budget = float(length) - total
    targets = list(durations)
    need = min_scenes - budget
    shrunk = False
    if need > 0.01 and durations:
        room = [max(0.0, d - f) for d, f in zip(durations, floors)]
        avail = sum(room)
        take = min(need, avail)
        if avail > 0.01:
            targets = [d - take * (r / avail) for d, r in zip(durations, room)]
            shrunk = take > 0.01
        need -= take
    scene_budget = float(length) - sum(targets)
    return {'targets': targets, 'scene_budget': scene_budget,
            'short_by': max(0.0, need), 'shrunk': shrunk, 'min_scenes': min_scenes}


def card_budget_line(length, card_secs, labels=None):
    """'15 s = title 3.0 + scenes 8.4 + end 3.6 - transitions 0.6' style text
    pieces for the Ready? panel (the browser builds its own; this keeps the
    server's wording and the tests in one place)."""
    labels = labels or ['title', 'end card']
    parts = [f'{l} {c:.1f}' for l, c in zip(labels, card_secs)]
    scenes = float(length) - sum(card_secs)
    return f'{length:g} s = ' + ' + '.join(parts) + f' + scenes {scenes:.1f}'


# ---------------------------------------------------------------- script

_TIME_RE = re.compile(
    r'^\s*[\[\(]?\s*(?:@\s*)?'
    r'(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,:](\d{1,3}))?'
    r'\s*(?:s|sec)?\s*[\]\)]?\s*[-–—:]*\s*', re.I)
_AT_RE = re.compile(r'^\s*@\s*(\d+(?:[.,]\d+)?)\s*s?\b\s*[-–—:]*\s*', re.I)
_VO_TAG_RE = re.compile(r'^\s*(?:V\.?O\.?|VOICE\s*OVER|NARRATOR|NARRATION|SOT)\s*[:\-–—]\s*', re.I)
_OPT_RE = re.compile(r'[\[\(]\s*(?:optional|opt)\s*[\]\)]|\bOPTIONAL\s*:', re.I)


def tc_to_seconds(h, m, s, frac):
    """Clock fields to seconds. Two fields are MM:SS; three are HH:MM:SS. The
    fraction is milliseconds if it has three digits, else a decimal."""
    total = (int(h) if h else 0) * 3600 + int(m) * 60 + int(s)
    if frac:
        total += float('0.' + frac)
    return float(total)


def parse_vo_script(text):
    """Read narration text into lines: [{'at': seconds|None, 'text', 'optional'}].

    A leading time marks where the line goes on the plug's timeline:
        00:03 VO: Abangan ngayong gabi
        [0:07.5] ...        @12.5s ...
    Lines without a time follow the previous line. A line marked (optional) is
    the first thing dropped if the narration will not fit. Delivery cues in
    [brackets] are removed."""
    lines = []
    for raw in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        at = None
        m = _AT_RE.match(line)
        if m:
            at = float(m.group(1).replace(',', '.'))
            line = line[m.end():]
        else:
            m = _TIME_RE.match(line)
            if m and (m.group(1) is not None or int(m.group(3)) < 60):
                try:
                    at = tc_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
                    line = line[m.end():]
                except ValueError:
                    at = None
        line = _VO_TAG_RE.sub('', line)
        optional = bool(_OPT_RE.search(line))
        line = _OPT_RE.sub(' ', line)
        line = re.sub(r'\[[^\]]*\]', ' ', line)
        line = re.sub(r'\s+', ' ', line).strip()
        if line:
            lines.append({'at': at, 'text': line, 'optional': optional})
    return lines


def strip_vo_markers(text):
    """The spoken words only: what selection and matching should see."""
    return ' '.join(l['text'] for l in parse_vo_script(text))


def has_vo_times(text):
    return any(l['at'] is not None for l in parse_vo_script(text))


_NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen '
    'eighteen nineteen twenty'.split())}
_NUMBER_WORDS.update({'thirty': '30', 'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'})


def norm_tokens(text):
    """Words of a text for matching a script against a transcript: lower case, no accents, digits split from
    letters ('8:50PM' -> 8 50 pm), number words as digits ('eight fifty' -> 8 50), 'p.m.' as one word."""
    text = unicodedata.normalize('NFKD', str(text or '').lower())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    raw = re.findall(r"[a-z0-9ñ']+", text)
    toks = []
    for t in raw:
        m = re.match(r"^(\d+)(am|pm|st|nd|rd|th)$", t)
        toks.extend(m.groups() if m else [t])
    out = []
    for t in toks:
        t = _NUMBER_WORDS.get(t, t)
        if out and t == 'm' and out[-1] in ('a', 'p'):
            out[-1] += 'm'
        else:
            out.append(t)
    return out


def words_from_segments(segments):
    """Spread each segment's words evenly across its time: coarse stand-ins for
    word times when the speech service gave line times only."""
    words = []
    for seg in segments or []:
        toks = (seg.get('text') or '').split()
        if not toks:
            continue
        s, e = float(seg.get('start') or 0), float(seg.get('end') or 0)
        if e <= s:
            continue
        step = (e - s) / len(toks)
        for k, tok in enumerate(toks):
            words.append({'word': tok, 'start': s + k * step, 'end': s + (k + 1) * step - 0.01})
    return words


def _span_len(flat, at, want_side, direction, floor, times=None, max_gap=0.6):
    """How many transcript words next to a matched block belong to the script words around it. The transcript
    may split or join words the script does not ('TASKFORCE' heard as 'task force'), so the count is the one whose
    letters best match the script words. A word the recording does not clearly contain gets none: the next
    words are then another line's, never taken by position alone. Words after a pause longer than `max_gap`
    are not part of the stretch either."""
    n = len(want_side)
    target = ''.join(want_side)
    reach = 0                                   # how many words can be taken without crossing a long pause
    if times:
        j = at
        while True:
            if direction < 0:
                if j - 1 < floor:
                    break
                gap = times[j][0] - times[j - 1][1] if j < len(times) else 0.0
                j -= 1
            else:
                if j >= len(times):
                    break
                gap = times[j][0] - times[j - 1][1] if j > 0 else 0.0
                j += 1
            if gap > max_gap and abs(j - at) >= 1 and reach == 0 and direction != 0:
                # the very first word beyond the block already sits after a long pause
                break
            reach += 1
            if reach >= n + 2 or (gap > max_gap):
                break
    else:
        reach = n + 2
    best_k, best_r = 0, 0.0
    for k in range(1, min(reach, n + 2) + 1):
        if direction < 0:
            lo = at - k
            if lo < floor:
                continue
            cand = ''.join(flat[lo:at])
        else:
            if at + k > len(flat):
                continue
            cand = ''.join(flat[at:at + k])
        r = difflib.SequenceMatcher(None, cand, target).ratio()
        if r > best_r + 1e-9 or (abs(r - best_r) < 1e-9 and abs(k - n) < abs(best_k - n)):
            best_k, best_r = k, r
    return best_k if best_r >= 0.6 else 0


def _best_window(flat, want, lo, hi, taken):
    """The stretch of `flat` (transcript tokens) in [lo, hi) that holds the script line best: -> (score, start,
    blocks) or None. A recording often holds several takes of a line, or the whole spot's narration; the
    line is one contiguous stretch of speech, so only stretches of len(want)+4 tokens are compared, never
    pieces picked from different places."""
    n = len(want)
    wset = set(want)
    best = None
    for i in range(lo, hi):
        if flat[i] not in wset or any(t0 <= i < t1 for t0, t1 in taken):
            continue
        sm = difflib.SequenceMatcher(None, flat[i:i + n + 4], want, autojunk=False)
        blocks = [bl for bl in sm.get_matching_blocks() if bl.size]
        if not blocks:
            continue
        score = sum(bl.size for bl in blocks) / n
        spread = blocks[-1].a + blocks[-1].size - blocks[0].a
        key = (round(score, 6), -spread)
        if best is None or key > best[0]:
            best = (key, i, blocks)
    return None if best is None else (best[0][0], best[1], best[2])


def align_script(lines, words, min_score=0.4):
    """Find each script line in the VO's words.

    Returns one entry per line: {'w0', 'w1', 'score'} (word indexes, inclusive)
    or {'w0': None, ...} when the line was not found. Matching is by shared
    words in sequence inside one stretch of speech, so a small mishearing does
    not lose the line and takes of other lines are not mixed in. Lines are looked for after the
    one before them first (the usual order), then anywhere in the recording."""
    toks = [norm_tokens(w.get('word', '')) for w in words]
    flat, owner = [], []
    for i, tk in enumerate(toks):
        for t in tk:
            flat.append(t)
            owner.append(i)
    times = [(float(words[o].get('start', 0.0)), float(words[o].get('end', 0.0))) for o in owner]
    out, taken = [], []
    cursor = 0
    for ln in lines:
        want = norm_tokens(ln['text'])
        if not want or not flat:
            out.append({'w0': None, 'w1': None, 'score': 0.0})
            continue
        found = _best_window(flat, want, cursor, len(flat), taken) if cursor < len(flat) else None
        in_order = True
        if (found is None or found[0] < 0.7) and cursor > 0:
            other = _best_window(flat, want, 0, cursor, taken)
            if other is not None and (found is None or other[0] > found[0] + 0.15):
                found, in_order = other, False
        if found is None or found[0] < min_score:
            out.append({'w0': None, 'w1': None, 'score': round(found[0], 2) if found else 0.0})
            continue
        score, i0, blocks = found
        first, last = blocks[0], blocks[-1]
        lead_n = first.b                                      # script words before the first match
        tail_n = len(want) - (last.b + last.size)             # ... and after the last
        a0 = i0 + first.a
        k0 = _span_len(flat, a0, want[:lead_n], -1, 0, times) if lead_n else 0
        f0 = max(0, a0 - k0)
        a1 = i0 + last.a + last.size
        k1 = _span_len(flat, a1, want[len(want) - tail_n:], 1, 0, times) if tail_n else 0
        f1 = min(len(flat) - 1, max(a1 - 1, a1 + k1 - 1))
        for t0, t1 in taken:                                  # never into the words another line holds
            if f0 < t1 and f1 >= t0:
                if t0 > f0:
                    f1 = min(f1, t0 - 1)
                else:
                    f0 = max(f0, t1)
        f1 = max(f0, f1)
        taken.append((f0, f1 + 1))
        out.append({'w0': owner[f0], 'w1': owner[max(f0, f1)], 'score': round(score, 2)})
        if in_order:
            cursor = max(cursor, f1 + 1)
    return out


# ---------------------------------------------------------------- narration

PAUSE_KEEP = 0.15       # shortest pause kept when tightening
INLINE_PAUSE_MAX = 0.35 # longest pause kept inside a line: a recorded dramatic pause ("TASK ... FORCE") is not placed as such
SPLIT_GAP = 0.6         # a pause this long inside a line can be tightened
LINE_GAP = 0.25         # default pause between lines with no time of their own
MAX_TEMPO = 1.08        # the most the narration is sped up (pitch is kept)
VO_FADE = 0.03          # fade on every cut edge
VO_TAIL = 0.15          # keep this much of the plug's end free of narration


def _tidy_words(words):
    out = []
    for w in words or []:
        try:
            s, e = float(w['start']), float(w['end'])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({'word': w.get('word', ''), 'start': s, 'end': max(e, s)})
    out.sort(key=lambda x: x['start'])
    return out


def spoken_length(words, w0, w1, pause_max=None):
    """Seconds the words w0..w1 take once placed on the plug: the words themselves, with pauses between them
    kept no longer than INLINE_PAUSE_MAX."""
    pause_max = INLINE_PAUSE_MAX if pause_max is None else pause_max
    total = 0.0
    for i in range(w0, w1 + 1):
        total += max(0.0, words[i]['end'] - words[i]['start'])
        if i < w1:
            total += min(pause_max, max(0.0, words[i + 1]['start'] - words[i]['end']))
    return total


def _line_elements(words, w0, w1, lo, hi):
    """Cut ranges for one line: [{'a','b','gap_before'}], split at long pauses.
    The line gets gap-aware room before its first word and after its last, and
    nothing reaches past `lo`/`hi` (the neighbours' speech)."""
    pairs = [(w['start'], w['end']) for w in words]
    first, last = pairs[w0], pairs[w1]
    gap_prev = (first[0] - pairs[w0 - 1][1]) if w0 > 0 else None
    gap_next = (pairs[w1 + 1][0] - last[1]) if w1 + 1 < len(pairs) else None
    a = max(lo, first[0] - _room(gap_prev, 0.12))
    b = min(hi, last[1] + _room(gap_next, 0.15))
    a = min(a, first[0])
    b = max(b, last[1])
    cuts = []
    for i in range(w0, w1):
        if pairs[i + 1][0] - pairs[i][1] >= SPLIT_GAP:
            cuts.append((pairs[i][1] + 0.1, pairs[i + 1][0] - 0.1))
    elems, seg_a = [], a
    for end_i, start_next in cuts:
        elems.append({'a': seg_a, 'b': end_i})
        seg_a = start_next
    elems.append({'a': seg_a, 'b': b})
    prev_b = None
    for el in elems:
        el['gap_before'] = None if prev_b is None else max(0.0, el['a'] - prev_b)
        prev_b = el['b']
    return elems


LEAD_AUTO_MAX = 1.5     # the most a line is brought forward, under the picture before it, to fit (a J-cut)


def plan_vo(lines, words, *, start_at=0.0, window_end=None, max_tempo=MAX_TEMPO, aligned=None, lead_max=0.0):
    """Cut the recorded narration at word edges and place it.

    lines    parse_vo_script() output (with no script, one line per spoken
             sentence, only the first with a time).
    words    Whisper words of the recording [{'word','start','end'}], times
             relative to the file (any trim already applied).
    start_at where the first line starts when it has no time of its own.
    window_end  the narration must end by this plug time (None = no limit).

    Returns {'pieces', 'notes', 'fits', 'end', 'lines'}. A piece is one cut of
    the recording: {'a','b','at','tempo','fade_in','fade_out','line'}.

    When the narration is too long for the room it has, in order: pauses (and
    the silence around the words) are tightened to 0.15 s; then, if `lead_max`
    allows, the line is brought forward under the picture before it (a J-cut)
    by as much as it is short, never more than `lead_max` seconds, never
    before the line ahead of it has finished, never before 0; then the speech
    is sped up by at most 8 %, lines marked optional are dropped from the end,
    and as the last step the end is trimmed at a word edge. The cut never
    falls inside a word. A narration that fits is placed as it is: no
    tightening, no lead. (A line that runs past its clip simply carries on
    over the next picture, an L-cut, up to the next line's start.)"""
    words = _tidy_words(words)
    notes = []
    if not words or not lines:
        return {'pieces': [], 'notes': ['No speech found in the recording.'],
                'fits': True, 'end': start_at, 'lines': []}
    aligned = aligned or align_script(lines, words)

    bounds = []
    for k, al in enumerate(aligned):
        if al['w0'] is None:
            bounds.append(None)
            continue
        # neighbours by where they sit in the recording (a line may be recorded out of script order)
        prev_hi = max((o['w1'] for j, o in enumerate(aligned) if j != k and o['w1'] is not None and o['w1'] < al['w0']), default=None)
        next_lo = min((o['w0'] for j, o in enumerate(aligned) if j != k and o['w0'] is not None and o['w0'] > al['w1']), default=None)
        lo = words[prev_hi]['end'] + 0.01 if prev_hi is not None else max(0.0, words[al['w0']]['start'] - 0.3)
        hi = words[next_lo]['start'] - 0.01 if next_lo is not None else words[al['w1']]['end'] + 0.3
        bounds.append(_line_elements(words, al['w0'], al['w1'], lo, hi))

    info, runs = [], []
    for k, ln in enumerate(lines):
        row = {'text': ln['text'], 'status': 'ok', 'at': None, 'end': None,
               'score': aligned[k]['score'], 'optional': ln['optional']}
        if aligned[k]['w0'] is not None:
            ws = words[aligned[k]['w0']:aligned[k]['w1'] + 1]
            row['heard'] = ' '.join(f"{w['word'].strip()}@{w['start']:.1f}" for w in ws[:14])
            nxt = min((o['w0'] for j, o in enumerate(aligned) if j != k and o['w0'] is not None and o['w0'] > aligned[k]['w1']), default=None)
            gap = words[aligned[k]['w1'] + 1:nxt] if nxt is not None else []
            if gap:
                row['strays'] = ' '.join(f"{w['word'].strip()}@{w['start']:.1f}" for w in gap[:8])
                notes.append(f'Words after line {k + 1} that belong to no script line: {row["strays"]}.')
        info.append(row)
        if bounds[k] is None:
            row['status'] = 'missing'
            notes.append(f'Line {k + 1} was not found in the recording: "{ln["text"][:50]}".')
            continue
        explicit = ln['at'] is not None
        if explicit or not runs:
            runs.append({'at': ln['at'] if explicit else float(start_at), 'items': []})
        elif runs[-1]['items']:
            # Keep the pause the speaker actually left between these lines.
            prev_last = runs[-1]['items'][-1][1][-1]
            bounds[k][0]['gap_before'] = min(1.5, max(PAUSE_KEEP, bounds[k][0]['a'] - prev_last['b']))
        runs[-1]['items'].append((k, bounds[k]))

    pieces, prev_end = [], None
    for ri, run in enumerate(runs):
        deadline = window_end
        if ri + 1 < len(runs):
            nxt = runs[ri + 1]['at'] - 0.1
            deadline = nxt if deadline is None else min(deadline, nxt)
        floor = 0.0 if prev_end is None else prev_end + 0.1
        got = _fit_run(run, deadline, lines, info, max_tempo, notes, words, lead_max=lead_max, lead_floor=floor)
        pieces.extend(got)
        if got:
            prev_end = max(p['at'] + (p['b'] - p['a']) / p['tempo'] for p in got)

    end = max((p['at'] + (p['b'] - p['a']) / p['tempo'] for p in pieces), default=start_at)
    fits = window_end is None or end <= window_end + 0.02
    return {'pieces': pieces, 'notes': notes, 'fits': fits, 'end': round(end, 3), 'lines': info}


def _fit_run(run, deadline, lines, info, max_tempo, notes, words, lead_max=0.0, lead_floor=0.0):
    """Lay out one run from its anchor and fit it before `deadline`.
    Returns that run's pieces."""
    anchor = run['at']
    cur = anchor                       # where the run starts: the anchor, or earlier when it is led (J-cut)
    lead_by = 0.0
    items = [(k, [dict(e) for e in els]) for k, els in run['items']]

    def flat(items_):
        seq = []
        for k, els in items_:
            for n, e in enumerate(els):
                natural = e['gap_before'] if e['gap_before'] is not None else (LINE_GAP if n == 0 else PAUSE_KEEP)
                if n > 0:
                    natural = min(natural, INLINE_PAUSE_MAX)
                seq.append({'line': k, 'a': e['a'], 'b': e['b'], 'first': n == 0,
                            'natural': max(PAUSE_KEEP, natural)})
        return seq

    def layout(seq, squeeze, tempo):
        """squeeze 0 keeps the natural pauses, 1 tightens them to PAUSE_KEEP."""
        t, out = cur, []
        for idx, e in enumerate(seq):
            if idx > 0:
                t += PAUSE_KEEP + (e['natural'] - PAUSE_KEEP) * (1.0 - squeeze)
            dur = (e['b'] - e['a']) / tempo
            out.append({'line': e['line'], 'a': e['a'], 'b': e['b'], 'at': t, 'tempo': tempo, 'dur': dur})
            t += dur
        return out, t

    seq = flat(items)
    laid, end = layout(seq, 0.0, 1.0)
    how = None
    if deadline is not None and end > deadline + 0.02:
        laid, end = layout(seq, 1.0, 1.0)
        how = 'tightened'
        if end > deadline + 0.02 and lead_max > 0.01:
            # Silence is out and it still does not fit: bring it forward, under the picture before it.
            shift = min(float(lead_max), max(0.0, anchor - float(lead_floor)), end - deadline)
            if shift > 0.02:
                cur, lead_by = anchor - shift, shift
                laid, end = layout(seq, 1.0, 1.0)
                notes.append(f'Narration line {items[0][0] + 1} starts {shift:.1f}s early, under the picture before it '
                             f'(a J-cut), because it did not fit.')
        if end > deadline + 0.02:
            need = (end - cur) / max(deadline - cur, 0.01)
            laid, end = layout(seq, 1.0, min(max_tempo, need))
            how = 'sped up'
        while end > deadline + 0.02:
            opt = [k for k, _els in items if lines[k]['optional']]
            if not opt:
                break
            drop = opt[-1]
            items = [(k, els) for k, els in items if k != drop]
            info[drop]['status'] = 'dropped'
            notes.append(f'Dropped optional line {drop + 1} to fit: "{lines[drop]["text"][:50]}".')
            seq = flat(items)
            if not seq:
                laid, end = [], anchor
                break
            laid, end = layout(seq, 1.0, max_tempo)
            how = 'sped up'
        if seq and end > deadline + 0.02:
            laid, end = layout(seq, 1.0, max_tempo)
            laid, end, cut_line = _trim_to(laid, deadline, words)
            how = 'trimmed'
            if cut_line is not None:
                notes.append(f'Narration was cut at a word edge to end by {deadline:.1f}s.')
        if how == 'tightened':
            notes.append('Pauses were tightened to fit the narration.')
        elif how == 'sped up':
            notes.append(f'Narration sped up slightly (at most {int(round((max_tempo - 1) * 100))} %) to fit.')

    have = {}
    for p in laid:
        have.setdefault(p['line'], []).append(p)
    for k, _els in items:
        row = info[k]
        if k not in have:
            if row['status'] != 'dropped':
                row['status'] = 'cut off'
            continue
        row['at'] = round(have[k][0]['at'], 2)
        row['end'] = round(have[k][-1]['at'] + have[k][-1]['dur'], 2)
        if lead_by > 0.02 and k == items[0][0]:
            row['lead'] = round(lead_by, 2)
        if how == 'trimmed' and (k == laid[-1]['line']) and row['status'] == 'ok':
            row['status'] = 'trimmed'
        elif how in ('tightened', 'sped up') and row['status'] == 'ok':
            row['status'] = how
    return [{'line': p['line'], 'a': round(p['a'], 3), 'b': round(p['b'], 3), 'at': round(p['at'], 3),
             'tempo': round(p['tempo'], 4), 'fade_in': VO_FADE, 'fade_out': VO_FADE} for p in laid]


def _trim_to(laid, deadline, words):
    """Cut a laid-out run so it ends by `deadline`, on a word edge.
    Returns (kept pieces, end time, line that was cut or None)."""
    kept, cut_line = [], None
    end = laid[0]['at'] if laid else 0.0
    for p in laid:
        p_end = p['at'] + p['dur']
        if p_end <= deadline + 0.02:
            kept.append(p)
            end = p_end
            continue
        cut_line = p['line']
        room = (deadline - p['at']) * p['tempo']         # source seconds that still fit
        if room > 0.1:
            limit = p['a'] + room
            fit = [w for w in words if w['start'] >= p['a'] - 0.01 and w['end'] + 0.05 <= limit]
            if fit:
                nxt = next((w['start'] for w in words if w['start'] > fit[-1]['start']), None)
                b = min(fit[-1]['end'] + 0.08, limit, (nxt - 0.01) if nxt is not None else 1e9)
                if b - p['a'] > 0.1:
                    q = dict(p)
                    q['b'] = b
                    q['dur'] = (b - p['a']) / p['tempo']
                    kept.append(q)
                    end = q['at'] + q['dur']
        break
    return kept, end, cut_line


def vo_filter(pieces, sample_rate=44100):
    """ffmpeg filter_complex that builds the edited narration from input 0:
    each piece trimmed at its word edges, sped up if needed, faded, and laid
    end to end with silence between. Returns (graph, out_label) or (None,
    None) when there is nothing to build."""
    if not pieces:
        return None, None
    pieces = sorted(pieces, key=lambda p: p['at'])
    n = len(pieces)
    parts = []
    if n == 1:
        parts.append('[0:a]acopy[src0]')
    else:
        parts.append('[0:a]asplit=%d' % n + ''.join(f'[src{i}]' for i in range(n)))
    labels = []
    t = 0.0
    seg = 0
    for i, p in enumerate(pieces):
        gap = p['at'] - t
        if gap > 0.002:
            parts.append(f'anullsrc=r={sample_rate}:cl=stereo,atrim=duration={gap:.4f}[gap{seg}]')
            labels.append(f'[gap{seg}]')
            seg += 1
        dur_src = p['b'] - p['a']
        dur = dur_src / p['tempo']
        fi = min(p.get('fade_in', VO_FADE), dur / 3)
        fo = min(p.get('fade_out', VO_FADE), dur / 3)
        chain = [f'atrim=start={p["a"]:.4f}:end={p["b"]:.4f}', 'asetpts=PTS-STARTPTS']
        if abs(p['tempo'] - 1.0) > 0.001:
            chain.append(f'atempo={p["tempo"]:.4f}')
        chain.append(f'aformat=sample_rates={sample_rate}:channel_layouts=stereo')
        chain.append(f'afade=t=in:st=0:d={fi:.4f}')
        chain.append(f'afade=t=out:st={max(0.0, dur - fo):.4f}:d={fo:.4f}')
        parts.append(f'[src{i}]' + ','.join(chain) + f'[pc{i}]')
        labels.append(f'[pc{i}]')
        t = p['at'] + dur
    parts.append(''.join(labels) + f'concat=n={len(labels)}:v=0:a=1[vo]')
    return ';'.join(parts), '[vo]'
