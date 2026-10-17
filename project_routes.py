"""
Projects for Episodic Plug and Schedule Plug -- the same idea as Vertical
Shorts projects.

A project is the episode a plug is made for, written down once (programme,
episode, air date, description, a picture, who it is assigned to, a status
and a deadline). Plugs made for it are filed under it. The store and the
rules for a project's fields are shorts_projects.py, shared by all three
features; this module is the HTTP side for the two that did not have
projects: who may see a project (its group's members, and admins), and the
routes to list, create, edit, delete and picture one.

Each feature registers itself once (see `register`) with where its projects
live on disk, the permission that opens its tab, and a function that counts
what is filed under each project. Everything else -- group visibility, the
JSON a project is sent as, what is running in it -- is the same for both,
and the same as in shorts.py, which keeps its own copy of these rules and is
untouched.
"""
import os
import re
import threading
import time

from flask import request, jsonify, session, send_from_directory, has_request_context, g as _req

from core import app, _client_ip
from library_db import LIBRARY_DIR, audit_log
from auth import require_permission, group_membership
import shorts_projects as sp


class Feature:
    """What differs between the features that have projects.

    kind        'episodic' or 'schedule': the URL prefix and the audit-log name.
    permission  the permission that opens the feature's tab.
    noun        (singular, plural) for what is filed in a project, for messages.
    counts      () -> {project id: {'count': n, 'last': timestamp, 'poster': url or None}}
    job_words   {job kind: label} for what is running, shown on a project's card.
    """

    def __init__(self, kind, permission, noun, root, counts, job_words=None):
        self.kind = kind
        self.permission = permission
        self.noun = noun
        self.root = root
        self.counts = counts
        self.job_words = job_words or {}


_FEATURES = {}
_JOBS = {}                  # kind -> {job id: (project id, label)}, for what each feature has started
_JOBS_LOCK = threading.Lock()


def default_root(name):
    return os.path.abspath(os.environ.get(name.upper() + '_DIR') or os.path.join(LIBRARY_DIR, name))


# --------------------------------------------------------------------------
# Who may see a project
# --------------------------------------------------------------------------

def _groups():
    """({user id: frozenset of group ids}, {group id: name}), read once per request."""
    if has_request_context():
        if not hasattr(_req, '_pr_groups'):
            _req._pr_groups = group_membership()
        return _req._pr_groups
    return group_membership()


def project_groups(proj):
    """The groups that may see a project, as a set: always the groups its creator is in NOW. Nobody picks a
    project's group: it follows its creator, so moving someone to another group moves their projects with
    them. (A group_id saved by an earlier version is ignored.) {None} when the creator is in no group or no
    longer exists -- then only admins see it. Shared with Vertical Shorts (shorts.py)."""
    users, groups = _groups()
    gids = set(users.get(proj.get('user_id')) or ()) & set(groups)
    return gids or {None}


def group_names(proj):
    """The names of the groups a project belongs to, for display."""
    names = _groups()[1]
    return sorted((names[g] for g in project_groups(proj) if g is not None), key=str.lower)


def viewer_groups():
    return set(_groups()[0].get(session.get('user_id')) or ())


_project_groups, _viewer_groups = project_groups, viewer_groups


def may_see(proj):
    """A project is its group's: everyone in that group with access to the feature sees it, what is filed in it
    and what is running in it; other groups do not. A project with no group is seen by accounts with no group
    and admins. Admins see every project."""
    if not has_request_context():           # a job thread: whoever started it was allowed to
        return True
    if session.get('role') == 'admin':
        return True
    if session.get('user_id') is None:
        return False
    return bool(_project_groups(proj) & (_viewer_groups() or {None}))


def may_change(proj):
    """Delete a project: whoever made it, or an admin."""
    if not has_request_context():
        return True
    if session.get('role') == 'admin':
        return True
    uid = session.get('user_id')
    return uid is not None and proj.get('user_id') == uid


def visible_project(kind, pid):
    """The project, or None when there is none or the caller's group may not see it (the same answer either
    way, so another group's project ids reveal nothing)."""
    feat = _FEATURES.get(kind)
    proj = sp.load(feat.root, pid) if feat and pid else None
    return proj if proj and may_see(proj) else None


