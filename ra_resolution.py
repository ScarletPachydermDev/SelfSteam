#!/usr/bin/env python3
"""Set a 3D core's internal resolution to a good default for the device.

Every 3D core in RetroArch can render above its console's native
resolution, and every one names that setting differently -- some take a
multiplier, some a size, all with their own spellings -- so this is a
table, one row per core, of what to set.

The target is 1080p on every device: the multiple of the console's own
picture that fits 1080 lines. A Steam Deck gets one step lower, since
its GPU is the weakest SelfSteam runs on and its own screen is only 800
lines anyway. A fixed target rather than one that follows the screen:
it is predictable, easy to reason about, and lets anyone who wants more
simply raise it in Core Options.

It only ever changes a value it wrote itself or the core's untouched
default. Once someone picks a resolution in RetroArch's Core Options,
that is theirs, and this never touches it again -- otherwise every
launch would quietly put back what they had just changed.

Run by the launcher just before RetroArch starts, beside ra_overscan's
own pre-launch step and for the same reason: RetroArch reads its options
when the game starts and writes them back when it exits, so anything
written while it runs is lost.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ra_overscan  # noqa: E402 -- shared paths and log

# Per core library: the folder RetroArch keeps its options in (the name
# the core reports for itself), the option, its default, the values it
# accepts in order (from native up to comfortably past the target; the
# largest are left out), and the 1080p value. The Deck gets the value just
# before the 1080p one in that list.
#
# Every key, value list and default was read out of the core itself --
# each core loaded and asked for its options through the libretro API
# (2026-09-29, buildbot nightlies), not recalled.
#
# The 1080p value is the multiple of the console's picture that fits in
# 1080 lines. For cores that take a size, the tallest size no taller than
# 1080. DS and 3DS show their two screens stacked by default, so their
# picture is twice as tall as one screen, and 2x is what fits.
#
# Not here, on purpose: PCSX-ReARMed, whose only option is an on/off
# enhancement built on ARM-only code that does nothing on x86 hardware,
# and Beetle Saturn, which has no resolution option at all. And every 2D
# core: rendering pixel art larger adds no detail.
_CORES = {
    "parallel_n64_libretro.so": {
        "name": "ParaLLEl N64", "key": "parallel-n64-screensize", "default": "640x480",
        "values": ["320x240", "640x480", "960x720", "1280x960", "1440x1080",
                   "1600x1200", "1920x1440", "2240x1680", "2880x2160"],
        "target": "1440x1080",
    },
    "mupen64plus_next_libretro.so": {
        "name": "Mupen64Plus-Next", "key": "mupen64plus-43screensize", "default": "640x480",
        "values": ["320x240", "640x480", "960x720", "1280x960", "1440x1080",
                   "1600x1200", "1920x1440", "2240x1680", "2560x1920", "2880x2160"],
        "target": "1440x1080",
    },
    "mednafen_psx_hw_libretro.so": {
        "name": "Beetle PSX HW", "key": "beetle_psx_hw_internal_resolution", "default": "1x(native)",
        "values": ["1x(native)", "2x", "4x", "8x", "16x"],
        "target": "4x",
    },
    "swanstation_libretro.so": {
        "name": "SwanStation", "key": "swanstation_GPU_ResolutionScale", "default": "1",
        "values": [str(n) for n in range(1, 15)],
        "target": "4",
    },
    "kronos_libretro.so": {
        "name": "Kronos", "key": "kronos_resolution_mode", "default": "original",
        "values": ["original", "2X", "4X", "8X"],
        "target": "4X",
    },
    "flycast_libretro.so": {
        "name": "Flycast", "key": "reicast_internal_resolution", "default": "640x480",
        "values": ["320x240", "640x480", "800x600", "960x720", "1024x768", "1280x960",
                   "1440x1080", "1600x1200", "1920x1440", "2560x1920", "2880x2160"],
        "target": "1440x1080",
    },
    "pcsx2_libretro.so": {
        "name": "LRPS2", "key": "pcsx2_upscale_multiplier", "default": "1x (Native)",
        "values": ["1x (Native)", "2x", "4x", "8x"],
        "target": "2x",
    },
    "ppsspp_libretro.so": {
        "name": "PPSSPP", "key": "ppsspp_internal_resolution", "default": "480x272",
        "values": ["480x272", "960x544", "1440x816", "1920x1088", "2400x1360",
                   "2880x1632", "3360x1904", "3840x2176"],
        "target": "1440x816",
    },
    "desmume_libretro.so": {
        "name": "DeSmuME", "key": "desmume_internal_resolution", "default": "256x192",
        "values": ["256x192", "512x384", "768x576", "1024x768", "1280x960"],
        "target": "512x384",
    },
    "melonds_libretro.so": {
        "name": "melonDS", "key": "melonds_opengl_resolution", "default": "1x native (256x192)",
        "values": ["1x native (256x192)", "2x native (512x384)", "3x native (768x576)",
                   "4x native (1024x768)"],
        "target": "2x native (512x384)",
        # melonDS only scales with its OpenGL renderer; its default
        # software renderer ignores the resolution option entirely.
        "requires": {"melonds_opengl_renderer": ("disabled", "enabled")},
    },
    "azahar_libretro.so": {
        "name": "Azahar", "key": "citra_resolution_factor", "default": "1",
        "values": [str(n) for n in range(1, 11)],
        "target": "2",
    },
    "citra_libretro.so": {
        "name": "Citra", "key": "citra_resolution_factor", "default": "1x (Native)",
        "values": ["1x (Native)", "2x", "3x", "4x"],
        "target": "2x",
    },
}

# Boards that get one step lower: the Steam Deck, LCD and OLED.
_LOWER_STEP_BOARDS = {"Jupiter", "Galileo"}


def _board_name():
    try:
        with open("/sys/devices/virtual/dmi/id/board_name", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def value_for(core, board=None):
    """The value to set for this core on this device."""
    board = _board_name() if board is None else board
    target = core["target"]
    if board in _LOWER_STEP_BOARDS:
        i = core["values"].index(target)
        return core["values"][max(0, i - 1)]
    return target


# --- ownership ------------------------------------------------------------


def _notes_path():
    return os.path.join(ra_overscan._state_dir(), "resolution.json")


def _load_notes():
    try:
        with open(_notes_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_notes(notes):
    try:
        os.makedirs(os.path.dirname(_notes_path()), exist_ok=True)
        with open(_notes_path(), "w", encoding="utf-8") as fh:
            json.dump(notes, fh, indent=1)
    except OSError:
        pass


def _read_value(path, key):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"')
    except OSError:
        pass
    return None


def _write_value(path, key, value):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    except OSError:
        return False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f'{key} = "{value}"\n'
            break
    else:
        lines.append(f'{key} = "{value}"\n')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError:
        return False
    return True


def _ours(notes, path, key):
    """Every value this module has written to this setting in this file.
    v0.1.27 kept one note per file, for the N64 resolution key alone;
    those count too, or every install that shipped with it would take
    its own earlier value for a player's choice and never move again."""
    return {v for v in (notes.get(f"{path}::{key}"), notes.get(path)) if v}


