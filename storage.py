"""
Disk housekeeping: what is using the drive, how long finished output is kept,
and a guard that stops a render when the drive is nearly full.

Requested by JUN after the system temp folder grew to 1.8 TB. Admin-only,
under Config > Storage.

Kept output (saved plugs, shorts batches, artwork animations) used to stay
until someone deleted it. Each kind can now have a retention in days (0 = keep
forever, the default, so nothing disappears until an admin asks for it), and
any single item can be pinned to be exempt. Settings live in
storage_config.json next to the script, like branding_config.json.
"""
import json
import os
import re
import shutil
import threading
import time

from flask import jsonify, request, session

import core
from core import app, _client_ip
import library_db
from library_db import LIBRARY_DIR, audit_log
import pipeline
import shorts
import schedule

CONFIG_FILE = os.environ.get('STORAGE_CONFIG_FILE') or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                   'storage_config.json')
KINDS = ('trailers', 'shorts', 'schedule')
KIND_LABELS = {'trailers': 'Highlights / episodic plugs', 'shorts': 'Vertical Shorts batches',
               'schedule': 'Artwork animations'}
DEFAULT_MIN_FREE_GB = 5.0
_LOCK = threading.Lock()
_PIN_KEY = re.compile(r'^(trailers|shorts|schedule):[\w.-]{1,80}$')


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_settings():
    cfg = {'retention_days': {k: 0 for k in KINDS}, 'min_free_gb': DEFAULT_MIN_FREE_GB, 'pinned': []}
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for k in KINDS:
        try:
            cfg['retention_days'][k] = max(0, int((raw.get('retention_days') or {}).get(k, 0)))
        except (TypeError, ValueError):
            pass
    try:
        cfg['min_free_gb'] = max(0.0, float(raw.get('min_free_gb', DEFAULT_MIN_FREE_GB)))
    except (TypeError, ValueError):
        pass
    cfg['pinned'] = [p for p in (raw.get('pinned') or []) if isinstance(p, str) and _PIN_KEY.match(p)]
    return cfg


def save_settings(cfg):
    with _LOCK:
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, CONFIG_FILE)


# --------------------------------------------------------------------------
# What is on disk
# --------------------------------------------------------------------------

def _tree_size(path):
    total = 0
    for dp, _dn, fn in os.walk(path):
        for x in fn:
            try:
                total += os.path.getsize(os.path.join(dp, x))
            except OSError:
                pass
    return total


def _trailer_items():
    conn = library_db.database.connect()
    try:
        rows = conn.execute('SELECT id, orig_name, filename, created_at, username FROM trailers').fetchall()
    finally:
        conn.close()
    try:
        names = os.listdir(LIBRARY_DIR)
    except OSError:
        names = []
    out = []
    for r in rows:
        base = os.path.splitext(r['filename'])[0]
        size = 0
        for n in names:                      # the file and its converted copies (<base>_*.ext)
            if n == r['filename'] or n.startswith(base + '_'):
                try:
                    size += os.path.getsize(os.path.join(LIBRARY_DIR, n))
                except OSError:
                    pass
        out.append({'kind': 'trailers', 'id': str(r['id']), 'name': r['orig_name'] or r['filename'],
                    'bytes': size, 'created': r['created_at'], 'owner': r['username']})
    return out


def _shorts_items():
    out = []
    try:
        names = os.listdir(shorts.SHORTS_DIR)
    except OSError:
        return out
    for name in names:
        p = os.path.join(shorts.SHORTS_DIR, name)
        if not (os.path.isdir(p) and shorts._BATCH_ID.match(name)):
            continue
        m = shorts._read_manifest(p) or {}
        try:
            created = float(m.get('created') or os.path.getmtime(p))
        except (TypeError, ValueError, OSError):
            created = os.path.getmtime(p)
        out.append({'kind': 'shorts', 'id': name, 'name': m.get('orig_name') or name, 'bytes': _tree_size(p),
                    'created': created, 'owner': m.get('username'), 'busy': m.get('status') == 'rendering'})
    return out


