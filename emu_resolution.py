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
import subprocess
import sys
import xml.etree.ElementTree as ET

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


def _play():
    """Play!'s config.xml: <Config> holding <Preference Name= Type= Value=>
    entries (its framework's CConfig). Values are powers of two."""
    path = _flatpak("org.purei.Play", "config", "Play Data Files", "config.xml")
    name = "renderer.opengl.resfactor"

    def _node(root):
        for pref in root.iter("Preference"):
            if pref.get("Name") == name:
                return pref
        return None

    def read():
        try:
            node = _node(ET.parse(path).getroot())
        except (OSError, ET.ParseError):
            return None
        return node.get("Value") if node is not None else None

    def write(value):
        # A fresh file only when there is none: one that exists but will
        # not parse is Play!'s, and is left alone rather than replaced.
        if os.path.isfile(path):
            tree = ET.parse(path)
        else:
            tree = ET.ElementTree(ET.Element("Config"))
        node = _node(tree.getroot())
        if node is None:
            node = ET.SubElement(tree.getroot(), "Preference",
                                 {"Name": name, "Type": "integer"})
        node.set("Value", value)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return path, read, write


# Each row: the setting, its default, the 1080p value, the Deck's.
#
# Every key, file and default was read from the emulator's own source,
# and for the six installed on the test Steam Machine checked against a
# real config file too. DS and 3DS stack two screens, so 2x is what fits
# for them; melonDS also needs its OpenGL renderer, the only one that
# scales. Not here: apps whose console already renders at 720p or 1080p
# (Switch, Wii U, PS3, PS4, Vita, Xbox 360), where 1080p means no
# change; Rosalie's Mupen GUI, not yet checked; and PPSSPP, which
# matches its window by itself.
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
    "Play!": (_play(), "1", "2", "1"),
}

# Apps whose config may be created when it is missing. Play! only writes
# config.xml once a setting is changed, so it may never exist at all, and
# it has no first-run setup a partial file could get in the way of: a
# preference that is not in the file just takes its default.
CREATE_IF_MISSING = {"Play!"}

# Settings that have to change with the resolution for it to do anything.
REQUIRES = {
    "melonDS": [(_ini(_flatpak("net.kuribo64.melonDS", "config", "melonDS", "melonDS.toml"),
                      "3D", "Renderer"), "0", "1")],
}

