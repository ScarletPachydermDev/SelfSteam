# How it works

## Pieces

- **Flatpak app** (`io.github.ScarletPachydermDev.SelfSteam`), x86_64, on
  the Freedesktop runtime. One entrypoint, `selfsteam_launcher.py`:
  opened from the app menu it sets up the service and shows the code
  screen; run with `--service` it is the server.
- **Background service**: a systemd user unit (`selfsteam.service`) that
  runs the web server (`selfsteam_server.py`) on port **8845**, at boot and
  through Game Mode.
- **Host access**: the server reaches outside its sandbox with
  `flatpak-spawn --host` (`host_exec.py`) to stop and start Steam, install
  Flatpaks and AppImages, and run its full-screen windows. It has
  `--filesystem=host` to write Steam's files and read ROMs anywhere.

## Adding a shortcut

1. The web UI queues the change (`pending_queue.py`).
2. **Save changes and restart Steam** stops Steam, shows the splash,
   writes `shortcuts.vdf` (`shortcuts_vdf.py`) and the grid artwork into
   `userdata/<id>/config/grid`, then starts Steam again
   (`maintenance.py`, `steam_restart.py`).
3. Every shortcut runs through the launcher wrapper in
   `~/.local/share/Steam/selfsteam-launcher/` (`launch.sh`), which, just
   before the game starts, applies the default resolution
   (`ra_resolution.py`, `emu_resolution.py`), N64 overscan cropping
   (`ra_overscan.py`) and Vita3K's install-then-run step (`vita3k_launch.py`).

## Full-screen windows

The code screen (`code_screen.py`) and the "Restarting Steam…" splash
(`splash.py`) run on the host, not in the sandbox, drawn with the system's
SDL2 and FreeType through ctypes (`sdl_screen.py`). The server copies them
to `~/.local/share/selfsteam/` when it starts.

- The **SelfSteam** Steam shortcut runs the code screen directly, so Steam
  treats it as the game: it gets the screen and the controller.
- The code itself only lives in the server's memory; the screen reads it
  from `GET /code`, answered on `127.0.0.1` only.

## Emulators and RetroArch

- `standalone_emulators.py`: install, configure and launch every Emulators
  tab app; keys, firmware, DLC and updates; `.nsz` conversion; Preflight.
- `retroarch_cores.py`: consoles, cores, BIOS files and RetroArch settings.
- `appimage_apps.py`: the Apps tab's AppImages.
- `sgdb_client.py`: SteamGridDB artwork.

## Updates

The server compares its own version with the installed one every 15
minutes, and restarts onto a new one. Opening SelfSteam does the same
check at once. The service unit ends a leftover server before every start,
so a restart never finds the old one still holding the port.
