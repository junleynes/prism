"""User accounts, login/logout, and the /admin/users management page.

Depends on: core (app, rate limiter, dummy-hash, default password) and
database (the PostgreSQL connection pool; accounts live in the `users`,
`groups` and `group_permissions` tables -- see schema.sql).
"""
import os, re, time, functools, secrets
from flask import request, session, redirect, jsonify, render_template
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from core import app, _client_ip, _login_limiter, _DUMMY_PW_HASH, ensure_csrf_token
from library_db import load_branding, audit_log
import database

def _safe_next(dest):
    """Only ever redirect to a path on this app -- an absolute or
    protocol-relative 'next' would be an open-redirect vector."""
    dest = dest or '/'
    if not dest.startswith('/') or dest.startswith('//'):
        return '/'
    return dest

def _establish_session(user):
    """Promotes a verified user to a fully signed-in session. Called only
    after EVERY required factor has passed -- password alone for an account
    without 2FA, password + code for one with it."""
    session.pop('pending_2fa_uid', None)
    session.pop('pending_2fa_next', None)
    session.permanent = True
    session['authed'] = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['sv'] = user.get('session_version') or 0
    # Fresh CSRF token per sign-in, and only after every factor has passed --
    # so a token handed out during the half-authenticated 2FA step can't be
    # reused against the fully-authenticated session.
    session['csrf_token'] = secrets.token_urlsafe(32)
    # One-shot marker for the very next page render: tells index.html's
    # tab-restore script to skip restoring the last-open tab from
    # localStorage and land on the server's default (Dashboard) instead,
    # since "just logged in" should always start fresh regardless of
    # whatever tab a previous session (possibly a different user, on a
    # shared machine) happened to leave active. main.py's '/' route pops
    # this immediately, so it only applies to this one login.
    session['just_logged_in'] = True
    user_touch_login(user['id'])

# Set to False only by the test suite (see tests/conftest.py), whose tests
# mostly hand-write sessions for made-up accounts with no row in the users
# table, or one that does not match it.
SESSION_REVALIDATE = True

def _revalidate_session():
    """Every request from a signed-in session is checked against the account
    as it is now, not as it was at sign-in (a Flask session is a signed
    cookie, so nothing in it can be revoked by itself): the account must
    still exist and be active, and its session_version must be the one the
    session started with. The role and username are refreshed from the
    database, so a demoted admin stops being an admin on their next click."""
    if not SESSION_REVALIDATE or not session.get('authed') or request.path.startswith('/static/'):
        return None
    uid = session.get('user_id')
    row = None
    if uid is not None:
        conn = database.connect()
        try:
            row = conn.execute('SELECT username, role, is_active, session_version FROM users WHERE id=%s',
                               (uid,)).fetchone()
        finally:
            conn.close()
    if row is None or not row['is_active'] or (row['session_version'] or 0) != (session.get('sv') or 0):
        session.clear()
        if request.path.startswith(('/api/', '/admin/', '/uploads/', '/library/', '/download/')):
            return jsonify(error='Your session has ended. Sign in again.'), 401
        return redirect('/')
    session['role'] = row['role']
    session['username'] = row['username']
    return None

# Before core.py's CSRF and access checks, which trust session['role'].
app.before_request_funcs.setdefault(None, []).insert(0, _revalidate_session)

