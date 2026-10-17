"""What a promo script says besides its timecodes.

The Episodic Plug already reads the video column of a script (the M1 / M2
timecodes) and cuts those moments. A script also says, in the other column,
what the voice-over has to say and where, how it should sound, which music to
use, and what the tune-in card reads. This module reads that.

Editors mark the voice-over in the script itself: the lines to be recorded are
highlighted (yellow), the delivery note ("VO TONE: MAANGAS") has its own
colour, and a line sits on the same row as the clip it plays over. So the
layout carries the information, and it is read from the layout first. A script
that has no highlighting (a plain text file, a scan) is read by the language
model instead, from the text alone.

Nothing here runs ffmpeg or Flask. pdfplumber is only needed to read a PDF's
layout; without it, or without a recognisable layout, `read_script_brief`
returns None and the script is used exactly as before.

A brief is:
    {'vo':    [{'text', 'anchor': {'kind': 'cue'|'card'|'order', 'material', 'time', 'out', 'label'},
                'how': 'row'|'words'|'order'|'model'}],
     'tone':  'MAANGAS' | None,
     'tcard': {'title', 'lines', 'highlighted'} | None,
     'music': 'FIREWALL_DEVIL DANCE_NAC_ACT-SUS' | None,
     'sot':   [{'material', 'time', 'out', 'lines'}],        # dialogue taken from the footage
     'notes': [..], 'script_id': '...', 'read_by': 'layout' | 'AI model' | 'text'}
"""
import io
import json
import re

# Words that mark a voice-over line as the tune-in line: it plays over the cards.
_TUNE_IN = re.compile(r'\b(?:on\s+gma|gma\s+prime|gtv|weekdays?|weeknights?|simulcast|tonight|bukas)\b|\b\d{1,2}[:.]\d{2}\s*[ap]\.?m\b|\b\d{1,2}\s*[ap]\.?m\b', re.I)
_TONE = re.compile(r'^\W*vo\s*tone\s*[:\-–—]?\s*(.*)$', re.I)
_CUE_TAG = re.compile(r'^\s*(?:m|mat|mats|material|master|reel)\s*(\d{1,2})\b', re.I)
LINE_TOL = 3.0                  # words whose tops are this close (points) are one row
ROW_REACH = 26.0                # a voice-over line belongs to a clip whose row is this close to it (about two lines)
BLOCK_GAP = 1.6                 # lines closer than this many line heights are one block


# ---------------------------------------------------------------- colours

def colour_name(c):
    """'yellow', 'cyan', ... for a fill colour that is a highlight; None for black, white and grey."""
    if c is None:
        return None
    if isinstance(c, (int, float)):
        return None                                    # a grey level
    try:
        vals = [float(v) for v in c]
    except (TypeError, ValueError):
        return None
    if len(vals) == 1:
        return None
    if len(vals) == 4:                                 # CMYK -> RGB
        cc, mm, yy, kk = vals
        vals = [(1 - cc) * (1 - kk), (1 - mm) * (1 - kk), (1 - yy) * (1 - kk)]
    if len(vals) != 3:
        return None
    r, g, b = vals
    hi, lo = max(vals), min(vals)
    if hi < 0.55 or hi - lo < 0.35:                    # dark, or a grey/white/pastel too weak to be a highlighter
        return None
    if r > 0.7 and g > 0.7 and b < 0.45:
        return 'yellow'
    if g > 0.7 and b > 0.7 and r < 0.45:
        return 'cyan'
    if g > 0.7 and r < 0.55 and b < 0.55:
        return 'green'
    if r > 0.7 and b > 0.6 and g < 0.5:
        return 'pink'
    if r > 0.7 and 0.35 < g < 0.8 and b < 0.35:
        return 'orange'
    return 'other'


