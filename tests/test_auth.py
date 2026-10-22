"""
Tests for auth.py's password policy (pure) and the account/TOTP lifecycle
(backed by a real PostgreSQL test database, emptied per test by the users_db
fixture -- these functions genuinely touch the database, so a pure-function test would
not actually exercise the real code path).
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with mock.patch('requests.post'), mock.patch('requests.get'):
    import auth


class TestPasswordPolicy:
    def test_too_short_is_rejected(self):
        ok, reason = auth._password_policy_error('short1')
        assert ok is False
        assert '12' in reason

    def test_exactly_minimum_length_is_accepted(self):
        ok, reason = auth._password_policy_error('a' * 12)
        assert ok is True

    def test_common_password_rejected_even_if_long_enough(self):
        ok, reason = auth._password_policy_error('password123')
        assert ok is False

    def test_genuinely_strong_password_accepted(self):
        ok, reason = auth._password_policy_error('correct horse battery staple 42')
        assert ok is True

    def test_empty_password_rejected(self):
        ok, reason = auth._password_policy_error('')
        assert ok is False

    def test_none_password_rejected_not_crashed(self):
        ok, reason = auth._password_policy_error(None)
        assert ok is False


class TestAccountAndTotpLifecycle:
    """Each test starts with empty account tables via the users_db fixture in
    conftest.py, so these can freely create/modify accounts without any
    cross-test interference or touching a real deployment's data."""

    def test_user_create_enforces_password_policy(self, users_db):
        ok, err = auth.user_create('newuser', 'short')
        assert ok is False
        assert auth.user_get_by_username('newuser') is None

    def test_user_create_success(self, users_db):
        ok, err = auth.user_create('newuser', 'a-genuinely-long-password-1', role='user')
        assert ok is True
        user = auth.user_get_by_username('newuser')
        assert user is not None
        assert user['role'] == 'user'
        assert bool(user['is_active']) is True

    def test_self_registration_creates_inactive_account(self, users_db):
        ok, err = auth.user_create('pending', 'a-genuinely-long-password-1', role='user', active=False)
        assert ok is True
        user = auth.user_get_by_username('pending')
        assert bool(user['is_active']) is False

    def test_totp_not_enabled_until_confirmed(self, users_db):
        auth.user_create('totpuser', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('totpuser')
        secret, uri = auth.user_totp_begin_enrolment(user['id'])
        assert secret is not None
        assert uri.startswith('otpauth://totp/')
        # Enrolment started but not confirmed -- must still read as disabled.
        still_user = auth.user_get(user['id'])
        assert bool(still_user['totp_enabled']) is False

    def test_totp_wrong_code_does_not_enable(self, users_db):
        auth.user_create('totpuser2', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('totpuser2')
        auth.user_totp_begin_enrolment(user['id'])
        codes, err = auth.user_totp_confirm(user['id'], '000000')
        assert codes is None
        assert err is not None
        assert bool(auth.user_get(user['id'])['totp_enabled']) is False

    def test_totp_correct_code_enables_and_issues_backup_codes(self, users_db):
        import pyotp
        auth.user_create('totpuser3', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('totpuser3')
        secret, uri = auth.user_totp_begin_enrolment(user['id'])
        valid_code = pyotp.TOTP(secret).now()
        codes, err = auth.user_totp_confirm(user['id'], valid_code)
        assert err is None
        assert len(codes) == 10
        assert bool(auth.user_get(user['id'])['totp_enabled']) is True

    def test_backup_code_works_once_only(self, users_db):
        import pyotp
        auth.user_create('totpuser4', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('totpuser4')
        secret, uri = auth.user_totp_begin_enrolment(user['id'])
        codes, err = auth.user_totp_confirm(user['id'], pyotp.TOTP(secret).now())
        one_code = codes[0]
        fresh_user = auth.user_get(user['id'])
        assert auth.user_totp_verify(fresh_user, one_code) is True
        # Re-fetch: the used code must be consumed, not reusable
        fresh_user2 = auth.user_get(user['id'])
        assert auth.user_totp_verify(fresh_user2, one_code) is False


class TestAccountLockout:
    def test_lockout_triggers_after_max_failures(self, users_db):
        auth.user_create('lockme', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('lockme')
        locked_until = None
        for _ in range(auth.LOGIN_MAX_FAILURES):
            locked_until = auth.user_note_failed_login(user['id'])
        assert locked_until is not None
        fresh = auth.user_get(user['id'])
        assert auth.user_lockout_remaining(fresh) > 0

    def test_not_locked_before_reaching_the_threshold(self, users_db):
        auth.user_create('notyet', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('notyet')
        for _ in range(auth.LOGIN_MAX_FAILURES - 1):
            auth.user_note_failed_login(user['id'])
        fresh = auth.user_get(user['id'])
        assert auth.user_lockout_remaining(fresh) == 0

    def test_successful_login_clears_the_failure_count(self, users_db):
        auth.user_create('recovers', 'a-genuinely-long-password-1')
        user = auth.user_get_by_username('recovers')
        for _ in range(auth.LOGIN_MAX_FAILURES - 1):
            auth.user_note_failed_login(user['id'])
        auth.user_touch_login(user['id'])
        fresh = auth.user_get(user['id'])
        assert fresh['failed_logins'] == 0
        assert fresh['locked_until'] is None


class TestPresence:
    """Who is signed in now: kept in memory, from each request's session."""

    def _reset(self):
        with auth._PRESENCE_LOCK:
            auth._PRESENCE.clear()

    def _touch(self, uid, name, role, sid, ip='10.0.0.5', ua='Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537'):
        with auth.app.test_request_context('/api/library', headers={'User-Agent': ua}, environ_base={'REMOTE_ADDR': ip}):
            from flask import session
            session['sid'] = sid
            auth._touch_presence(uid, name, role)

    def test_only_recent_sessions_count_and_an_admin_sees_the_address(self):
        self._reset()
        self._touch(1, 'jun', 'admin', 'a1')
        self._touch(1, 'jun', 'admin', 'a2', ip='10.0.0.9')
        self._touch(2, 'ana', 'user', 'b1')
        with auth._PRESENCE_LOCK:
            auth._PRESENCE[(3, 'c1')] = {'since': 0, 'seen': 0, 'username': 'old', 'role': 'user', 'ip': 'x', 'device': 'y'}
        snap = auth.presence_snapshot(True, (1, 'a1'))
        assert snap['sessions'] == 3 and sorted(u['username'] for u in snap['users']) == ['ana', 'jun']
        jun = next(u for u in snap['users'] if u['username'] == 'jun')
        assert len(jun['sessions']) == 2 and any(s['you'] for s in jun['sessions'])
        assert {s['ip'] for s in jun['sessions']} == {'10.0.0.5', '10.0.0.9'}
        assert jun['sessions'][0]['device'] == 'Chrome on Windows'
        assert 'old' not in [u['username'] for u in snap['users']]
        plain = auth.presence_snapshot(False, (2, 'b1'))
        assert all('ip' not in s and 'device' not in s for u in plain['users'] for s in u['sessions']), 'only an admin sees these'

    def test_the_dashboards_own_polling_is_not_activity(self):
        self._reset()
        with auth.app.test_request_context('/api/dashboard/presence'):
            from flask import session
            session['sid'] = 'p1'
            auth._touch_presence(1, 'jun', 'admin')
        assert auth.presence_snapshot(True)['sessions'] == 0

    def test_route_lists_who_is_here(self):
        self._reset()
        self._touch(2, 'ana', 'user', 'b1')
        c = auth.app.test_client()
        with c.session_transaction() as s:
            s.update(authed=True, user_id=1, username='jun', role='admin', csrf_token='t', sid='a1')
        d = c.get('/api/dashboard/presence').get_json()
        assert d['ok'] and [u['username'] for u in d['users']] == ['ana'] and d['window_minutes'] == 5
        assert auth.app.test_client().get('/api/dashboard/presence').status_code in (401, 403)
