#!/bin/sh
# Every shortcut SelfSteam creates points Steam's own "exe" field at this
# script, with the real command living in the shortcut's LaunchOptions --
# so this runs a few fixups and then exec's that command unchanged.
#
# It is needed for GAMES just as much as for browsers, despite the name it
# used to have (launch-browser.sh, from back when this project only made
# browser shortcuts). Specifically:
#
#   * The resolution sync below is NOT a browser workaround. Gamescope
#     gives every non-Steam shortcut its own nested Xwayland, pinned at
#     whatever resolution it started at -- so an emulator docked to a 4K
#     TV keeps rendering at, say, 1280x800 and gets upscaled. The app
#     cannot fix this itself either way: the nested display *reports* the
#     stale size, so there is nothing for it to adapt to. If anything a
#     game cares about real render resolution more than a web page does.
#
#   * Under Flatpak Steam this wrapper is outright mandatory, for any
#     shortcut type. That install gets a different body (see create_
#     webapp.py's _FLATPAK_STEAM_LAUNCH_SCRIPT) ending in `flatpak-spawn
#     --host`, because Flatpak Steam's sandbox has its own /usr and cannot
#     see host binaries at all -- no wrapper means nothing launches.
#
#   * Only the LD_PRELOAD strip below has browser-specific evidence behind
#     it (see its own comment). It is cheap and plausibly useful for
#     emulators too, but that has not been demonstrated.
#
# Steam sets LD_PRELOAD for its overlay in every child process it launches,
# regardless of the shortcut's AllowOverlay setting (confirmed via
# coredumpctl during kiosk-launcher testing: it crashed a bundled Electron
# build hard). Stripping it here before exec'ing the real browser avoids
# that class of crash for any browser we shell out to.
unset LD_PRELOAD

# Ask Gamescope to match this shortcut's nested resolution to whatever
# it's actually outputting right now (Deck screen or a docked TV) rather
# than staying pinned to a stale resolution. No-ops harmlessly if not
# running under Gamescope (e.g. Desktop Mode) or on any error. The sleep
# gives Gamescope a moment to apply the resize before the browser starts
# querying display info.
python3 "$(dirname "$0")/sync_gamescope_resolution.py" 2>/dev/null
sleep 0.3

exec "$@"