def end_sessions(uid):
    """Signs the account out everywhere: every existing session stops working."""
    conn = database.connect()
    conn.execute('UPDATE users SET session_version = session_version + 1 WHERE id=%s', (uid,))
    conn.commit()
    conn.close()

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    # A pending 2FA challenge: the password was already accepted, and we're
    # waiting on the code. Held in the session (not a hidden form field) so the
    # "password was correct" fact can't be forged by posting a crafted form
    # straight to the second step.
    pending_uid = session.get('pending_2fa_uid')
    if request.method == 'POST':
        if not _login_limiter.allow(_client_ip()):
            error = 'Too many attempts. Wait a few minutes and try again.'
        elif pending_uid:
            # ---- Step 2: verify the TOTP (or backup) code ----
            user = user_get(pending_uid)
            if not user or not user['is_active']:
                session.pop('pending_2fa_uid', None)
                error = 'That account is no longer available. Sign in again.'
            elif user_totp_verify(user, request.form.get('totp_code')):
                session.pop('pending_2fa_uid', None)
                _establish_session(user)
                return redirect(_safe_next(request.form.get('next')))
            else:
                locked_until = user_note_failed_login(user['id'])
                if locked_until:
                    session.pop('pending_2fa_uid', None)
                    error = f'Too many failed codes. This account is locked for {LOGIN_LOCKOUT_MINUTES} minutes.'
                else:
                    error = 'That code was not correct. Try again, or use one of your backup codes.'
        else:
            # ---- Step 1: username + password ----
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            user = user_get_by_username(username)
            # Run check_password_hash even on a miss (against a dummy hash) so
            # a nonexistent username doesn't respond measurably faster than a
            # wrong password -- that timing gap would otherwise leak which
            # usernames exist.
            pw_ok = check_password_hash(user['password_hash'] if user else _DUMMY_PW_HASH, password)
            locked_secs = user_lockout_remaining(user) if user else 0
            if locked_secs:
                # Deliberately the same message whether or not the password was
                # right -- confirming "correct password, but locked" would tell
                # an attacker they've found valid credentials.
                error = f'Too many failed attempts. Try again in {max(1, locked_secs // 60)} minute(s).'
            elif user and pw_ok and user['is_active']:
                if user['totp_enabled']:
                    # Hold the session in a half-authenticated state: enough to
                    # remember WHO is signing in, not enough to reach anything.
                    session['pending_2fa_uid'] = user['id']
                    session['pending_2fa_next'] = _safe_next(request.form.get('next'))
                    pending_uid = user['id']
                else:
                    _establish_session(user)
                    return redirect(_safe_next(request.form.get('next')))
            elif user and not user['is_active']:
                error = 'This account is disabled or waiting for admin approval.'
            else:
                if user:
                    user_note_failed_login(user['id'])
                error = 'Incorrect username or password.'
    # GET /login → send people to the landing page (login lives there now).
    # POST failures and the 2FA step re-render the landing with the form state
    # so the user never leaves the marketing + sign-in experience.
    if request.method == 'GET' and not pending_uid and not error:
        nxt = request.args.get('next', '/')
        if nxt and nxt != '/':
            return redirect('/?next=' + nxt + '#signin')
        return redirect('/#signin')

    nxt = request.form.get('next') or request.args.get('next', '/')
    nxt = _safe_next(nxt)
    brand = load_branding()
    return render_template('landing.html',
                           brand_name=brand['name'],
                           brand_tagline=brand['tagline'],
                           brand_theme=brand['theme_colors'],
                           landing_enabled=brand['landing_enabled'],
                           login_error=error,
                           register_error=None,
                           register_success=None,
                           pending_2fa=bool(pending_uid),
                           next_url=nxt,
                           auth_panel='signin' if (error or pending_uid) else None)


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Self-service registration. Accounts are created inactive until an admin enables them."""
    from pipeline import load_branding
    brand = load_branding()
    if request.method == 'GET':
        return redirect('/#register')

    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    password2 = request.form.get('password2') or ''
    error = None
    success = None
    if password != password2:
        error = 'Passwords do not match.'
    else:
        ok, err = user_create(username, password, role='user', active=False)
        if ok:
            success = ('Account requested. An administrator must approve it before you can sign in.')
        else:
            error = err

    return render_template(
        'landing.html',
        brand_name=brand['name'],
        brand_tagline=brand['tagline'],
        brand_theme=brand['theme_colors'],
        landing_enabled=brand['landing_enabled'],
        login_error=error if not success else None,
        register_error=error if not success else None,
        register_success=success,
        pending_2fa=False,
        next_url='/',
        auth_panel='register' if not success else 'register-done',
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ---- Two-factor enrolment (self-service, any signed-in account) ----
# Deliberately not admin-gated: 2FA protects the individual account, so every
# user manages their own. An admin can only turn someone else's OFF (see
# /admin/users/<uid>/2fa/reset below) for the lost-phone case -- they can't
# enrol on another user's behalf, since that would mean the admin holding a
# secret that's supposed to be the user's alone.
@app.route('/api/admin/pending-users')
def api_admin_pending_users():
    """Accounts waiting for admin approval, for the dashboard's Pending
    access requests card. Admin-only, same as anything else touching the
    account list.

    There's no schema column distinguishing "self-registered, never yet
    approved" from "an admin manually disabled a former employee" -- both
    just end up is_active=0. Treating every disabled account as "pending"
    would be actively misleading (a deliberately offboarded account isn't
    something needing action), so this narrows to is_active=0 AND
    last_login IS NULL as a reasonable proxy: an account that's never once
    been used is far more likely still awaiting its first approval than
    one an admin turned off after someone had already been using it."""
    if session.get('role') != 'admin':
        return jsonify(ok=False, error='Admin access required.'), 403
    pending = [u for u in user_list() if not u['is_active'] and not u['last_login']]
    return jsonify(ok=True, count=len(pending),
                   items=[{'username': u['username'], 'created_at': u['created_at']} for u in pending[:5]])

@app.route('/api/2fa/status')
def api_2fa_status():
    if not session.get('authed'):
        return jsonify(ok=False, error='Not signed in.'), 403
    user = user_get(session['user_id'])
    return jsonify(ok=True, enabled=bool(user and user['totp_enabled']),
                   available=totp_available(),
                   backup_remaining=len([c for c in (user['totp_backup_codes'] or '').split('\n') if c]) if user else 0)

@app.route('/api/2fa/begin', methods=['POST'])
def api_2fa_begin():
    """Generates a fresh secret and returns it plus a scannable QR. Does NOT
    enable 2FA -- see api_2fa_confirm. Re-running this before confirming just
    replaces the pending secret, so restarting a half-finished setup is safe."""
    if not session.get('authed'):
        return jsonify(ok=False, error='Not signed in.'), 403
    if not totp_available():
        return jsonify(ok=False, error='2FA support is not installed on this server (pip install pyotp qrcode).'), 501
    secret, uri = user_totp_begin_enrolment(session['user_id'])
    if not secret:
        return jsonify(ok=False, error='Could not start 2FA setup.'), 500
    qr_data_uri = None
    try:
        import qrcode, io, base64
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        qr_data_uri = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        # A missing/broken qrcode lib shouldn't block enrolment -- the secret
        # can still be typed into the app by hand, so this degrades to
        # manual entry rather than failing outright.
        print(f'2FA QR render failed (falling back to manual entry): {e}')
    return jsonify(ok=True, secret=secret, uri=uri, qr=qr_data_uri)

@app.route('/api/2fa/confirm', methods=['POST'])
def api_2fa_confirm():
    """Verifies a code against the pending secret and, on success, switches 2FA
    on and returns the one-time backup codes. These are the ONLY time the
    plaintext codes exist -- only their hashes are stored."""
    if not session.get('authed'):
        return jsonify(ok=False, error='Not signed in.'), 403
    data = request.get_json(silent=True) or {}
    codes, err = user_totp_confirm(session['user_id'], data.get('code'))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, backup_codes=codes)

@app.route('/api/2fa/disable', methods=['POST'])
def api_2fa_disable():
    """Turns 2FA off for the signed-in account. Requires a currently-valid
    code (or backup code) rather than just a session -- otherwise anyone who
    got hold of an already-signed-in browser could quietly strip the second
    factor off the account."""
    if not session.get('authed'):
        return jsonify(ok=False, error='Not signed in.'), 403
    user = user_get(session['user_id'])
    if not user or not user['totp_enabled']:
        return jsonify(ok=True)   # already off; nothing to do
    data = request.get_json(silent=True) or {}
    if not user_totp_verify(user, data.get('code')):
        return jsonify(ok=False, error='Enter a current code from your authenticator app (or a backup code) to turn 2FA off.'), 400
    user_totp_disable(session['user_id'])
    # Other sessions end (see user_totp_disable); this one, which just proved the code, carries on.
    session['sv'] = (user_get(session['user_id']) or {}).get('session_version') or 0
    return jsonify(ok=True)


# ---- User accounts (PostgreSQL) ----
# Simple username/password accounts with an admin/user role, replacing the
# old shared passphrase. Stored in the same PostgreSQL database as everything
# else (users, groups, group_permissions -- see schema.sql).

# Every permission a group can be granted, one per gated tab. Deliberately
# does NOT include Docs (informational, no access implications), or
# Config/API/admin-users (those stay exclusively role='admin', unaffected by
# groups, same as before this feature existed -- a group granting every
# permission below still isn't the same thing as being an admin).
#
# 'text_to_speech' covers both the standalone Text to Speech tab AND the
# promo generator's narration section, which share the same underlying
# /api/vo/preview endpoint -- see require_permission's OR-semantics below.
# The Manage Voices sub-tab (registering/deleting shared voices) stays
# admin-only regardless of this permission, same reasoning as Config: it's a
# shared global resource, not a per-job action.
AVAILABLE_PERMISSIONS = [
    ('promo_generation', 'Generate Promo Plug'),
    ('vertical_shorts', 'Vertical Shorts'),
    ('schedule_plug', 'Schedule Plug'),
    ('music_generation', 'Music Generation'),
    ('text_to_sfx', 'Text to SFX'),
    ('text_to_speech', 'Text to Speech (voice cloning/narration)'),
    ('speech_to_text', 'Speech to Text'),
    ('scene_detection', 'Scene Detection'),
    ('ai_chat', 'AI Chat'),
    ('player', 'Player'),
    ('docs', 'Docs'),
]
_PERMISSION_KEYS = {k for k, _ in AVAILABLE_PERMISSIONS}
# Any tab that works with media. The network shares, the staged-file folder
# (/uploads/) and the probe/convert helpers serve all of them, so they need one
# of these -- not AI Chat or Docs alone.
MEDIA_PERMISSIONS = tuple(k for k, _ in AVAILABLE_PERMISSIONS if k not in ('ai_chat', 'docs'))

def users_db_init():
    # The tables themselves come from schema.sql. Notes on columns that live
    # there, kept here because the behaviour is this module's:
    #
    # An account may be in any number of groups (user_groups). Its tabs are
    # every tab any of them grants; an account in no group gets only the
    # ungated ones (Dashboard, and Docs: NO_GROUP_PERMISSIONS). Admins are not
    # limited by groups.
    #
    # There are no FOREIGN KEYs from user_groups / group_permissions to users
    # or groups, matching the SQLite schema this replaced (which never enforced
    # one). Deleting a group or an account removes its rows explicitly
    # (group_delete, user_delete below).
    database.init_schema()
    conn = database.connect()
    count = conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
    if count == 0:
        # Bootstrap: no accounts exist yet, so create a default admin rather
        # than locking the first deploy out entirely. Override with
        # ADMIN_USERNAME/ADMIN_PASSWORD env vars; otherwise this falls back to
        # a random one-time password, printed below.
        username = (os.environ.get('ADMIN_USERNAME', '').strip() or 'admin')
        password = os.environ.get('ADMIN_PASSWORD', '').strip() or secrets.token_urlsafe(12)
        conn.execute(
            'INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (%s,%s,%s,1,%s)',
            (username, generate_password_hash(password), 'admin', time.time()))
        conn.commit()
        print('=' * 64)
        print(' * No user accounts found -- created a default admin account:')
        print(f'     username: {username}')
        print(f'     password: {password}')
        print('   This password is random and shown only now. Sign in, add real')
        print('   accounts from Admin > Users, and change it (Admin > Users > Set).')
        print('=' * 64)
    conn.close()

def user_get_by_username(username):
    if not username:
        return None
    conn = database.connect()
    row = conn.execute('SELECT * FROM users WHERE username=%s', (username,)).fetchone()
    conn.close()
    return dict(row) if row else None

def user_get(uid):
    conn = database.connect()
    row = conn.execute('SELECT * FROM users WHERE id=%s', (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def user_list():
    """Every account, with group_ids / group_names: the groups it is in, by name."""
    conn = database.connect()
    rows = conn.execute(
        'SELECT u.id, u.username, u.role, u.is_active, u.created_at, u.last_login, u.totp_enabled '
        'FROM users u ORDER BY LOWER(u.username)'
    ).fetchall()
    links = conn.execute(
        'SELECT ug.user_id, g.id, g.name FROM user_groups ug JOIN groups g ON g.id = ug.group_id '
        'ORDER BY LOWER(g.name)').fetchall()
    conn.close()
    by_user = {}
    for r in links:
        by_user.setdefault(r['user_id'], []).append((r['id'], r['name']))
    out = []
    for r in rows:
        u = dict(r)
        mine = by_user.get(u['id'], [])
        u['group_ids'] = [g for g, _ in mine]
        u['group_names'] = [n for _, n in mine]
        out.append(u)
    return out

def user_count_active_admins(exclude_id=None):
    conn = database.connect()
    rows = conn.execute("SELECT id FROM users WHERE role='admin' AND is_active=1").fetchall()
    conn.close()
    return len([r for r in rows if r['id'] != exclude_id])

# ---- Password policy ----
# 12 characters rather than the previous 6. Six is trivially brute-forcible
# and was only ever defensible on a LAN-only deployment; anything reachable
# from outside needs a length that actually costs something to guess. Length
# is deliberately the only hard requirement -- forced character-class rules
# reliably produce "Password1!" rather than genuinely stronger secrets, and
# NIST's own guidance now recommends against them.
MIN_PASSWORD_LENGTH = int(os.environ.get('MIN_PASSWORD_LENGTH', 12))

def _password_policy_error(password):
    """Returns (ok, reason)."""
    pw = password or ''
    if len(pw) < MIN_PASSWORD_LENGTH:
        return False, f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
    if pw.lower() in ('password', 'password123', '123456789012', 'administrator'):
        return False, 'That password is too common -- pick something less guessable.'
    return True, None

_USERNAME_OK = re.compile(r'^[A-Za-z0-9._@-]+$')

def user_create(username, password, role='user', active=True):
    """Create an account. `active=False` is used for self-registration (pending admin approval)."""
    username = (username or '').strip()
    if not username:
        return False, 'Username is required.'
    if len(username) < 3:
        return False, 'Username must be at least 3 characters.'
    if len(username) > 64:
        return False, 'Username is too long.'
    if not _USERNAME_OK.match(username):
        # Usernames are shown all over the app and in the audit log; markup
        # characters in one are never legitimate.
        return False, 'Use only letters, digits, dots, dashes, underscores or @ in a username.'
    ok, why = _password_policy_error(password)
    if not ok:
        return False, why
    if role not in ('admin', 'user'):
        role = 'user'
    conn = database.connect()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (%s,%s,%s,%s,%s)',
            (username, generate_password_hash(password), role, 1 if active else 0, time.time()))
        conn.commit()
        return True, None
    except database.IntegrityError:
        return False, 'That username is already taken.'
    finally:
        conn.close()

def user_set_password(uid, password):
    ok, why = _password_policy_error(password)
    if not ok:
        return False, why
    conn = database.connect()
    conn.execute('UPDATE users SET password_hash=%s, session_version = session_version + 1 WHERE id=%s',
                 (generate_password_hash(password), uid))
    conn.commit()
    conn.close()
    return True, None

def user_set_role(uid, role):
    if role not in ('admin', 'user'):
        return False, 'Invalid role.'
    if role == 'user' and user_count_active_admins(exclude_id=uid) == 0:
        target = user_get(uid)
        if target and target['role'] == 'admin' and target['is_active']:
            return False, "Can't remove the last active admin."
    conn = database.connect()
    conn.execute('UPDATE users SET role=%s WHERE id=%s', (role, uid))
    conn.commit()
    conn.close()
    return True, None

def user_set_active(uid, active):
    if not active and user_count_active_admins(exclude_id=uid) == 0:
        target = user_get(uid)
        if target and target['role'] == 'admin':
            return False, "Can't disable the last active admin."
    conn = database.connect()
    conn.execute('UPDATE users SET is_active=%s, session_version = session_version + %s WHERE id=%s',
                 (1 if active else 0, 0 if active else 1, uid))
    conn.commit()
    conn.close()
    return True, None

def user_delete(uid):
    target = user_get(uid)
    if target and target['role'] == 'admin' and target['is_active'] and user_count_active_admins(exclude_id=uid) == 0:
        return False, "Can't delete the last active admin."
    conn = database.connect()
    conn.execute('DELETE FROM users WHERE id=%s', (uid,))
    conn.execute('DELETE FROM user_groups WHERE user_id=%s', (uid,))
    conn.commit()
    conn.close()
    return True, None

def user_touch_login(uid):
    conn = database.connect()
    # A successful sign-in clears any accumulated failure count and lockout --
    # otherwise a user who fumbled their password a few times would stay one
    # mistake away from a lockout indefinitely.
    conn.execute('UPDATE users SET last_login=%s, failed_logins=0, locked_until=NULL WHERE id=%s',
                 (time.time(), uid))
    conn.commit()
    conn.close()

# ---- Per-account lockout (complements the per-IP rate limit in core.py) ----
LOGIN_MAX_FAILURES = int(os.environ.get('LOGIN_MAX_FAILURES', 10))
LOGIN_LOCKOUT_MINUTES = int(os.environ.get('LOGIN_LOCKOUT_MINUTES', 15))

def user_note_failed_login(uid):
    """Counts a failed attempt against the ACCOUNT and locks it temporarily
    once the threshold is hit. The existing per-IP limiter can't see a
    distributed attempt (many source IPs, one target account), which is the
    realistic shape of an attack on a public-facing login."""
    conn = database.connect()
    row = conn.execute('SELECT failed_logins FROM users WHERE id=%s', (uid,)).fetchone()
    fails = (row['failed_logins'] if row else 0) + 1
    locked_until = None
    if fails >= LOGIN_MAX_FAILURES:
        locked_until = time.time() + LOGIN_LOCKOUT_MINUTES * 60
        fails = 0   # reset the counter so the next lockout needs a fresh run of failures
    conn.execute('UPDATE users SET failed_logins=%s, locked_until=%s WHERE id=%s', (fails, locked_until, uid))
    conn.commit()
    conn.close()
    return locked_until

def user_lockout_remaining(user):
    """Seconds left on an active lockout, or 0 if not locked."""
    if not user or not user['locked_until']:
        return 0
    return max(0, int(user['locked_until'] - time.time()))

# ---- TOTP two-factor (Google Authenticator / Authy / 1Password compatible) ----
def _totp_lib():
    """pyotp imported lazily so the app still starts (with 2FA simply
    unavailable) on a deployment that hasn't installed it yet, rather than
    failing at import time and taking the whole service down."""
    try:
        import pyotp
        return pyotp
    except ImportError:
        return None

def totp_available():
    return _totp_lib() is not None

def user_totp_begin_enrolment(uid):
    """Generates and stores a fresh secret WITHOUT enabling 2FA yet, and
    returns (secret, otpauth_uri). Enabling only happens once the user proves
    they can produce a valid code from it (see user_totp_confirm) -- so
    abandoning enrolment halfway leaves the account exactly as it was."""
    pyotp = _totp_lib()
    if not pyotp:
        return None, None
    secret = pyotp.random_base32()
    conn = database.connect()
    row = conn.execute('SELECT username FROM users WHERE id=%s', (uid,)).fetchone()
    username = row['username'] if row else 'user'
    conn.execute('UPDATE users SET totp_secret=%s, totp_enabled=0 WHERE id=%s', (secret, uid))
    conn.commit()
    conn.close()
    try:
        issuer = load_branding()['name']
    except Exception:
        issuer = 'PRISM'
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
    return secret, uri

def _hash_backup_code(code):
    return generate_password_hash(code.strip().upper())

def user_totp_confirm(uid, code):
    """Verifies `code` against the pending secret and, on success, switches 2FA
    on and returns freshly generated one-time backup codes. Returns (codes,
    error) -- codes are shown to the user exactly once here, since only their
    hashes are stored."""
    pyotp = _totp_lib()
    if not pyotp:
        return None, '2FA support is not installed on this server (pip install pyotp).'
    conn = database.connect()
    row = conn.execute('SELECT totp_secret FROM users WHERE id=%s', (uid,)).fetchone()
    if not row or not row['totp_secret']:
        conn.close()
        return None, 'Start setup again -- no pending 2FA secret for this account.'
    # valid_window=1 accepts the adjacent 30s step either side, which absorbs
    # ordinary clock drift between the phone and the server without meaningfully
    # widening the guess space.
    if not pyotp.TOTP(row['totp_secret']).verify((code or '').strip(), valid_window=1):
        conn.close()
        return None, 'That code was not correct. Check your phone and try again.'
    codes = [secrets.token_hex(4).upper() for _ in range(10)]
    hashed = '\n'.join(_hash_backup_code(c) for c in codes)
    conn.execute('UPDATE users SET totp_enabled=1, totp_backup_codes=%s WHERE id=%s', (hashed, uid))
    conn.commit()
    conn.close()
    return codes, None

def user_totp_verify(user, code):
    """Checks `code` against the account's TOTP secret, then against its unused
    backup codes. A matching backup code is CONSUMED (removed from the stored
    list) so it genuinely works only once."""
    pyotp = _totp_lib()
    code = (code or '').strip()
    if not code or not user or not user['totp_secret']:
        return False
    if pyotp and pyotp.TOTP(user['totp_secret']).verify(code, valid_window=1):
        return True
    stored = (user['totp_backup_codes'] or '').split('\n')
    for h in stored:
        if h and check_password_hash(h, code.upper()):
            remaining = '\n'.join(x for x in stored if x != h)
            conn = database.connect()
            conn.execute('UPDATE users SET totp_backup_codes=%s WHERE id=%s', (remaining, user['id']))
            conn.commit()
            conn.close()
            return True
    return False

def user_totp_disable(uid):
    """Also ends the account's sessions: a reset is for a lost or stolen phone."""
    conn = database.connect()
    conn.execute('UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_backup_codes=NULL, '
                 'session_version = session_version + 1 WHERE id=%s', (uid,))
    conn.commit()
    conn.close()

