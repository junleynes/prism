"""Encryption at rest for the secrets PRISM has to keep in a readable form:
network share and destination passwords, AI service API keys, and two-factor
secrets. (Account passwords and 2FA backup codes are hashed, not stored.)

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under a key kept
in its own file, DATA_KEY_FILE (default: .data_key next to the app). It is
separate from the session key on purpose: rotating SECRET_KEY_FILE signs
everyone out, it does not destroy saved passwords. Losing .data_key does: the
saved passwords and API keys must be entered again, and accounts with 2FA set
it up again. Back it up with the database.

Encrypted values look like 'enc:v1:<token>'. Anything else is a value saved
before encryption existed and is returned as it is (callers re-save it
encrypted; see the migrations in library_db, auth and pipeline).

Imports nothing from PRISM, so any module can use it.
"""
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PREFIX = 'enc:v1:'
DATA_KEY_FILE = os.environ.get('DATA_KEY_FILE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.data_key')

_lock = threading.Lock()
_fernet = None


class SecretError(RuntimeError):
    """A value is encrypted but cannot be decrypted (wrong or replaced .data_key)."""


def _box():
    global _fernet
    if _fernet is not None:
        return _fernet
    with _lock:
        if _fernet is None:
            try:
                with open(DATA_KEY_FILE, 'rb') as f:
                    key = f.read().strip()
            except FileNotFoundError:
                key = Fernet.generate_key()
                try:
                    fd = os.open(DATA_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as f:
                        f.write(key)
                except FileExistsError:           # another process made it first: use theirs
                    with open(DATA_KEY_FILE, 'rb') as f:
                        key = f.read().strip()
            _fernet = Fernet(key)
    return _fernet


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    """'' and None stay as they are (nothing to protect); an already encrypted value is not wrapped twice."""
    if value is None or value == '' or is_encrypted(value):
        return value
    return PREFIX + _box().encrypt(str(value).encode('utf-8')).decode('ascii')


def decrypt(value):
    if not is_encrypted(value):
        return value
    try:
        return _box().decrypt(value[len(PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken as e:
        raise SecretError(f'A saved secret cannot be decrypted with {DATA_KEY_FILE} -- was the key file '
                          'replaced? Enter the password or key again.') from e


def decrypt_or_blank(value):
    """For display paths that must not fail: '' when the value cannot be decrypted."""
    try:
        return decrypt(value)
    except SecretError:
        return ''
