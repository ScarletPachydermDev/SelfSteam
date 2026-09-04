#!/bin/sh
# Installs the SelfSteam server as a boot-persistent systemd user service.
#
# Not sandboxed (no Flatpak yet), so unlike e.g. Jellyfin's Flatpak --
# which has to ship a unit template plus a copy-paste setup script,
# because its sandbox can't touch ~/.config/systemd/user or run
# `systemctl --user`/`loginctl` itself -- this script just does
# everything directly.
set -e

INSTALL_DIR="$HOME/.local/opt/selfsteam"
SERVICE_FILE="$HOME/.config/systemd/user/selfsteam.service"
# ~/.config/selfsteam, matching config.py's own CONFIG_DIR -- if this
# app already ran once under its previous "gridge" name, config.py's
# own module-level migration already carried config.json/
# pending_queue.json/remembered_devices.json over to this path the
# first time selfsteam_server.py started, so the check below just finds
# them already there. This only matters for a genuinely fresh machine
# that has never run either name's Python side yet.
CONFIG_FILE="$HOME/.config/selfsteam/config.json"
# Gridge's own desktop app was never renamed, just its GH repo slug (now
# gridge-desktop) -- its real Flatpak app-id and internal config path
# are unchanged, so this keeps pointing at "Gridge" on purpose.
FLATPAK_CONFIG_FILE="$HOME/.var/app/io.github.ScarletPachydermDev.Gridge/config/gridge/config.json"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

echo "Installing SelfSteam to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py "$SCRIPT_DIR/launch.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.py "$INSTALL_DIR/launch.sh"

mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$FLATPAK_CONFIG_FILE" ]; then
        echo "Found an existing SteamGridDB API key from Gridge's desktop app -- reusing it."
        cp "$FLATPAK_CONFIG_FILE" "$CONFIG_FILE"
    else
        printf "SteamGridDB API key (from steamgriddb.com/profile/preferences/api): "
        read -r api_key < /dev/tty
        printf '{"sgdb_api_key": "%s"}\n' "$api_key" > "$CONFIG_FILE"
    fi
    # One-time marker -- see config.py's own get_pending_first_show/
    # set_pending_first_show for why this only happens here, on a
    # genuinely fresh install (this branch only runs when CONFIG_FILE
    # didn't already exist), not on a later re-run of this same script
    # (e.g. to pick up a code update, per this script's own README).
    python3 -c "
import json
with open('$CONFIG_FILE') as f:
    data = json.load(f)
data['pending_first_show'] = True
with open('$CONFIG_FILE', 'w') as f:
    json.dump(data, f, indent=2)
"
else
    echo "Using existing config at $CONFIG_FILE"
fi

mkdir -p "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=SelfSteam
After=network-online.target

[Service]
Type=simple
# Optional (leading "-"): only present on a gamescope/SteamOS session --
# without it, DISPLAY/XDG_RUNTIME_DIR aren't in this service's own
# environment at all (confirmed live: a systemd user service does not
# inherit the graphical session's env vars automatically), which
# silently breaks the auth screen and maintenance splash, both of which
# need a real X11 DISPLAY to foreground a window. Same file
# steam-launcher.service itself reads from.
EnvironmentFile=-%t/gamescope-environment
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/selfsteam_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# loginctl enable-linger is what lets this keep running at boot with no
# active login session -- same piece Jellyfin's own setup script needs,
# just runnable directly here instead of printed for the user to paste.
loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable selfsteam.service
# restart, not just enable --now: re-running this script after a code/
# unit-file update should apply it to the running instance, not leave
# a stale already-running process untouched.
systemctl --user restart selfsteam.service

echo
echo "SelfSteam is running and will start automatically on boot."
systemctl --user --no-pager status selfsteam.service
