"""Screen-pairing auth: no accounts, no passwords to remember -- a random
code is shown on the TV itself, and only someone who can see that
screen (or was told the code by whoever is) can get in. Same pattern
ChimeraOS's own `chimera_app/authenticator.py` uses for exactly this
kind of shared living-room device.
"""
import hashlib
import json
import os
import secrets
import string
import threading
import time

import config

# Same alphabet ChimeraOS excludes: characters that look alike on a TV
# from across the room (O/0, I/1, S/5, J looks like lowercase l).
_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OI0S5J")
CODE_LENGTH = 6
CODE_TTL = 600  # seconds a shown code stays valid if unused
SESSION_TTL = 12 * 60 * 60  # seconds a successful login stays valid
REMEMBER_TTL = 90 * 24 * 60 * 60  # seconds a "remember this device" token stays valid if used

_lock = threading.Lock()
_code = None
_code_expires_at = 0.0
_sessions = {}  # token -> expires_at

# Unlike _sessions (in-memory, meant to reset on every server restart --
# a restart already means walking back over to re-enter a fresh code),
# remembered devices are meant to survive restarts, so they're persisted
# to disk. Only the hash is ever written: the file is plain JSON with no
# extra protection, so a raw token in it would be as good as the token
# itself to anyone who can read the file.
_REMEMBERED_FILE = os.path.join(config.CONFIG_DIR, "remembered_devices.json")


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _load_remembered():
    if not os.path.exists(_REMEMBERED_FILE):
        return {}
    with open(_REMEMBERED_FILE) as f:
        return json.load(f)


def _save_remembered(data):
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with open(_REMEMBERED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def remember_device():
    """Issues a new long-lived device token and persists its hash.
    Called once, right after a successful code entry with "remember this
    device" checked."""
    token = secrets.token_urlsafe(32)
    with _lock:
        data = _load_remembered()
        data[_hash_token(token)] = time.time() + REMEMBER_TTL
        _save_remembered(data)
    return token


def is_remembered(token):
    """True if token is a valid, unexpired remembered-device token.
    Sliding expiry: a valid use pushes expiry out another REMEMBER_TTL,
    so an actively-used device is never logged out on its own, but one
    that's abandoned (lost, wiped, sold) still lapses eventually instead
    of trusting it forever."""
    if not token:
        return False
    key = _hash_token(token)
    with _lock:
        data = _load_remembered()
        expires = data.get(key)
        if expires is None:
            return False
        if time.time() > expires:
            del data[key]
            _save_remembered(data)
            return False
        data[key] = time.time() + REMEMBER_TTL
        _save_remembered(data)
        return True


def forget_all_devices():
    """Revokes every remembered device at once -- the only revocation
    granularity offered, since this app has no per-device identity to
    show a user in the first place (just an opaque token each browser
    happens to hold)."""
    with _lock:
        _save_remembered({})


def _generate_code():
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def current_code():
    """The active code, generating a fresh one if there isn't one or the
    previous one expired unused."""
    global _code, _code_expires_at
    with _lock:
        if _code is None or time.time() > _code_expires_at:
            _code = _generate_code()
            _code_expires_at = time.time() + CODE_TTL
        return _code


def try_login(submitted):
    """Check submitted against the active code. On success, invalidates
    the code (so it can't be reused) and returns a new session token.
    Returns None on failure."""
    global _code, _code_expires_at
    with _lock:
        if _code is None or time.time() > _code_expires_at:
            return None
        if not secrets.compare_digest(submitted.strip().upper(), _code):
            return None
        _code = None
        _code_expires_at = 0.0
        token = secrets.token_urlsafe(24)
        _sessions[token] = time.time() + SESSION_TTL
        return token


def is_authenticated(token):
    if not token:
        return False
    with _lock:
        expires = _sessions.get(token)
        if expires is None:
            return False
        if time.time() > expires:
            del _sessions[token]
            return False
        return True
