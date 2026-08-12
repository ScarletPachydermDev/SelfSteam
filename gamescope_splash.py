"""Foreground a window in gamescope's compositor via its own X11 control
properties (STEAM_GAME + GAMESCOPECTRL_BASELAYER_APPID) -- the same
mechanism gamescope-fg uses, reimplemented here so Gridge Server has no
runtime dependency on Chimera being installed.

Confirmed live (2026-08-12) that a window registered this way must be
re-registered every time -- it does not stay foregrounded across a
Steam stop/start cycle. Skipping the re-registration on a later
maintenance window left gamescope with no valid foreground client at
all (Steam gone, old registration stale) and produced a real frozen/
color-shifted screen, since gamescope has no fallback and assumes
Steam is always the base "shell" layer.
"""
import re
import subprocess
import time

import host_exec

_WINDOW_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s")
_SKIP_TITLES = ("steam", "steamoverlay", "steamwebhelper", "steam big picture mode", "mangoapp overlay window")


def _run(argv, **kwargs):
    return subprocess.run(host_exec.wrap(argv), capture_output=True, text=True, **kwargs)


def _find_new_window(display):
    """Best-effort: the most recently mapped non-Steam window with a
    real size. Small poll loop instead of a fixed sleep, since window
    mapping time varies with what's being launched."""
    for _ in range(20):
        result = _run(["xwininfo", "-display", display, "-root", "-tree"])
        candidates = []
        for line in result.stdout.splitlines():
            if not _WINDOW_RE.match(line):
                continue
            if not re.search(r"\d{3,}x\d{3,}", line):
                continue
            lowered = line.lower()
            if any(skip in lowered for skip in _SKIP_TITLES):
                continue
            candidates.append(_WINDOW_RE.match(line).group(1))
        if candidates:
            return candidates[-1]
        time.sleep(0.2)
    return None


def foreground(win_id, display=":0"):
    """Mark win_id as gamescope's foreground base layer."""
    _run(["xprop", "-display", display, "-id", win_id, "-f", "STEAM_GAME", "32c", "-set", "STEAM_GAME", "1"])
    original = _run(["xprop", "-display", display, "-root"]).stdout
    match = re.search(r"GAMESCOPECTRL_BASELAYER_APPID.*=\s*(.+)", original)
    prior_value = match.group(1).strip() if match else "0"
    _run([
        "xprop", "-display", display, "-root", "-format", "GAMESCOPECTRL_BASELAYER_APPID", "32co",
        "-set", "GAMESCOPECTRL_BASELAYER_APPID", f"1,{prior_value}",
    ])
    return prior_value


def restore(prior_value, display=":0"):
    _run([
        "xprop", "-display", display, "-root", "-format", "GAMESCOPECTRL_BASELAYER_APPID", "32co",
        "-set", "GAMESCOPECTRL_BASELAYER_APPID", prior_value,
    ])


def launch_foregrounded(argv, display=":0"):
    """Launch argv, find its window, and foreground it. Returns
    (process, prior_baselayer_value) -- caller is responsible for
    terminating the process and calling restore() when done."""
    proc = subprocess.Popen(host_exec.wrap(argv), start_new_session=True)
    win_id = _find_new_window(display)
    if win_id is None:
        return proc, None
    prior_value = foreground(win_id, display)
    return proc, prior_value
