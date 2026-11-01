# Decisions

Why some things are the way they are. Longer reasoning is in the code
comments of the files named.

**Linux on x86_64 only.** SelfSteam drives Steam's Linux Game Mode
(gamescope) and installs Linux Flatpaks and AppImages. The Steam Deck and Steam Machine are x86_64, and most of
the emulators and AppImages SelfSteam installs are only published for it.

**Own Flatpak repo, not Flathub.** SelfSteam has to reach outside its
sandbox (Steam's files, restarting Steam, installing emulators, a systemd
service), which Flathub only allows with a strong case, and its manifest
was written with AI help, which Flathub does not accept.

**Freedesktop runtime, not GNOME.** Nothing in SelfSteam uses GTK any more,
so the much larger GNOME runtime bought nothing.

**Code screen and splash in SDL, on the host.** Run from the Steam
shortcut, the code screen is the game Steam is running, so Steam puts it in
front and hands it the controller with no window tricks. The earlier GTK
screen, shown from the sandbox, could not be closed with a controller and
had to close itself on a timer. (`sdl_screen.py`, `code_screen.py`)

**FreeType, not SDL2_ttf.** SteamOS 3.9 dropped SDL2_ttf; FreeType is the
engine it was built on, and every desktop Linux has it. (`sdl_screen.py`)

**The server never shows the code on its own.** Only the app and its Steam
shortcut open the code screen, so nothing pops up over a game when a phone
opens the page anymore.

**1080p by default, one step lower on a Steam Deck, never over your own
setting.** 3D consoles render above their own resolution by default, so
games look sharp on a TV without any setup.

- *Target:* the multiple of the console's own picture that fits 1080 lines.
  It is fixed, not matched to the screen: predictable, and anyone who wants
  more can raise it in the emulator.
- *Steam Deck:* one step lower than that, since its GPU is the weakest
  SelfSteam runs on and its own screen is 800 lines anyway.
- *Your setting wins:* SelfSteam only changes a value that is still the
  emulator's default, or one it set itself (it keeps a note of what it
  wrote). Once you pick a resolution in the emulator, it is never touched
  again.
- *When:* when a shortcut is created or saved, and again just before each
  launch. The launch-time pass reaches an emulator installed in that same
  step, whose settings file does not exist until its first run.
- *Which ones:* only 3D consoles. Rendering 2D pixel art larger adds no
  detail, so 2D cores and emulators are left alone. Consoles that already
  render at 720p or 1080p (Switch, Wii U, PS3, PS4, Vita, Xbox 360) gain
  nothing and are left alone too.

RetroArch tab (`ra_resolution.py`), per core:

| Console | Core | 1080p value |
|---|---|---|
| Nintendo 64 | ParaLLEl N64, Mupen64Plus-Next | 1440x1080 |
| PlayStation 1 | Beetle PSX HW | 4x |
| PlayStation 1 | SwanStation | 4x |
| PlayStation 2 | LRPS2 | 2x |
| Sega Saturn | Kronos | 4x |
| Sega Dreamcast | Flycast | 1440x1080 |
| PSP | PPSSPP | 1440x816 |
| Nintendo DS | DeSmuME, melonDS | 2x (two screens stacked) |
| Nintendo 3DS | Azahar, Citra | 2x (two screens stacked) |

Not included: PCSX-ReARMed (its only option is an ARM-only enhancement)
and Beetle Saturn (no resolution option). melonDS is also switched to its
OpenGL renderer, the only one that scales.

Emulators tab (`emu_resolution.py`):

| Emulator | Console | 1080p value |
|---|---|---|
| Dolphin (and Wheel Wizard) | GameCube, Wii | 2x |
| PCSX2 | PlayStation 2 | 2x |
| Play! | PlayStation 2 | 2x |
| DuckStation | PlayStation 1 | 4x |
| xemu | Xbox | 2x |
| gopher64 | Nintendo 64 | 4x |
| M64Py | Nintendo 64 | 1440x1080 |
| Flycast | Dreamcast | 1080 lines |
| melonDS | Nintendo DS | 2x (OpenGL renderer) |
| Azahar | Nintendo 3DS | 2x |

Not included: Rosalie's Mupen GUI (not checked yet), and PPSSPP, which
already matches its window by itself.

**Changes are queued, then Steam restarts once.** Steam only reads
shortcuts when it starts, and a file written behind a running Steam can be
overwritten by it.
