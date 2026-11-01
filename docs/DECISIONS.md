# Decisions

Why some things are the way they are. Longer reasoning is in the code
comments of the files named.

**x86_64 only.** The Steam Deck and Steam Machine are x86_64, and most of
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
setting.** A fixed, predictable target; SelfSteam only changes a value it
set itself or the emulator's default. (`ra_resolution.py`,
`emu_resolution.py`)

**Changes are queued, then Steam restarts once.** Steam only reads
shortcuts when it starts, and a file written behind a running Steam can be
overwritten by it.
