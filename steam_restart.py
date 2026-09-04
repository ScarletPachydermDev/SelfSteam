"""Restart Steam so it picks up new/changed non-Steam shortcuts.

Kill/wait/relaunch pattern (kill, then poll for actual exit instead of
guessing a fixed delay, then launch) matches
github.com/SteamGridDB/steam-rom-manager's stop-start-steam.ts, which
the user specifically pointed to as feeling seamless. Flatpak-vs-native
detection uses steam_paths' filesystem checks instead of asking the
`flatpak` CLI (which SRM does) -- confirmed that CLI's view of
installed refs can be isolated from the host's actual installs (e.g.
from inside a distrobox/toolbox container), even though the
filesystem paths themselves are shared and visible.
"""
import os
import subprocess
import time

import host_exec
import steam_paths

POLL_INTERVAL = 0.5
POLL_TIMEOUT = 60
# Cold-start-after-reinstall margin for the post-launch wait specifically
# (see _launch_and_wait) -- confirmed live on X1: a steady-state
# kill+relaunch comes back in ~5s, but right after another Flatpak app
# install/reinstall was contending for disk/repo access in parallel
# (Bazaar, in this case), Steam's own bwrap/pressure-vessel bootstrap
# took well over 60s to reach the point where its unwrapped "steam"
# binary process exists for pidof to see -- the launch itself was never
# broken (Popen already detached it), but the wait gave up before
# confirming success, which is what actually looked like "not
# restarting" from the UI side.
LAUNCH_POLL_TIMEOUT = 150


class SteamStopError(RuntimeError):
    """Steam was asked to exit and didn't, within POLL_TIMEOUT."""

# Functions:
#   is_steam_running() -- True if a Steam process is currently running (native or Flatpak).
#   _shutdown_argv() -- the right `-shutdown` command for the installed Steam (native vs Flatpak).
#   stop_steam() -- ask Steam to exit, wait for it; False if it refused.
#   restart_steam() -- stop Steam (raising if it refuses), then relaunch it.
#   steam_pids() -- pids of any currently-running Steam process.
#   _launch_and_wait(argv) -- launches argv, then polls until Steam is confirmed running again.
#   launch_flatpak_steam_detached() -- fire-and-forget launch, used right after installing Steam.


def is_steam_running():
    """True if a Steam process is currently running on the host (native
    or Flatpak) -- public since onboarding also uses this to distinguish
    "still downloading/not started yet" from "running, just waiting on
    login/post-login sync" while it waits for a fresh install to finish."""
    return subprocess.run(host_exec.wrap(["pidof", "steam"]), capture_output=True).returncode == 0


def _shutdown_argv():
    """The right "ask Steam to quit" command for whichever Steam is
    actually installed, or None if neither shape is available.

    A Flatpak Steam usually has no host-side `steam` binary at all, so
    the request has to go through `flatpak run <app-id> -shutdown`;
    a native install has the binary and takes `steam -shutdown`
    directly. Checked in that order against the real detected install
    root rather than guessing from PATH alone."""
    try:
        root = steam_paths.find_steam_root()
    except steam_paths.SteamNotFoundError:
        root = None

    if root == os.path.expanduser(steam_paths.FLATPAK_ROOT):
        flatpak = host_exec.which("flatpak")
        if flatpak:
            return [flatpak, "run", "com.valvesoftware.Steam", "-shutdown"]

    if host_exec.which("steam"):
        return ["steam", "-shutdown"]

    flatpak = host_exec.which("flatpak")
    if flatpak:
        return [flatpak, "run", "com.valvesoftware.Steam", "-shutdown"]
    return None


