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


def _install_and_start_service():
    # Same real unit shape as install.sh's own -- EnvironmentFile pulls
    # DISPLAY/XDG_RUNTIME_DIR from gamescope-session.target's own env
    # file on a real Game Mode session (see install.sh's own comment on
    # why this is required, not optional, for the auth screen/
    # maintenance splash to be able to open a window at all). ExecStart
    # runs this exact same launcher again, just with --service this
    # time, so systemd's own view of "the command" and a user manually
    # running `flatpak run ... --service` themselves are identical.
    unit = f"""[Unit]
Description=SelfSteam
After=network-online.target

[Service]
Type=simple
EnvironmentFile=-%t/gamescope-environment
ExecStart=flatpak run {_APP_ID} --service
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    _host_run(
        ["sh", "-c", "mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/" + _SERVICE_NAME],
        input=unit, text=True, check=True,
    )
    # loginctl enable-linger is what lets the service keep running at
    # boot with no active login session -- same reasoning as install.sh's
    # own call.
    _host_run(["loginctl", "enable-linger"])
    _host_run(["systemctl", "--user", "daemon-reload"])
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
            launch_args=["/usr/bin/flatpak", "run", _APP_ID],
        )
    except Exception:  # noqa: BLE001 -- no Steam, or a userdata it cannot write
        return
    _notify("SelfSteam", "Added SelfSteam to your Steam library. Restarting Steam in 5 seconds so it shows up.")
    time.sleep(5)
    try:
        steam_restart.restart_steam()
    except Exception:  # noqa: BLE001
        _notify("SelfSteam", "Restart Steam to see the SelfSteam shortcut.")


def _show_code_via_server():
    """Ask the running server to show the pairing code, by opening its
    /login page as a signed-out visitor would. The code only exists in
    the server's own memory: a screen shown from this process would
    carry a code of its own that the server has never heard of, and
    could never be used. Launched from a Steam shortcut, this process is
    also the "game" Steam is watching, and a window it foregrounds fights
    Steam over the screen (seen on a Steam Machine: the code blinked,
    then a black screen). Leaving the window to the server, and exiting,
    takes the same path as every other time the code is shown.

    Retries briefly: on a first install the service has only just been
    started and may not be listening yet."""
    url = f"http://127.0.0.1:{os.environ.get('SELFSTEAM_SERVER_PORT', '8845')}/login?launcher=1"
    for _ in range(20):
        try:
            urllib.request.urlopen(url, timeout=5).read()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def launcher_main():
    """What runs when the user clicks the installed app icon. Sets up
    the persistent background service on the very first run only (a
    no-op check every time after that); always shows the pairing screen
    regardless of whether this run was the one that just installed the
    service or not -- clicking the app is a deliberate "I want to log
    in" action every time, not just a first-run-only trigger."""
    if not _service_installed():
        _install_and_start_service()
        _notify("SelfSteam", "Running in the background -- will persist through Game Mode.")
        # Already shown our own first-run notice directly above, right
        # now -- clears the marker so the background gamescope-entry
        # watcher (selfsteam_server._watch_for_first_gamescope_entry)
        # doesn't also fire a second, redundant pairing screen the next
        # time Game Mode happens to be entered.
        config.set_pending_first_show(False)
        _add_selfsteam_shortcut()
    if not _show_code_via_server():
        _notify("SelfSteam", "SelfSteam's background service isn't responding, so no code can be shown.")


def main():
    if "--service" in sys.argv[1:]:
        selfsteam_server.main()
    else:
        launcher_main()


if __name__ == "__main__":
    main()
