"""Retimes caption files that are zero-based and uncut onto a video that is combined and has cuts.

A broadcast episode often arrives as several SCC files (one per part, each starting at 0:00:00) while the
video is one file with parts of the programme cut out. The captions cannot just be shifted: after a cut every
later caption has moved earlier. So each caption is looked for in what the speech-to-text heard, the few
that are found with confidence become anchors, and everything else is placed from the anchors:

  * Inside one file nothing is cut, so the spacing between captions is right. One anchor fixes where a
    stretch sits in the video; a cut shows up as the offset between two anchors getting smaller.
  * Whisper is not exact in Tagalog, so a caption is matched by how much its letters resemble a stretch of
    the transcript (not word for word), and only strong, long-enough matches are used as anchors.
  * Speaker labels (>> MYLA:) and sound descriptors ([LAUGHS], music notes) are not in the speech, so they are
    taken out of the text used for matching and left untouched in the output (unless asked to remove them).

Everything is in seconds; the SCC writer works in frames at 29.97 fps."""
import bisect
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

import captionfile

FRAME = captionfile._FRAME
TOL = 2.5                  # seconds the offset may wander inside a stretch with no cut (matching is not exact to the word)
MIN_ANCHOR = 0.62          # similarity a caption needs to be an anchor
MIN_CHARS = 10             # shortest spoken text worth anchoring on
CHECK_BELOW = 0.45         # a placed caption that matches this badly is flagged
MAX_GAP_CUES = 80

_DESCRIPTOR = re.compile(r'\[[^\]]*\]|\([^)]*\)|♪+|♫+|[♩♬]+')
_LABEL = re.compile(r"^\s*(?:(?:>>|-)\s*)?[A-ZÑ][A-ZÑa-z0-9 .'\-]{0,24}:\s*")
_ARROWS = re.compile(r'^\s*(?:>>+|-)\s+|\s>>+\s')


def strip_descriptors(text):
    """The text without [SOUNDS], (SOUNDS) and music notes."""
    return ' '.join(_DESCRIPTOR.sub(' ', text).split())


def strip_labels(text):
    """The text without speaker labels (MYLA:) and >> speaker-change marks."""
    out = []
    for piece in re.split(r'(?<=[^\w])(?=>>)', text) if '>>' in text else [text]:
        piece = _LABEL.sub('', piece.strip())
        piece = _ARROWS.sub(' ', piece)
        if piece.strip():
            out.append(piece.strip())
    return ' '.join(' '.join(out).replace('>>', ' ').split())


def clean_text(text, labels=True, descriptors=True):
    t = text
    if descriptors:
        t = strip_descriptors(t)
    if labels:
        t = strip_labels(t)
    return ' '.join(t.split())


def norm(text):
    """Lowercase letters and digits only, accents folded -- what two spellings are compared on."""
    t = unicodedata.normalize('NFKD', text.lower())
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"(?<=[a-z])['\u2019`](?=[a-z])", '', t)        # s'ya, 'yung -> sya: one word, as it is often spelt
    t = re.sub(r'[^a-z0-9 ]+', ' ', t)
    # Filipino spelling swaps o/u and e/i freely (ito/eto, mo/mu, sige/sigi): compare them as one letter.
    t = t.translate(_TAGALOG_FOLD)
    return ' '.join(t.split())


_TAGALOG_FOLD = str.maketrans('oe', 'ui')


def _words(text):
    return norm(text).split()


# ---- the transcript ----

def transcript_words(words, segments):
    """[(norm_word, start, end)]. Word timings when the service gave them, else each segment's words
    spread evenly over its time."""
    out = []
    if words:
        for w in words:
            for piece in _words(w.get('word', '')):
                out.append((piece, float(w['start']), float(w['end'])))
        if out:
            return out
    for sg in segments or []:
        ws = _words(sg.get('text', ''))
        if not ws:
            continue
        a, b = float(sg['start']), float(sg['end'])
        step = (b - a) / len(ws)
        for i, w in enumerate(ws):
            out.append((w, a + i * step, a + (i + 1) * step))
    return out


