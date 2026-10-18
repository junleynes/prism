"""App instance, session/cookie config, and the request-level access gate.

Everything else in this app (auth, the trailer library, the media pipeline)
imports `app` from here. Kept dependency-free of the other modules so there's
no risk of circular imports.
"""
import os, re, shutil, tempfile, threading, time, secrets, hmac
from collections import deque
from flask import Flask, request, jsonify, session, redirect, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import generate_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

if os.environ.get('TRUST_PROXY_HEADERS', '').lower() in ('1', 'true', 'yes'):
    from werkzeug.middleware.proxy_fix import ProxyFix
    _proxy_hops = int(os.environ.get('TRUST_PROXY_HOPS', 1))
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops,
                             x_host=_proxy_hops, x_prefix=_proxy_hops)
UPLOAD_DIR_PREFIX = 'prism_up_'


# Runs before this module's own folder is made, so it is a plain pattern: what
# tempfile.mkdtemp() named its folders before the prefix above existed.
_LEGACY_DIR = re.compile(r'^tmp[a-z0-9_]{8}$')
_HEARTBEAT_EVERY = 300          # a running instance touches its .pid this often...
_HEARTBEAT_STALE = 1800         # ...so one untouched this long belongs to a run that is gone
_IDLE_LEGACY = 6 * 3600         # an old-style folder nothing in has changed for this long is abandoned


def _newest_and_size(path):
    newest, size = os.path.getmtime(path), 0
    for dp, _dn, fn in os.walk(path):
        for x in fn:
            try:
                st = os.stat(os.path.join(dp, x))
            except OSError:
                continue
            newest, size = max(newest, st.st_mtime), size + st.st_size
    return newest, size


# The names PRISM gives the files it keeps in an upload folder. An old-style folder (tmpXXXXXXXX) in a folder
# other programs also use is only taken for PRISM's when it holds one of these.
PRISM_FILE_PREFIXES = ('net_', 'thumb_', 'src_', 'shclip_', 'shpart_', 'sfx_', 'voedit_', 'tts_', 'stt_', 'matched_',
                       'woosh_sfx_', 'with_sfx_', 'vorefsrc_', 'voready_', 'voprevref_', 'vopreview_', 'voiceclone_',
                       'vo_stt_', 'ttsref_', 'trailer_', 'titlecard_', 'endcard_', 'sotnorm_', 'sildet_', 'she_',
                       'seg_', 'score_', 'parts_', 'norm_', 'lb_', 'gen_', 'finalmix_', 'export_', 'bgmready_',
                       'bgmfit_', 'beat_video_', 'cardvo_', 'votight_', 'schedmaster_')


def _holds_prism_files(path):
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(n.startswith(PRISM_FILE_PREFIXES) for n in names)


def stale_upload_dirs(parent, keep=None, legacy=False, now=None):
    """[(path, bytes)] of the per-run upload folders under `parent` that no
    running instance owns.

    Each run makes a fresh folder and nothing used to remove the old ones, so
    every restart or crash orphaned whatever staging and render files were in
    it -- the way the temp folder grew to 1.8 TB. A prism_up_* folder is live
    while its .pid file is being touched (see _claim_upload_folder), and stale
    once it has not been for half an hour. (Not by asking whether the process
    exists: process ids are reused, quickly on Windows, and signalling a pid
    there can kill it.) With `legacy`, folders named the way mkdtemp() named
    them before this existed (tmpXXXXXXXX) are included once nothing in them
    has changed for six hours: in the folder set in UPLOAD_TEMP_DIR, which
    the deployment gave to this app, whatever they hold (`legacy=True`); in
    a folder other programs use too, such as the system temp folder, only
    those holding files named the way PRISM names its own (`legacy='signature'`)."""
    now = time.time() if now is None else now
    out = []
    try:
        names = os.listdir(parent)
    except OSError:
        return out
    for name in names:
        p = os.path.join(parent, name)
        if p == keep or os.path.islink(p) or not os.path.isdir(p):
            continue
        try:
            if name.startswith(UPLOAD_DIR_PREFIX):
                try:
                    beat = os.path.getmtime(os.path.join(p, '.pid'))
                except OSError:
                    beat = None
                if beat is not None:
                    if now - beat < _HEARTBEAT_STALE:
                        continue
                    size = _newest_and_size(p)[1]
                else:
                    newest, size = _newest_and_size(p)       # no marker: only once it has sat idle a while
                    if now - newest < _IDLE_LEGACY:
                        continue
            elif legacy and _LEGACY_DIR.match(name):
                # legacy True: the folder was given to this app (UPLOAD_TEMP_DIR). 'signature': a folder
                # shared with other programs (the system temp folder), where it must also hold PRISM's files.
                if legacy == 'signature' and not _holds_prism_files(p):
                    continue
                newest, size = _newest_and_size(p)
                if now - newest < _IDLE_LEGACY:
                    continue
            else:
                continue
        except OSError:
            continue
        out.append((p, size))
    return out


