#!/usr/bin/env python3
"""Internal resolution for the Emulators tab apps: 1080p by default.

The same rule as RetroArch's 3D cores (ra_resolution.py): the multiple
of the console's own picture that fits 1080 lines, one step lower on a
Steam Deck. Every app stores the setting differently, so this is a
table, one row per app.

Applied in two places, from this one table:

  * when a shortcut is created or saved (standalone_emulators.
    configure_resolution), and
  * just before the game launches, by the launcher.

The launch-time pass is what reaches an emulator SelfSteam installed in
that same Create. Several of these run a first-time setup when their
config is missing, so a config is only ever edited once it exists --
and for a fresh install that is not until its first launch. Seen with
Flycast on a Steam Machine: installed by a Create, it never got its
resolution, because its config did not exist yet at the time.

Preflight edits some of these same files (PCSX2, DuckStation, xemu,
gopher64) for controllers. It reads the whole file and replaces only the
controller sections, and the launcher runs this to completion before
handing over to Preflight, so Preflight reads a file that already has
the resolution in it and writes it back unchanged.

Only ever changes a value this module wrote itself or the app's
default: anything else was picked in the emulator's own settings and
stays.
"""

import json
import os
import sys

_HOME = os.path.expanduser("~")


def _flatpak(app_id, *parts):
    return os.path.join(_HOME, ".var", "app", app_id, *parts)


# --- INI and flat TOML, line by line ---------------------------------------
# Not configparser: M64Py's file holds Qt @Variant blobs, Dolphin's holds
# values configparser would reformat, and every line that is not being
# changed must come back out exactly as it went in.


