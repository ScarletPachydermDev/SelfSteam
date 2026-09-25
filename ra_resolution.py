#!/usr/bin/env python3
"""Pick an N64 core's internal resolution to suit the screen it is on.

An N64 game renders at 240 lines. The core can render it at a multiple
of that, and the right multiple depends on the screen: a 4K TV can show
far more detail than a Deck's own 800-line panel. The same device can
also move between them -- a Deck docked to a TV -- so this is decided
at every launch, from the display the game is about to open on, rather
than once when the shortcut is made.

The rule is the tallest resolution the core offers that is no taller
than the screen. Rendering above the screen's own height only spends GPU
time on detail nobody can see.

Two limits keep it from asking more than the hardware can give, since
this has no way to measure how well a game actually runs:

  * a ceiling for the console generation, and
  * a ceiling for the device, where it is one SelfSteam recognises, and
    a cautious one where it is not.

And one rule keeps it out of the player's way: it only ever changes a
value it wrote itself, or the core's untouched default. Once someone
picks a resolution in RetroArch's own Core Options, that is theirs, and
this never touches it again -- otherwise every launch would quietly put
back what they had just changed.

Run by the launcher just before RetroArch starts, beside ra_overscan's
own pre-launch step and for the same reason: RetroArch reads its options
when the game starts and writes them back when it exits, so anything
written while it runs is lost.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import ra_overscan  # noqa: E402 -- core and ROM parsing, shared paths

# The core's option, the sizes it accepts (read out of each core's own
# binary, not recalled), and the value a fresh install starts with.
# Only the 4:3 sizes: these cores' 16:9 sizes belong to a widescreen hack
# that stretches the picture, which is not this module's call to make.
_CORES = {
    "parallel_n64_libretro.so": {
        "key": "parallel-n64-screensize",
        "default": "640x480",
        "sizes": [
            "320x240", "640x480", "960x720", "1280x960", "1440x1080",
            "1600x1200", "1920x1440", "2240x1680", "2880x2160",
        ],
    },
    "mupen64plus_next_libretro.so": {
        "key": "mupen64plus-43screensize",
        "default": "640x480",
        "sizes": [
            "320x240", "640x480", "960x720", "1280x960", "1440x1080",
            "1600x1200", "1920x1440", "2240x1680", "2560x1920",
            "2880x2160",
        ],
    },
}

# N64 is cheap to render at any of these on current hardware, so the
# generation ceiling is simply 4K's height. Heavier consoles, when they
# come, are where this starts to bite.
_GENERATION_CEILING = 2160

# Heights, by the board name the firmware reports. The Steam Machine has
# headroom to spare for N64 and is left to the generation ceiling. A
# Deck docked to a 4K TV is a different story, so it stops at 1080p.
# Anything unrecognised gets a cautious middle value rather than being
# trusted with 4K.
_DEVICE_CEILINGS = {
    "Fremont": None,   # Steam Machine
    "Jupiter": 1080,   # Steam Deck LCD
    "Galileo": 1080,   # Steam Deck OLED
}
_UNKNOWN_DEVICE_CEILING = 1440


def _board_name():
    try:
        with open("/sys/devices/virtual/dmi/id/board_name", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _ceiling():
    board = _board_name()
    device = _DEVICE_CEILINGS.get(board, _UNKNOWN_DEVICE_CEILING)
    if device is None:
        return _GENERATION_CEILING
    return min(device, _GENERATION_CEILING)


def _screen_height():
    """The height of the display the game is about to open on.

    Under Gamescope that is the shortcut's own nested display, which the
    launcher has just asked to match the real output -- and which also
    carries Steam's own resolution setting, so a player who told Steam
    to run games at 1440p gets 1440p here too. In Desktop Mode it is the
    desktop. None if there is no display to ask.
    """
    name = os.environ.get("DISPLAY")
    if not name:
        return None
    try:
        from Xlib import display
        d = display.Display(name)
        height = d.screen().height_in_pixels
        d.close()
        return height
    except Exception:
        return None


def pick(sizes, screen_height, ceiling):
    """The tallest size no taller than the screen or the ceiling."""
    limit = min(screen_height, ceiling)
    best = sizes[0]
    for size in sizes:
        if int(size.split("x")[1]) <= limit:
            best = size
    return best


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
    except OSError:
        return False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f'{key} = "{value}"\n'
            break
    else:
        lines.append(f'{key} = "{value}"\n')
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError:
        return False
    return True


def _set_if_ours(path, core, value, notes):
    """Write value to one options file, unless the player owns it."""
    current = _read_value(path, core["key"])
    ours = notes.get(path)
    if current is not None and current != core["default"] and current != ours:
        # Someone chose this in RetroArch. Theirs from now on.
        return
    if current != value and _write_value(path, core["key"], value):
        ra_overscan._log(f"resolution {value} in {os.path.basename(path)}")
    notes[path] = value


def apply(core_path, rom_path):
    core = _CORES.get(os.path.basename(core_path or ""))
    overscan_core = ra_overscan.core_for_path(core_path)
    if not core or not overscan_core:
        return None
    height = _screen_height()
    if not height:
        return None
    value = pick(core["sizes"], height, _ceiling())

    # Both files, because a game's own options file overrides the
    # core-wide one entirely -- including this key, frozen at whatever
    # it held when the game's file was first made.
    folder = os.path.join(ra_overscan._ra_config_dir(), "config", overscan_core["name"])
    paths = [os.path.join(folder, overscan_core["name"] + ".opt"),
             ra_overscan._per_game_opt_path(overscan_core["name"], rom_path)]
    notes = _load_notes()
    for path in paths:
        if os.path.isfile(path):
            _set_if_ours(path, core, value, notes)
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
