# Development

## Layout

One file per job, at the repo root:

| File | Job |
|---|---|
| `selfsteam_server.py` | Web server and every page |
| `selfsteam_launcher.py` | Flatpak entrypoint, service setup, Steam shortcut |
| `standalone_emulators.py` | Emulators tab |
| `retroarch_cores.py` | RetroArch tab |
| `appimage_apps.py`, `flathub_browse.py` | Apps tab |
| `create_webapp.py`, `shortcuts_vdf.py` | Writing Steam shortcuts and artwork |
| `sdl_screen.py`, `code_screen.py`, `splash.py` | Host-side SDL screens |
| `launch.sh` | Launcher wrapper every shortcut runs through |
| `packaging/` | Flatpak manifest, metainfo (release notes), icons |
| `vendor/` | Bundled third-party code and art |

## Running a test copy

Run the server from a checkout on another port, beside the installed app:

```
SELFSTEAM_SERVER_PORT=8847 python3 selfsteam_server.py
```

On SteamOS, run it with the Flatpak's Python so its libraries are there:

```
flatpak run --command=python3 io.github.ScarletPachydermDev.SelfSteam selfsteam_server.py
```

## Checks

`ruff check` (settings in `ruff.toml`) looks for real mistakes and skips
`vendor/`. GitHub runs it on every push.

## Releases

1. Add a `<release>` entry to
   `packaging/io.github.ScarletPachydermDev.SelfSteam.metainfo.xml`.
2. Tag `vX.Y.Z` and push the tag.
3. GitHub Actions builds the Flatpak, attaches it to the release and
   publishes the update repo on `gh-pages`.

Builds of `master` without a tag produce a test bundle; it does not carry
the update address, so install releases on devices you keep.
