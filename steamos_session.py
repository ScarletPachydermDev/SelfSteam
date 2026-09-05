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
#   is_gamescope_session() -- True if steam-launcher.service exists (SteamOS install; both modes).
#   is_game_mode_active() -- True only if a gamescope compositor is actually running.
#   steam_is_the_session_client() -- True when stopping Steam would tear down the whole session.
#   _systemctl(*args) -- one `systemctl --user` call.
#   enter_maintenance_mode() -- mask + stop Steam so shortcuts.vdf/artwork writes can't be clobbered.
#   exit_maintenance_mode() -- bring Steam back (unmask+start on gamescope, restart_steam elsewhere).


def is_gamescope_session():
    """True if steam-launcher.service exists as a systemd user unit.

    NOTE: this is really "is this a SteamOS-style install", not "is
    Game Mode active right now" -- SteamOS ships that unit in desktop
    mode too, so this returns True there as well (confirmed live
    2026-09-04 on a real KDE/kwin desktop session). Callers that mask/
    stop the unit are fine with that, since it's what manages Steam in
    both modes; callers making a genuine gamescope-vs-desktop UI
    decision should not rely on this alone."""
    result = subprocess.run(
        host_exec.wrap(["systemctl", "--user", "cat", SERVICE]),
        capture_output=True,
    )
    return result.returncode == 0


def is_game_mode_active():
    """True only if a gamescope compositor is actually running right
    now -- i.e. this really is Game Mode, as opposed to a SteamOS
    desktop session (which is_gamescope_session() also returns True
    for, see its own docstring).

    Deliberately a live process check, not the presence of
    /run/user/<uid>/gamescope-environment: confirmed live (2026-09-04)
    that file is left behind by a previous Game Mode session and still
    claims XDG_CURRENT_DESKTOP=gamescope long after the user has
    switched to a plain KDE desktop session, so trusting it reports
    Game Mode when the real session env says KDE/plasma and no
    gamescope process exists at all.

    Matched by name *prefix*, not `pgrep -x gamescope`: confirmed live
    in a real Game Mode session that the compositor's own process name
    is "gamescope-wl", so an exact match finds nothing and reports
    desktop mode while Game Mode is plainly running -- which would
    have sent exit_maintenance_mode() down the desktop relaunch path
    and brought Steam back as a windowed app instead of Game Mode.
    The looser match also covers start-gamescope-session, which only
    exists while a gamescope session is coming up or running anyway,
    so it isn't a false positive for this question."""
    result = subprocess.run(
        host_exec.wrap(["pgrep", "gamescope"]),
        capture_output=True,
    )
    return result.returncode == 0


def _systemctl(*args):
    return subprocess.run(host_exec.wrap(["systemctl", "--user", *args]), capture_output=True, text=True)


def steam_is_the_session_client():
    """True when Game Mode is running but Steam is the gamescope
    session's own foreground client rather than its own systemd
    service -- i.e. stopping Steam would take the whole session down
    with it, not just Steam.

    Confirmed by reading the real session scripts (2026-09-05).
    ChimeraOS's gamescope-session-plus -- which Bazzite and Nobara both
    ship too -- runs Steam in the foreground and then tears the
    compositor down the moment it returns:

        $CLIENTCMD          # Steam, in the foreground
        ...
        # When the client exits, kill gamescope nicely
        kill $gamescope_pid

    So `steam -shutdown` there ends the entire Game Mode session, and
    whether the user lands back in it depends on their display manager
    restarting the session -- the unit itself declares no Restart=.
    That is a far worse outcome than the missing shortcut this whole
    stop/restart dance exists to avoid, so SelfSteam refuses rather
    than risking it.

    SteamOS and CachyOS are NOT this shape: both ship a real
    steam-launcher.service that owns Steam independently of the
    session, so stopping it leaves the session up (verified live on
    SteamOS, and CachyOS's own unit is near-identical). Keyed on that
    unit's absence for exactly that reason."""
    return is_game_mode_active() and not is_gamescope_session()


# Deliberately does NOT promise the changes will show up on their own.
# enter_maintenance_mode raises before apply_fn ever runs, so
# shortcuts.vdf is untouched -- only the pending queue survives (it is
# cleared after a successful commit, not before). Telling someone their
# changes would appear "next time Steam restarts" would reproduce the
# exact symptom this whole area exists to fix: waiting for a shortcut
# that was never written.
_SESSION_CLIENT_MESSAGE = (
    "On this system Steam is its own Game Mode session's client, so SelfSteam "
    "can't restart it without closing the whole session. Nothing was changed and "
    "your queued changes are still here -- exit Game Mode, or quit Steam yourself, "
    "then apply them again."
)