def _schedule_items():
    out = []
    try:
        names = os.listdir(schedule.SCHEDULE_DIR)
    except OSError:
        return out
    for name in names:
        p = os.path.join(schedule.SCHEDULE_DIR, name)
        if not (os.path.isdir(p) and schedule._PLUG_ID.match(name)):
            continue
        m = schedule._load_manifest(name) or {}
        try:
            created = float(m.get('created') or os.path.getmtime(p))
        except (TypeError, ValueError, OSError):
            created = os.path.getmtime(p)
        out.append({'kind': 'schedule', 'id': name, 'name': m.get('title') or name, 'bytes': _tree_size(p),
                    'created': created, 'owner': m.get('username')})
    return out


def inventory():
    """Every kept item of every kind, with size, age and whether it is pinned."""
    pinned = set(load_settings()['pinned'])
    items = _trailer_items() + _shorts_items() + _schedule_items()
    for it in items:
        it['pinned'] = f"{it['kind']}:{it['id']}" in pinned
    return items


def orphans():
    """Upload folders left behind by runs that are gone: {'count', 'bytes'}."""
    keep = pipeline.app.config['UPLOAD_FOLDER']
    found = [x for par, legacy in core.upload_parents() for x in core.stale_upload_dirs(par, keep, legacy)]
    return {'count': len(found), 'bytes': sum(b for _p, b in found)}


def volumes():
    """Free and total space on each distinct drive the app writes to."""
    seen, out = set(), []
    for label, path in (('Temp / uploads', pipeline.app.config['UPLOAD_FOLDER']), ('Library', LIBRARY_DIR),
                        ('Vertical Shorts', shorts.SHORTS_DIR), ('Artwork animations', schedule.SCHEDULE_DIR)):
        try:
            du = shutil.disk_usage(path)
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev in seen:
            for v in out:
                if v['dev'] == dev:
                    v['labels'].append(label)
            continue
        seen.add(dev)
        out.append({'dev': dev, 'labels': [label], 'free': du.free, 'total': du.total})
    for v in out:
        v.pop('dev')
    return out


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def _remove_item(it):
    """Deletes one kept item. True if it is gone."""
    kind, iid = it['kind'], it['id']
    if kind == 'trailers':
        try:
            return bool(library_db.library_delete(int(iid)))
        except (OSError, ValueError):
            return False
    root = shorts.SHORTS_DIR if kind == 'shorts' else schedule.SCHEDULE_DIR
    path = os.path.join(root, iid)
    tomb = os.path.join(root, f'.deleting_{iid}_{os.urandom(2).hex()}')
    try:
        os.rename(path, tomb)              # all-or-nothing: fails if a file is open (Windows) and leaves it intact
    except OSError:
        return False
    shutil.rmtree(tomb, ignore_errors=True)
    return True


def expired_items(now=None, settings=None):
    """The items past their kind's retention, not pinned and not rendering."""
    now = time.time() if now is None else now
    cfg = settings or load_settings()
    pinned = set(cfg['pinned'])
    out = []
    for it in inventory():
        days = cfg['retention_days'].get(it['kind'], 0)
        if not days or it.get('busy') or f"{it['kind']}:{it['id']}" in pinned:
            continue
        if now - float(it['created'] or now) > days * 86400:
            out.append(it)
    return out


def clean(dry_run=False, now=None):
    """Applies the retention settings. Returns {'items': n, 'bytes': n, 'failed': n}."""
    gone = failed = freed = 0
    for it in expired_items(now):
        if dry_run:
            gone += 1
            freed += it['bytes']
        elif _remove_item(it):
            gone += 1
            freed += it['bytes']
        else:
            failed += 1
    freed_tmp = 0
    if not dry_run:
        try:
            freed_tmp = pipeline.sweep_upload_folder()
            keep = pipeline.app.config['UPLOAD_FOLDER']
            for par, legacy in core.upload_parents():
                freed_tmp += core.reap_stale_upload_dirs(par, keep=keep, legacy=legacy)
        except Exception as e:                                  # noqa: BLE001
            print(f'Storage clean: temp sweep failed: {e}')
    return {'items': gone, 'bytes': freed, 'failed': failed, 'temp_bytes': freed_tmp}


# --------------------------------------------------------------------------
# The guard that stops a render on a nearly full drive
# --------------------------------------------------------------------------

