"""RetroArch console/core catalog and Flatpak launch/install helpers.

RetroArch itself is a single Flatpak app (org.libretro.RetroArch) --
individual console "cores" are separate .so files it loads at launch
via -L, not separate Flatpak packages of their own. Real prebuilt core
binaries come straight from libretro's own public buildbot (the same
source RetroDECK/EmuDeck/Portmaster all pull from), since RetroArch's
built-in Core Downloader is a GUI-only menu action with no headless
equivalent -- confirmed live: buildbot.libretro.com/nightly/linux/x86_64/latest/
lists real, currently-served <core>_libretro.so.zip files matching
every core name below.
"""
import os
import shlex
import shutil
import subprocess
import urllib.request
import zipfile

import host_exec

RETROARCH_APP_ID = "org.libretro.RetroArch"
_BUILDBOT_BASE = "https://buildbot.libretro.com/nightly/linux/x86_64/latest"

# (console group, libretro core name, needs BIOS, core's own real display
# name). Wider than the original 7-console starter list, but still a
# curated pick -- one or more cores per mainstream system RetroArch/
# libretro actually ships a real, currently-published core build for
# (every entry below confirmed against buildbot.libretro.com/nightly/
# linux/x86_64/latest/ for the .so.zip itself, and against libretro's own
# libretro-core-info repo for its corename field, not guessed), not the
# full ~150-entry libretro catalog (which includes homebrew/fantasy-
# console/engine-port cores this picker has no reason to list). BIOS
# flags are best-effort based on each core/system's own documented
# requirements, not individually live-tested the way PS1/mGBA were.
#
# More than one core per console group is deliberate (N64 currently has
# two) -- every core actually available gets its own picker entry rather
# than this module picking a single "best" one on the user's behalf, so
# the group name alone is NOT a unique identifier; the picker shows (and
# state stores) "<group> - <corename>" as the real unique value instead.
_CONSOLE_ENTRIES = [
    ("Atari 2600", "stella", False, "Stella"),
    ("Atari 7800", "prosystem", True, "ProSystem"),
    ("Nintendo (NES)", "nestopia", False, "Nestopia"),
    # corename confirmed via libretro-core-info's own fceumm_libretro.info
    # ("FCEUmm") and mesen_libretro.info ("Mesen").
    ("Nintendo (NES)", "fceumm", False, "FCEUmm"),
    ("Nintendo (NES)", "mesen", False, "Mesen"),
    # Confirmed real and non-experimental via libretro's own core-info
    # entry (citra_libretro.info: is_experimental="false") -- needs
    # decrypted ROMs to function per that same entry's description, not
    # a system-file/BIOS requirement this module's own needs_bios flag
    # is meant to gate on.
    ("Nintendo 3DS", "citra", False, "Citra"),
    ("Super Nintendo", "snes9x", False, "Snes9x"),
    # corename confirmed via libretro-core-info's own bsnes_libretro.info
    # ("bsnes") and mesen-s_libretro.info ("Mesen-S").
    ("Super Nintendo", "bsnes", False, "bsnes"),
    ("Super Nintendo", "mesen-s", False, "Mesen-S"),
    ("Nintendo 64", "mupen64plus_next", False, "Mupen64Plus-Next"),
    ("Nintendo 64", "parallel_n64", False, "ParaLLEl N64"),
    ("Game Boy", "gambatte", False, "Gambatte"),
    ("Game Boy Color", "gambatte", False, "Gambatte"),
    ("Game Boy Advance", "mgba", False, "mGBA"),
    ("Nintendo DS", "melonds", False, "melonDS"),
    ("Sega Master System", "genesis_plus_gx", False, "Genesis Plus GX"),
    ("Sega Game Gear", "genesis_plus_gx", False, "Genesis Plus GX"),
    ("Sega Genesis", "genesis_plus_gx", False, "Genesis Plus GX"),
    ("Sega CD", "genesis_plus_gx", True, "Genesis Plus GX"),
    ("Sega 32X", "picodrive", True, "PicoDrive"),
    ("Sega Saturn", "mednafen_saturn", True, "Beetle Saturn"),
    ("Sega Dreamcast", "flycast", True, "Flycast"),
    ("PlayStation 1", "pcsx_rearmed", True, "PCSX-ReARMed"),
    ("PlayStation 2", "pcsx2", True, "LRPS2"),
    ("PSP", "ppsspp", False, "PPSSPP"),
    ("TurboGrafx-16 / PC Engine", "mednafen_pce", False, "Beetle PCE"),
    ("Neo Geo", "fbneo", True, "FinalBurn Neo"),
    ("Neo Geo Pocket", "mednafen_ngp", False, "Beetle NeoPop"),
    ("WonderSwan", "mednafen_wswan", False, "Beetle WonderSwan"),
    ("Atari Lynx", "handy", True, "Handy"),
    ("ColecoVision", "gearcoleco", True, "Gearcoleco"),
    ("Intellivision", "freeintv", True, "FreeIntv"),
    ("Vectrex", "vecx", False, "vecx"),
    ("MSX", "bluemsx", True, "blueMSX"),
    ("Commodore Amiga", "puae", True, "PUAE"),
    ("Commodore 64", "vice_x64sc", False, "VICE x64sc"),
]

