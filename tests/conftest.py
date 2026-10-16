"""
Shared pytest fixtures for the whole suite.

pipeline.py makes a couple of module-level calls out to local services when
imported directly (checking Ollama/Whisper/etc. reachability for the
service-status banner). Patching requests before importing means the test
run doesn't depend on any of those actually being up, and doesn't hang or
error out on a machine that doesn't have them installed at all -- exactly
the situation this sandbox and CI both are in.
"""
import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('LIBRARY_DIR', '/tmp/prism_test_library')
os.environ.setdefault('SECRET_KEY_FILE', '/tmp/prism_test_library/.secret_key')

# The suite runs against a real PostgreSQL database and TRUNCATEs tables, so it
# only ever uses TEST_DATABASE_URL -- never DATABASE_URL, which on a developer
# or server machine points at real data. Create a scratch database first:
#     createdb prism_test
#     export TEST_DATABASE_URL=postgresql://prism:PASSWORD@localhost:5432/prism_test
_test_db = os.environ.get('TEST_DATABASE_URL', '').strip()
if not _test_db:
    pytest.exit('TEST_DATABASE_URL is not set. The tests run against a real PostgreSQL '
                'database and empty its tables, so point it at a scratch database, e.g.\n'
                '    postgresql://prism:PASSWORD@localhost:5432/prism_test', returncode=2)
os.environ['DATABASE_URL'] = _test_db      # core.py's load_dotenv() never overrides a set variable

with mock.patch('requests.post'), mock.patch('requests.get'):
    import pipeline  # noqa: F401 -- imported once here so every test module can just `import pipeline`

from tests.dbutil import reset_tables, USER_TABLES


@pytest.fixture(autouse=True)
def _assume_ai_services_reachable(monkeypatch):
    """_run_trailer_job now refuses to start a job at all when AI Vision
    (Ollama) or faster-whisper is unreachable (see the module docstring
    above for why this sandbox never has either actually running, and
    test_service_preflight_check.py for the dedicated coverage of that
    refusal itself). Every OTHER test that exercises generation end-to-end
    predates that gate and depends on the *previous* behavior -- a request
    to an unreachable service failing individually and the pipeline
    degrading gracefully (a neutral AI score, no dialogue-aware cutting) --
    to actually reach the scene-selection/rendering logic it's testing.
    Autouse, so every test gets the preflight check itself stubbed out
    (reporting every service 'up') without needing to remember to opt in
    file by file; the *actual* per-call requests.post/get still go out
    unmocked (or mocked per-test) exactly as before, so the graceful
    degradation this suite already relies on is unaffected."""
    monkeypatch.setattr(pipeline, '_check_service',
                        lambda name, url, path='/', timeout=3: {'name': name, 'url': url, 'status': 'up'})


@pytest.fixture(autouse=True)
def _sessions_for_made_up_accounts(request, monkeypatch):
    """Most tests sign in by writing a session by hand for a made-up account
    (user 1 'admin', user 7 'ana'...) that has no row in the users table, or
    whose id belongs to some other row a previous test left. Production checks
    every request against the account (auth._revalidate_session); that check
    is off for those tests. Tests marked real_sessions -- the ones about
    sessions -- get the production rule."""
    if request.node.get_closest_marker('real_sessions'):
        return
    with mock.patch('requests.post'), mock.patch('requests.get'):
        import auth
    monkeypatch.setattr(auth, 'SESSION_REVALIDATE', False)


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_sessions: sessions must belong to an existing account, as in production')


@pytest.fixture
def users_db(monkeypatch):
    """Empty account tables for one test, so account/TOTP/lockout tests can
    freely create and modify accounts with no cross-test interference. Runs the
    real users_db_init() afterwards -- exercising the actual init path
    (including its bootstrap-admin-if-empty behavior) rather than hand-building
    rows that could drift from it."""
    with mock.patch('requests.post'), mock.patch('requests.get'):
        import auth
    reset_tables(*USER_TABLES)
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'TestBootstrapPass123')
    auth.users_db_init()
    yield