def _ids(values):
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return out

def user_groups(uid):
    """The ids of the groups an account is in."""
    conn = database.connect()
    rows = conn.execute('SELECT group_id FROM user_groups WHERE user_id=%s', (uid,)).fetchall()
    conn.close()
    return {r['group_id'] for r in rows}

def user_set_groups(uid, group_ids):
    """Puts an account in exactly these groups (an empty list: none)."""
    gids = _ids(group_ids)
    conn = database.connect()
    if not conn.execute('SELECT 1 FROM users WHERE id=%s', (uid,)).fetchone():
        conn.close()
        return False, 'No such account.'
    known = {r['id'] for r in conn.execute('SELECT id FROM groups').fetchall()}
    if gids - known:
        conn.close()
        return False, 'No such group.'
    conn.execute('DELETE FROM user_groups WHERE user_id=%s', (uid,))
    conn.executemany('INSERT INTO user_groups (user_id, group_id) VALUES (%s,%s)', [(uid, g) for g in sorted(gids)])
    conn.commit()
    conn.close()
    return True, None

def user_set_group(uid, group_id):
    """The one-group form, kept for the older admin page and API: the account
    ends up in only this group, or in none for None/0/''."""
    return user_set_groups(uid, [group_id] if group_id else [])

def group_set_members(gid, user_ids):
    """Makes exactly these accounts the group's members, leaving their other groups alone."""
    uids = _ids(user_ids)
    conn = database.connect()
    if not conn.execute('SELECT 1 FROM groups WHERE id=%s', (gid,)).fetchone():
        conn.close()
        return False, 'No such group.'
    known = {r['id'] for r in conn.execute('SELECT id FROM users').fetchall()}
    if uids - known:
        conn.close()
        return False, 'No such account.'
    conn.execute('DELETE FROM user_groups WHERE group_id=%s', (gid,))
    conn.executemany('INSERT INTO user_groups (user_id, group_id) VALUES (%s,%s)', [(u, gid) for u in sorted(uids)])
    conn.commit()
    conn.close()
    return True, None