# (label, core, needs_bios), sorted alphabetically by the combined
# "<group> - <corename>" label -- single source of truth for ordering,
# so every consumer (the picker dropdown, BIOS lookups, install/launch)
# sees the same alphabetical order with no separate sort step of its own.
CONSOLES = sorted(
    (
        (f"{group} - {core_display}", core, needs_bios)
        for group, core, needs_bios, core_display in _CONSOLE_ENTRIES
    ),
    key=lambda entry: entry[0].lower(),
)
_CORE_FOR_CONSOLE = {label: core for label, core, _ in CONSOLES}
CONSOLES_NEEDING_BIOS = {label for label, _, needs in CONSOLES if needs}


def _cores_dir():
    # Where RetroArch's own Flatpak sandbox looks for cores -- same
    # ~/.var/app/<app-id>/... layout every Flatpak app's persistent data
    # lives under, confirmed live against a real RetroArch install
    # rather than assumed from the general pattern alone.
    return os.path.expanduser(f"~/.var/app/{RETROARCH_APP_ID}/config/retroarch/cores")


def core_path(console):
    core = _CORE_FOR_CONSOLE.get(console)
    if not core:
        return None
    return os.path.join(_cores_dir(), f"{core}_libretro.so")


def core_installed(console):
    path = core_path(console)
    return path is not None and os.path.exists(path)