def low_space_error():
    """A message if a drive the render writes to is below the minimum, else None."""
    need = load_settings()['min_free_gb'] * 1024 ** 3
    if need <= 0:
        return None
    for label, path in (('temp', pipeline.app.config['UPLOAD_FOLDER']), ('library', LIBRARY_DIR)):
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        if free < need:
            return (f'Not enough disk space: {free / 1024 ** 3:.1f} GB free on the {label} drive, '
                    f'and {need / 1024 ** 3:.1f} GB is the minimum for a render. '
                    f'Ask an admin to free space (Config > Storage).')
    return None


# --------------------------------------------------------------------------
# Routes (admin only)
# --------------------------------------------------------------------------

def _admin():
    return session.get('role') == 'admin'


@app.route('/api/admin/storage')
def api_admin_storage():
    if not _admin():
        return jsonify(ok=False, error='Admin access required.'), 403
    cfg = load_settings()
    items = inventory()
    kinds = {}
    for k in KINDS:
        mine = sorted((i for i in items if i['kind'] == k), key=lambda i: i['bytes'], reverse=True)
        kinds[k] = {'label': KIND_LABELS[k], 'count': len(mine), 'bytes': sum(i['bytes'] for i in mine),
                    'retention_days': cfg['retention_days'][k], 'largest': mine[:20]}
    folder = pipeline.app.config['UPLOAD_FOLDER']
    would = clean(dry_run=True)
    old = orphans()
    return jsonify(ok=True, kinds=kinds, min_free_gb=cfg['min_free_gb'], volumes=volumes(), orphans=old,
                   temp={'bytes': _tree_size(folder), 'ttl_hours': round(pipeline.UPLOAD_TTL / 3600, 1),
                         'cap_gb': pipeline.UPLOAD_MAX_GB},
                   due={'items': would['items'], 'bytes': would['bytes'] + old['bytes']})


@app.route('/api/admin/storage/settings', methods=['POST'])
def api_admin_storage_settings():
    if not _admin():
        return jsonify(ok=False, error='Admin access required.'), 403
    body = request.get_json(silent=True) or {}
    cfg = load_settings()
    try:
        for k in KINDS:
            if k in (body.get('retention_days') or {}):
                days = int(body['retention_days'][k])
                if days < 0 or days > 3650:
                    raise ValueError
                cfg['retention_days'][k] = days
        if 'min_free_gb' in body:
            gb = float(body['min_free_gb'])
            if gb < 0 or gb > 100000:
                raise ValueError
            cfg['min_free_gb'] = gb
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Days must be a whole number (0 keeps everything) and the minimum free space a number of GB.'), 400
    save_settings(cfg)
    audit_log('storage_settings', detail=json.dumps({'retention_days': cfg['retention_days'], 'min_free_gb': cfg['min_free_gb']}),
              user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return jsonify(ok=True)


@app.route('/api/admin/storage/pin', methods=['POST'])
def api_admin_storage_pin():
    if not _admin():
        return jsonify(ok=False, error='Admin access required.'), 403
    body = request.get_json(silent=True) or {}
    key = str(body.get('key') or '')
    if not _PIN_KEY.match(key):
        return jsonify(ok=False, error='Unknown item.'), 400
    cfg = load_settings()
    pins = [p for p in cfg['pinned'] if p != key]
    if body.get('pinned'):
        pins.append(key)
    cfg['pinned'] = pins
    save_settings(cfg)
    return jsonify(ok=True, pinned=bool(body.get('pinned')))


@app.route('/api/admin/storage/clean', methods=['POST'])
def api_admin_storage_clean():
    if not _admin():
        return jsonify(ok=False, error='Admin access required.'), 403
    r = clean()
    audit_log('storage_clean', detail=f"{r['items']} items, {r['bytes'] + r['temp_bytes']} bytes freed",
              user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
    return jsonify(ok=True, **r)


# --------------------------------------------------------------------------
# Daily retention run
# --------------------------------------------------------------------------

_RUN_EVERY = int(os.environ.get('STORAGE_CLEAN_INTERVAL', 6 * 3600))


def _loop():
    while True:
        time.sleep(_RUN_EVERY)
        try:
            r = clean()
            if r['items']:
                print(f"Retention removed {r['items']} item(s), {r['bytes'] / 1024 ** 3:.2f} GB")
        except Exception as e:                                  # noqa: BLE001
            print(f'Retention run error: {e}')


def start_background():
    threading.Thread(target=_loop, daemon=True, name='storage-retention').start()
