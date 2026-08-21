"""Orchestrates a Steam maintenance window: stop Steam, show a splash so
Game Mode doesn't sit on a frozen/stale frame, run the caller's write,
then bring Steam back.

Live-tested sequence (2026-08-12) -- see steamos_session.py and
gamescope_splash.py docstrings for why each piece is necessary. Splash
is only meaningful on a gamescope session; on plain desktop Linux there's
a normal window manager so nothing needs to be foregrounded manually.
"""
import os
import subprocess
import sys

import gamescope_splash
import steamos_session
import window_titles

_SPLASH_SCRIPT = os.path.join(os.path.dirname(__file__), "splash.py")

# Functions:
#   run_with_steam_stopped(apply_fn, message) -- stop Steam, show a splash, run apply_fn, restart Steam.


def run_with_steam_stopped(apply_fn, message="Applying changes…"):
    """Stop Steam, show a splash (gamescope sessions only), call
    apply_fn() with Steam verifiably down, then restart Steam.
    apply_fn's return value is passed through. Steam is always
    restarted, even if apply_fn raises."""
    on_gamescope = steamos_session.is_gamescope_session()
    steamos_session.enter_maintenance_mode()

    splash_proc = None
    baselayer_prior = None
    if on_gamescope:
        splash_proc, baselayer_prior = gamescope_splash.launch_foregrounded(
            [sys.executable, _SPLASH_SCRIPT, message], window_titles.SPLASH_TITLE
        )

    try:
        return apply_fn()
    finally:
        if splash_proc is not None:
            splash_proc.terminate()
            try:
                splash_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                splash_proc.kill()
        if baselayer_prior is not None:
            gamescope_splash.restore(baselayer_prior)
        steamos_session.exit_maintenance_mode()