def retroarch_installed():
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return False
    result = subprocess.run(
        host_exec.wrap([flatpak, "info", RETROARCH_APP_ID]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def install_retroarch():
    flatpak = host_exec.which("flatpak")
    subprocess.run(
        host_exec.wrap([flatpak, "install", "--user", "-y", "flathub", RETROARCH_APP_ID]),
        check=True,
    )


def install_core(console):
    """Downloads the real prebuilt core .so from libretro's buildbot and
    places it where RetroArch's Flatpak sandbox expects it. Raises
    ValueError for an unknown console, urllib/zipfile's own exceptions
    for a real download/extract failure -- surfaced to the caller
    rather than swallowed, since a shortcut built against a core that
    silently failed to install would just fail to launch later with no
    clue why."""
    core = _CORE_FOR_CONSOLE.get(console)
    if not core:
        raise ValueError(f"No known RetroArch core for console: {console}")
    cores_dir = _cores_dir()
    os.makedirs(cores_dir, exist_ok=True)
    url = f"{_BUILDBOT_BASE}/{core}_libretro.so.zip"
    zip_path = os.path.join(cores_dir, f"{core}_libretro.so.zip")
    urllib.request.urlretrieve(url, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(cores_dir)
    os.remove(zip_path)


def _system_dir():
    # RetroArch's documented BIOS/firmware location -- confirmed live
    # (this directory is created alongside cores/saves/states/etc. the
    # first time RetroArch itself runs). Cores like pcsx_rearmed scan
    # here, not wherever the ROM happens to live.
    return os.path.expanduser(f"~/.var/app/{RETROARCH_APP_ID}/config/retroarch/system")


# Real expected BIOS filenames per console, confirmed against each
# core's own page under docs.libretro.com/library/<core> (not guessed) --
# ("any", [...]) means any one of these satisfies the requirement
# (region variants, alternate dumps); ("all", [...]) means every one of
# them has to be present (FreeIntv's exec.bin/grom.bin are two separate,
# both-required files, not alternatives of each other). A console
# missing from this dict has no single reliably-checkable filename (PS2's
# LRPS2 core accepts any filename at all; Neo Geo/FBNeo and MSX/blueMSX
# both need a whole romset/folder dropped in, not one system-dir file;
# Sega 32X's own BIOS docs list no required file at all) -- those consoles
# just always show the picker, same as before this existed.
#
# Keyed by the same combined "<group> - <corename>" label CONSOLES now
# uses, not the bare group name -- each of these BIOS files is really
# tied to one specific core's own system dir, not the console group as a
# whole (a second core for one of these systems would need its own entry
# here too, since it may expect a different filename or directory).
_BIOS_FILENAMES = {
    "Atari 7800 - ProSystem": ("any", ["7800 BIOS (U).rom"]),
    "Sega CD - Genesis Plus GX": ("any", ["bios_CD_U.bin", "bios_CD_E.bin", "bios_CD_J.bin"]),
    "Sega Saturn - Beetle Saturn": ("any", ["sega_101.bin", "mpr-17933.bin"]),
    "Sega Dreamcast - Flycast": ("any", ["dc/dc_boot.bin"]),
    "PlayStation 1 - PCSX-ReARMed": ("any", ["scph5501.bin", "scph1001.bin", "scph7001.bin", "scph101.bin", "PSXONPSP660.bin"]),
    "Atari Lynx - Handy": ("any", ["lynxboot.img"]),
    "ColecoVision - Gearcoleco": ("any", ["colecovision.rom", "coleco.rom"]),
    "Intellivision - FreeIntv": ("all", ["exec.bin", "grom.bin"]),
    "Commodore Amiga - PUAE": ("any", ["kick34005.A500", "kick37175.A500", "kick40063.A600", "kick39106.A1200", "kick40068.A1200", "kick33180.A500"]),
}


def bios_installed(console):
    """Real on-disk check, same idea as standalone_emulators.keys_installed/
    firmware_installed -- lets a second/third/... game for a console that
    already has its BIOS in place skip picking it again. Returns the
    real filename(s) actually found (comma-joined for "all"-mode
    consoles needing more than one), or None -- the picker shows this
    directly rather than a vague "installed" label.

    Matched case-insensitively against what's actually on disk --
    install_bios keeps whatever case the picked file originally had
    (real BIOS dumps routinely ship as e.g. "SCPH1001.BIN", not the
    lowercase "scph1001.bin" _BIOS_FILENAMES lists), and this runs on a
    case-sensitive filesystem, so a naive os.path.isfile against the
    lowercase name alone missed a BIOS that was genuinely already
    there -- confirmed live, a real install left behind on disk as
    "SCPH1001.BIN" kept showing the picker again instead of "already
    installed"."""
    entry = _BIOS_FILENAMES.get(console)
    if not entry:
        return None
    mode, filenames = entry
    system_dir = _system_dir()
    try:
        on_disk = {f.lower(): f for f in os.listdir(system_dir)}
    except FileNotFoundError:
        on_disk = {}
    present = [on_disk[fn.lower()] for fn in filenames if fn.lower() in on_disk]
    if mode == "all":
        return ", ".join(present) if len(present) == len(filenames) else None
    return present[0] if present else None


def install_bios(bios_src_path):
    """Copies a picked BIOS file into RetroArch's system directory,
    keeping its own filename -- the picker never needs to know each
    core's exact expected BIOS filename, since RetroArch/the core
    itself matches by scanning the directory, not a hardcoded path."""
    system_dir = _system_dir()
    os.makedirs(system_dir, exist_ok=True)
    dest = os.path.join(system_dir, os.path.basename(bios_src_path))
    shutil.copy2(bios_src_path, dest)
    return dest


def launch_args(console, romfile):
    """argv for launching `console`'s core against romfile, already
    shell-quoted where needed -- same "ready to ' '.join() into
    LaunchOptions" contract as browser_launcher.kiosk_launch_args.
    ROM filenames routinely have spaces/parens in them ("Chrono Trigger
    (USA).sfc"), and LaunchOptions is stored/parsed as one shell-like
    string, so both the core path and the ROM path need quoting or they
    silently word-split into bogus arguments -- same footgun already
    documented in browser_launcher.py's own user-agent handling."""
    flatpak = host_exec.which("flatpak")
    core = core_path(console)
    if not flatpak or not core:
        return None
    # -f/--fullscreen: confirmed real via RetroArch's own --help output
    # ("Start the program in fullscreen regardless of config setting")
    # -- without it RetroArch opens windowed, which is what a shortcut
    # meant to launch straight into a game shouldn't do.
    return [flatpak, "run", RETROARCH_APP_ID, "-f", "-L", shlex.quote(core), shlex.quote(romfile)]
