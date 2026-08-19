#!/usr/bin/env bash
# Kill and relaunch selfsteam_server.py on X1 with a correctly-resolved
# GUI environment. Run from a dev machine as:
#   ssh migtorr@192.168.8.153 'bash -s' < deploy/restart_on_x1.sh
#
# The XAUTHORITY glob below is the one part of this that's bitten us
# more than once: the real Mutter Xwayland auth file is named
# ".mutter-Xwaylandauth.<random>" (e.g. ".mutter-Xwaylandauth.7TBLU3"),
# NOT ".mutter-Xwayland.<random>" -- a glob missing "auth" silently
# resolves to an empty string with no error anywhere obvious (GTK apps
# and the /login curl check both still work fine), but anything needing
# a real X connection -- notably steam_restart.py's Steam relaunch --
# then dies silently right after Steam's own bootstrapper prints
# "Overriding TZ to ..." and never actually opens a window or registers
# a `steam` pid. This script exists specifically so that glob is typed
# correctly exactly once, instead of retyped (and mistyped) in ad-hoc
# SSH one-liners every session.
set -euo pipefail

PID=$(pgrep -af selfsteam_server | grep -v grep | awk '{print $1}' || true)
if [ -n "$PID" ]; then
    kill "$PID"
    sleep 1
fi

XDG_RUNTIME_DIR=$(loginctl show-user migtorr -p RuntimePath --value 2>/dev/null || echo "/run/user/$(id -u migtorr)")
DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
DISPLAY=:0
WAYLAND_DISPLAY=wayland-0
XAUTHORITY=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name ".mutter-Xwaylandauth.*" 2>/dev/null | head -1)

echo "XAUTHORITY=$XAUTHORITY"

cd ~/selfsteam-dev
env XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    DISPLAY="$DISPLAY" WAYLAND_DISPLAY="$WAYLAND_DISPLAY" XAUTHORITY="$XAUTHORITY" \
    nohup python3 selfsteam_server.py > /tmp/selfsteam.log 2>&1 &
disown

sleep 2
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8845/login
