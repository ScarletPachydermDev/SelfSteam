#!/bin/sh
# Installs Gridge Server as a boot-persistent systemd user service.
#
# Not sandboxed (no Flatpak yet), so unlike e.g. Jellyfin's Flatpak --
# which has to ship a unit template plus a copy-paste setup script,
# because its sandbox can't touch ~/.config/systemd/user or run
# `systemctl --user`/`loginctl` itself -- this script just does
# everything directly.
set -e

INSTALL_DIR="$HOME/.local/opt/gridge-web-server"
SERVICE_FILE="$HOME/.config/systemd/user/gridge-server.service"
CONFIG_FILE="$HOME/.config/gridge/config.json"
FLATPAK_CONFIG_FILE="$HOME/.var/app/io.github.ScarletPachydermDev.Gridge/config/gridge/config.json"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

echo "Installing Gridge Server to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py "$SCRIPT_DIR/launch-browser.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.py "$INSTALL_DIR/launch-browser.sh"

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
else
    echo "Using existing config at $CONFIG_FILE"
fi

mkdir -p "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Gridge Server
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
ExecStart=/usr/bin/python3 $INSTALL_DIR/gridge_server.py
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
systemctl --user enable gridge-server.service
# restart, not just enable --now: re-running this script after a code/
# unit-file update should apply it to the running instance, not leave
# a stale already-running process untouched.
systemctl --user restart gridge-server.service

echo
echo "Gridge Server is running and will start automatically on boot."
systemctl --user --no-pager status gridge-server.service