# ---- Groups (per-tab access control) ----
def group_list():
    """Every group with its permission set and member count, for the
    Groups admin panel."""
    conn = database.connect()
    groups = [dict(r) for r in conn.execute('SELECT id, name, created_at FROM groups ORDER BY LOWER(name)')]
    for g in groups:
        perms = conn.execute('SELECT permission FROM group_permissions WHERE group_id=%s', (g['id'],)).fetchall()
        g['permissions'] = sorted(p['permission'] for p in perms)
        g['member_ids'] = sorted(r['user_id'] for r in conn.execute(
            'SELECT user_id FROM user_groups WHERE group_id=%s', (g['id'],)).fetchall())
        g['member_count'] = len(g['member_ids'])
    conn.close()
    return groups

def group_get(gid):
    conn = database.connect()
    row = conn.execute('SELECT id, name, created_at FROM groups WHERE id=%s', (gid,)).fetchone()
    if not row:
        conn.close()
        return None
    g = dict(row)
    perms = conn.execute('SELECT permission FROM group_permissions WHERE group_id=%s', (gid,)).fetchall()
    g['permissions'] = sorted(p['permission'] for p in perms)
    conn.close()
    return g

def group_create(name):
    name = (name or '').strip()
    if not name:
        return False, 'Enter a name for the group.', None
    conn = database.connect()
    try:
        cur = conn.execute('INSERT INTO groups (name, created_at) VALUES (%s,%s) RETURNING id',
                           (name, time.time()))
        gid = cur.fetchone()['id']
        conn.commit()
        return True, None, gid
    except database.IntegrityError:
        return False, 'A group with that name already exists.', None
    finally:
        conn.close()