def get_ini_key(path, section, key):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]"
            continue
        if in_section and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def set_ini_key(path, section, key, value, sep="="):
    """Set one key in one [section], creating the section (and the file)
    if missing, leaving every other line exactly as it was."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    new_line = f"{key}{sep}{value}\n"
    header = f"[{section}]"
    in_section = False
    insert_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                insert_at = i
                break
            in_section = stripped == header
            continue
        if in_section and line.split("=", 1)[0].strip() == key:
            if line == new_line:
                return
            lines[i] = new_line
            break
    else:
        if in_section:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_line)
        else:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines += [header + "\n", new_line]
    if insert_at is not None:
        lines.insert(insert_at, new_line)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _ini(path, section, key, sep=" = ", extra=()):
    """A setting stored as one key in an INI or flat TOML file. extra:
    (key, value) pairs written with it, such as a Qt "\\default" marker."""
    def read():
        return get_ini_key(path, section, key)

    def write(value):
        set_ini_key(path, section, key, value, sep=sep)
        for extra_key, extra_value in extra:
            set_ini_key(path, section, extra_key, extra_value, sep=sep)
    return path, read, write


def _gopher64():
    path = _flatpak("io.github.gopher64.gopher64", "config", "gopher64", "config.json")

    def read():
        try:
            with open(path, encoding="utf-8") as fh:
                return str(json.load(fh).get("video", {}).get("upscale"))
        except (OSError, ValueError, AttributeError):
            return None

    def write(value):
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
        config.setdefault("video", {})["upscale"] = int(value)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4)
    return path, read, write


def _m64py():
    """M64Py's resolution is the core's own window size: two keys that
    only mean anything together, handled as one "WIDTHxHEIGHT" value."""
    path = _flatpak("net.sourceforge.m64py.M64Py", "config", "mupen64plus", "mupen64plus.cfg")

    def read():
        w = get_ini_key(path, "Video-General", "ScreenWidth")
        h = get_ini_key(path, "Video-General", "ScreenHeight")
        return f"{w}x{h}" if w and h else None

    def write(value):
        w, h = value.split("x")
        set_ini_key(path, "Video-General", "ScreenWidth", w, sep=" = ")
        set_ini_key(path, "Video-General", "ScreenHeight", h, sep=" = ")
    return path, read, write


# Each row: the setting, its default, the 1080p value, the Deck's.
#
# Every key, file and default was read from the emulator's own source,
# and for the six installed on the test Steam Machine checked against a
# real config file too. DS and 3DS stack two screens, so 2x is what fits
# for them; melonDS also needs its OpenGL renderer, the only one that
# scales. Not here: apps whose console already renders at 720p or 1080p
# (Switch, Wii U, PS3, PS4, Vita, Xbox 360), where 1080p means no
# change; Play!, whose setting could not be confirmed; Rosalie's Mupen
# GUI, not yet checked; and PPSSPP, which matches its window by itself.
SETTINGS = {
    "Dolphin": (_ini(_flatpak("org.DolphinEmu.dolphin-emu", "config", "dolphin-emu", "GFX.ini"),
                     "Settings", "InternalResolution"), "1", "2", "1"),
    "PCSX2": (_ini(_flatpak("net.pcsx2.PCSX2", "config", "PCSX2", "inis", "PCSX2.ini"),
                   "EmuCore/GS", "upscale_multiplier"), "1", "2", "1"),
    "DuckStation": (_ini(os.path.join(_HOME, ".local", "share", "duckstation", "settings.ini"),
                         "GPU", "ResolutionScale"), "1", "4", "3"),
    "xemu": (_ini(_flatpak("app.xemu.xemu", "data", "xemu", "xemu", "xemu.toml"),
                  "display.quality", "surface_scale"), "1", "2", "1"),
    "gopher64": (_gopher64(), "1", "4", "2"),
    "M64Py": (_m64py(), "640x480", "1440x1080", "1280x960"),
    "Flycast": (_ini(_flatpak("org.flycast.Flycast", "config", "flycast", "emu.cfg"),
                     "config", "rend.Resolution"), "480", "1080", "960"),
    "melonDS": (_ini(_flatpak("net.kuribo64.melonDS", "config", "melonDS", "melonDS.toml"),
                     "3D.GL", "ScaleFactor"), "1", "2", "1"),
    "Azahar": (_ini(_flatpak("org.azahar_emu.Azahar", "config", "azahar-emu", "qt-config.ini"),
                    "Renderer", "resolution_factor", sep="=",
                    extra=(("resolution_factor\\default", "false"),)), "1", "2", "1"),
}

# Settings that have to change with the resolution for it to do anything.
REQUIRES = {
    "melonDS": [(_ini(_flatpak("net.kuribo64.melonDS", "config", "melonDS", "melonDS.toml"),
                      "3D", "Renderer"), "0", "1")],
}

# How a launch command names each app: its Flatpak ID, or for an AppImage
# a fragment of its path. Wheel Wizard plays in the real Dolphin, with
# Dolphin's own config, so it maps to Dolphin.
_COMMAND_MARKERS = {
    "org.DolphinEmu.dolphin-emu": "Dolphin",
    "io.github.TeamWheelWizard.WheelWizard": "Dolphin",
    "net.pcsx2.PCSX2": "PCSX2",
    "/DuckStation/": "DuckStation",
    "app.xemu.xemu": "xemu",
    "io.github.gopher64.gopher64": "gopher64",
    "net.sourceforge.m64py.M64Py": "M64Py",
    "org.flycast.Flycast": "Flycast",
    "net.kuribo64.melonDS": "melonDS",
    "org.azahar_emu.Azahar": "Azahar",
}


def _same(a, b):
    """True if two values mean the same: "1" and "1.000000" are one PCSX2
    multiplier, not two."""
    if a is None or b is None:
        return a == b
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _deck():
    try:
        with open("/sys/devices/virtual/dmi/id/board_name", encoding="utf-8") as fh:
            return fh.read().strip() in ("Jupiter", "Galileo")  # Deck LCD, OLED
    except OSError:
        return False


def _notes_path():
    # The real home, not $XDG_DATA_HOME: inside SelfSteam's Flatpak that
    # is redirected to its own sandbox, and the launcher -- which runs on
    # the host -- has to read the same notes.
    return os.path.join(_HOME, ".local", "share", "selfsteam", "emulator-resolution.json")


def apply(name):
    """Set this app's resolution if its config exists and the player has
    not chosen one."""
    setting = SETTINGS.get(name)
    if not setting:
        return
    try:
        with open(_notes_path(), encoding="utf-8") as fh:
            notes = json.load(fh)
    except (OSError, ValueError):
        notes = {}
    rows = [(setting[0], setting[1], setting[3] if _deck() else setting[2], "resolution")]
    rows += [(acc, default, value, f"requires{i}")
             for i, (acc, default, value) in enumerate(REQUIRES.get(name, []))]
    changed = False
    for (path, read, write), default, value, label in rows:
        if not os.path.isfile(path):
            continue
        note_key = f"{name}::{label}"
        current = read()
        if current is not None and not _same(current, default) and not _same(current, notes.get(note_key)):
            continue  # a player's choice
        if not _same(current, value):
            write(value)
        if notes.get(note_key) != value:
            notes[note_key] = value
            changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(_notes_path()), exist_ok=True)
            with open(_notes_path(), "w", encoding="utf-8") as fh:
                json.dump(notes, fh, indent=1)
        except OSError:
            pass


def app_for_command(argv):
    joined = " ".join(argv)
    for marker, name in _COMMAND_MARKERS.items():
        if marker in joined:
            return name
    return None


def main(argv):
    name = app_for_command(argv[1:])
    if name:
        try:
            apply(name)
        except Exception:
            pass  # never stop a game from starting
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
