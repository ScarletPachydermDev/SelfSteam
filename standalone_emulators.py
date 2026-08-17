"""Standalone (non-RetroArch) emulator catalog and launch helpers.

Unlike retroarch_cores.py -- one RetroArch app loading many interchangeable
cores via a single -L flag -- each entry here is its own separate, self-
contained Flatpak application with its own CLI conventions. There's no
shared launch pattern across them, so each gets its own small
args-builder function rather than a generic one.

Deliberately Flatpak-only for now: no AppImage/native-binary discovery
the way EmuDeck's own per-emulator wrapper scripts do (searching a known
folder for an AppImage, falling back to Flatpak, chmod +x, etc.) --
that's real extra complexity worth adding later if it's actually needed,
not assumed up front. Every entry here is a Flatpak app already
confirmed to exist on Flathub under the id listed.
"""
import shlex

import host_exec


def _dolphin_args(romfile):
    # -b/--batch: exit Dolphin when the game closes instead of returning
    # to its own game list UI. -e/--exec: load and run this file
    # directly. No BIOS/IPL needed -- Dolphin ships its own built-in
    # GameCube IPL emulation (HLE) by default; a real console's IPL/font
    # file is an optional accuracy enhancement, not required to boot.
    #
    # -C Dolphin.Display.Fullscreen=True: Dolphin has no dedicated
    # --fullscreen switch (confirmed via its own --help), only this
    # generic -C <System>.<Section>.<Key>=<Value> config-override flag.
    # "Dolphin.Display.Fullscreen" was inferred from Dolphin.ini's own
    # [Display] section (not persisted anywhere -C overrides could be
    # read back from to confirm ahead of time) and confirmed live by
    # launching a real shortcut on X1 -- it actually opens fullscreen.
    return ["-b", "-C", "Dolphin.Display.Fullscreen=True", "-e", shlex.quote(romfile)]


# Each entry: app_id (real Flathub id), needs_bios (whether the picker
# should show a BIOS/firmware field at all -- Dolphin doesn't need one),
# and args(romfile) -> argv (already shell-quoted where needed, same
# "ready to append after flatpak run <app_id>" contract as
# retroarch_cores.launch_args).
EMULATORS = {
    "Nintendo GameCube / Wii (Dolphin)": {
        "app_id": "org.DolphinEmu.dolphin-emu",
        "needs_bios": False,
        "args": _dolphin_args,
    },
}


def installed(name):
    entry = EMULATORS.get(name)
    if not entry:
        return False
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return False
    import subprocess
    result = subprocess.run(
        host_exec.wrap([flatpak, "info", entry["app_id"]]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def install(name):
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    import subprocess
    flatpak = host_exec.which("flatpak")
    subprocess.run(
        host_exec.wrap([flatpak, "install", "--user", "-y", "flathub", entry["app_id"]]),
        check=True,
    )


def launch_args(name, romfile):
    entry = EMULATORS.get(name)
    if not entry:
        return None
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return None
    return [flatpak, "run", entry["app_id"], *entry["args"](romfile)]
