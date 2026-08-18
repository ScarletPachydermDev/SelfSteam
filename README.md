# SelfSteam

A headless companion to [Gridge](https://github.com/ScarletPachydermDev/gridge-desktop) -- a small web UI for adding Steam shortcuts (with SteamGridDB artwork) from another device, while the target machine stays in Steam's Game Mode.

No JavaScript. Plain server-rendered HTML forms.

## Status

Live-tested end to end on real hardware: web request in, a random code shown full-screen on the TV to log in (same pattern ChimeraOS's own authenticator and `emerytech/couchside` both use -- no accounts, "can you see the screen" is the access control), Steam stopped, a full-screen "please wait" splash, the shortcut + artwork written safely while Steam is down, Steam restarted, shortcut launches correctly -- now running as a real boot-persistent systemd user service via `install.sh`.

Not yet done:

- Flatpak packaging (currently installs as plain `.py` files, see `install.sh`)
- Session cookies are in-memory only -- a server restart logs everyone out (no persistence layer yet, by design for now)

## Why a restart is needed at all

Writing to Steam's `shortcuts.vdf` while Steam is running gets silently overwritten by Steam's own periodic re-save -- confirmed live, and matches [ChimeraOS's own documented behavior](https://github.com/ChimeraOS/chimera). So this stops Steam first, applies the write, then restarts it -- automated so it doesn't feel like a jarring reset:

1. `steamos_session.py` masks and stops Steam's systemd unit (SteamOS's `gamescope-session.target` has `Upholds=steam-launcher.service`, which restarts Steam instantly on a plain `stop` -- masking is what actually holds it down).
2. `gamescope_splash.py` + `splash.py` show a full-screen "please wait" window, foregrounded in gamescope's compositor via the same `STEAM_GAME`/`GAMESCOPECTRL_BASELAYER_APPID` X11 properties `gamescope-fg` uses (no dependency on any third-party tool).
3. The shortcut + artwork write happens via the same backend code [Gridge](https://github.com/ScarletPachydermDev/gridge-desktop) itself uses.
4. Steam is unmasked and restarted.

On plain desktop Linux (no `gamescope-session.target`), this falls back to a simple kill/relaunch with no splash needed, since a normal window manager is already there.

## Installing

```
./install.sh
```

Installs to `~/.local/opt/selfsteam`, reuses the SteamGridDB API key from Gridge's desktop app if it's already set up (or asks for one), and installs + starts a systemd user service that survives reboots (`loginctl enable-linger` + `systemctl --user enable`). Not sandboxed yet, so this can just do all of that directly -- unlike e.g. Jellyfin's Flatpak, which has to ship a unit template plus a copy-paste setup script because its sandbox can't touch `~/.config/systemd/user` or run `systemctl`/`loginctl` itself.

Re-running `install.sh` after pulling an update restarts the service to pick up the change.

On a gamescope/SteamOS session, the generated unit pulls `DISPLAY`/`XDG_RUNTIME_DIR` from `%t/gamescope-environment` -- the same file `steam-launcher.service` itself uses. Without it, a systemd user service doesn't inherit the graphical session's environment at all, which silently breaks the auth screen and maintenance splash (confirmed live: `Gdk.Display.get_default()` returns `None` and the launched window just crashes).

### Running it directly, without installing

```
python3 selfsteam_server.py
```

Needs a SteamGridDB API key, either in `STEAMGRIDDB_API_KEY` or via SelfSteam's own config file (`$XDG_CONFIG_HOME/selfsteam/config.json`) -- or Gridge's GUI-written one directly (`$XDG_CONFIG_HOME/gridge/config.json`), which `config.py` migrates automatically on first run if the SelfSteam one doesn't exist yet.

## License

[MIT](LICENSE)
