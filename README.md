# SelfSteam

A headless companion, a small web UI for adding Steam shortcuts (with SteamGridDB artwork) from another device, while the target machine stays in Steam's Game Mode.

![](screenshots/main.png)

## Status

Live-tested end to end on real hardware: web request in, a random code shown full-screen on the TV to log in (same pattern ChimeraOS's own authenticator and `emerytech/couchside` both use -- no accounts, "can you see the screen" is the access control), Steam stopped, a full-screen "please wait" splash, the shortcut + artwork written safely while Steam is down, Steam restarted, shortcut launches correctly -- now running as a real boot-persistent systemd user service via `install.sh`.

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

## Code overview

| File | Purpose |
|---|---|
| `selfsteam_server.py` | The whole app -- headless web UI (HTTP server + HTML/CSS/JS) for adding Steam shortcuts from another device on the network. Everything else supports this. |
| `create_webapp.py` | Core shortcut-creation logic: SGDB search, artwork download, writing the actual `shortcuts.vdf` entry + grid images. |
| `shortcuts_vdf.py` | Low-level reader/writer for Steam's binary `shortcuts.vdf` format itself. |
| `retroarch_cores.py` | RetroArch's console/core catalog -- install RetroArch + a specific core, build launch args, track BIOS requirements. |
| `standalone_emulators.py` | The non-RetroArch emulator catalog (PCSX2, RPCS3, Eden, etc.) -- install (Flathub or AppImage), launch args, keys/firmware/BIOS handling. |
| `streaming_services.py` | Lookup table mapping a bare name (e.g. "Netflix") to its real URL + SGDB search term. |
| `service_resolver.py` | Turns free-text search input into a resolved URL, using `streaming_services.py` or raw URL parsing. |
| `sgdb_client.py` | Minimal SteamGridDB API client (stdlib only) -- searches games, fetches artwork candidates. |
| `pending_queue.py` | The "staged changes" queue -- shortcuts to add/remove, held until the next "Save changes and restart Steam" commit. |
| `maintenance.py` | Orchestrates one full Steam maintenance window: stop Steam, show a splash, run the actual write, restart Steam. |
| `steamos_session.py` | Gamescope/SteamOS-aware version of "stop/start Steam" (masks `steam-launcher.service` instead of a plain kill, since systemd would just relaunch it). |
| `steam_restart.py` | Plain-desktop kill/wait/relaunch of Steam, with the correct Flatpak-vs-native launch path. |
| `steam_paths.py` | Locates Steam's real userdata directory across native and Flatpak installs. |
| `auth.py` | The pairing-code auth scheme itself -- code generation, session/remember-device tokens, no passwords. |
| `auth_display.py` | Shows/dismisses the on-screen pairing code window on demand. |
| `auth_screen.py` | The actual fullscreen GTK window that displays the pairing code. |
| `config.py` | Persistent app settings (SGDB API key, etc.) as JSON. |
| `host_exec.py` | Escapes SelfSteam's own Flatpak sandbox to run commands / locate binaries on the real host. |
| `multipart_upload.py` | Minimal streaming `multipart/form-data` parser for file uploads (ROMs, BIOS, keys) -- avoids buffering huge files in memory. |
| `browser_picker.py` | Detects which Flatpak web browsers are installed, for the "which browser opens this URL shortcut" dropdown. |
| `browser_launcher.py` | Builds the kiosk-mode launch command for non-Edge browsers. |
| `edge_launcher.py` | Same, specifically for detecting/launching Microsoft Edge. |
| `gamescope_splash.py` | Foregrounds a window inside gamescope's compositor via its own X11 control protocol. |
| `splash.py` | The standalone "please wait" fullscreen window shown while Steam is down mid-maintenance. |
| `sync_gamescope_resolution.py` | Sizes a shortcut's nested Xwayland display to match gamescope's real resolution. |
| `window_titles.py` | Shared window-title constants used by the launcher and the foregrounding logic to find each other's windows. |

Rough shape: `selfsteam_server.py` is the app; `create_webapp.py`/`shortcuts_vdf.py`/`retroarch_cores.py`/`standalone_emulators.py` do the actual shortcut work; the `auth_*` files handle pairing; the `steam_*`/`maintenance.py`/`steamos_session.py` group handles the Steam-restart dance; and the rest are small focused utilities each solving one specific platform quirk.

## License

[MIT](LICENSE)