def group_set_permissions(gid, permissions):
    """Replaces a group's whole permission set with `permissions` (any
    subset of AVAILABLE_PERMISSIONS' keys -- unknown keys are dropped rather
    than rejected outright, so a stale checkbox from a future/past version
    of this list can't wedge the save)."""
    perms = sorted(set(permissions or []) & _PERMISSION_KEYS)
    conn = database.connect()
    if not conn.execute('SELECT 1 FROM groups WHERE id=%s', (gid,)).fetchone():
        conn.close()
        return False, 'No such group.'
    conn.execute('DELETE FROM group_permissions WHERE group_id=%s', (gid,))
    conn.executemany('INSERT INTO group_permissions (group_id, permission) VALUES (%s,%s)',
                      [(gid, p) for p in perms])
    conn.commit()
    conn.close()
    return True, None

def group_rename(gid, name):
    name = (name or '').strip()
    if not name:
        return False, 'Enter a name for the group.'
    conn = database.connect()
    try:
        conn.execute('UPDATE groups SET name=%s WHERE id=%s', (name, gid))
        conn.commit()
        return True, None
    except database.IntegrityError:
        return False, 'A group with that name already exists.'
    finally:
        conn.close()

def group_delete(gid):
    """Its members leave it and keep their other groups (one left in no group
    keeps only NO_GROUP_PERMISSIONS). Its rows in user_groups and
    group_permissions are removed here, explicitly: there is no FK cascade
    (see users_db_init)."""
    conn = database.connect()
    if not conn.execute('SELECT 1 FROM groups WHERE id=%s', (gid,)).fetchone():
        conn.close()
        return False, 'No such group.'
    conn.execute('DELETE FROM user_groups WHERE group_id=%s', (gid,))
    conn.execute('DELETE FROM group_permissions WHERE group_id=%s', (gid,))
    conn.execute('DELETE FROM groups WHERE id=%s', (gid,))
    conn.commit()
    conn.close()
    return True, None