def project_exists(kind, pid):
    """True if the project is on disk, whoever may see it: for telling a plug that is filed from one that is not."""
    feat = _FEATURES.get(kind)
    return bool(feat and pid and sp.load(feat.root, pid))


def existing_ids(kind):
    """The ids of every project on disk, whoever may see them."""
    feat = _FEATURES.get(kind)
    return [p['project_id'] for p in sp.list_all(feat.root)] if feat else []


def project_name(kind, pid):
    feat = _FEATURES.get(kind)
    proj = sp.load(feat.root, pid) if feat and pid else None
    return sp.display_name(proj) if proj else None


# --------------------------------------------------------------------------
# What is running in a project
# --------------------------------------------------------------------------

def track_job(kind, job_id, project_id, job_kind=None):
    """Remembers that `job_id` belongs to a project, so the project's card can show it while it runs. Held in
    memory: a restart forgets it, and the jobs are marked interrupted then anyway."""
    if not project_id:
        return
    with _JOBS_LOCK:
        _JOBS.setdefault(kind, {})[job_id] = (project_id, job_kind)


def _jobs(feat):
    """{project id: [{'job_id', 'label', 'state' ('waiting'|'running'), 'percent', 'step'}]} for this feature's
    jobs that have not finished, running ones first."""
    with _JOBS_LOCK:
        mine = dict(_JOBS.get(feat.kind) or {})
    if not mine:
        return {}
    import pipeline                         # imported late: pipeline registers its features while it loads
    rows = pipeline.job_list_all()
    out = {}
    for jid, (pid, jkind) in mine.items():
        j = rows.get(jid)
        if not j or j.get('done'):
            with _JOBS_LOCK:                # finished: nothing more to show, and nothing to remember
                _JOBS.get(feat.kind, {}).pop(jid, None)
            continue
        out.setdefault(pid, []).append({
            'job_id': jid, 'label': feat.job_words.get(jkind) or 'Working',
            'state': 'waiting' if j.get('status') == 'queued' else 'running',
            'percent': max(0, min(99, int(j.get('percent') or 0))), 'step': j.get('step') or '',
            'created': j.get('created') or 0})
    for pid in out:
        out[pid].sort(key=lambda x: (x['state'] != 'running', x['created']))
        for x in out[pid]:
            x.pop('created', None)
    return out


# --------------------------------------------------------------------------
# A project as the pages see it
# --------------------------------------------------------------------------

def _public(feat, proj, counts=None, jobs=None):
    pid = proj['project_id']
    c = (counts or {}).get(pid) or {'count': 0, 'last': None, 'poster': None}
    status = sp.normal_status(proj.get('status')) or sp.DEFAULT_STATUS
    own = f"/api/{feat.kind}/projects/{pid}/thumb?v={int(proj.get('updated') or 0)}" if proj.get('thumb') else None
    return {'project_id': pid, 'title': proj.get('title'), 'episode': proj.get('episode') or '',
            'episode_title': proj.get('episode_title') or '', 'artists': proj.get('artists') or '',
            'air_date': proj.get('air_date'), 'description': proj.get('description') or '',
            'status': status, 'status_label': sp.STATUSES[status],
            'assigned_to': proj.get('assigned_to') or '', 'deadline': proj.get('deadline') or None,
            'name': sp.display_name(proj), 'created': proj.get('created'), 'updated': proj.get('updated'),
            'username': proj.get('username'), 'has_thumb': bool(proj.get('thumb')),
            'thumb_url': own or c['poster'],
            'count': c['count'], 'last_activity': c['last'] or proj.get('created'),
            'can_delete': may_change(proj), 'jobs': (jobs or {}).get(pid) or [],
            'group_name': ', '.join(group_names(proj)) or None}


def _thumb_upload():
    """JPEG bytes for the picture sent with this request, or None if none was. Decoded once and stored only as
    our own re-encoding, whatever ALLOW_LOCAL_MEDIA_UPLOAD says (see shorts.py: it is never handed to ffmpeg)."""
    f = request.files.get('thumbnail')
    if f is None or not f.filename:
        return None
    return sp.make_thumbnail(f.stream.read(sp.THUMB_MAX_BYTES + 1), f.filename)