def mark_words(words, rects):
    """Adds 'hl' (highlight colour name or None) and 'ul' (underlined) to every word."""
    fills = []
    unders = []
    for r in rects or []:
        h = float(r['bottom']) - float(r['top'])
        w = float(r['x1']) - float(r['x0'])
        name = colour_name(r.get('non_stroking_color'))
        if name and h >= 5 and w >= 4:
            fills.append((float(r['x0']), float(r['top']), float(r['x1']), float(r['bottom']), name))
        elif h <= 1.5 and w >= 6 and not name:
            unders.append((float(r['x0']), float(r['x1']), float(r['top'])))
    out = []
    for wd in words:
        wd = dict(wd, hl=None, ul=False)
        area = max(1e-6, (wd['x1'] - wd['x0']) * (wd['bottom'] - wd['top']))
        best = 0.0
        for x0, t, x1, b, name in fills:
            ox = min(wd['x1'], x1) - max(wd['x0'], x0)
            oy = min(wd['bottom'], b) - max(wd['top'], t)
            if ox > 0 and oy > 0 and ox * oy / area > best:
                best = ox * oy / area
                if best >= 0.5:
                    wd['hl'] = name
        for x0, x1, y in unders:
            ox = min(wd['x1'], x1) - max(wd['x0'], x0)
            if ox >= 0.6 * (wd['x1'] - wd['x0']) and abs(y - wd['bottom']) <= 3.0:
                wd['ul'] = True
        out.append(wd)
    return out


# ---------------------------------------------------------------- lines and blocks

def _lines_of(words):
    """Words of one column -> lines [{'text','y0','y1','hl','ul'}], top to bottom."""
    rows = []
    for w in sorted(words, key=lambda w: (w['top'], w['x0'])):
        for row in rows:
            if abs(row[0]['top'] - w['top']) <= LINE_TOL:
                row.append(w)
                break
        else:
            rows.append([w])
    lines = []
    for row in rows:
        row.sort(key=lambda w: w['x0'])
        hl = [w['hl'] for w in row if w['hl']]
        lines.append({'text': ' '.join(w['text'] for w in row), 'y0': min(w['top'] for w in row),
                      'y1': max(w['bottom'] for w in row),
                      'x0': min(w['x0'] for w in row), 'x1': max(w['x1'] for w in row),
                      'hl': max(set(hl), key=hl.count) if len(hl) * 2 >= len(row) else None,
                      'ul': sum(1 for w in row if w['ul']) * 2 >= len(row)})
    lines.sort(key=lambda l: l['y0'])
    return lines


WRAP_REACH = 0.72       # a line that ends short of this share of the column's width was ended by the writer, not by the wrap


def _line_ends_a_line(ln, right_edge, left_edge):
    """True when a highlighted line is the last of its voice-over line: it ends in a full stop, question or
    exclamation mark, or it stops well short of where the column wraps. ('TASKFORCE FIREWALL' over
    '8:50PM ON GMA PRIME' are two lines to record, not one wrapped.)"""
    if re.search(r'[.?!]["\u201d\')]*$', ln['text'].strip()):
        return True
    width = right_edge - left_edge
    return width > 60 and (ln['x1'] - left_edge) < WRAP_REACH * width


def _blocks(lines, same):
    """Group consecutive lines where `same(prev, line)` and the gap is under about a line and a half."""
    out = []
    for ln in lines:
        if out:
            prev = out[-1][-1]
            height = max(6.0, prev['y1'] - prev['y0'])
            if ln['y0'] - prev['y1'] <= BLOCK_GAP * height - height and same(prev, ln):
                out[-1].append(ln)
                continue
        out.append([ln])
    return out


