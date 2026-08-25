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
#   host_xauthority() -- copies the host's real X11 auth cookie in, for GTK apps this sandbox launches.
#   launch_foregrounded(argv, title) -- launches argv, finds its window by title, foregrounds it.

_XAUTH_COPY_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "selfsteam", "xauthority",
)


def host_xauthority():
    """Copies the host's own X11 auth cookie into a file this sandbox
    can actually read, and returns its path -- --socket=x11 in the
    Flatpak manifest only grants reachability to the X server's own
    socket, not a valid credential to authenticate against it.
    Confirmed live (2026-08-25, real Steam Machine, gamescope session
    launched via a systemd --user service): DISPLAY set but no
    XAUTHORITY at all made every launch of auth_screen.py crash before
    ever mapping a window -- "Authorization required, but no
    authorization protocol specified" from X11 itself, then
    Gdk.Display.get_default() coming back None and a hard crash in
    auth_screen.py's own _screen_scale(). launch_foregrounded's own
    DISPLAY-only fix below (2026-08-21) covers reachability but never
    covered authentication -- this was a real gap in that fix, not a
    new problem.

    The real auth file can't just be pointed at directly either:
    /run/user/<uid>/ isn't visible inside this sandbox at all even
    with --filesystem=host (same exclusion the Flatpak manifest's own
    ~/.var/app comment documents for a different directory) -- it has
    to be read host-side via flatpak-spawn --host and its bytes copied
    in. Tries $XAUTHORITY, then ~/.Xauthority (a plain desktop
    session), then gamescope's own generated
    /run/user/<uid>/xauth_<random> file last (confirmed live as what a
    gamescope session actually uses -- neither of the first two exist
    there). Returns None if nothing is found (silently skip
    XAUTHORITY) rather than raising, so a DISPLAY-only connection
    attempt can still happen instead of crashing before even trying;
    when running unsandboxed (dev-test) this is a no-op passthrough of
    whatever XAUTHORITY the real environment already has."""
    if not host_exec.IN_FLATPAK:
        return os.environ.get("XAUTHORITY")
    cmd = (
        'for f in "$XAUTHORITY" "$HOME/.Xauthority" /run/user/$(id -u)/xauth_*; do '
        '[ -s "$f" ] && cat "$f" && exit 0; done; exit 1'
    )
    result = subprocess.run(host_exec.wrap(["sh", "-c", cmd]), capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    os.makedirs(os.path.dirname(_XAUTH_COPY_PATH), exist_ok=True)
    with open(_XAUTH_COPY_PATH, "wb") as f:
        f.write(result.stdout)
    return _XAUTH_COPY_PATH
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
    launched process can actually open a display -- XAUTHORITY (see
    host_xauthority's own docstring) is what actually lets it
    authenticate against that display, not just reach it."""
    env = {**os.environ, "DISPLAY": display}
    xauth = host_xauthority()
    if xauth:
        env["XAUTHORITY"] = xauth
    proc = subprocess.Popen(argv, start_new_session=True, env=env)
    win_id = _find_window_by_title(display, window_title)
    if win_id is None:
        return proc, None
    prior_value = foreground(win_id, display)
    return proc, prior_value


def launch_foregrounded_and_wait(argv, window_titles, display=":0"):
    """Same foregrounding as launch_foregrounded above, for a caller
    that needs the opposite launch/lifecycle shape: argv is launched
    exactly as given (the caller does its own host_exec.wrap if it
    needs to run on the host at all, e.g. a real Flatpak app rather
    than this app's own sandbox-only script -- no DISPLAY/XAUTHORITY
    injection here either, since a normal `flatpak run` genuinely does
    get real display access some other way, unlike launch_foregrounded's
    own launched-directly-with-no-sandbox script), and this call
    blocks until the process actually exits, returning its exit code,
    instead of handing back a live Popen for the caller to manage
    teardown on later.

    Confirmed live (2026-08-25) as a real, separate bug from the auth
    screen's own foregrounding gap: RPCS3's --installfw dialog
    ("Welcome to RPCS3") genuinely opened and sat waiting for input,
    just invisible behind Steam's own UI the whole time -- gamescope
    never auto-focuses a new X11 client the way a normal WM would, so
    without this, a firmware install looked identically stuck to a
    real hang from the web UI's own perspective (a blocking request
    forever waiting on a dialog nobody could see or click), even
    though the process itself was working fine underneath.

    window_titles is a str or an ordered list of them -- confirmed live
    (2026-08-25, same day) that a single title wasn't enough: once
    "Welcome to RPCS3" closes, RPCS3 opens a *second*, different window
    ("RPCS3 Firmware Installer") to actually do the install, which the
    first version of this function never looked for again after its
    one-time startup check, leaving it just as invisible as the bug
    this function was written to fix in the first place. Polls in a
    loop instead of a single check-at-startup: each pass tries every
    title in order and foregrounds whichever one is currently open
    (skipping the xprop calls entirely if it's already the foregrounded
    one), so a later window in the sequence gets the same treatment the
    moment it actually appears, however many of them there turn out to
    be. Only the *first* real foreground's own prior_value is kept
    (subsequent ones would just be this function's own previous
    override, not the true original state), so restore() at the end
    always returns to whatever was actually foregrounded before any of
    this started, not to a stale intermediate step of it."""
    if isinstance(window_titles, str):
        window_titles = [window_titles]
    proc = subprocess.Popen(argv, start_new_session=True)
    prior_value = _foreground_while(window_titles, lambda: proc.poll() is not None, display)
    if prior_value is not None:
        restore(prior_value, display)
    return proc.returncode


def _foreground_while(window_titles, is_done, display):
    """Shared polling core for launch_foregrounded_and_wait above and
    launch_foregrounded_until below -- foregrounds whichever of
    window_titles is currently open (re-checked every pass, so a later
    window in the sequence gets foregrounded the moment it actually
    appears, not just whatever was open at the very start), until
    is_done() returns true. Returns whatever prior_value should
    eventually be passed to restore() (None if nothing was ever
    actually foregrounded)."""
    prior_value = None
    foregrounded_win_id = None
    while not is_done():
        found = False
        for title in window_titles:
            win_id = _find_window_by_title(display, title)
            if win_id is None:
                continue
            found = True
            if win_id != foregrounded_win_id:
                captured = foreground(win_id, display)
                if prior_value is None:
                    prior_value = captured
                foregrounded_win_id = win_id
            break
        if not found:
            time.sleep(0.5)
    return prior_value


def launch_foregrounded_until(argv, window_titles, is_done, display=":0"):
    """Like launch_foregrounded_and_wait above, but for a process that
    doesn't reliably exit on its own once its real job is finished --
    confirmed live (2026-08-25, both on X1 and a real Steam Machine):
    RPCS3 keeps its full main window open indefinitely after a
    firmware install completes, rather than quitting the way
    --installfw's own blocking-call assumption expected, so waiting on
    the process's own exit code (launch_foregrounded_and_wait's own
    contract) just hangs forever with nothing left for anyone to
    click. Polls is_done() (a zero-arg callable -- e.g. "has the real
    firmware file this install writes actually appeared on disk")
    instead, and returns the still-running Popen once it's satisfied,
    so the caller can decide how to actually end the process for real
    -- a plain terminate() on this outer wrapper doesn't reliably tear
    down a Flatpak's real sandboxed process any more than it did for
    this app's own self-restart (see selfsteam_server.py's own
    _watch_for_update_and_restart, which uses a real `flatpak kill`
    instead for exactly this reason)."""
    if isinstance(window_titles, str):
        window_titles = [window_titles]
    proc = subprocess.Popen(argv, start_new_session=True)
    prior_value = _foreground_while(window_titles, lambda: is_done() or proc.poll() is not None, display)
    if prior_value is not None:
        restore(prior_value, display)
    return proc
