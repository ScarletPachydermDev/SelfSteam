#!/usr/bin/env python3
"""Work out how much black border an N64 game draws, and crop it away.

The N64 tells its video chip where the picture starts and stops, and
plenty of games set that window smaller than the full frame. On a CRT
that was invisible, because televisions hid roughly a tenth of the
signal behind the bezel. On a modern screen the leftover is black bars,
and they are baked into the emulated picture, so no aspect-ratio setting
can reach them. The fix is a crop, and the right amount differs per
game: Wave Race 64 needs one, GoldenEye 007 needs none.

Nobody is going to sit down and measure that by hand for every game, so
this measures it while the game is being played.

Why not measure it unattended before first launch? RetroArch will happily
run a fixed number of frames and screenshot the result (--max-frames-ss),
but with no one holding a controller that lands on the title screen, and
title screens are often letterboxed *more* than the game is. Cropping to
one would slice the HUD off the actual gameplay.

Why not just crop whatever is black, once? Because games legitimately go
black: fades, night scenes, dark water. A one-shot look risks sampling a
fade and cropping most of the picture away. So this samples repeatedly
and keeps the *largest* lit area it ever sees. A dark frame produces a
small lit area and simply loses. Being wrong in this direction is also
the safe one: too little crop leaves a thin border, too much eats the
HUD.

Run in the background by the launcher. It writes a per-game core-options
file, which RetroArch loads automatically for that ROM and no other, so
the answer is remembered and nothing else is touched. It keeps refining
over the first few sessions of a game rather than trusting one, then
stops for good.
"""

import json
import os
import socket
import sys
import time
import zlib

# RetroArch's UDP command interface. Needs network_cmd_enable in
# retroarch.cfg, which SelfSteam turns on alongside this.
_CMD_HOST = "127.0.0.1"
_CMD_PORT = 55355

# A short wait for the game to get going, then sample briskly. This does
# not need to dodge menus: a menu is letterboxed more than the game, so
# it reports a bigger leftover, and the smallest reading across the
# session is the one that counts. Short enough that a pass completes in
# under a minute, because a pass that does not finish teaches nothing --
# two launches during testing were quit inside the old 75-second wait
# and did exactly nothing.
_FIRST_SAMPLE_DELAY = 30.0
_SAMPLE_INTERVAL = 10.0
_SAMPLE_COUNT = 12
# Write nothing until there are a few samples, so one unlucky frame
# does not become the answer.
_MIN_SAMPLES = 3

# A pixel counts as lit above this. Not zero: video has noise, and the
# "black" border is not always exactly 0 once a shader or the VI's own
# filtering has touched it.
_BLACK_THRESHOLD = 24

# How much of a line has to be lit before it counts as picture rather
# than border. A couple of percent: enough to ignore the faint bleed at
# the border's inner edge, small enough that a thin sliver of real
# content at the very top or bottom of a frame still registers.
_MIN_LIT_FRACTION = 0.02

# How much of the measured leftover to add each pass. Less than all of
# it so the crop converges from below and never overshoots into the
# picture; the remainder is taken on the following pass. Simulated
# against a PAL frame measured with the NTSC scale, which is the worst
# case this has to survive: settles in four passes, never more than a
# pixel over, and at most a few pixels short.
_STEP_FRACTION = 0.9

# Below this a border is not worth cropping, and is more likely to be a
# dark edge of the picture than a real border.
_MIN_BORDER_NATIVE = 2

# GLideN64's offsets are counted in the N64's own 320x240 frame, not in
# the upscaled framebuffer the screenshot is in. Getting this wrong
# crops exactly double and cuts the HUD off.
_NATIVE_WIDTH = 320

# Per core, because every core names these differently. Both N64 cores
# happen to take the crop as four pixel offsets plus a switch, which is
# what this knows how to fill in. Cores for other systems often expose
# overscan as a plain on/off instead, with the amount decided for you;
# those cannot use a measurement and are not listed here.
_CORES = {
    "parallel_n64_libretro.so": {
        "name": "ParaLLEl N64",
        "keys": {
            "enable": "parallel-n64-gliden64-EnableOverscan",
            "left": "parallel-n64-gliden64-OverscanLeft",
            "right": "parallel-n64-gliden64-OverscanRight",
            "top": "parallel-n64-gliden64-OverscanTop",
            "bottom": "parallel-n64-gliden64-OverscanBottom",
        },
    },
    "mupen64plus_next_libretro.so": {
        "name": "Mupen64Plus-Next",
        "keys": {
            "enable": "mupen64plus-EnableOverscan",
            "left": "mupen64plus-OverscanLeft",
            "right": "mupen64plus-OverscanRight",
            "top": "mupen64plus-OverscanTop",
            "bottom": "mupen64plus-OverscanBottom",
        },
    },
}


