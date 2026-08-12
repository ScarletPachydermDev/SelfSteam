"""Shows/dismisses the pairing-code screen on demand. Foregrounds it in
gamescope on a Game Mode session (same mechanism as the maintenance
splash); on plain desktop it's just a normal window, since there's
already a window manager. Doesn't touch Steam at all -- unlike
maintenance.py's splash, this never needs Steam stopped."""
import os
import subprocess
import sys
import threading

import auth
import gamescope_splash
import steamos_session
import window_titles

_AUTH_SCREEN_SCRIPT = os.path.join(os.path.dirname(__file__), "auth_screen.py")

_lock = threading.Lock()
_proc = None
_baselayer_prior = None
_shown_code = None


def ensure_shown():
    """Show the current code on screen if it isn't already being shown
    for this exact code. Safe to call on every unauthenticated
    request -- no-ops if already showing the right code."""
    global _proc, _baselayer_prior, _shown_code
    code = auth.current_code()
    with _lock:
        if _proc is not None and _shown_code == code and _proc.poll() is None:
            return
        _dismiss_locked()
        if steamos_session.is_gamescope_session():
            _proc, _baselayer_prior = gamescope_splash.launch_foregrounded(
                [sys.executable, _AUTH_SCREEN_SCRIPT, code], window_titles.AUTH_SCREEN_TITLE
            )
        else:
            _proc = subprocess.Popen([sys.executable, _AUTH_SCREEN_SCRIPT, code])
            _baselayer_prior = None
        _shown_code = code
    threading.Timer(auth.CODE_TTL, dismiss).start()


def dismiss():
    with _lock:
        _dismiss_locked()


def _dismiss_locked():
    global _proc, _baselayer_prior, _shown_code
    if _proc is not None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    if _baselayer_prior is not None:
        gamescope_splash.restore(_baselayer_prior)
    _proc = None
    _baselayer_prior = None
    _shown_code = None
