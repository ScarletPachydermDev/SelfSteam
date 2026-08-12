# Gridge Web Server

A headless companion to [Gridge](https://github.com/ScarletPachydermDev/Gridge) -- a small web UI for adding Steam shortcuts (with SteamGridDB artwork) from another device, while the target machine stays in Steam's Game Mode.

No JavaScript. Plain server-rendered HTML forms.

## Status

Early prototype, live-tested end to end on real hardware (2026-08-12): web request in, Steam stopped, a full-screen "please wait" splash shown in Game Mode, the shortcut + artwork written safely while Steam is down, Steam restarted, shortcut launches correctly.

Not yet done:

- Flatpak packaging (currently runs as plain `.py` files via `python3 gridge_server.py`)
- Boot-persistence (systemd user service + `loginctl enable-linger`, matching the pattern `org.jellyfin.JellyfinServer`'s Flatpak uses)
- **No authentication on the web UI yet** -- anyone reachable on the LAN can currently add shortcuts and trigger a Steam restart. Don't expose this beyond a trusted home network.

## Why a restart is needed at all

Writing to Steam's `shortcuts.vdf` while Steam is running gets silently overwritten by Steam's own periodic re-save -- confirmed live, and matches [ChimeraOS's own documented behavior](https://github.com/ChimeraOS/chimera). So this stops Steam first, applies the write, then restarts it -- automated so it doesn't feel like a jarring reset:

1. `steamos_session.py` masks and stops Steam's systemd unit (SteamOS's `gamescope-session.target` has `Upholds=steam-launcher.service`, which restarts Steam instantly on a plain `stop` -- masking is what actually holds it down).
2. `gamescope_splash.py` + `splash.py` show a full-screen "please wait" window, foregrounded in gamescope's compositor via the same `STEAM_GAME`/`GAMESCOPECTRL_BASELAYER_APPID` X11 properties `gamescope-fg` uses (no dependency on any third-party tool).
3. The shortcut + artwork write happens via the same backend code [Gridge](https://github.com/ScarletPachydermDev/Gridge) itself uses.
4. Steam is unmasked and restarted.

On plain desktop Linux (no `gamescope-session.target`), this falls back to a simple kill/relaunch with no splash needed, since a normal window manager is already there.

## Running it

```
python3 gridge_server.py
```

Needs a SteamGridDB API key, either in `STEAMGRIDDB_API_KEY` or via the same config file Gridge's GUI writes (`$XDG_CONFIG_HOME/gridge/config.json`).

## License

[MIT](LICENSE)
