"""Screen-pairing auth: no accounts, no passwords to remember -- a random
code is shown on the TV itself, and only someone who can see that
screen (or was told the code by whoever is) can get in. Same pattern
ChimeraOS's own `chimera_app/authenticator.py` uses for exactly this
kind of shared living-room device.
"""
import secrets
import string
import threading
import time

# Same alphabet ChimeraOS excludes: characters that look alike on a TV
# from across the room (O/0, I/1, S/5, J looks like lowercase l).
_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OI0S5J")
CODE_LENGTH = 6
CODE_TTL = 600  # seconds a shown code stays valid if unused
SESSION_TTL = 12 * 60 * 60  # seconds a successful login stays valid

_lock = threading.Lock()
_code = None
_code_expires_at = 0.0
_sessions = {}  # token -> expires_at


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
