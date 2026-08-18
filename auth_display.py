"""Shows/dismisses the pairing-code screen on demand. Foregrounds it in
gamescope on a Game Mode session (same mechanism as the maintenance
splash); on plain desktop it's just a normal window, since there's
already a window manager. Doesn't touch Steam at all -- unlike
maintenance.py's splash, this never needs Steam stopped."""
import os
import socket
import subprocess
import sys
import threading

import auth
import gamescope_splash
import steamos_session
import window_titles

_AUTH_SCREEN_SCRIPT = os.path.join(os.path.dirname(__file__), "auth_screen.py")

# Same env var + default selfsteam_server.py itself reads its port from
# -- duplicated rather than imported, since selfsteam_server.py already
# imports this module (auth_display) and a reverse import would be
# circular.
_PORT = os.environ.get("SELFSTEAM_SERVER_PORT", "8845")

_lock = threading.Lock()
_proc = None
_baselayer_prior = None
_shown_code = None


def _local_ip():
    """Best-guess LAN-facing IP: opens a UDP "connection" to a public
    address (no packet actually sent for UDP) purely to ask the OS
    routing table which local interface/IP it would use -- the
    standard portable trick, since there's no other reliable way to
    find "the" LAN IP on a machine that may have several interfaces."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


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
        args = [sys.executable, _AUTH_SCREEN_SCRIPT, code, socket.gethostname(), _local_ip(), _PORT]
        if steamos_session.is_gamescope_session():
            _proc, _baselayer_prior = gamescope_splash.launch_foregrounded(
                args, window_titles.AUTH_SCREEN_TITLE
            )
        else:
            _proc = subprocess.Popen(args)
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