def group_membership():
    """({user id: frozenset of group ids}, {group id: group name}) in two
    queries, for code that decides what groups may see across many records at
    once. An account in no group is absent from the first dict."""
    conn = database.connect()
    users = {}
    for r in conn.execute('SELECT user_id, group_id FROM user_groups').fetchall():
        users.setdefault(r['user_id'], set()).add(r['group_id'])
    groups = {r['id']: r['name'] for r in conn.execute('SELECT id, name FROM groups').fetchall()}
    conn.close()
    return {u: frozenset(g & groups.keys()) for u, g in users.items()}, groups

# What an account in no group may open (the Dashboard is never gated).
NO_GROUP_PERMISSIONS = frozenset({'docs'})

def user_permissions(user_id, role):
    """The permission keys a session has: everything for an admin; for anyone
    else every key any of their groups grants, or NO_GROUP_PERMISSIONS when
    they are in no group at all (an admin puts them in one to give access)."""
    if role == 'admin':
        return set(_PERMISSION_KEYS)
    if not user_id:
        return set()
    conn = database.connect()
    member = conn.execute('SELECT 1 FROM user_groups ug JOIN groups g ON g.id = ug.group_id '
                          'WHERE ug.user_id=%s LIMIT 1', (user_id,)).fetchone()
    if not member:
        conn.close()
        return set(NO_GROUP_PERMISSIONS)
    granted = conn.execute('SELECT DISTINCT gp.permission FROM user_groups ug '
                           'JOIN group_permissions gp ON gp.group_id = ug.group_id WHERE ug.user_id=%s',
                           (user_id,)).fetchall()
    conn.close()
    return {p['permission'] for p in granted} & _PERMISSION_KEYS

