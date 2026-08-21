"""SteamOS Game Mode-aware Steam maintenance window.

On a plain desktop Linux session, steam_restart.restart_steam() (kill,
wait, relaunch) is enough. On a SteamOS gamescope-session, it is not:
gamescope-session.target has `Upholds=steam-launcher.service`, which
makes systemd restart Steam within the same instant a plain `kill`/
`systemctl stop` takes it down -- confirmed live via journal
timestamps (Stopped/Starting are seconds apart with no external
trigger visible). `systemctl --user mask` is what actually holds it
down: it defeats Upholds= without touching gamescope-session.target
itself, which would tear down the whole compositor instead of just
Steam.

Writing to shortcuts.vdf while Steam is running gets silently
clobbered by Steam's own periodic re-save (confirmed live, and matches
ChimeraOS's own documented behavior) -- so a maintenance window where
Steam is verifiably down is required before any shortcuts_vdf/artwork
write, not optional polish.
"""
import subprocess
import time

import host_exec
import steam_restart

SERVICE = "steam-launcher.service"
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 30

# Functions:
#   is_gamescope_session() -- True if steam-launcher.service exists as a systemd user unit.
#   _systemctl(*args) -- one `systemctl --user` call.
#   enter_maintenance_mode() -- mask + stop Steam so shortcuts.vdf/artwork writes can't be clobbered.
#   exit_maintenance_mode() -- bring Steam back (unmask+start on gamescope, restart_steam elsewhere).


def is_gamescope_session():
    """True if steam-launcher.service exists as a systemd user unit --
    i.e. we're on a SteamOS-style Game Mode session, not plain desktop
    Linux."""
    result = subprocess.run(
        host_exec.wrap(["systemctl", "--user", "cat", SERVICE]),
        capture_output=True,
    )
    return result.returncode == 0


def _systemctl(*args):
    return subprocess.run(host_exec.wrap(["systemctl", "--user", *args]), capture_output=True, text=True)


def enter_maintenance_mode():
    """Mask + stop Steam so file writes to shortcuts.vdf/grid artwork
    can't be clobbered by Steam's own background re-save. Falls back to
    the plain kill/relaunch instrumentation on non-gamescope desktop
    sessions, where there's no Upholds= to fight and no clobbering
    concern once Steam is actually down."""
    if not is_gamescope_session():
        pids = steam_restart.steam_pids()
        if pids:
            subprocess.run(host_exec.wrap(["kill", "-15", *pids]), capture_output=True)
        waited = 0.0
        while steam_restart.is_steam_running() and waited < steam_restart.POLL_TIMEOUT:
            time.sleep(steam_restart.POLL_INTERVAL)
            waited += steam_restart.POLL_INTERVAL
        return

    _systemctl("mask", SERVICE)
    _systemctl("stop", SERVICE)
    waited = 0.0
    while steam_restart.is_steam_running() and waited < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL


def exit_maintenance_mode():
    """Bring Steam back. On gamescope sessions this is unmask + start;
    on plain desktop it's the existing restart_steam() launch path."""
    if not is_gamescope_session():
        steam_restart.restart_steam()
        return

    _systemctl("unmask", SERVICE)
    _systemctl("start", SERVICE)