def stop_steam():
    """Asks Steam to exit, then polls until it actually does. Returns
    True once no Steam process is left, False if it outlived
    POLL_TIMEOUT and is still running.

    Steam's own `-shutdown` request first, not `kill -15` -- confirmed
    live (2026-09-04) on a real SteamOS *desktop-mode* session that a
    plain SIGTERM to Steam's own pid does nothing there: SteamOS runs
    Steam under its own steam-launcher.service, whose unit sets
    KillSignal=SIGCONT and does termination itself via
    `ExecStop=... kill -TERM $(pgrep -P $MAINPID || echo $MAINPID)` --
    and that ExecStop is itself broken when $MAINPID comes through
    empty (pgrep prints its usage text, kill then gets no pid at all,
    the unit fails with status=2/INVALIDARGUMENT). Steam survived a
    real SIGTERM for six hours straight that way, while the shutdown
    request brought it down in about a second. SIGTERM stays as a
    fallback for anything that doesn't respond to the official request,
    which is also what every non-SteamOS install relied on before this
    and is known to work there.

    The request is addressed to whichever Steam is actually installed
    (see _shutdown_argv) rather than assuming a host `steam` binary:
    on a Flatpak-only install there is no such binary at all, which
    would otherwise mean not just a failed request but a raised
    FileNotFoundError when SelfSteam itself isn't sandboxed (host_exec.
    wrap only prefixes flatpak-spawn when it is).
    """
    if not is_steam_running():
        return True

    argv = _shutdown_argv()
    if argv:
        try:
            subprocess.run(host_exec.wrap(argv), capture_output=True)
        except OSError:
            # No usable shutdown command on this host -- fall straight
            # through to SIGTERM below rather than failing the commit.
            pass
    waited = 0.0
    while is_steam_running() and waited < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    if not is_steam_running():
        return True

    pids = steam_pids()
    if pids:
        subprocess.run(host_exec.wrap(["kill", "-15", *pids]), capture_output=True)
    while is_steam_running() and waited < POLL_TIMEOUT * 2:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    return not is_steam_running()


def restart_steam():
    """Stops Steam, waits for it to really exit, then relaunches it.

    Raises SteamStopError if Steam refused to exit -- deliberately not
    a silent carry-on. Confirmed live as a real, user-visible failure:
    a commit that writes shortcuts.vdf behind a still-running Steam
    leaves that Steam holding a stale in-memory shortcut list, so the
    new shortcut simply never appears (and can be lost outright if
    Steam later rewrites the file from that stale copy). Before this,
    that path just fell through to relaunching an already-running Steam
    and reported success, which read as "SelfSteam created the shortcut
    but it isn't there."
    """
    if not stop_steam():
        raise SteamStopError(
            "Steam didn't shut down when asked, so its shortcut list couldn't be "
            "reloaded -- please quit Steam yourself and start it again to see this shortcut"
        )

    try:
        root = steam_paths.find_steam_root()
    except steam_paths.SteamNotFoundError:
        return

    if root == os.path.expanduser(steam_paths.FLATPAK_ROOT):
        flatpak = host_exec.which("flatpak")
        if flatpak:
            _launch_and_wait([flatpak, "run", "com.valvesoftware.Steam"])
        return

    # Prefer the launcher script at its known absolute path within the
    # detected root over a PATH-based `steam` lookup: the script lives on
    # the shared host filesystem either way, but the launcher binary
    # normally installed to /usr/bin isn't visible from inside a
    # distrobox/toolbox container (separate root filesystem, only home
    # is shared) -- confirmed this is exactly why shutil.which("steam")
    # found nothing there even though native Steam is genuinely installed.
    launcher = os.path.join(root, "steam.sh")
    if os.path.exists(launcher):
        _launch_and_wait([launcher, "-silent"])
    elif host_exec.which("steam"):
        _launch_and_wait(["steam", "-silent"])


def steam_pids():
    result = subprocess.run(host_exec.wrap(["pidof", "steam"]), capture_output=True, text=True)
    return result.stdout.split()


def _launch_and_wait(argv):
    subprocess.Popen(host_exec.wrap(argv), start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    waited = 0.0
    while not is_steam_running() and waited < LAUNCH_POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL


def launch_flatpak_steam_detached():
    """Launch Steam (Flatpak) right after installing it, detached from
    our own process (start_new_session=True) so it keeps running even
    if SelfSteam itself closes. The Flathub Steam package is just a small
    bootstrap downloader -- launching it is what actually triggers the
    real client's multi-minute first-time download/install, which
    otherwise happens completely silently with no visible progress at
    all (confirmed: looks exactly like the installer just did nothing).
    Fire-and-forget; doesn't wait for the pid the way _launch_and_wait
    does, since the caller already has its own longer-lived polling for
    "is Steam actually usable yet" (a real userdata dir, which only
    appears once the user has logged in)."""
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return
    subprocess.Popen(
        host_exec.wrap([flatpak, "run", "com.valvesoftware.Steam"]),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
