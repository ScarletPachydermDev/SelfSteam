#!/usr/bin/env python3
"""Flatpak entrypoint dispatch -- the one binary the manifest exports,
argv-dispatched between two completely different jobs depending on how
it was invoked:

  flatpak run io.github.ScarletPachydermDev.SelfSteam            -- the
      user clicked the app (or a .desktop launcher) -- see launcher_main().
  flatpak run io.github.ScarletPachydermDev.SelfSteam --service  -- the
      systemd user service's own ExecStart -- just runs the real server
      (selfsteam_server.main(), unchanged, watcher thread and all).

Kept as a separate small file rather than teaching selfsteam_server.py
itself about argv/click-to-launch concerns -- that file is the HTTP
server, this one is "what does clicking the installed app icon do",
matching this project's existing pattern of small focused modules
(see the many single-purpose files already here) rather than one file
doing two unrelated jobs.
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request

import config
import host_exec
import selfsteam_server
import steam_restart

_SERVICE_NAME = "selfsteam.service"
_APP_ID = "io.github.ScarletPachydermDev.SelfSteam"

# Functions:
#   _host_run(argv, **kwargs) -- subprocess.run via host_exec.wrap, for host-escape calls.
#   _service_installed() -- whether the real host systemd unit already exists.
#   _install_and_start_service() -- writes + enables + starts the real host systemd unit.
#   _notify(title, body) -- a host notify-send call.
#   launcher_main() -- what runs when the user clicks the installed app icon.
#   main() -- argv dispatch: --service runs the server, anything else runs launcher_main().


def _host_run(argv, **kwargs):
    return subprocess.run(host_exec.wrap(argv), **kwargs)


def _service_installed():
    # `systemctl --user cat` fails (non-zero) if the unit doesn't exist
    # at all -- same real on-disk check install.sh's own equivalent
    # setup implicitly relies on, just asked of the real host's systemd
    # via flatpak-spawn --host rather than run unsandboxed.
    result = _host_run(["systemctl", "--user", "cat", _SERVICE_NAME], capture_output=True)
    return result.returncode == 0


def _unit_text():
    # EnvironmentFile pulls DISPLAY/XDG_RUNTIME_DIR from
    # gamescope-session.target's own env file on a Game Mode session, so
    # windows the service opens (the maintenance splash) can reach the
    # screen. ExecStart runs this same launcher with --service.
    #
    # ExecStop: flatpak runs the sandboxed server in a scope of its own,
    # outside this service, so stopping or restarting the service ended
    # only the outer `flatpak run` and the server kept port 8845; the new
    # one could not start (seen on a Steam Machine: a restart loop, the
    # old version still answering). `flatpak kill` ends the sandbox
    # itself, as the update watcher already does. ExecStartPre does it
    # again before every start, for a leftover ExecStop missed (seen:
    # with the outer `flatpak run` already gone, systemd skipped it).
    return f"""[Unit]
Description=SelfSteam
After=network-online.target

[Service]
Type=simple
EnvironmentFile=-%t/gamescope-environment
ExecStartPre=-flatpak kill {_APP_ID}
ExecStart=flatpak run {_APP_ID} --service
ExecStop=-flatpak kill {_APP_ID}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _write_unit():
    """Write the unit if it differs from what is installed. Returns
    True if it changed."""
    path = "~/.config/systemd/user/" + _SERVICE_NAME
    current = _host_run(["sh", "-c", f"cat {path} 2>/dev/null"], capture_output=True, text=True).stdout
    if current == _unit_text():
        return False
    _host_run(
        ["sh", "-c", f"mkdir -p ~/.config/systemd/user && cat > {path}"],
        input=_unit_text(), text=True, check=True,
    )
    _host_run(["systemctl", "--user", "daemon-reload"])
    return True


def _install_and_start_service():
    _write_unit()
    # loginctl enable-linger is what lets the service keep running at
    # boot with no active login session.
    _host_run(["loginctl", "enable-linger"])
    _host_run(["systemctl", "--user", "enable", "--now", _SERVICE_NAME])


def _notify(title, body):
    # Shells out to the host's own notify-send rather than going through
    # a sandboxed D-Bus/portal notification -- the host escape hatch
    # (--talk-name=org.freedesktop.Flatpak) is already required for the
    # systemctl/loginctl calls above, so this reuses the exact same
    # mechanism instead of asking for a second, notification-specific
    # permission just for this one message.
    _host_run(["notify-send", title, body])


_ARTWORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "steam-artwork")


def _game_running():
    """True while Steam is running a game: every game Steam starts runs
    under its reaper, with "SteamLaunch AppId=" on the command line."""
    # "[S]" so the pattern does not match itself: the flatpak-spawn
    # carrying this pgrep is visible to it too, with the pattern in its
    # own command line, and made every check say a game was running.
    return _host_run(["pgrep", "-f", "[S]teamLaunch AppId="], capture_output=True).returncode == 0


def _add_shortcut_after_update():
    """Add the SelfSteam shortcut, once, from the service: on a fresh
    install just after launcher_main starts it, and on an existing one
    after the update that brings this. The Steam restart that makes
    it appear waits until no game is running: an update can land in the
    middle of one."""
    if config.load().get("selfsteam_shortcut_added"):
        return
    while _game_running():
        time.sleep(30)
    _add_selfsteam_shortcut()


def _add_selfsteam_shortcut():
    """Add a "SelfSteam" shortcut to Steam that opens the pairing code
    screen, with SelfSteam's own artwork, then restart Steam so it shows
    up. Steam only reads shortcuts.vdf at start, so without the restart
    the shortcut would not appear until the next one. Warns first: the
    restart closes Steam, which in Game Mode blanks the screen briefly.

    Skipped quietly when Steam is not installed or has never been run
    (no userdata folder to write to) -- SelfSteam still works without
    it, from another device."""
    import create_webapp  # heavy import; only needed on this one run
    assets = {
        os.path.splitext(f)[0]: os.path.join(_ARTWORK_DIR, f)
        for f in os.listdir(_ARTWORK_DIR)
    }
    try:
        create_webapp.register_steam_shortcut(
            "SelfSteam", None, assets,
            # The code screen itself, run directly: as the game Steam is
            # running, Steam puts it in front and gives it the controller.
            launch_args=["/usr/bin/python3", selfsteam_server.CODE_SCREEN_PATH],
        )
    except Exception:  # noqa: BLE001 -- no Steam, or a userdata it cannot write
        return
    config.save(selfsteam_shortcut_added=True)
    if not steam_restart.is_steam_running():
        return  # Steam reads the new shortcut when it next starts
    _notify("SelfSteam", "Added SelfSteam to your Steam library. Restarting Steam in 5 seconds so it shows up.")
    time.sleep(5)
    try:
        steam_restart.restart_steam()
    except Exception:  # noqa: BLE001
        _notify("SelfSteam", "Restart Steam to see the SelfSteam shortcut.")


def _wait_for_server():
    """On a first install the service has only just been started and
    may not be listening yet; the code screen needs it for the code."""
    url = f"http://127.0.0.1:{os.environ.get('SELFSTEAM_SERVER_PORT', '8845')}/code"
    for _ in range(20):
        try:
            urllib.request.urlopen(url, timeout=5).read()
            return
        except OSError:
            time.sleep(0.5)


def _show_code_screen():
    """Run the code screen on the host (see code_screen.py on why it
    runs there), from the copy the server keeps outside the Flatpak --
    put there now too, in case the server has not started yet."""
    selfsteam_server._install_code_screen()
    _host_run(["python3", selfsteam_server.CODE_SCREEN_PATH])


def launcher_main():
    """What runs when the user clicks the installed app icon. Sets up
    the persistent background service on the very first run only (a
    no-op check every time after that); always shows the pairing screen
    regardless of whether this run was the one that just installed the
    service or not -- clicking the app is a deliberate "I want to log
    in" action every time, not just a first-run-only trigger. The code
    screen is only ever opened from here and from the Steam shortcut."""
    if not _service_installed():
        _install_and_start_service()
        _notify("SelfSteam", "Running in the background -- will persist through Game Mode.")
        # The SelfSteam shortcut is added by the service just started
        # (_add_shortcut_after_update), the same way as after an update;
        # adding it here as well would restart Steam twice.
    _wait_for_server()
    _show_code_screen()


def main():
    if "--service" in sys.argv[1:]:
        try:
            # Brings a unit from an older version up to date; it takes
            # effect from the service's next start.
            _write_unit()
        except Exception:  # noqa: BLE001 -- never stop the server starting
            pass
        threading.Thread(target=_add_shortcut_after_update, daemon=True).start()
        selfsteam_server.main()
    else:
        launcher_main()


if __name__ == "__main__":
    main()