def _clean(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _cue_of(text):
    """{'material','time','out','desc'} for a line that carries a clip timecode, else None."""
    import pipeline as p
    parsed = p._parse_timecode_line(text)
    if not parsed:
        return None
    secs, desc, meta = parsed
    m = _CUE_TAG.match(text)
    return {'material': int(m.group(1)) if m else None, 'time': float(secs),
            'out': float(meta['out']) if meta.get('out') is not None else None, 'desc': desc}


def _label(cue):
    def tc(s):
        s = int(round(s))
        return f'{s // 60}:{s % 60:02d}'
    out = f'M{cue["material"]} ' if cue.get('material') else ''
    return out + tc(cue['time']) + (f'-{tc(cue["out"])}' if cue.get('out') else '')


# ---------------------------------------------------------------- the layout reader

def read_layout(pages):
    """pages: [{'width', 'words': [...], 'rects': [...]}] (pdfplumber's words and rects). -> brief, or None
    when no page has a two-column layout with anything to find in it."""
    import pipeline as p
    brief = {'vo': [], 'tone': None, 'tcard': None, 'music': None, 'sot': [], 'notes': [], 'script_id': None,
             'read_by': 'layout'}
    found = False
    for pg in pages:
        words = mark_words(pg['words'], pg['rects'])
        rows = p._group_words_into_rows(words)
        split = p._detect_two_column_split(rows, pg['width']) if rows else None
        if split is None:
            continue
        left = [w for w in words if w['x0'] < split]
        right = [w for w in words if w['x0'] >= split]
        L, R = _lines_of(left), _lines_of(right)
        found = _read_page(L, R, brief) or found
    if not found:
        return None
    _anchor(brief)
    return brief


def _read_page(L, R, brief):
    """One page's lines into the brief. True when it found anything worth having."""
    got = False
    # ---- the left column: clips, and the tune-in card's text
    cues, other = [], []
    for ln in L:
        cue = _cue_of(ln['text'])
        (cues if cue else other).append((ln, cue))
    body_top = min([ln['y0'] for ln, _ in cues] + [ln['y0'] for ln in R if ln['hl'] or _TONE.match(ln['text'])] + [1e9])
    clip_blocks = []
    for ln, cue in cues:
        clip_blocks.append({'cue': cue, 'y0': ln['y0'], 'y1': ln['y1'], 'lines': [cue['desc']] if cue['desc'] else []})
    rest = [ln for ln, _ in other if ln['y0'] >= body_top - 1]
    for ln in rest:
        # A line right under a clip's line, with nothing between, is that clip's description.
        owner = next((b for b in clip_blocks if 0 <= ln['y0'] - b['y1'] <= 6 and not ln['ul']), None)
        if owner:
            owner['lines'].append(_clean(ln['text']))
            owner['y1'] = ln['y1']
            ln['_owned'] = True
    tail = [ln for ln in rest if not ln.get('_owned')]
    tblocks = _blocks(tail, lambda a, b: True)
    if tblocks:
        last = max(tblocks, key=lambda b: (len(b), b[0]['y0']))        # the biggest block under the clips
        if not brief['tcard'] and (any(l['ul'] for l in last) or len(last) >= 2):
            brief['tcard'] = {'title': _clean(last[0]['text']), 'lines': [_clean(l['text']) for l in last],
                              'highlighted': [_clean(l['text']) for l in last if l['hl']],
                              'y0': last[0]['y0'], 'y1': last[-1]['y1']}
            got = True
    brief.setdefault('_clips', []).extend(clip_blocks)
    if clip_blocks:
        got = True
    # ---- the right column: voice-over, delivery note, music, dialogue taken from the footage
    header = [ln for ln in R if ln['y0'] < body_top - 1]
    body = [ln for ln in R if ln['y0'] >= body_top - 1]
    for i, ln in enumerate(header):
        t = _clean(ln['text'])
        if re.match(r'^[A-Z]{2,}_[A-Z0-9_]+', t) and not brief['script_id']:
            brief['script_id'] = t.split()[0]
        if 'MUSIC' in t.upper() and t.startswith('/'):
            tail_lines = [_clean(x['text']) for x in header[i:i + 4]]
            brief['music'] = _music_from(tail_lines) or brief['music']
        if re.search(r'end\s*card|title\s*card|lift\s+from', t, re.I):
            brief['notes'].append(t)
    right_edge = max([ln['x1'] for ln in R] + [0.0])
    left_edge = min([ln['x0'] for ln in R] + [right_edge])
    i = 0
    while i < len(body):
        ln = body[i]
        t = _clean(ln['text'])
        m = _TONE.match(t)
        if m:
            brief['tone'] = _clean(m.group(1)).strip('() ').upper() or brief['tone']
            got = True
        elif ln['hl'] == 'yellow':
            blk = [ln]
            while i + 1 < len(body) and body[i + 1]['hl'] == 'yellow' and body[i + 1]['y0'] - blk[-1]['y1'] <= 6 \
                    and not _line_ends_a_line(blk[-1], right_edge, left_edge):
                blk.append(body[i + 1])
                i += 1
            text = _clean(' '.join(b['text'] for b in blk))
            brief['vo'].append({'text': text, 'y0': blk[0]['y0'], 'y1': blk[-1]['y1']})
            got = True
        else:
            cue = _cue_of(t)
            if cue and _CUE_TAG.match(t):
                lines = []
                while i + 1 < len(body) and not body[i + 1]['hl'] and not _cue_of(body[i + 1]['text']) \
                        and body[i + 1]['y0'] - body[i]['y1'] <= 6:
                    i += 1
                    lines.append(_clean(body[i]['text']))
                brief['sot'].append(dict(cue, lines=lines, y0=ln['y0']))
                got = True
        i += 1
    return got


def _music_from(lines):
    """The track name out of a wrapped music path: the last line is the file's own name."""
    names = [l for l in lines[1:] if l and not l.startswith('/')]
    if names:
        return re.sub(r'^\W+|\W+$', '', names[-1]) or None
    return None


def _anchor(brief):
    """Say which clip, or the card, each voice-over line plays over."""
    clips = brief.pop('_clips', [])
    tc = brief['tcard']
    for k, v in enumerate(brief['vo']):
        mid = (v['y0'] + v['y1']) / 2.0
        anchor, how = None, None
        if tc and tc['y0'] - 15 <= mid <= tc['y1'] + 15:
            anchor, how = {'kind': 'card', 'label': 'the tune-in card'}, 'row'
        if anchor is None and clips:
            def dist(b):
                return 0.0 if b['y0'] - 4 <= mid <= b['y1'] else min(abs(mid - b['y0']), abs(mid - b['y1']))
            best = min(clips, key=dist)
            if dist(best) <= ROW_REACH:
                c = best['cue']
                anchor = {'kind': 'cue', 'material': c['material'], 'time': c['time'], 'out': c['out'], 'label': _label(c)}
                how = 'row'
        if anchor is None and _TUNE_IN.search(v['text']):
            anchor, how = {'kind': 'card', 'label': 'the tune-in card'}, 'words'
        if anchor is None:
            anchor, how = {'kind': 'order', 'label': f'line {k + 1}'}, 'order'
        v['anchor'], v['how'] = anchor, how
    for v in brief['vo']:
        v.pop('y0', None)
        v.pop('y1', None)
    if tc:
        tc.pop('y0', None)
        tc.pop('y1', None)
    for s in brief['sot']:
        s.pop('y0', None)


# ---------------------------------------------------------------- reading a file

def pdf_pages(raw):
    """pdfplumber's view of each page, or None."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        out = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for pg in pdf.pages:
                out.append({'width': pg.width, 'words': pg.extract_words(keep_blank_chars=False),
                            'rects': pg.rects})
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- the language model

def pages_text(pages):
    """Both columns of a layout, row by row, with ' | ' where the columns part: what a model should read
    (the ordinary extractor keeps only the video column of a two-column script)."""
    rows_out = []
    for pg in pages or []:
        rows = []
        for w in sorted(pg['words'], key=lambda w: (w['top'], w['x0'])):
            for row in rows:
                if abs(row[0]['top'] - w['top']) <= LINE_TOL:
                    row.append(w)
                    break
            else:
                rows.append([w])
        for row in rows:
            row.sort(key=lambda w: w['x0'])
            text = row[0]['text']
            for a, b in zip(row, row[1:]):
                text += (' | ' if b['x0'] - a['x1'] > 30 else ' ') + b['text']
            rows_out.append(text)
    return '\n'.join(rows_out)

def llm_prompt(text, cues=()):
    cue_lines = '\n'.join(f'- {_label(c)}' for c in cues) or '- (none found)'
    return (
        'You read a TV promo editor\'s script and pick out what the voice-over says. The script lists the '
        'shots to use (with source timecodes like "M1 3:33-37"), then the voice-over lines to be recorded, '
        'sometimes the dialogue that comes with the footage itself (sound on tape, not for the voice-over), a '
        'delivery note such as "VO TONE: ...", a music file, and the text of the closing tune-in card '
        '(show title, days, channel, time).\n\n'
        f'Shots named in the script:\n{cue_lines}\n\n'
        'Reply with JSON only, in this shape:\n'
        '{"vo": [{"text": "<the voice-over line exactly as written>", "covers": "<the shot label it plays over, '
        'for example M1 3:33-37, or \\"card\\" for the tune-in line at the end>"}], '
        '"tone": "<delivery note or null>", "title": "<the show title on the card or null>", '
        '"music": "<music file name or null>", "card_lines": ["<each line of the tune-in card>"]}\n'
        'The voice-over is only what the narrator is to say. Dialogue that comes with a shot is not voice-over. '
        'A line that sells the time slot and channel is the tune-in line and covers "card". '
        'Use null instead of guessing.\n\nTHE SCRIPT\n' + str(text or '')[:6000])


def parse_llm_reply(reply, cues=()):
    """A model's reply -> a brief, or None."""
    m = re.search(r'\{.*\}', str(reply or ''), re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    by_label = {_label(c).lower().replace(' ', ''): c for c in cues}
    vo = []
    for row in obj.get('vo') if isinstance(obj.get('vo'), list) else []:
        if not isinstance(row, dict) or not _clean(row.get('text')):
            continue
        covers = _clean(row.get('covers')).lower().replace(' ', '').replace('—', '-').replace('–', '-')
        cue = by_label.get(covers)
        if cue is None and covers:
            cue = next((c for k, c in by_label.items() if covers and (k.startswith(covers) or covers.startswith(k))), None)
        if cue is not None:
            anchor = {'kind': 'cue', 'material': cue['material'], 'time': cue['time'], 'out': cue['out'], 'label': _label(cue)}
        elif 'card' in covers or _TUNE_IN.search(str(row.get('text'))):
            anchor = {'kind': 'card', 'label': 'the tune-in card'}
        else:
            anchor = {'kind': 'order', 'label': f'line {len(vo) + 1}'}
        vo.append({'text': _clean(row['text']), 'anchor': anchor, 'how': 'model'})
    tone = _clean(obj.get('tone')).strip('() ').upper() if obj.get('tone') else None
    title = _clean(obj.get('title')) if obj.get('title') else None
    lines = [_clean(x) for x in obj.get('card_lines') if _clean(x)] if isinstance(obj.get('card_lines'), list) else []
    if not (vo or tone or title or lines):
        return None
    return {'vo': vo, 'tone': tone or None, 'music': _clean(obj.get('music')) or None, 'sot': [], 'notes': [],
            'script_id': None, 'read_by': 'AI model',
            'tcard': {'title': title or (lines[0] if lines else None), 'lines': lines, 'highlighted': []}
            if (title or lines) else None}


# ---------------------------------------------------------------- putting it together

def read_script_brief(raw, filename='', text='', cues=(), ask_llm=None):
    """The brief for an uploaded script, or None. `text` is the plain text the existing reader got from the
    file; `ask_llm(prompt) -> reply text` is optional. A reader that finds nothing, or fails, returns None:
    the script then does what it did before this existed."""
    brief = None
    name = str(filename or '').lower()
    try:
        pages = None
        if name.endswith('.pdf'):
            pages = pdf_pages(raw)
            brief = read_layout(pages) if pages else None
        if pages and not (brief and brief['vo']):
            text = pages_text(pages) or text
        if (brief is None or not brief['vo']) and ask_llm is not None and (text or '').strip():
            seen = parse_llm_reply(ask_llm(llm_prompt(text, cues)), cues)
            if seen:
                if brief:                                       # keep what the layout found, add what it could not
                    for k in ('tone', 'music', 'tcard'):
                        if not brief.get(k) and seen.get(k):
                            brief[k] = seen[k]
                    brief['vo'] = seen['vo']
                    brief['read_by'] = 'layout and AI model'
                else:
                    brief = seen
        if brief is None and not name.endswith('.pdf'):
            brief = _plain_text(text)
    except Exception as e:                                      # a script must never break a job
        print(f'Script brief not read: {e}')
        return None
    if brief and not (brief['vo'] or brief['tone'] or brief['tcard'] or brief['music']):
        return None
    return brief


def _plain_text(text):
    """A text script with explicit 'VO:' lines."""
    vo = []
    for ln in str(text or '').splitlines():
        m = re.match(r'^\s*(?:v\.?o\.?|voice\s*over|narrator)\s*[:\-–—]\s*(.+)$', ln, re.I)
        if m and _clean(m.group(1)):
            t = _clean(m.group(1))
            vo.append({'text': t, 'anchor': {'kind': 'card' if _TUNE_IN.search(t) else 'order', 'label': ''}, 'how': 'text'})
    if not vo:
        return None
    for k, v in enumerate(vo):
        if v['anchor']['kind'] == 'order':
            v['anchor']['label'] = f'line {k + 1}'
    return {'vo': vo, 'tone': None, 'tcard': None, 'music': None, 'sot': [], 'notes': [], 'script_id': None, 'read_by': 'text'}


# ---------------------------------------------------------------- from anchors to plug times

VO_LEAD = 0.12          # a line starts this long after its clip does
CARD_LEAD = 0.2         # ... and this long after the card does


def timeline_starts(clip_durs, card_durs, xfade):
    """(clip_starts, card_starts) on the plug's timeline: clips back to back, then the cards, each
    overlapping the one before by the crossfade."""
    starts, t = [], 0.0
    for d in list(clip_durs) + list(card_durs):
        starts.append(t)
        t += float(d) - float(xfade)
    return starts[:len(clip_durs)], starts[len(clip_durs):]


def card_targets(texts, n_cards):
    """Which card each tune-in line is said over: 0 the title card, 1 the end card. With two cards and more than
    one such line, the line that reads as the schedule ("8:50PM ON GMA PRIME", "weeknights") is the end card's and
    the others are the title card's; when they all read alike the first is the title card's and the rest the end
    card's. A single line, or a plug with one card, keeps everything on the first card (the line then carries on
    over the cards that follow)."""
    n = len(texts)
    if n_cards < 2 or n < 2:
        return [0] * n
    sched = [bool(_TUNE_IN.search(t or '')) for t in texts]
    if any(sched) and not all(sched):
        return [1 if x else 0 for x in sched]
    return [0] + [1] * (n - 1)


def place_lines(vo, clips, clip_starts, card_starts, xfade=0.3, jcut=0.0, card_jcut=0.0):
    """Times for the script's voice-over lines.

    clips        the plug's clips in order: [{'material', 'start' (source time in), 'selected_dur'}]
    Returns (lines, notes): lines are cutcraft-style {'at': seconds|None, 'text': ..., 'optional': False}. A line
    with no time of its own follows the one before it (two lines on one clip are said one after the other).
    jcut         seconds a line may start before its picture (a J-cut): the voice comes in under the end of the
                 clip before. Nothing starts before 0, and the first clip has nothing before it. A line that runs past
                 the end of its clip carries on over the next picture by itself (an L-cut)."""
    jcut = max(0.0, float(jcut or 0.0))
    lines, notes, taken = [], [], {}
    card_ix = [k for k, v in enumerate(vo) if v['anchor']['kind'] == 'card']
    which = dict(zip(card_ix, card_targets([vo[k]['text'] for k in card_ix], len(card_starts or []))))
    for k, v in enumerate(vo):
        a = v['anchor']
        at = None
        if a['kind'] == 'cue':
            idx = _clip_for(a, clips)
            if idx is None:
                notes.append(f'"{v["text"][:40]}" sits by {a["label"]}, which is not in the plug: it follows the line before.')
            else:
                at = clip_starts[idx] + VO_LEAD - (min(jcut, clip_starts[idx] + VO_LEAD) if idx > 0 else 0.0)
                if idx in taken:
                    at = None                                  # a second line on the same clip: after the first
                taken[idx] = True
        elif a['kind'] == 'card':
            if card_starts:
                ci = min(which.get(k, 0), len(card_starts) - 1)
                # Only the title card's line is led by the cards' own lead (it is the one that has to fit them all).
                lead_in = max(jcut, max(0.0, float(card_jcut or 0.0))) if ci == 0 else jcut
                at = card_starts[ci] + CARD_LEAD - min(lead_in, card_starts[ci] + CARD_LEAD)
                if ('card', ci) in taken:
                    at = None                                  # a second line on the same card: after the first
                taken[('card', ci)] = True
                if ci == 1:
                    notes.append(f'"{v["text"][:40]}" is said over the end card, which is where it starts.')
            else:
                notes.append(f'"{v["text"][:40]}" is the tune-in line but the plug has no card: it follows the line before.')
        elif a['kind'] == 'order':
            at = None if k else (clip_starts[0] + VO_LEAD if clip_starts else 0.0)
        lines.append({'at': at, 'text': v['text'], 'optional': False})
    return lines, notes


def _clip_for(anchor, clips):
    """The clip a cue anchor names: same material, and the cue's time inside the clip's source span."""
    best, best_d = None, None
    for i, c in enumerate(clips):
        if anchor.get('material') and c.get('material') and c['material'] != anchor['material']:
            continue
        start = float(c.get('trim_start', c.get('start', 0.0)))
        end = start + float(c.get('selected_dur') or c.get('duration') or 0.0)
        t = float(anchor['time'])
        d = 0.0 if start - 0.6 <= t <= end + 0.6 else min(abs(t - start), abs(t - end))
        if d <= 1.5 and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


def vo_script_text(lines):
    """cutcraft.parse_vo_script's text for lines from place_lines."""
    out = []
    for l in lines:
        # "8:50PM ON GMA PRIME" with no time of its own would be read as the time 8:50: a tag keeps it a line.
        tag = 'VO: ' if l['at'] is None and re.match(r'\s*\d', l['text']) else ''
        out.append((f'@{l["at"]:.2f}s ' if l['at'] is not None else '') + tag + l['text'])
    return '\n'.join(out)