def user_can(user_id, role, *permissions):
    """True if the session identified by (user_id, role) may use a feature
    gated behind ANY of the given permission keys (multiple keys = OR, for
    the handful of endpoints two different tabs both depend on -- see
    require_permission's docstring). See user_permissions for who has what."""
    if role == 'admin':
        return True
    if not user_id:
        return False
    granted = user_permissions(user_id, role)
    return any(p in granted for p in permissions)

def require_permission(*permissions):
    """Route decorator: 403s (JSON for /api/, redirect to / otherwise)
    unless the session has at least one of the given permissions per
    user_can() above. Multiple permissions are OR'd together -- needed for
    the couple of endpoints two different tabs both genuinely depend on
    (e.g. /api/vo/preview is the real 'Render narration' action for both
    the standalone Text to Speech tab and the promo generator's narration
    section, so it's gated with require_permission('text_to_speech',
    'promo_generation') rather than picking one and quietly breaking the
    other)."""
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            if not user_can(session.get('user_id'), session.get('role'), *permissions):
                if request.path.startswith('/api/'):
                    return jsonify(error="Your account doesn't have access to this feature."), 403
                return redirect('/')
            return f(*args, **kwargs)
        return wrapped
    return decorator

def admin_reset_if_requested():
    """Recovery path for a lost admin password, independent of whatever's
    already in the users table -- set RESET_ADMIN=1 (plus optionally ADMIN_USERNAME/
    ADMIN_PASSWORD) and restart. Unlike the first-run bootstrap in
    users_db_init(), this runs every startup and always takes effect when the
    flag is set, whether or not accounts already exist: if the named account
    exists it's reset to an active admin with a new password; if not, it's
    created. Meant to be turned back off after use.
    """
    if os.environ.get('RESET_ADMIN', '').strip().lower() not in ('1', 'true', 'yes'):
        return
    username = (os.environ.get('ADMIN_USERNAME', '').strip() or 'admin')
    password = os.environ.get('ADMIN_PASSWORD', '').strip() or secrets.token_urlsafe(12)
    existing = user_get_by_username(username)
    conn = database.connect()
    if existing:
        conn.execute('UPDATE users SET password_hash=%s, role=%s, is_active=1 WHERE id=%s',
                     (generate_password_hash(password), 'admin', existing['id']))
    else:
        conn.execute(
            'INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (%s,%s,%s,1,%s)',
            (username, generate_password_hash(password), 'admin', time.time()))
    conn.commit()
    conn.close()
    print('=' * 64)
    print(f' * RESET_ADMIN was set -- {"reset" if existing else "created"} admin account:')
    print(f'     username: {username}')
    print(f'     password: {password}')
    print('   Remove RESET_ADMIN from the environment and restart, or it will')
    print('   reset this account back to this password on every future boot.')
    print('=' * 64)

users_db_init()
admin_reset_if_requested()

# ---- Admin: user management ----
# Config > Users drives everything below through JSON. The old standalone,
# server-rendered /admin/users page and its form-posting routes were removed:
# nothing used them any more, and the page wrote usernames into its HTML
# unescaped (a self-registered username could run script as an admin).
@app.route('/admin/users')
def admin_users():
    """Old bookmarks land in the app, where Config > Users is one click away."""
    return redirect('/')