def reap_stale_upload_dirs(parent, keep=None, legacy=False):
    """Deletes what stale_upload_dirs finds. Returns bytes freed."""
    freed = 0
    for p, size in stale_upload_dirs(parent, keep, legacy):
        shutil.rmtree(p, ignore_errors=True)
        if not os.path.exists(p):
            freed += size
    if freed:
        print(f'Removed {freed / (1024**3):.2f} GB of upload folders left by earlier runs')
    return freed


app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp(prefix=UPLOAD_DIR_PREFIX)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024

_upload_temp_dir = os.environ.get('UPLOAD_TEMP_DIR', '').strip()
if _upload_temp_dir:
    # A deployment's system drive can genuinely run out of space from this
    # folder alone -- it holds every uploaded/staged source file plus every
    # intermediate processing file for as long as a render takes. Explicitly
    # setting this env var redirects all of that to a chosen drive/folder
    # (e.g. a data drive with real headroom) instead of wherever the OS
    # default temp location happens to be, which on Windows is normally on
    # the system drive regardless of where the app itself is installed.
    #
    # Still uses mkdtemp() (a fresh, randomly-named subfolder under the
    # chosen parent) rather than the chosen folder directly -- preserves the
    # existing "clean slate every process start" behavior other code
    # depends on, just relocates WHERE that fresh folder lives.
    try:
        os.makedirs(_upload_temp_dir, exist_ok=True)
        app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp(prefix=UPLOAD_DIR_PREFIX, dir=_upload_temp_dir)
    except OSError as e:
        # Deliberately does not fall back silently: silently using the
        # system temp location anyway is exactly the outcome this setting
        # exists to prevent, so a misconfigured/inaccessible folder needs to
        # be loud about it rather than quietly doing the wrong thing.
        print('=' * 64)
        print(f' UPLOAD_TEMP_DIR is set to "{_upload_temp_dir}" but could not be')
        print(f' created or used: {e}')
        print(' Falling back to the system temp location for this run --')
        print(' fix the path or its permissions, then restart.')
        print('=' * 64)

def upload_parents():
    """[(folder, legacy)] where this app's upload folders live: the folder the
    current one is in, and the system temp folder. Old-style names are looked
    for in both: anywhere in the folder UPLOAD_TEMP_DIR chose, and elsewhere
    only in folders holding PRISM's own files."""
    folder = app.config['UPLOAD_FOLDER']
    parent = os.path.dirname(folder)
    chosen = bool(_upload_temp_dir) and os.path.realpath(parent) == os.path.realpath(_upload_temp_dir)
    out = [(parent, True if chosen else 'signature')]
    if os.path.realpath(tempfile.gettempdir()) != os.path.realpath(parent):
        out.append((tempfile.gettempdir(), 'signature'))
    return out


def _claim_upload_folder():
    """Marks this run's folder live (a .pid file touched every few minutes),
    clears the folders of runs that are gone, and removes our own on a clean
    exit."""
    import atexit
    folder = app.config['UPLOAD_FOLDER']
    marker = os.path.join(folder, '.pid')

    def beat():
        try:
            with open(marker, 'w') as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def loop():
        while True:
            time.sleep(_HEARTBEAT_EVERY)
            beat()
    beat()
    threading.Thread(target=loop, daemon=True, name='upload-heartbeat').start()
    for par, legacy in upload_parents():
        try:
            reap_stale_upload_dirs(par, keep=folder, legacy=legacy)
        except Exception as e:                                  # noqa: BLE001
            print(f'Upload folder cleanup error: {e}')
    atexit.register(lambda: shutil.rmtree(folder, ignore_errors=True))


_claim_upload_folder()

_SECRET_KEY_FILE = os.environ.get('SECRET_KEY_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key'))
try:
    with open(_SECRET_KEY_FILE, 'rb') as _f:
        app.secret_key = _f.read().strip()
    if not app.secret_key:
        raise ValueError('empty key file')
except (FileNotFoundError, ValueError):
    app.secret_key = secrets.token_hex(32).encode()
    try:
        with open(_SECRET_KEY_FILE, 'wb') as _f:
            _f.write(app.secret_key)
        try:
            os.chmod(_SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(f'Could not persist {_SECRET_KEY_FILE} ({e}); sessions will not survive a restart until this is writable.')
app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('SESSION_LIFETIME_DAYS', 1)) * 86400
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FORCE_HTTPS', '').lower() in ('1', 'true', 'yes')


class _SessionInterface(SecureCookieSessionInterface):
    """Marks the session cookie Secure whenever the request came over HTTPS
    (directly, or through a proxy when TRUST_PROXY_HEADERS is on), not only
    when FORCE_HTTPS says so -- so it is never sent back over plain HTTP by
    mistake. Plain-HTTP installs (a LAN with no TLS) still work."""
    def get_cookie_secure(self, app):
        try:
            return bool(app.config['SESSION_COOKIE_SECURE'] or request.is_secure)
        except RuntimeError:                  # no request (tests building a session by hand)
            return bool(app.config['SESSION_COOKIE_SECURE'])


