"""Run by launch.sh before every shortcut starts. If the command is Cemu's
Flatpak and Cemu has no settings.xml yet, write a minimal one so Cemu
skips its Getting Started wizard and boots straight into the game.

standalone_emulators._cemu_configure_game_dir already does this when a
shortcut is created, but that only covers the moment of creation. Wiping
or resetting Cemu's config afterwards brought the wizard back on the next
launch, and games froze behind it (confirmed live on the Machine with
Wind Waker HD). Cemu gates the wizard purely on settings.xml existing
(CemuApp.cpp), so writing the file here is enough.

Fullscreen and the separate GamePad window are on, the same two
choices the wizard offers (element names taken from a settings.xml the
wizard itself saved). Graphics use Vulkan (Graphic/api 1; a file
without it falls back to 0, OpenGL). Audio is set to Cubeb because a settings.xml without it loads as
DirectSound, which doesn't exist on Linux; see _cemu_fix_audio_api in
standalone_emulators.py for the freeze that causes. Kept standalone
(stdlib only) because this file is copied next to launch.sh, away from
the rest of SelfSteam.
"""
import os
import sys

CEMU_APP_ID = "info.cemu.Cemu"

_MINIMAL_SETTINGS = """<?xml version='1.0' encoding='utf-8'?>
<content><fullscreen>true</fullscreen><open_pad>true</open_pad><Graphic><api>1</api></Graphic><Audio><api>3</api><TVDevice>default</TVDevice></Audio></content>
"""


def ensure_cemu_settings(argv):
    if CEMU_APP_ID not in argv:
        return
    path = os.path.join(os.path.expanduser("~"), ".var", "app", CEMU_APP_ID,
                        "config", "Cemu", "settings.xml")
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(_MINIMAL_SETTINGS)


if __name__ == "__main__":
    try:
        ensure_cemu_settings(sys.argv[1:])
    except Exception:
        pass
