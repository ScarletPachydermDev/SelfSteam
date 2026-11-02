#!/usr/bin/env python3
"""One RetroArch save folder per console: saves/<console>/.

RetroArch names a game's save after the ROM file alone, and SelfSteam
keeps every core's saves in one shared folder (retroarch_cores.
share_saves_between_cores, so switching cores keeps the save). That
lets two consoles' games with the same file name share one save:
"Game.n64" and "Game.gbc" both write "Game.srm", and playing one wipes
the other. Reported to
EmuDeck as data loss (dragoonDorise/EmuDeck#1341).

A folder per console fixes both at once: cores for one console still
share it, and two consoles never do. RetroArch has no "per console"
option of its own (only per core or per ROM folder, and uploads all
land in one folder), so the folder is given per launch with
--appendconfig, which overrides savefile_directory for that run only.

Run by launch.sh in place of the shortcut's own command, like
vita3k_launch.py: it adds the override and then execs the command. Any
failure runs the command unchanged, so a game always starts.

The first time a game launches this way, its save is moved from the
shared folder into the console's, unless one is already there. A save
that two games already wrote over cannot be split back apart; it goes
with whichever game is launched first.
"""

import os
import re
import sys

_RA_ID = "org.libretro.RetroArch"
_RA_CONFIG = os.path.expanduser(f"~/.var/app/{_RA_ID}/config/retroarch")
_OVERRIDES = os.path.join(_RA_CONFIG, "selfsteam-saves")

# Core file name -> console, for cores that run one console.
_CONSOLE_FOR_CORE = {
    "mupen64plus_next": "Nintendo 64",
    "parallel_n64": "Nintendo 64",
    "mgba": "Game Boy Advance",
    "vbam": "Game Boy Advance",
    "melonds": "Nintendo DS",
    "desmume": "Nintendo DS",
    "blastem": "Sega Genesis",
    "picodrive": "Sega 32X",
    "mednafen_saturn": "Sega Saturn",
    "kronos": "Sega Saturn",
    "flycast": "Sega Dreamcast",
    "pcsx_rearmed": "PlayStation 1",
    "mednafen_psx_hw": "PlayStation 1",
    "swanstation": "PlayStation 1",
    "pcsx2": "PlayStation 2",
    "ppsspp": "PSP",
    "mednafen_pce": "PC Engine",
    "mednafen_pce_fast": "PC Engine",
    "fbneo": "Neo Geo",
    "mednafen_ngp": "Neo Geo Pocket",
    "mednafen_wswan": "WonderSwan",
    "mednafen_lynx": "Atari Lynx",
    "handy": "Atari Lynx",
    "gearcoleco": "ColecoVision",
    "freeintv": "Intellivision",
    "vecx": "Vectrex",
    "bluemsx": "MSX",
    "fmsx": "MSX",
    "puae": "Commodore Amiga",
    "vice_x64sc": "Commodore 64",
    "vice_x64": "Commodore 64",
}

# Cores that run several consoles: the ROM's extension says which.
_GAME_BOY = {".gb": "Game Boy", ".gbc": "Game Boy Color"}
_CONSOLE_FOR_EXTENSION = {
    "gambatte": _GAME_BOY,
    "sameboy": _GAME_BOY,
    "genesis_plus_gx": {
        ".sms": "Sega Master System", ".gg": "Sega Game Gear",
        ".md": "Sega Genesis", ".gen": "Sega Genesis", ".smd": "Sega Genesis", ".bin": "Sega Genesis",
        ".cue": "Sega CD", ".chd": "Sega CD", ".iso": "Sega CD",
    },
}

# RetroArch's save file types: cartridge RAM, real-time clock, and the
# N64's separate memory types (EEPROM, SRAM, FlashRAM, Controller Pak).
_SAVE_EXTENSIONS = (".srm", ".sav", ".rtc", ".eep", ".sra", ".fla", ".mpk")


def _core_and_rom(argv):
    core = None
    for i, arg in enumerate(argv):
        if arg == "-L" and i + 1 < len(argv):
            core = argv[i + 1]
        elif arg.startswith("-L") and len(arg) > 2:
            core = arg[2:]
    if not core or not argv or not os.path.isfile(argv[-1]):
        return None, None
    name = os.path.basename(core.strip("'\""))
    return name.removesuffix("_libretro.so"), argv[-1]


def console_for(core, rom):
    by_extension = _CONSOLE_FOR_EXTENSION.get(core)
    if by_extension:
        return by_extension.get(os.path.splitext(rom)[1].lower())
    return _CONSOLE_FOR_CORE.get(core)


def _shared_saves_dir():
    """savefile_directory from retroarch.cfg, or RetroArch's default."""
    try:
        with open(os.path.join(_RA_CONFIG, "retroarch.cfg"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep and key.strip() == "savefile_directory":
                    value = value.strip().strip('"')
                    if value and value != "default":
                        return os.path.expanduser(value)
    except OSError:
        pass
    return os.path.join(_RA_CONFIG, "saves")


def _move_old_saves(shared, folder, rom):
    stem = os.path.splitext(os.path.basename(rom))[0]
    for ext in _SAVE_EXTENSIONS:
        old = os.path.join(shared, stem + ext)
        new = os.path.join(folder, stem + ext)
        if os.path.isfile(old) and not os.path.exists(new):
            os.replace(old, new)


def with_save_folder(argv):
    """argv with --appendconfig for the game's console folder, or argv
    unchanged when it is not a RetroArch launch this knows."""
    if not any(_RA_ID in arg for arg in argv):
        return argv
    core, rom = _core_and_rom(argv)
    console = core and console_for(core, rom)
    if not console:
        return argv
    shared = _shared_saves_dir()
    folder = os.path.join(shared, console)
    os.makedirs(folder, exist_ok=True)
    _move_old_saves(shared, folder, rom)
    os.makedirs(_OVERRIDES, exist_ok=True)
    override = os.path.join(_OVERRIDES, re.sub(r"[^A-Za-z0-9]+", "-", console).strip("-") + ".cfg")
    with open(override, "w", encoding="utf-8") as fh:
        fh.write(f'savefile_directory = "{folder}"\n')
    return argv[:-1] + ["--appendconfig", override, argv[-1]]


def main(argv):
    command = argv[1:]
    if not command:
        return 1
    try:
        command = with_save_folder(command)
    except Exception:
        pass  # never stop a game from starting
    os.execvp(command[0], command)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