def _trigrams(word):
    w = f' {word} '
    return {w[i:i + 3] for i in range(len(w) - 2)}


class Transcript:
    def __init__(self, tw):
        self.tw = tw
        self.starts = [x[1] for x in tw]
        self.index = defaultdict(list)
        for i, (w, _, _) in enumerate(tw):
            for g in _trigrams(w):
                self.index[g].append(i)

    def text_between(self, t0, t1):
        i = bisect.bisect_left(self.starts, t0)
        j = bisect.bisect_right(self.starts, t1)
        return ' '.join(x[0] for x in self.tw[i:j])

    def candidates(self, spoken, top=4):
        """[(score, start, end)] best places in the transcript for `spoken` (normalised, space-joined)."""
        n = len(spoken.split())
        if not self.tw or n == 0:
            return []
        hits = [0] * len(self.tw)
        grams = set()
        for w in spoken.split():
            grams |= _trigrams(w)
        for g in grams:
            post = self.index.get(g)
            if not post or len(post) > 600:
                continue
            for p in post:
                hits[p] += 1
        win = n + 2
        pref = [0]
        for h in hits:
            pref.append(pref[-1] + h)
        scored = []
        for s in range(0, max(1, len(self.tw) - win + 1)):
            v = pref[min(len(self.tw), s + win)] - pref[s]
            if v:
                scored.append((v, s))
        scored.sort(reverse=True)
        out, used = [], []
        for v, s in scored[:top * 6]:
            if any(abs(s - u) < max(2, n // 2) for u in used):
                continue
            used.append(s)
            best = (0.0, 0, 0)
            for lo in range(max(0, s - 1), s + 2):
                for ln in range(max(1, n - 1), n + 3):
                    hi = min(len(self.tw), lo + ln)
                    if hi <= lo:
                        continue
                    seg = ' '.join(x[0] for x in self.tw[lo:hi])
                    r = SequenceMatcher(None, spoken, seg).ratio()
                    if r > best[0]:
                        best = (r, lo, hi)
            if best[0] > 0:
                lo, hi = self._refine(spoken.split(), max(0, best[1] - 3), min(len(self.tw), best[2] + 3))
                seg = ' '.join(x[0] for x in self.tw[lo:hi])
                out.append((max(best[0], SequenceMatcher(None, spoken, seg).ratio()),
                            self.tw[lo][1], self.tw[hi - 1][2]))
            if len(out) >= top:
                break
        return sorted(out, reverse=True)

    def _refine(self, cw, lo, hi):
        """Which of the transcript words lo..hi-1 the caption's words line up with (word by word, each pair judged
        by how alike their letters are): trims the extra words the first match took at either end, so the
        caption's start time is that of its own first word."""
        ww = [x[0] for x in self.tw[lo:hi]]
        n, m = len(cw), len(ww)
        if n == 0 or m == 0:
            return lo, hi
        NEG = -1e9
        dp = [[NEG] * (m + 1) for _ in range(n + 1)]
        back = [[None] * (m + 1) for _ in range(n + 1)]
        for j in range(m + 1):
            dp[0][j] = 0.0
        for i in range(1, n + 1):
            for j in range(0, m + 1):
                best, how = NEG, None
                if dp[i - 1][j] > NEG:
                    best, how = dp[i - 1][j] - 0.4, (i - 1, j)
                if j > 0:
                    if dp[i][j - 1] > NEG and dp[i][j - 1] - 0.3 > best:
                        best, how = dp[i][j - 1] - 0.3, (i, j - 1)
                    if dp[i - 1][j - 1] > NEG:
                        v = dp[i - 1][j - 1] + SequenceMatcher(None, cw[i - 1], ww[j - 1]).ratio() - 0.5
                        if v > best:
                            best, how = v, (i - 1, j - 1)
                dp[i][j], back[i][j] = best, how
        j = max(range(m + 1), key=lambda x: dp[n][x])
        i, end, first = n, j, None
        while i > 0 and back[i][j]:
            pi, pj = back[i][j]
            if pi == i - 1 and pj == j - 1:
                first = pj
            i, j = pi, pj
        if first is None:
            return lo, hi
        return lo + first, max(lo + first + 1, lo + end)

    def similarity_near(self, spoken, t0, t1):
        """How well `spoken` matches what was said between t0 and t1 (0..1)."""
        seg = self.text_between(t0 - 1.0, t1 + 1.0)
        if not seg or not spoken:
            return 0.0
        return SequenceMatcher(None, spoken, seg).ratio() if len(seg) <= 2 * len(spoken) + 40 else \
            max((SequenceMatcher(None, spoken, seg[i:i + len(spoken) + 20]).ratio()
                 for i in range(0, max(1, len(seg) - len(spoken)), max(5, len(spoken) // 3))), default=0.0)


# ---- aligning ----

def _flatten(parts, labels=True, descriptors=True):
    cues = []
    for pi, part in enumerate(parts):
        for ci, c in enumerate(part):
            sp = norm(clean_text(c['text'], labels, descriptors))
            cues.append({'part': pi, 'idx': ci, 'cue': c, 'start': c['start'], 'end': c['end'],
                         'spoken': sp, 'drop': False, 'offset': None, 'status': None, 'score': None})
    return cues


def _anchor_candidates(cues, tr):
    anchors = []
    for k, c in enumerate(cues):
        sp = c['spoken']
        if len(sp.replace(' ', '')) < MIN_CHARS or len(sp.split()) < 3:
            continue
        for score, t0, _t1 in tr.candidates(sp):
            if score >= MIN_ANCHOR:
                anchors.append({'k': k, 'score': score, 'd': t0 - c['start'], 't': t0,
                                'w': score * min(len(sp), 80)})
    return anchors


def _chain(cues, anchors):
    """The best run of anchors that is consistent: video time rises, and inside one file the offset only ever
    gets smaller by a cut (it never grows)."""
    anchors.sort(key=lambda a: (a['k'], -a['score']))
    best = [a['w'] for a in anchors]
    prev = [-1] * len(anchors)
    for i, a in enumerate(anchors):
        for j in range(i - 1, -1, -1):
            b = anchors[j]
            if a['k'] - b['k'] > MAX_GAP_CUES:
                break
            if b['k'] >= a['k']:
                continue
            if a['t'] < b['t'] - 0.3:
                continue
            same = cues[a['k']]['part'] == cues[b['k']]['part']
            if same and a['d'] > b['d'] + TOL:
                continue
            gain = best[j] + a['w'] * (0.6 if (same and a['d'] < b['d'] - TOL) else 1.0)
            if gain > best[i]:
                best[i], prev[i] = gain, j
    if not anchors:
        return []
    i = max(range(len(anchors)), key=lambda x: best[x])
    chain = []
    while i != -1:
        chain.append(anchors[i])
        i = prev[i]
    return chain[::-1]


def _drop_lone_outliers(cues, chain):
    """A cut needs more than one weak match to believe in: a single anchor, not very strong, whose offset differs
    from the anchors on both sides is a false match, not a cut."""
    out = list(chain)
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(out):
            p = cues[a['k']]['part']
            same = [x for x in out if cues[x['k']]['part'] == p]
            if len(same) < 2:
                continue
            j = same.index(a)
            prev = same[j - 1] if j > 0 else None
            nxt = same[j + 1] if j + 1 < len(same) else None
            differs = lambda o: o is None or abs(o['d'] - a['d']) > TOL
            small = all(o is None or abs(o['d'] - a['d']) < 2 * TOL for o in (prev, nxt))
            if differs(prev) and differs(nxt) and (a['score'] < 0.85 or small):
                out.pop(i)
                changed = True
                break
    return out


def _score(c, d, tr):
    if not c['spoken']:
        return None
    return tr.similarity_near(c['spoken'], c['start'] + d, c['end'] + d)


def _resolve_gap(cues, lo, hi, d_prev, d_next, tr, a_end0, b_start0):
    """Cues lo..hi (indices into `cues`, same file) lie between an anchor with offset d_prev and one with the
    smaller d_next: a cut sits somewhere in between. Cues before it keep d_prev, cues after it d_next, and the
    ones in the cut are dropped."""
    n = hi - lo + 1
    cut = d_prev - d_next
    left = [0.0] * (n + 1)      # cost of cues lo..lo+i-1 kept on the d_prev side
    right = [0.0] * (n + 1)     # cost of cues lo+i..hi kept on the d_next side
    sp = [_score(cues[lo + i], d_prev, tr) for i in range(n)]
    sn = [_score(cues[lo + i], d_next, tr) for i in range(n)]
    for i in range(n):
        left[i + 1] = left[i] + (0.5 if sp[i] is None else 1 - sp[i])
    for i in range(n - 1, -1, -1):
        right[i] = right[i + 1] + (0.5 if sn[i] is None else 1 - sn[i])
    DROP = 0.55
    EXCESS, EXCESS_SLACK = 0.35, 4.0
    best = None
    for i in range(n + 1):               # cues before i are kept before the cut
        for j in range(i, n + 1):        # cues from j on are kept after it; i..j-1 are dropped
            a_end = cues[lo + i - 1]['end'] if i > 0 else a_end0
            b_start = cues[lo + j]['start'] if j < n else b_start0
            gap = b_start - a_end
            if gap < cut - 1.5:
                continue
            cost = left[i] + right[j] + DROP * (j - i)
            # The cut removes exactly `cut` seconds. Dropped captions spanning far more than that took out
            # captions Whisper simply did not hear (music, a mumble, a name): each second over is a cost.
            cost += EXCESS * max(0.0, gap - cut - EXCESS_SLACK)
            if best is None or cost < best[0]:
                best = (cost, i, j)
    if best is None:
        return n, n          # nothing fits: keep them all on the earlier side
    return best[1], best[2]


def align(parts, words, segments, labels=True, descriptors=True, drop_unsure=False):
    """Places every caption of `parts` (a list of cue lists, each zero-based and in order) on the video the
    transcript came from. -> {'cues': [flat list with offset/status/new start & end], 'cuts': [...], ...}"""
    cues = _flatten(parts, labels, descriptors)
    tr = Transcript(transcript_words(words, segments))
    chain = _chain(cues, _anchor_candidates(cues, tr)) if tr.tw else []
    by_part = defaultdict(list)
    for a in chain:
        by_part[cues[a['k']]['part']].append(a)
    chain = _drop_lone_outliers(cues, chain)
    by_part = defaultdict(list)
    for a in chain:
        by_part[cues[a['k']]['part']].append(a)
    cuts = []
    last_end_video = 0.0
    for pi in range(len(parts)):
        idx = [k for k, c in enumerate(cues) if c['part'] == pi]
        if not idx:
            continue
        ans = by_part.get(pi, [])
        if not ans:
            # No anchor in this file: it is put straight after the one before it, and every caption is flagged.
            d = last_end_video - cues[idx[0]]['start']
            for k in idx:
                cues[k].update(offset=d, status='check', score=None)
            last_end_video = cues[idx[-1]]['end'] + d
            continue
        for a in ans:
            cues[a['k']].update(offset=a['d'], status='matched', score=a['score'])
        # before the first anchor and after the last
        for k in idx:
            if k < ans[0]['k']:
                cues[k].update(offset=ans[0]['d'], status='placed')
            elif k > ans[-1]['k']:
                cues[k].update(offset=ans[-1]['d'], status='placed')
        for a, b in zip(ans, ans[1:]):
            lo, hi = a['k'] + 1, b['k'] - 1
            if abs(a['d'] - b['d']) <= TOL:
                for k in range(lo, hi + 1):
                    f = ((cues[k]['start'] - cues[a['k']]['start']) /
                         max(1e-6, cues[b['k']]['start'] - cues[a['k']]['start']))
                    cues[k].update(offset=a['d'] + (b['d'] - a['d']) * f, status='placed', bracketed=True)
                continue
            cuts.append({'part': pi, 'after_cue': a['k'], 'before_cue': b['k'], 'seconds': round(a['d'] - b['d'], 2)})
            if hi < lo:
                continue
            i, j = _resolve_gap(cues, lo, hi, a['d'], b['d'], tr, cues[a['k']]['end'], cues[b['k']]['start'])
            for t in range(hi - lo + 1):
                k = lo + t
                if t < i:
                    cues[k].update(offset=a['d'], status='placed')
                elif t >= j:
                    cues[k].update(offset=b['d'], status='placed')
                else:
                    cues[k].update(offset=a['d'], status='dropped', drop=True)
        ends = [cues[k]['end'] + cues[k]['offset'] for k in idx if cues[k]['offset'] is not None and not cues[k]['drop']]
        last_end_video = max(ends) if ends else last_end_video
    # judge the placed ones, and flag the doubtful
    for c in cues:
        if c['drop'] or c['offset'] is None:
            continue
        if c['status'] == 'placed' and c['spoken']:
            c['score'] = tr.similarity_near(c['spoken'], c['start'] + c['offset'], c['end'] + c['offset'])
            if c['score'] < CHECK_BELOW and not c.get('bracketed'):
                # Between two matched captions that agree on the offset there is no cut, so the caption is
                # in this stretch whether or not Whisper heard it (music, sound tags, a mumbled line).
                c['status'] = 'check'
        if drop_unsure and c['status'] == 'check' and by_part.get(c['part']):
            # Only inside a file that has anchors: a file with none is all 'check', and dropping it would lose it whole.
            c['drop'] = True
            c['status'] = 'dropped'
            c['unsure'] = True
            continue
        c['new_start'] = max(0.0, c['start'] + c['offset'])
        c['new_end'] = max(c['new_start'] + 0.2, c['end'] + c['offset'])
    # video order, never overlapping the next caption
    kept = [c for c in cues if not c['drop'] and c.get('new_start') is not None]
    kept.sort(key=lambda c: (c['new_start'], c['part'], c['idx']))
    for a, b in zip(kept, kept[1:]):
        if a['new_end'] > b['new_start']:
            a['new_end'] = max(a['new_start'] + 0.2, b['new_start'])
    return {'cues': cues, 'cuts': cuts, 'anchors': len(chain), 'transcript_words': len(tr.tw)}



def _merge_stretches(stretches, tol=0.05):
    """Neighbouring stretches that put the sound at the same offset are one: a hole in the matching (a bar of
    music laid over it, a line the fingerprint did not catch) with no shift across it is not a cut."""
    out = []
    for v0, v1, s0 in stretches:
        if out and abs((s0 - v0) - (out[-1][2] - out[-1][0])) <= tol:
            out[-1] = (out[-1][0], max(v1, out[-1][1]), out[-1][2])
        else:
            out.append((v0, v1, s0))
    return out


def align_audio(parts, stretches, part_offsets, part_durations, labels=True, descriptors=True, video_duration=None):
    """The same result as align(), from where the sound says each stretch of the cut video comes from
    (audioalign.align_files): every caption is moved by the stretch it falls in; a caption that runs into a
    cut keeps the part that is still in the video; one wholly inside a cut is dropped.

    Where the sound was not matched (a hole between two stretches, or before the first / after the last) the
    video there still comes from the source next to it, so a caption is kept, and marked to check, when it
    falls in the stretch of source that fills the hole from either side; only the middle, the length the cut
    really removes, is dropped."""
    cues = _flatten(parts, labels, descriptors)
    shifts = []
    for pi, part in enumerate(parts):
        _c, shift = captionfile.fit_to_video([dict(x) for x in part], part_durations[pi] if pi < len(part_durations) else 0)
        shifts.append(shift or 0.0)
    raw = list(stretches)
    stretches = _merge_stretches(raw)
    # (source start, source end, video start, video end, guessed): the pieces a caption can be found in
    pieces = [(s0, s0 + (v1 - v0), v0, v1, False) for v0, v1, s0 in stretches]
    for (v0, v1, s0), (w0, w1, t0) in zip(stretches, stretches[1:]):
        hv, se = w0 - v1, s0 + (v1 - v0)
        if hv > 0.05 and t0 > se:
            # Where in the hole the cut sits is not known, so each side fills half of it (no two captions on top
            # of each other); what lies between is the cut.
            hv = min(hv, t0 - se) / 2
            pieces.append((se, se + hv, v1, v1 + hv, True))             # filling the hole from the earlier side
            pieces.append((t0 - hv, t0, w0 - hv, w0, True))             # and from the later side
    if stretches:
        v0, v1, s0 = stretches[0]
        if v0 > 0.05:
            pieces.append((max(0.0, s0 - v0), s0, v0 - min(v0, s0), v0, True))      # the video before the first match
        if video_duration and video_duration - stretches[-1][1] > 0.05:
            v0, v1, s0 = stretches[-1]
            tail = video_duration - v1
            pieces.append((s0 + (v1 - v0), s0 + (v1 - v0) + tail, v1, v1 + tail, True))

    def where(src):
        for k, (a0, a1, v0, v1, guess) in enumerate(pieces):
            if a0 - 1e-6 <= src < a1 + 1e-6:
                return k, v0 + (src - a0)
        return None, None

    for c in cues:
        pi = c['part']
        if pi >= len(part_offsets):
            c.update(drop=True, status='dropped')
            continue
        base = part_offsets[pi] - shifts[pi]
        a, b = base + c['start'], base + c['end']
        ka, va = where(a)
        kb, vb = where(b - 0.01)
        if ka is None and kb is None:
            c.update(drop=True, status='dropped')
            continue
        status = 'matched'
        if ka is None:
            va, status = pieces[kb][2], 'placed'             # starts in a cut: the part after it
        if kb is None:
            vb, status = pieces[ka][3], 'placed'             # ends in a cut: the part before it
        elif ka is not None and kb != ka and (pieces[ka][4] or pieces[kb][4] or pieces[kb][2] - pieces[ka][3] > 0.05):
            vb, status = pieces[ka][3], 'placed'
        if (ka is not None and pieces[ka][4]) or (kb is not None and pieces[kb][4]):
            status = 'check'                                 # put where the video's own unmatched stretch says
        if vb - va < 0.3:
            c.update(drop=True, status='dropped')
            continue
        c.update(offset=va - c['start'], status=status, score=1.0 if status != 'check' else None, new_start=va, new_end=vb)
    kept = [c for c in cues if not c['drop'] and c.get('new_start') is not None]
    kept.sort(key=lambda c: (c['new_start'], c['part'], c['idx']))
    for x, y in zip(kept, kept[1:]):
        if x['new_end'] > y['new_start']:
            x['new_end'] = max(x['new_start'] + 0.2, y['new_start'])
    cuts = []
    for (v0, v1, s0), (w0, w1, t0) in zip(stretches, stretches[1:]):
        gone = t0 - (s0 + (v1 - v0))
        if gone > 0.04:
            pi = max(k for k, o in enumerate(part_offsets) if o <= s0 + (v1 - v0) + 1e-6)
            cuts.append({'part': pi, 'at_video': round(v1, 2), 'seconds': round(gone, 2)})
    return {'cues': cues, 'cuts': cuts, 'anchors': len(stretches), 'transcript_words': 0, 'by': 'audio'}


# ---- writing ----

def _srt_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f'{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}'.replace(',1000', ',999')


def write_srt(result, labels=False, descriptors=False):
    """SRT of the retimed captions; `labels` / `descriptors` True removes that kind from the text."""
    rows = []
    for c in sorted((c for c in result['cues'] if not c['drop'] and c.get('new_start') is not None),
                    key=lambda c: (c['new_start'], c['part'], c['idx'])):
        text = c['cue']['text']
        if descriptors:
            text = strip_descriptors(text)
        if labels:
            text = strip_labels(text)
        text = ' '.join(text.split())
        if text:
            rows.append((c['new_start'], c['new_end'], text))
    return '\n'.join(f'{i + 1}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n' for i, (a, b, t) in enumerate(rows))


def _tc_frames(tc):
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2})$', tc.strip())
    if not m:
        return None, None
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    frames = (h * 3600 + mi * 60 + s) * 30 + f
    drop = sep == ';'
    if drop:
        total_min = h * 60 + mi
        frames -= 2 * (total_min - total_min // 10)
    return frames, drop


def _frames_tc(frames, drop):
    frames = max(0, int(frames))
    if drop:
        d, m = divmod(frames, 17982)
        frames += 18 * d + (2 * ((m - 2) // 1798) if m >= 2 else 0)
    f = frames % 30
    s = (frames // 30) % 60
    mi = (frames // 1800) % 60
    h = frames // 108000
    return f'{h:02d}:{mi:02d}:{s:02d}{";" if drop else ":"}{f:02d}'


def write_scc(part_texts, parts, result):
    """One SCC for the whole video: every part's lines with their timecodes moved to where their captions
    landed, the lines of dropped captions removed. The caption codes themselves are not touched, so styling,
    positioning, speaker labels and descriptors stay exactly as they were. `part_texts[i]` is the raw text of
    the i-th file (None for a file that is not SCC, which makes this return None)."""
    if any(t is None for t in part_texts):
        return None
    by_part = defaultdict(dict)
    for c in result['cues']:
        by_part[c['part']][c['idx']] = c
    out = []                      # (video_frames, order, words, line text after the timecode)
    order = 0
    drop_tc = None
    for pi, text in enumerate(part_texts):
        cues = parts[pi]
        owner = {}
        for ci, cue in enumerate(cues):
            for li in cue.get('lines') or []:
                owner.setdefault(li, ci)
            if cue.get('erase') is not None:
                owner[cue['erase']] = ci
        ordered = sorted(range(len(cues)), key=lambda i: cues[i]['start'])
        starts = [cues[i]['start'] for i in ordered]
        lines = text.replace('\r', '\n').split('\n')
        last_offset = None
        for li, raw in enumerate(lines):
            seg = raw.strip().split(None, 1)
            if len(seg) < 2:
                continue
            frames, drop = _tc_frames(seg[0])
            if frames is None:
                continue
            if drop_tc is None:
                drop_tc = drop
            ci = owner.get(li)
            if ci is None:
                # a line no caption owns (set-up codes): moves with the caption that comes next
                nxt = bisect.bisect_left(starts, frames * FRAME)
                ci = ordered[nxt] if nxt < len(ordered) else (ordered[-1] if ordered else None)
            c = by_part[pi].get(ci) if ci is not None else None
            if c is None:
                continue
            if c['drop']:
                continue
            off = c.get('offset')
            if off is None:
                continue
            shift = int(round(off / FRAME))
            out.append((frames + shift, order, len(seg[1].split()), seg[1]))
            order += 1
    if not out:
        return None
    out.sort(key=lambda r: (r[0], r[1]))
    drop = bool(drop_tc)
    lines = ['Scenarist_SCC V1.0', '']
    prev_end = -1
    for fr, _o, nwords, body in out:
        fr = max(fr, prev_end)          # a line cannot start before the one before it has been sent
        lines.append(f'{_frames_tc(fr, drop)}\t{body}')
        lines.append('')
        prev_end = fr + nwords
    return '\n'.join(lines)


def report(result):
    """Rows for the review list: one per caption."""
    rows = []
    for c in result['cues']:
        rows.append({'part': c['part'] + 1, 'text': c['cue']['text'], 'old': round(c['start'], 2),
                     'new': None if c['drop'] or c.get('new_start') is None else round(c['new_start'], 2),
                     'status': c['status'] or 'placed', 'unsure': bool(c.get('unsure')), 'score': None if c['score'] is None else round(c['score'], 2)})
    return rows


def summary(result):
    cs = result['cues']
    count = lambda s: sum(1 for c in cs if c['status'] == s)
    return {'captions': len(cs), 'matched': count('matched'), 'placed': count('placed'),
            'check': count('check'), 'dropped': count('dropped') - sum(1 for c in cs if c.get('unsure')),
            'unsure_dropped': sum(1 for c in cs if c.get('unsure')), 'cuts': result['cuts'],
            'anchors': result['anchors']}