def enter_maintenance_mode():
    """Mask + stop Steam so file writes to shortcuts.vdf/grid artwork
    can't be clobbered by Steam's own background re-save. Falls back to
    the plain kill/relaunch instrumentation on non-gamescope desktop
    sessions, where there's no Upholds= to fight and no clobbering
    concern once Steam is actually down."""
    # Checked before either branch: on these systems Steam *is* the
    # session, so there is no safe way to stop it here at all (see
    # steam_is_the_session_client). Raising leaves Steam running and
    # the queue intact, so nothing is written behind a live Steam and
    # nothing is lost.
    if steam_is_the_session_client():
        raise steam_restart.SteamStopError(_SESSION_CLIENT_MESSAGE)

    if not is_gamescope_session():
        # steam_restart.stop_steam(), not a bare `kill -15` -- see its
        # own docstring for why SIGTERM alone is useless on a real
        # SteamOS desktop session. Raising (rather than returning
        # quietly) is the whole point: the caller writes shortcuts.vdf
        # immediately after this, and doing that behind a live Steam is
        # exactly what silently loses a just-created shortcut.
        if not steam_restart.stop_steam():
            raise steam_restart.SteamStopError(
                "Steam didn't shut down when asked, so changes weren't applied -- "
                "please quit Steam yourself and try again"
            )
        return

    _systemctl("mask", SERVICE)
    _systemctl("stop", SERVICE)
    waited = 0.0
    while steam_restart.is_steam_running() and waited < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    # `systemctl stop` alone isn't enough to trust -- confirmed live
    # (2026-09-04) on a real SteamOS *desktop-mode* session, which
    # reaches this same branch because is_gamescope_session() only
    # checks that steam-launcher.service exists (it ships in both Game
    # Mode and desktop mode, see its own docstring). SteamOS's unit
    # does its own termination via
    # `ExecStop=... kill -TERM $(pgrep -P $MAINPID || echo $MAINPID)`,
    # which fails outright when $MAINPID comes through empty (pgrep
    # prints usage, kill gets no pid, unit exits
    # status=2/INVALIDARGUMENT) -- so the service "stops" while Steam
    # itself keeps running. stop_steam()'s own `steam -shutdown` is
    # what actually brings it down in that case.
    if steam_restart.is_steam_running():
        steam_restart.stop_steam()

    if steam_restart.is_steam_running():
        # Unmask again before raising -- leaving the unit masked would
        # strand Steam un-startable for the user afterward, which is a
        # worse failure than the one being reported.
        _systemctl("unmask", SERVICE)
        raise steam_restart.SteamStopError(
            "Steam didn't shut down when asked, so changes weren't applied -- "
            "please quit Steam yourself and try again"
        )


def exit_maintenance_mode():
    """Bring Steam back the way it was actually running.

    The systemd unit is only used when Game Mode is genuinely active
    (is_game_mode_active, a live gamescope check -- not
    is_gamescope_session, which is also True on a SteamOS *desktop*
    session). Confirmed live (2026-09-04) why that distinction matters:
    `systemctl start steam-launcher.service` launches Steam through
    SteamOS's own launcher, which adds `-steamos3 -steampal` and drops
    `-silent`, so Steam comes back up in Big Picture. On a desktop
    session where Steam had been running as an ordinary windowed app,
    that silently converted it to Big Picture every time SelfSteam
    applied changes. The plain restart_steam() path relaunches it as a
    normal desktop app instead, which is what it was.

    Still unmasks the unit either way -- enter_maintenance_mode masks
    it regardless of mode, so leaving it masked would strand Steam
    un-startable by its normal means afterward.

    Big Picture opened *manually inside* a desktop session is not
    restored, and deliberately isn't chased: confirmed live
    (2026-09-04) that Steam doesn't restore it either. Quitting Steam
    from Big Picture and relaunching it brings it back as an ordinary
    desktop window, and nothing about the mode is persisted anywhere --
    registry.vdf, localconfig.vdf and config.vdf were all diffed
    across the switch and across the exit, and the only delta was
    config.vdf's UI scale factors (exactly halved, a rendering artifact
    of Big Picture's own scale, not a mode flag). So relaunching as a
    desktop app *is* the faithful behaviour here, not a shortfall."""
    if is_gamescope_session():
        _systemctl("unmask", SERVICE)

    if is_game_mode_active():
        _systemctl("start", SERVICE)
        return

    steam_restart.restart_steam()
