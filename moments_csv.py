"""An editor's own list of moments (CSV) for Vertical Shorts.

Columns, by header name (any order): title, in, out, and optionally part
and note. With no header row the columns are title,in,out -- or just
in,out. Times are seconds (83.5), m:ss, h:mm:ss(.f) or h:mm:ss:ff /
h:mm:ss;ff (frames, at the video's frame rate). Comma, semicolon and tab
separated files are all read."""
import csv
import io
import re

MAX_ROWS = 60
MAX_BYTES = 256 * 1024

_NAMES = {
    'title': ('title', 'name', 'label', 'moment'),
    'in': ('in', 'start', 'tcin', 'timein', 'intime', 'begin'),
    'out': ('out', 'end', 'tcout', 'timeout', 'outtime', 'stop'),
    'part': ('part', 'episode', 'file', 'source'),
    'note': ('note', 'notes', 'comment', 'comments', 'remark'),
}
_TC = re.compile(r'^\d+([:;]\d{1,2}){0,3}(\.\d+)?$')


def _key(h):
    return re.sub(r'[^a-z]', '', (h or '').lower())


def is_time(tok):
    return bool(_TC.match((tok or '').strip())) and (tok.count(':') + tok.count(';') <= 3)


def to_seconds(tok, fps=25.0):
    """Seconds from a time as written, or ValueError."""
    t = (tok or '').strip()
    if not is_time(t):
        raise ValueError(t)
    parts = re.split(r'[:;]', t)
    if len(parts) == 4:
        h, m, s, f = parts
        return int(h) * 3600 + int(m) * 60 + int(s) + int(f) / max(1.0, round(float(fps)))
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse(raw):
    """(rows, error). Row: {title, in, out, part, note, line} with in/out
    still as written, so frame timecodes can wait for the frame rate."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            return None, 'That moments list is too large -- it should be a short CSV.'
        raw = raw.decode('utf-8-sig', errors='replace')
    raw = (raw or '').strip()
    if not raw:
        return None, 'The moments list is empty.'
    try:
        dialect = csv.Sniffer().sniff(raw[:2000], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    grid = [[c.strip() for c in r] for r in csv.reader(io.StringIO(raw), dialect) if any(c.strip() for c in r)]
    if not grid:
        return None, 'The moments list is empty.'
    cols = {}
    head = grid[0]
    if not any(is_time(c) for c in head):
        for i, h in enumerate(head):
            for role, names in _NAMES.items():
                if _key(h) in names and role not in cols:
                    cols[role] = i
        grid = grid[1:]
        if 'in' not in cols or 'out' not in cols:
            return None, 'The first row should name the columns: title, in, out (and optionally part, note).'
    else:
        n = len(head)
        cols = {'in': 0, 'out': 1} if n == 2 else {'title': 0, 'in': 1, 'out': 2}
        if n >= 5:
            cols.update(part=3, note=4)
        elif n == 4:
            cols['note'] = 3
    rows = []
    for ln, r in enumerate(grid, 1):
        def cell(role):
            i = cols.get(role)
            return r[i] if i is not None and i < len(r) else ''
        a, b = cell('in'), cell('out')
        if not is_time(a) or not is_time(b):
            return None, f'Row {ln}: "{a}" - "{b}" are not times. Use seconds, m:ss, h:mm:ss or h:mm:ss:ff.'
        part = cell('part')
        if part and not re.match(r'^(part\s*)?\d{1,2}$', part, re.I):
            return None, f'Row {ln}: the part should be a number (1, 2, ...), not "{part}".'
        rows.append({'title': ' '.join(cell('title').split())[:80], 'in': a, 'out': b,
                     'part': int(re.sub(r'\D', '', part)) if part else None,
                     'note': ' '.join(cell('note').split())[:200], 'line': ln})
    if not rows:
        return None, 'The moments list has no rows.'
    if len(rows) > MAX_ROWS:
        return None, f'The moments list has {len(rows)} rows; at most {MAX_ROWS} are read.'
    return rows, None
