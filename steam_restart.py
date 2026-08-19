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


def is_steam_running():
    """True if a Steam process is currently running on the host (native
    or Flatpak) -- public since onboarding also uses this to distinguish
    "still downloading/not started yet" from "running, just waiting on
    login/post-login sync" while it waits for a fresh install to finish."""
    return subprocess.run(host_exec.wrap(["pidof", "steam"]), capture_output=True).returncode == 0


def restart_steam():
    pids = steam_pids()
    if pids:
        subprocess.run(host_exec.wrap(["kill", "-15", *pids]), capture_output=True)

    waited = 0.0
    while is_steam_running() and waited < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

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