def _set_if_ours(path, key, default, value, notes, inherited=()):
    """Write value, unless the player owns this setting in this file.

    inherited: values that count as ours in this file too, because it
    was copied from one we wrote them to. A game's own options file is
    made by copying the core-wide one -- ra_overscan does exactly that --
    so it carries this module's value without a note of its own."""
    current = _read_value(path, key)
    note = f"{path}::{key}"
    ours = _ours(notes, path, key) | set(inherited)
    if current is not None and current != default and current not in ours:
        return  # someone chose this in RetroArch; theirs from now on
    if current != value and _write_value(path, key, value):
        ra_overscan._log(f"{key} = {value} in {os.path.basename(path)}")
    notes[note] = value


def apply(core_path, rom_path):
    core = _CORES.get(os.path.basename(core_path or ""))
    if not core:
        return None
    value = value_for(core)
    folder = os.path.join(ra_overscan._ra_config_dir(), "config", core["name"])
    # The core-wide file always -- created if RetroArch has not made it
    # yet, which on a core's very first game it will not have -- and the
    # game's own file where one exists, because that overrides the
    # core-wide one for every key, this one included.
    paths = [os.path.join(folder, core["name"] + ".opt")]
    per_game = ra_overscan._per_game_opt_path(core["name"], rom_path)
    if os.path.isfile(per_game):
        paths.append(per_game)
    notes = _load_notes()
    core_file = paths[0]
    for path in paths:
        for key, (default, wanted) in core.get("requires", {}).items():
            inherited = _ours(notes, core_file, key) if path != core_file else ()
            _set_if_ours(path, key, default, wanted, notes, inherited)
        inherited = _ours(notes, core_file, core["key"]) if path != core_file else ()
        _set_if_ours(path, core["key"], core["default"], value, notes, inherited)
    _save_notes(notes)
    return value


def main(argv):
    args = argv[1:]
    core_path = None
    for i, arg in enumerate(args):
        if arg == "-L" and i + 1 < len(args):
            core_path = args[i + 1]
    if not core_path or not args or not os.path.isfile(args[-1]):
        return 0
    try:
        apply(core_path, args[-1])
    except Exception:
        # Never let this stop someone playing.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
