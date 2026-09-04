#!/bin/sh
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
