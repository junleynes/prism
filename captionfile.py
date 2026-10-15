"""Caption files (SRT, WebVTT, SCC) -> cues [{'start', 'end', 'text'}] in seconds.

Used when an episode already has its captions: the speech-to-text pass is skipped and these
cues are the transcript. Timing is whatever the file says; nothing is guessed except a whole-hour
timecode offset (broadcast files often start at 01:00:00:00), which is reported."""
import re

MAX_BYTES = 3 * 1024 * 1024
EXTENSIONS = ('.srt', '.vtt', '.scc')


def decode_bytes(raw):
    """Text of a caption file: UTF-8 (with or without BOM), UTF-16 with BOM, else Latin-1."""
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', errors='replace')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def _clean(text):
    text = re.sub(r'<[^>]*>', '', text)            # <i>, <c.color>, <00:00:01.000>
    text = re.sub(r'\{\\[^}]*\}', '', text)        # {\an8}
    return ' '.join(text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ').split())


_TIME = r'(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})'
_CUE = re.compile(_TIME + r'\s*-->\s*' + _TIME)


def _secs(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int((ms + '00')[:3]) / 1000.0


def parse_srt_vtt(text):
    cues = []
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    i = 0
    while i < len(lines):
        m = _CUE.search(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _secs(*g[0:4]), _secs(*g[4:8])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i])
            i += 1
        t = _clean(' '.join(body))
        if t and end > start:
            cues.append({'start': start, 'end': end, 'text': t})
    return cues


# ---- SCC (CEA-608) ----
_BASIC = {0x2A: 'á', 0x5C: 'é', 0x5E: 'í', 0x5F: 'ó', 0x60: 'ú', 0x7B: 'ç', 0x7C: '÷', 0x7D: 'Ñ', 0x7E: 'ñ', 0x7F: ''}
_SPECIAL = ['®', '°', '½', '¿', '™', '¢', '£', '♪', 'à', ' ', 'è', 'â', 'ê', 'î', 'ô', 'û']
_EXT12 = list('ÁÉÓÚÜü‘¡*\'—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»')
_EXT13 = list('ÃãÍÌìÒòÕõ{}\\^_|~ÄäÖöß¥¤|ÅåØø┌┐└┘')
_FRAME = 1001.0 / 30000.0


def _scc_time(tc):
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2})$', tc.strip())
    if not m:
        return None
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    frames = (h * 3600 + mi * 60 + s) * 30 + f
    if sep == ';':
        total_min = h * 60 + mi
        frames -= 2 * (total_min - total_min // 10)
    return frames * _FRAME


def parse_scc(text):
    cues = []
    state = {'mode': 'pop', 'shown': '', 'hidden': '', 'open': None, 'line': '', 'line_t': None, 'row': None}

    def close(t):
        o = state['open']
        if o and o['text']:
            cues.append({'start': o['start'], 'end': max(t, o['start'] + 0.2), 'text': o['text']})
        state['open'] = None

    def show(text_, t):
        close(t)
        text_ = ' '.join(text_.split())
        if text_:
            state['open'] = {'start': t, 'text': text_}

    def flush_line(t):
        # roll-up / paint-on: a finished line is a cue of its own
        line = ' '.join(state['line'].split())
        if line and state['line_t'] is not None:
            cues.append({'start': state['line_t'], 'end': max(t, state['line_t'] + 0.5), 'text': line})
        state['line'], state['line_t'] = '', None

    def put(ch, t):
        if state['mode'] == 'pop':
            state['hidden'] += ch
        else:
            if state['line_t'] is None:
                state['line_t'] = t
            state['line'] += ch

    last_cc = None
    for raw_line in text.replace('\r', '\n').split('\n'):
        parts = raw_line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        t0 = _scc_time(parts[0])
        if t0 is None:
            continue
        last_cc = None
        for k, word in enumerate(parts[1].split()):
            if not re.fullmatch(r'[0-9a-fA-F]{4}', word):
                continue
            t = t0 + k * _FRAME
            b1, b2 = int(word[:2], 16) & 0x7F, int(word[2:], 16) & 0x7F
            if 0x10 <= b1 <= 0x1F:
                if (b1, b2) == last_cc:          # control codes are sent twice
                    last_cc = None
                    continue
                last_cc = (b1, b2)
                if b1 in (0x14, 0x15, 0x1C, 0x1D) and 0x20 <= b2 <= 0x2F:
                    if b2 == 0x20:
                        state['mode'] = 'pop'
                    elif b2 in (0x25, 0x26, 0x27):
                        state['mode'] = 'roll'
                    elif b2 == 0x29:
                        state['mode'] = 'paint'
                    elif b2 == 0x2C:                       # erase displayed memory
                        if state['mode'] == 'pop':
                            state['shown'] = ''
                            close(t)
                        else:
                            flush_line(t)
                    elif b2 == 0x2D:                       # carriage return
                        flush_line(t)
                    elif b2 == 0x2E:                       # erase non-displayed memory
                        state['hidden'] = ''
                    elif b2 == 0x2F:                       # end of caption: flip
                        state['shown'], state['hidden'] = state['hidden'], ''
                        show(state['shown'], t)
                    elif b2 == 0x21:
                        if state['mode'] == 'pop':
                            state['hidden'] = state['hidden'][:-1]
                        else:
                            state['line'] = state['line'][:-1]
                elif b1 in (0x11, 0x19) and 0x30 <= b2 <= 0x3F:
                    put(_SPECIAL[b2 - 0x30], t)
                elif b1 in (0x12, 0x1A, 0x13, 0x1B) and 0x20 <= b2 <= 0x3F:
                    tbl = _EXT12 if b1 in (0x12, 0x1A) else _EXT13
                    if state['mode'] == 'pop':
                        state['hidden'] = state['hidden'][:-1]
                    else:
                        state['line'] = state['line'][:-1]
                    put(tbl[b2 - 0x20] if b2 - 0x20 < len(tbl) else '', t)
                elif b2 >= 0x40:                           # preamble address code: a new row
                    row = (b1, b2 & 0x20)
                    if state['row'] is not None and state['row'] != row and state['mode'] == 'pop' and state['hidden'] and not state['hidden'].endswith(' '):
                        state['hidden'] += ' '
                    state['row'] = row
                continue
            last_cc = None
            for b in (b1, b2):
                if b >= 0x20:
                    put(_BASIC.get(b, chr(b)), t)
    if state['open']:
        close(state['open']['start'] + 2.5)
    if state['line_t'] is not None:
        flush_line(state['line_t'] + 2.5)
    cues.sort(key=lambda c: c['start'])
    # Roll-up lines stay on screen until the next one arrives.
    for a, b in zip(cues, cues[1:]):
        if a['end'] > b['start']:
            a['end'] = max(a['start'] + 0.2, b['start'])
    return cues


def parse_captions(raw, filename):
    """(cues, error). `raw` is the file's bytes."""
    name = (filename or '').lower()
    ext = name[name.rfind('.'):] if '.' in name else ''
    if ext not in EXTENSIONS:
        return None, f'"{filename}" is not a caption file this can read: use .srt, .vtt or .scc.'
    if len(raw) > MAX_BYTES:
        return None, f'"{filename}" is too large for a caption file.'
    text = decode_bytes(raw)
    if ext == '.scc' or text.lstrip().startswith('Scenarist_SCC'):
        cues = parse_scc(text)
    else:
        cues = parse_srt_vtt(text)
    if not cues:
        return None, f'No captions were found in "{filename}".'
    return cues, None


_SOUND = re.compile(r'\[[^\]]*\]|\([^)]*\)|\u266a[^\u266a]*\u266a?|\u266b[^\u266b]*\u266b?|[\u266a\u266b]+')
# A speaker's name in capitals, then a colon: "MYLA:", "MAN 2:", "DR. REYES:". Also the ">>" and "-" that
# mark a change of speaker.
_SPEAKER = re.compile(r"(?:(?<=^)|(?<=[.!?\]\u266a)]\s))\s*(?:>>+\s*)?[A-Z][A-Z0-9.'\u2019-]*(?:\s[A-Z0-9][A-Z0-9.'\u2019-]*){0,2}\s*:\s+")
_TURN = re.compile(r'^\s*(?:>>+|-+)\s+|(?<=[.!?])\s+-\s+(?=\S)')


def clean_cues(cues, sounds=True, speakers=True):
    """(cues, stats): captions without the sound descriptors ("[GRUNTS]", "(laughs)", the music notes and what
    they enclose) and/or the speaker labels ("MYLA:", ">>", a leading "-"). A caption left with nothing is
    dropped. stats = {'sounds': n, 'speakers': n, 'dropped': n}: how many captions each changed or removed."""
    stats = {'sounds': 0, 'speakers': 0, 'dropped': 0}
    if not (sounds or speakers):
        return cues, stats
    out = []
    for c in cues:
        text = c['text']
        if sounds:
            new = _SOUND.sub(' ', text)
            if new != text:
                stats['sounds'] += 1
            text = new
        if speakers:
            # The label may only be uncovered once a descriptor in front of it is gone.
            new = text
            for _ in range(3):
                step = _SPEAKER.sub(' ', new)
                step = _TURN.sub(' ', step)
                if step == new:
                    break
                new = step
            if ' '.join(new.split()) != ' '.join(text.split()):
                stats['speakers'] += 1
            text = new
        text = ' '.join(text.split())
        text = re.sub(r'^[\s,.;:-]+', '', text) if text and not re.search(r'\w', text[:1]) and re.search(r'\w', text) else text
        if not re.search(r'\w', text):
            stats['dropped'] += 1
            continue
        out.append(dict(c, text=text))
    return out, stats


def fit_to_video(cues, duration):
    """(cues, shift): captions timed on a broadcast clock (01:00:00:00 start) moved onto the video's own
    time by whole hours, when they would otherwise run past the end of the video. shift is in seconds."""
    if not cues or not duration:
        return cues, 0.0
    first = cues[0]['start']
    if cues[-1]['end'] <= duration + 30.0 or first < 3000:
        return cues, 0.0
    shift = 3600.0 * int(first // 3600)
    out = [dict(c, start=c['start'] - shift, end=c['end'] - shift) for c in cues if c['end'] - shift > 0]
    return out, shift