# ---- Admin: user management, JSON API ----
# Config > Users is a fetch-driven tab in the main app shell, like every
# other Config tab.
#
# All under /admin/ so _access_control's existing admin-only gate covers
# these automatically, same as everything else in this section.
def _user_json(u, current_uid):
    return {
        'id': u['id'], 'username': u['username'], 'role': u['role'],
        'is_active': bool(u['is_active']), 'group_ids': u['group_ids'],
        'group_names': u['group_names'], 'totp_enabled': bool(u['totp_enabled']),
        'created_at': u['created_at'], 'last_login': u['last_login'],
        'is_you': u['id'] == current_uid,
    }

def _group_json(g):
    return {'id': g['id'], 'name': g['name'], 'member_count': g['member_count'],
            'member_ids': g['member_ids'], 'permissions': sorted(g['permissions'])}

@app.route('/admin/api/users')
def admin_api_users():
    return jsonify(ok=True,
                    users=[_user_json(u, session.get('user_id')) for u in user_list()],
                    groups=[_group_json(g) for g in group_list()],
                    permissions=[{'key': k, 'label': label} for k, label in AVAILABLE_PERMISSIONS],
                    current_user_id=session.get('user_id'))

@app.route('/admin/api/users/create', methods=['POST'])
def admin_api_users_create():
    d = request.get_json(silent=True) or {}
    ok, err = user_create(d.get('username', ''), d.get('password', ''), d.get('role', 'user'))
    return jsonify(ok=ok, error=None if ok else err, notice='User created.' if ok else None)

@app.route('/admin/api/users/<int:uid>/role', methods=['POST'])
def admin_api_users_role(uid):
    d = request.get_json(silent=True) or {}
    ok, err = user_set_role(uid, d.get('role', 'user'))
    return jsonify(ok=ok, error=None if ok else err, notice='Role updated.' if ok else None)

@app.route('/admin/api/users/<int:uid>/toggle', methods=['POST'])
def admin_api_users_toggle(uid):
    target = user_get(uid)
    if not target:
        return jsonify(ok=False, error='User not found.')
    ok, err = user_set_active(uid, not target['is_active'])
    return jsonify(ok=ok, error=None if ok else err, notice='Status updated.' if ok else None)

@app.route('/admin/api/users/<int:uid>/2fa/reset', methods=['POST'])
def admin_api_users_2fa_reset(uid):
    target = user_get(uid)
    if not target:
        return jsonify(ok=False, error='No such account.')
    user_totp_disable(uid)
    return jsonify(ok=True, notice=f"Two-factor authentication cleared for {target['username']} — "
                                    "they can set it up again from Config > Security.")

@app.route('/admin/api/users/<int:uid>/password', methods=['POST'])
def admin_api_users_password(uid):
    d = request.get_json(silent=True) or {}
    ok, err = user_set_password(uid, d.get('password', ''))
    return jsonify(ok=ok, error=None if ok else err, notice='Password updated.' if ok else None)

@app.route('/admin/api/users/<int:uid>/delete', methods=['POST'])
def admin_api_users_delete(uid):
    if uid == session.get('user_id'):
        return jsonify(ok=False, error="You can't delete your own account while signed in as it.")
    ok, err = user_delete(uid)
    return jsonify(ok=ok, error=None if ok else err, notice='User deleted.' if ok else None)

@app.route('/admin/api/users/<int:uid>/group', methods=['POST'])
def admin_api_users_group(uid):
    d = request.get_json(silent=True) or {}
    raw = str(d.get('group_id', '') or '').strip()
    gid = int(raw) if raw.isdigit() else None
    ok, err = user_set_group(uid, gid)
    return jsonify(ok=ok, error=None if ok else err, notice='Group updated.' if ok else None)

@app.route('/admin/api/users/<int:uid>/groups', methods=['POST'])
def admin_api_users_groups(uid):
    """{group_ids: [...]}: the account is in exactly these groups."""
    d = request.get_json(silent=True) or {}
    ok, err = user_set_groups(uid, d.get('group_ids') or [])
    return jsonify(ok=ok, error=None if ok else err, notice='Groups updated.' if ok else None)

@app.route('/admin/api/groups/<int:gid>/members', methods=['POST'])
def admin_api_groups_members(gid):
    """{user_ids: [...]}: exactly these accounts are the group's members."""
    d = request.get_json(silent=True) or {}
    ok, err = group_set_members(gid, d.get('user_ids') or [])
    return jsonify(ok=ok, error=None if ok else err, notice='Members saved.' if ok else None)

@app.route('/admin/api/groups/create', methods=['POST'])
def admin_api_groups_create():
    d = request.get_json(silent=True) or {}
    ok, err, _gid = group_create(d.get('name', ''))
    return jsonify(ok=ok, error=None if ok else err,
                    notice='Group created -- check its permissions below, then assign accounts to it.' if ok else None)

@app.route('/admin/api/groups/<int:gid>/permissions', methods=['POST'])
def admin_api_groups_permissions(gid):
    d = request.get_json(silent=True) or {}
    ok, err = group_set_permissions(gid, d.get('permissions') or [])
    return jsonify(ok=ok, error=None if ok else err, notice='Permissions saved.' if ok else None)

@app.route('/admin/api/groups/<int:gid>/delete', methods=['POST'])
def admin_api_groups_delete(gid):
    ok, err = group_delete(gid)
    return jsonify(ok=ok, error=None if ok else err,
                    notice='Group deleted. Its members keep their other groups.' if ok else None)