# Settings written with the resolution, under the same rules, that are
# not about it. Dolphin: hybrid ubershaders (2), with every shader built
# before the game starts, so a new effect does not stutter the game the
# first time it is drawn. Values from Dolphin's ShaderCompilationMode.
ALSO = {
    "Dolphin": [
        (_ini(_flatpak("org.DolphinEmu.dolphin-emu", "config", "dolphin-emu", "GFX.ini"),
              "Settings", "ShaderCompilationMode"), "0", "2"),
        (_ini(_flatpak("org.DolphinEmu.dolphin-emu", "config", "dolphin-emu", "GFX.ini"),
              "Settings", "WaitForShadersBeforeStarting"), "False", "True"),
    ],
    # PCSX2's own per-game widescreen patches (patches.zip, in the
    # Flatpak). Each one sets the 16:9 aspect itself, so a game with no
    # patch keeps its 4:3 picture instead of being stretched.
    "PCSX2": [
        (_ini(_flatpak("net.pcsx2.PCSX2", "config", "PCSX2", "inis", "PCSX2.ini"),
              "EmuCore", "EnableWideScreenPatches"), "false", "true"),
    ],
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
    "org.purei.Play": "Play!",
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
    rows += [(acc, default, value, f"also{i}")
             for i, (acc, default, value) in enumerate(ALSO.get(name, []))]
    changed = False
    for (path, read, write), default, value, label in rows:
        if not os.path.isfile(path) and name not in CREATE_IF_MISSING:
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


# --- Dolphin widescreen ---------------------------------------------------
# Dolphin ships 16:9 codes for many GameCube and Wii games in its own
# Sys/GameSettings, off by default. This turns the game's one on, the
# same as ticking it under Properties > Gecko Codes: nothing is copied,
# only the code's name is written to the player's GameSettings/<ID>.ini.
# Once per game: if the player later unticks it, it stays off.

_DOLPHIN_ID = "org.DolphinEmu.dolphin-emu"
_DOLPHIN_USER = _flatpak(_DOLPHIN_ID, "data", "dolphin-emu")
# A disc header's magic word, and how far into the header it sits. The
# header is at 0 in an ISO/GCM, 0x200 in a WBFS and 0x58 in an RVZ/WIA,
# so the first 64 KiB is scanned rather than one offset trusted.
_DISC_MAGIC = ((b"\x5d\x1c\x9e\xa3", 0x18), (b"\xc2\x33\x9f\x3d", 0x1c))  # Wii, GameCube
_DISC_SCAN_BYTES = 0x10000
_WIDESCREEN_NAMES = ("16:9 widescreen", "widescreen", "16:9")


def disc_game_id(path):
    """The six-character game ID from a Dolphin disc image, or None."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read(_DISC_SCAN_BYTES)
    except OSError:
        return None
    for magic, offset in _DISC_MAGIC:
        at = buf.find(magic)
        while at != -1:
            start = at - offset
            if start >= 0:
                game_id = buf[start:start + 6]
                if len(game_id) == 6 and game_id.isalnum() and game_id.upper() == game_id:
                    return game_id.decode("ascii")
            at = buf.find(magic, at + 1)
    return None


def _dolphin_sys_settings():
    try:
        out = subprocess.run(["flatpak", "info", "--show-location", _DOLPHIN_ID],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    path = os.path.join(out, "files", "share", "dolphin-emu", "Sys", "GameSettings")
    return path if out and os.path.isdir(path) else None


def _widescreen_code(sys_dir, game_id):
    """("Gecko" or "ActionReplay", code name) for the game's 16:9 code in
    Dolphin's own files, read from <ID>.ini and the region-free <ID3>.ini."""
    found = []
    for name in (f"{game_id}.ini", f"{game_id[:3]}.ini"):
        try:
            with open(os.path.join(sys_dir, name), encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        section = None
        for line in lines:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section in ("Gecko", "ActionReplay") and line.startswith("$"):
                found.append((section, line[1:].split(" [", 1)[0].strip()))
    for wanted in _WIDESCREEN_NAMES:
        for section, code in found:
            if code.lower() == wanted or (wanted == "16:9" and "16:9" in code.lower()):
                return section, code
    return None


def apply_widescreen(argv):
    rom = next((a.strip("'\"") for a in reversed(argv) if os.path.isfile(a.strip("'\""))), None)
    game_id = rom and disc_game_id(rom)
    if not game_id:
        return
    try:
        with open(_notes_path(), encoding="utf-8") as fh:
            notes = json.load(fh)
    except (OSError, ValueError):
        notes = {}
    note_key = f"Dolphin::widescreen::{game_id}"
    if note_key in notes:
        return  # done once already; what the file says now is the player's
    sys_dir = _dolphin_sys_settings()
    code = sys_dir and _widescreen_code(sys_dir, game_id)
    if code:
        section, name = code
        user_ini = os.path.join(_DOLPHIN_USER, "GameSettings", f"{game_id}.ini")
        enabled = f"{section}_Enabled"
        if not _listed(user_ini, enabled, f"${name}"):
            _append_line(user_ini, enabled, f"${name}")
        set_ini_key(_flatpak(_DOLPHIN_ID, "config", "dolphin-emu", "Dolphin.ini"),
                    "Core", "EnableCheats", "True", sep=" = ")
    notes[note_key] = code[1] if code else None
    try:
        os.makedirs(os.path.dirname(_notes_path()), exist_ok=True)
        with open(_notes_path(), "w", encoding="utf-8") as fh:
            json.dump(notes, fh, indent=1)
    except OSError:
        pass


def _listed(path, section, line_text):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [line.strip() for line in fh]
    except OSError:
        return False
    in_section = False
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            in_section = line == f"[{section}]"
        elif in_section and line == line_text:
            return True
    return False


def _append_line(path, section, line_text):
    """Add a bare line (Dolphin's code lists are names, not key=value) to
    the end of [section], creating it and the file if missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines += [header + "\n", line_text + "\n"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line_text + "\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


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
            if name == "Dolphin":
                apply_widescreen(argv[1:])
        except Exception:
            pass  # never stop a game from starting
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