def register(feat):
    """Adds the project routes for one feature: /api/<kind>/projects ..., /api/<kind>/assignees."""
    _FEATURES[feat.kind] = feat
    kind, perm, one, many = feat.kind, feat.permission, feat.noun[0], feat.noun[1]

    def guard(fn):
        return require_permission(perm)(fn)

    def projects():
        if request.method == 'GET':
            counts, jobs = feat.counts(), _jobs(feat)
            items = [_public(feat, p, counts, jobs) for p in sp.list_all(feat.root) if may_see(p)]
            return jsonify(ok=True, items=items, is_admin=session.get('role') == 'admin')
        try:
            fields = sp.clean_fields(request.form)
            thumb = _thumb_upload()
        except sp.ProjectError as e:
            return jsonify(ok=False, error=str(e)), 400
        proj = sp.create(feat.root, fields, user_id=session.get('user_id'), username=session.get('username'),
                         thumb_jpeg=thumb)
        audit_log(f'{kind}_project_create', target=sp.display_name(proj),
                  user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
        return jsonify(ok=True, project=_public(feat, proj))

    def assignees():
        """Who a project can be assigned to: the active accounts that may use this feature and can see the
        project -- members of its group, and admins."""
        from auth import user_list, user_can
        proj = visible_project(kind, (request.args.get('project_id') or '').strip())
        if proj:
            want, every = _project_groups(proj), False
        else:
            want, every = _viewer_groups() or {None}, session.get('role') == 'admin'
        names = [u['username'] for u in user_list()
                 if u.get('is_active') and user_can(u['id'], u.get('role'), perm)
                 and (every or u.get('role') == 'admin' or (set(u.get('group_ids') or ()) or {None}) & want)]
        return jsonify(ok=True, items=names)

    def activity():
        jobs = _jobs(feat)
        return jsonify(ok=True, jobs={pid: j for pid, j in jobs.items() if visible_project(kind, pid)})

    def project(pid):
        proj = visible_project(kind, pid)
        if not proj:
            return jsonify(ok=False, error='That project no longer exists.'), 404
        if request.method == 'GET':
            return jsonify(ok=True, project=_public(feat, proj, feat.counts()))
        if request.method == 'DELETE':
            if not may_change(proj):
                return jsonify(ok=False, error='Only whoever created a project, or an admin, can delete it.'), 403
            held = (feat.counts().get(pid) or {}).get('count')
            if held:
                return jsonify(ok=False, error=f"This project still holds {held} {one if held == 1 else many}. "
                               'Delete them, or move them to another project, first: deleting a project does '
                               f'not delete {many}.'), 409
            sp.delete(feat.root, pid)
            audit_log(f'{kind}_project_delete', target=sp.display_name(proj),
                      user_id=session.get('user_id'), username=session.get('username'), ip=_client_ip())
            return jsonify(ok=True)
        try:
            fields = sp.clean_fields(request.form)
            thumb = _thumb_upload()
        except sp.ProjectError as e:
            return jsonify(ok=False, error=str(e)), 400
        proj = sp.update(feat.root, pid, fields, thumb_jpeg=thumb,
                         remove_thumb=request.form.get('remove_thumbnail') in ('1', 'true', 'on'))
        return jsonify(ok=True, project=_public(feat, proj, feat.counts()))

    def thumb(pid):
        proj = visible_project(kind, pid)
        if not proj or not proj.get('thumb'):
            return jsonify(ok=False, error='Not found'), 404
        resp = send_from_directory(sp.project_dir(feat.root, pid), 'thumb.jpg', conditional=True)
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp

    base = f'/api/{kind}'
    app.add_url_rule(f'{base}/projects', f'{kind}_projects', guard(projects), methods=['GET', 'POST'])
    app.add_url_rule(f'{base}/projects/activity', f'{kind}_projects_activity', guard(activity))
    app.add_url_rule(f'{base}/assignees', f'{kind}_assignees', guard(assignees))
    app.add_url_rule(f'{base}/projects/<pid>', f'{kind}_project', guard(project), methods=['GET', 'POST', 'DELETE'])
    app.add_url_rule(f'{base}/projects/<pid>/thumb', f'{kind}_project_thumb', guard(thumb))
