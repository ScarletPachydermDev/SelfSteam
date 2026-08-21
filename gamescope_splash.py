"""Foreground a window in gamescope's compositor via its own X11 control
properties (STEAM_GAME + GAMESCOPECTRL_BASELAYER_APPID) -- the same
mechanism gamescope-fg uses, reimplemented here so the SelfSteam server
has no runtime dependency on Chimera being installed.

Confirmed live (2026-08-12) that a window registered this way must be
re-registered every time -- it does not stay foregrounded across a
Steam stop/start cycle. Skipping the re-registration on a later
maintenance window left gamescope with no valid foreground client at
all (Steam gone, old registration stale) and produced a real frozen/
color-shifted screen, since gamescope has no fallback and assumes
Steam is always the base "shell" layer.
"""
import os
import re
import subprocess
import time

import host_exec

# Functions:
#   _run(argv) -- subprocess.run wrapper via host_exec.
#   _find_window_by_title(title) -- finds our own window by its exact title.
#   foreground(win_id) -- marks win_id as gamescope's foreground base layer.
#   restore(prior) -- restores whatever was foregrounded before.
#   launch_foregrounded(argv, title) -- launches argv, finds its window by title, foregrounds it.
_WINDOW_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s")


def _run(argv, **kwargs):
    return subprocess.run(host_exec.wrap(argv), capture_output=True, text=True, **kwargs)


def _find_window_by_title(display, title):
    """Find our own window by its exact title, set via Gtk's
    set_title(). Matching on "large + not Steam-titled" instead (an
    earlier version of this function) is not reliable: Steam's own
    Big Picture UI has several large, untitled internal sub-surfaces
    that pass a generic size-based filter just as easily as our real
    window does -- confirmed live (2026-08-12), it grabbed one of
    those instead of the real target while Steam was running. Only
    reproduces with Steam up; doesn't show with Steam fully stopped,
    since then there's nothing else large to false-positive on."""
    needle = f'"{title}"'
    for _ in range(20):
        result = _run(["xwininfo", "-display", display, "-root", "-tree"])
        for line in result.stdout.splitlines():
            match = _WINDOW_RE.match(line)
            if match and needle in line:
                return match.group(1)
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


def launch_foregrounded(argv, window_title, display=":0"):
    """Launch argv, find the window it opens (matched by exact title --
    the launched app must call Gtk set_title(window_title)), and
    foreground it. Returns (process, prior_baselayer_value) -- caller
    is responsible for terminating the process and calling restore()
    when done.

    argv itself is launched directly, NOT through host_exec.wrap --
    both real callers (auth_display.py's auth_screen.py, maintenance.py's
    splash.py) launch this app's own script, which only exists inside
    the Flatpak sandbox (/app/share/selfsteam/...) and needs the
    sandbox's own GTK4/PyGObject (org.gnome.Platform), neither of which
    exist on the bare host. host_exec.wrap is still correct for _run's
    own X11 property calls below (those genuinely need the host's real
    X server tools), just not for the launched app itself. Confirmed
    live as a real bug: wrapping this too made the auth screen silently
    fail with "No such file or directory" on every gamescope-session
    Flatpak install, since flatpak-spawn --host was trying to run a
    sandbox-only path directly on the host.

    Running argv directly like this also means it no longer inherits
    the host's DISPLAY/XAUTHORITY the way flatpak-spawn --host used
    to provide -- confirmed live (2026-08-21) the sandboxed process
    has none of DISPLAY, WAYLAND_DISPLAY or XAUTHORITY set at all, so
    Gdk.Display.get_default() came back None and the launched GTK app
    crashed in its own startup before ever mapping a window. DISPLAY
    is set explicitly below from this function's own `display` param
    (the same value already used for the xprop/xwininfo calls) so the
    launched process can actually open a display."""
    env = {**os.environ, "DISPLAY": display}
    proc = subprocess.Popen(argv, start_new_session=True, env=env)
    win_id = _find_window_by_title(display, window_title)
    if win_id is None:
        return proc, None
    prior_value = foreground(win_id, display)
    return proc, prior_value