app.session_interface = _SessionInterface()

_PUBLIC_PATHS = {'/', '/login', '/logout', '/register', '/branding/logo', '/branding/logo-dark', '/branding/favicon', '/branding/logo-mark'}
_API_PREFIXES = ('/api/', '/uploads/', '/library/', '/download/')
_ADMIN_PREFIX = '/admin/'

def _client_ip():
    """The address rate limits and the audit log go by. With
    TRUST_PROXY_HEADERS on, ProxyFix (above) has already set remote_addr from
    the hops the proxy added; anything else in X-Forwarded-For was written by
    the client, so it is never read here -- otherwise anyone could dodge the
    login and download limits by sending a different address each time."""
    return request.remote_addr or 'unknown'

class _RateLimiter:
    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self.buckets = {}
        self.lock = threading.Lock()
    def allow(self, key):
        now = time.time()
        with self.lock:
            dq = self.buckets.setdefault(key, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            if len(self.buckets) > 5000:
                stale = [k for k, v in self.buckets.items() if not v or now - v[-1] > self.window * 4]
                for k in stale:
                    self.buckets.pop(k, None)
            return True

_file_route_limiter = _RateLimiter(limit=int(os.environ.get('FILE_ROUTE_RATE_LIMIT', 40)), window=60)
_job_submit_limiter = _RateLimiter(limit=int(os.environ.get('JOB_SUBMIT_RATE_LIMIT', 12)), window=300)
_login_limiter = _RateLimiter(limit=int(os.environ.get('LOGIN_RATE_LIMIT', 8)), window=300)
# Access requests per address per hour: each one stores an account and hashes a password.
_register_limiter = _RateLimiter(limit=int(os.environ.get('REGISTER_RATE_LIMIT', 5)), window=3600)
# Largest request body accepted from someone not signed in (the sign-in and
# access-request forms are a few hundred bytes). Signed-in users keep
# MAX_CONTENT_LENGTH for uploads.
PUBLIC_MAX_BODY = 64 * 1024
_DUMMY_PW_HASH = generate_password_hash(secrets.token_hex(16))

@app.after_request
def _security_headers(resp):
    # Inject one shared, versioned stylesheet into every HTML page. Keeping
    # this at the app layer lets the login page and studio shell share the
    # same visual language without duplicating large CSS blocks in templates.
    if resp.content_type and resp.content_type.startswith('text/html'):
        try:
            body = resp.get_data(as_text=True)
            if '</head>' in body and '/static/ui-modern.css' not in body:
                body = body.replace('</head>', '<link rel="stylesheet" href="/static/ui-modern.css?v=7">\n</head>', 1)
                resp.set_data(body)
        except Exception:
            pass
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=()')
    resp.headers.setdefault('Content-Security-Policy', '; '.join([
        "default-src 'self'", "img-src 'self' data: blob:", "media-src 'self' blob:",
        "script-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:", "connect-src 'self'", "object-src 'none'",
        "base-uri 'self'", "form-action 'self'", "frame-ancestors 'self'",
    ]))
    if request.is_secure:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return resp

def ensure_csrf_token():
    tok = session.get('csrf_token')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['csrf_token'] = tok
    return tok

@app.before_request
def _limit_public_bodies():
    """A signed-out visitor can only reach the sign-in and access-request
    forms, so their requests are capped at PUBLIC_MAX_BODY instead of the
    multi-GB upload limit -- otherwise anyone could fill the server's temp
    disk by posting a huge body to /login. Checked as the body is read, so a
    chunked upload with no Content-Length is stopped too."""
    if not session.get('authed'):
        request.max_content_length = PUBLIC_MAX_BODY
    return None

@app.before_request
def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS') or request.path == '/login':
        return None
    if not session.get('authed'):
        return None
    sent = request.headers.get('X-CSRF-Token') or request.form.get('_csrf') or ''
    expected = session.get('csrf_token') or ''
    if not expected or not hmac.compare_digest(str(sent), str(expected)):
        return jsonify(ok=False, error='Your session token was missing or stale. Reload the page and try again.'), 403
    return None

@app.before_request
def _access_control():
    path = request.path
    if path in _PUBLIC_PATHS or path.startswith('/static/'):
        return None
    if path.startswith(('/uploads/', '/library/', '/download/')) or '/asset/' in path:
        if not _file_route_limiter.allow(_client_ip()):
            return jsonify(error='Too many requests. Slow down.'), 429
    if session.get('authed'):
        if path.startswith(_ADMIN_PREFIX) and session.get('role') != 'admin':
            if path.startswith('/api/'):
                return jsonify(error='Admin access required.'), 403
            return redirect('/')
        return None
    if path.startswith(_API_PREFIXES) or path.startswith(_ADMIN_PREFIX):
        return jsonify(error='Not authenticated. Sign in at /login.'), 401
    return redirect(url_for('login', next=request.full_path if request.query_string else path))

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm'}