def _ra_config_dir():
    return os.path.expanduser(
        "~/.var/app/org.libretro.RetroArch/config/retroarch"
    )


def _state_dir():
    return os.path.expanduser("~/.local/share/selfsteam/overscan")


def _log(message):
    """A short account of what the last calibration did, so a crop that
    came out wrong can be explained without running the whole thing
    again blind."""
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(os.path.join(_state_dir(), "log.txt"), "a",
                  encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _state_path(rom_path):
    base = os.path.splitext(os.path.basename(rom_path))[0]
    return os.path.join(_state_dir(), base + ".json")


def _load_state(rom_path):
    """What earlier sessions already measured for this game.

    Kept across launches on purpose. Samples taken early in a session
    can land on a menu, and menus are letterboxed *more* than the game
    is, so an answer written from them crops too much. Later samples can
    only ever shrink the crop, never grow it, so carrying the count
    forward lets a second and third session correct a first one instead
    of the first answer being final.
    """
    try:
        with open(_state_path(rom_path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"offsets": None, "done": False}


def _save_state(rom_path, state):
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(_state_path(rom_path), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


# --- PNG, without Pillow -------------------------------------------------
# SteamOS's python has no PIL and this runs on the user's machine, so it
# decodes the screenshot itself. Only what RetroArch actually writes is
# supported: 8-bit non-interlaced RGB or RGBA.


def _png_rows(path):
    """Yield each row of the image as a bytes of 8-bit samples, along
    with the sample count per pixel. Returns None if the file is not a
    shape this understands."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    pos = 8
    width = height = channels = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # 4 length + 4 type + body + 4 crc
        if kind == b"IHDR":
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, colour, _comp, _filt, interlace = body[8:13]
            if depth != 8 or interlace != 0 or colour not in (2, 6):
                return None
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if not width or not height or not channels:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        # A half-written file. The caller waits and tries again.
        return None
    stride = width * channels
    prev = bytearray(stride)
    rows = []
    at = 0
    for _ in range(height):
        if at >= len(raw):
            break
        filter_type = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        _unfilter(filter_type, line, prev, channels)
        rows.append(bytes(line))
        prev = line
    return width, height, channels, rows


def _unfilter(filter_type, line, prev, bpp):
    """Undo one PNG scanline filter in place (spec section 9.2)."""
    if filter_type == 0:
        return
    for i in range(len(line)):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        if filter_type == 1:
            line[i] = (line[i] + a) & 0xFF
        elif filter_type == 2:
            line[i] = (line[i] + b) & 0xFF
        elif filter_type == 3:
            line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
        elif filter_type == 4:
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pred) & 0xFF


def lit_box(path):
    """Measure one screenshot. Returns (borders, width) where borders is
    (left, right, top, bottom) in image pixels and width is the image's
    own width, which is what the offsets have to be scaled against.
    None if the image cannot be read or is entirely black."""
    decoded = _png_rows(path)
    if not decoded:
        return None
    width, height, channels, rows = decoded
    if not rows:
        return None

    # Every fourth pixel is plenty to find an edge, and keeps this cheap
    # enough to run beside a game without being noticed.
    step = 4 * channels
    sampled = len(range(0, width * channels, step))
    # A line counts as picture only when a real share of it is lit, not
    # when a single pixel is. The border's inner edge is not perfectly
    # black -- the video filtering bleeds a little brightness into it --
    # so an "any pixel" test stops a few pixels short and leaves a thin
    # bar behind, which is exactly what it did on the first working run.
    needed = max(2, int(sampled * _MIN_LIT_FRACTION))

    def row_lit(row):
        lit = 0
        for i in range(0, len(row), step):
            if row[i] > _BLACK_THRESHOLD:
                lit += 1
                if lit >= needed:
                    return True
        return False

    lit_rows = [y for y, row in enumerate(rows) if row_lit(row)]
    if not lit_rows:
        return None
    top, bottom = lit_rows[0], lit_rows[-1]

    # Same rule down the columns: count how many of the sampled rows are
    # lit at this x before accepting it as the edge of the picture.
    ys = range(top, bottom + 1, 4)
    col_needed = max(2, int(len(ys) * _MIN_LIT_FRACTION))

    def col_lit(x):
        lit = 0
        at = x * channels
        for y in ys:
            if rows[y][at] > _BLACK_THRESHOLD:
                lit += 1
                if lit >= col_needed:
                    return True
        return False

    left = right = None
    for x in range(0, width, 4):
        if col_lit(x):
            left = x
            break
    for x in range(width - 1 - ((width - 1) % 4), -1, -4):
        if col_lit(x):
            right = x
            break
    if left is None or right is None:
        return None
    return (left, width - 1 - right, top, height - 1 - bottom), width


# --- talking to a running RetroArch --------------------------------------


def _send(command):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(command.encode("utf-8"), (_CMD_HOST, _CMD_PORT))
        sock.close()
        return True
    except OSError:
        return False


def _request_screenshot():
    return _send("SCREENSHOT")


def _say(text):
    """Put a line on screen over the game. Best effort: if RetroArch is
    not listening there is nothing to tell anyone anyway."""
    _send(f"SHOW_MSG {text}")


def _newest_since(directory, known):
    """The newest file in directory that was not there before."""
    try:
        names = set(os.listdir(directory))
    except OSError:
        return None
    fresh = [os.path.join(directory, n) for n in names - known]
    if not fresh:
        return None
    return max(fresh, key=lambda p: os.path.getmtime(p))


# --- writing the answer down ---------------------------------------------


def _per_game_opt_path(core_name, rom_path):
    base = os.path.splitext(os.path.basename(rom_path))[0]
    return os.path.join(_ra_config_dir(), "config", core_name, base + ".opt")


def _write_options(core_name, rom_path, offsets, keys):
    """Write a per-game options file, based on the core's own current
    options so nothing else changes. RetroArch loads this automatically
    for this ROM only -- it needs game_specific_options, which SelfSteam
    turns on."""
    dest = _per_game_opt_path(core_name, rom_path)
    source = os.path.join(_ra_config_dir(), "config", core_name, core_name + ".opt")
    lines = []
    if os.path.isfile(source):
        with open(source, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

    wanted = {
        keys["enable"]: "Enabled",
        keys["left"]: str(offsets[0]),
        keys["right"]: str(offsets[1]),
        keys["top"]: str(offsets[2]),
        keys["bottom"]: str(offsets[3]),
    }
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            lines[i] = f'{key} = "{wanted[key]}"\n'
            seen.add(key)
    for key, value in wanted.items():
        if key not in seen:
            lines.append(f'{key} = "{value}"\n')

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return dest


def apply_pending(core_name, rom_path, keys):
    """Put the measured crop into the options file, before RetroArch
    starts.

    Not while it is running. RetroArch rewrites a game's options file
    when the game exits, from the values it read at startup, so anything
    written underneath it during play is reverted on quit. That is what
    kept a measured crop one step behind for ever: each session measured
    against a crop that had been rolled back, so the leftover never
    reached zero and it never settled. Writing before launch means
    RetroArch loads these values as its own and saves them back itself.
    """
    state = _load_state(rom_path)
    offsets = state.get("offsets")
    if not offsets or max(offsets) < _MIN_BORDER_NATIVE:
        return None
    if _applied_offsets(core_name, rom_path, keys) == list(offsets):
        return None  # already in force
    _write_options(core_name, rom_path, offsets, keys)
    _log(f"applied crop {offsets} before launch")
    return offsets


def _applied_offsets(core_name, rom_path, keys):
    """What the per-game options file is already cropping.

    The options file is the real state; this module's own notes are a
    cache of it. Reading it back means the two cannot drift apart -- if
    the notes are lost while a crop is in force, measuring a game that
    is already correct would otherwise read as "no borders here" and be
    remembered as needing none.
    """
    path = _per_game_opt_path(core_name, rom_path)
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"')
    except OSError:
        return [0, 0, 0, 0]
    if values.get(keys["enable"]) != "Enabled":
        return [0, 0, 0, 0]
    out = []
    for side in ("left", "right", "top", "bottom"):
        try:
            out.append(int(values.get(keys[side], "0")))
        except ValueError:
            out.append(0)
    return out


def calibrate(core_name, rom_path, keys):
    """Sample the running game and write its crop. Returns the offsets
    written, or None if there was nothing worth cropping.

    Writes after every sample once there are a few, rather than once at
    the end. Quitting the game kills this process with no warning --
    Steam takes the whole launch down together -- so a version that only
    saved at the end would throw away everything it had learned from
    anyone who played for slightly less than the full run. Each write
    supersedes the last, and later samples can only improve the answer,
    so being interrupted costs accuracy rather than the whole result.
    """
    state = _load_state(rom_path)
    if state.get("done"):
        return None  # nothing left to trim for this game

    shots_dir = os.path.join(_ra_config_dir(), "screenshots")
    os.makedirs(shots_dir, exist_ok=True)

    # What is already being cropped. Each session measures what is
    # still left over and adds most of it, so the crop creeps up on the
    # right answer from below over a session or two. Approaching from
    # below is the point: the cost of stopping early is a thin border,
    # while the cost of overshooting is a missing HUD.
    applied = list(state.get("offsets") or
                   _applied_offsets(core_name, rom_path, keys))
    extra = None
    taken = 0
    _log(f"calibrating {os.path.basename(rom_path)} on {core_name}")
    time.sleep(_FIRST_SAMPLE_DELAY)
    _log("woke up, starting to sample")
    for _ in range(_SAMPLE_COUNT - taken):
        try:
            known = set(os.listdir(shots_dir))
        except OSError:
            known = set()
        _log("asking for a screenshot")
        if not _request_screenshot():
            _log("could not reach RetroArch's command port")
            return None
        # RetroArch writes the file on its own schedule, not ours, and
        # the name appears in the directory before the bytes are all
        # there. Reading it too early gets a truncated file that will
        # not parse, so wait for the size to settle before measuring --
        # and keep waiting a little after that, since a screenshot of a
        # 4K frame takes a moment to compress.
        shot = None
        measured = None
        last_size = -1
        for _ in range(60):
            time.sleep(0.25)
            shot = shot or _newest_since(shots_dir, known)
            if not shot:
                continue
            try:
                size = os.path.getsize(shot)
            except OSError:
                continue
            if size and size == last_size:
                measured = lit_box(shot)
                if measured:
                    break
            last_size = size
        if not shot:
            # RetroArch has gone away, or is not listening. Either way
            # there is nothing more to measure.
            _log(f"no screenshot appeared after sample {taken + 1}")
            return None
        if not measured:
            _log(f"screenshot {os.path.basename(shot)} could not be read")
        # Ours to clean up: the player did not ask for these in their
        # screenshot folder.
        try:
            os.remove(shot)
        except OSError:
            pass
        if measured:
            taken += 1
            box, width = measured
            # The leftover border, in N64 pixels, damped so the crop
            # closes on the edge instead of jumping past it. The scale
            # is only approximate -- a PAL frame is 288 lines where an
            # NTSC one is 240, and nothing in a screenshot says which
            # this is -- and damping is what makes that not matter.
            scale = width / _NATIVE_WIDTH
            this = [round(b / scale * _STEP_FRACTION) for b in box]
            # Elementwise smallest across the session: a dark frame
            # reports a bigger border, and must not be the one that
            # decides the crop.
            extra = this if extra is None else [min(a, b) for a, b
                                                in zip(extra, this)]
            _log(f"sample {taken}: leftover {box} of {width}px -> add {extra}")
            if taken >= _MIN_SAMPLES:
                result = _commit(core_name, rom_path, applied, extra, keys)
                if max(extra) < 1:
                    # Nothing left to trim. Stop bothering the game for
                    # screenshots, and return rather than break: falling
                    # through to the commit below would write the same
                    # answer again and announce it a second time.
                    return result
        time.sleep(_SAMPLE_INTERVAL)

    return _commit(core_name, rom_path, applied, extra, keys)


def _commit(core_name, rom_path, applied, extra, keys):
    """Add this session's leftover to the crop already in force."""
    if extra is None:
        return None
    offsets = [a + e for a, e in zip(applied, extra)]
    if max(offsets) < _MIN_BORDER_NATIVE:
        # A game like GoldenEye, which fills its frame.
        _log(f"borders {offsets} too small to crop")
        _save_state(rom_path, {"offsets": offsets, "done": True})
        return None
    # Nothing worth adding means the crop has converged, and this game
    # never needs measuring again.
    done = max(extra) < 1
    _save_state(rom_path, {"offsets": offsets, "done": done})
    _log(f"crop now {offsets}{' (settled)' if done else ''}")
    if done:
        # Worth saying out loud: the screenshot flashes stop here, and
        # the crop only takes effect when the game is next started, so
        # without this the last thing seen is a border that looks
        # uncorrected.
        _say("SelfSteam: black borders measured, applied on next launch")
    return offsets


def _core_and_rom(argv):
    """Pick the core and ROM out of the command a shortcut runs, the
    same whole command launch.sh passes through. Returns (None, None)
    for anything that is not a RetroArch N64 launch, which is most of
    what this sees."""
    core = rom = None
    for i, arg in enumerate(argv):
        if arg == "-L" and i + 1 < len(argv):
            core = _CORES.get(os.path.basename(argv[i + 1]))
        elif arg.startswith("-L") and len(arg) > 2:
            core = _CORES.get(os.path.basename(arg[2:]))
    if not core:
        return None, None
    # The ROM is the trailing argument; RetroArch's own command line
    # puts the content path last.
    if len(argv) > 1 and os.path.isfile(argv[-1]):
        rom = argv[-1]
    return (core, rom) if rom else (None, None)


def main(argv):
    apply_only = "--apply" in argv
    core, rom_path = _core_and_rom([a for a in argv[1:] if a != "--apply"])
    if not core:
        return 0
    try:
        if apply_only:
            apply_pending(core["name"], rom_path, core["keys"])
            return 0
        calibrate(core["name"], rom_path, core["keys"])
    except Exception:
        # Never let a calibration problem stop someone playing.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
