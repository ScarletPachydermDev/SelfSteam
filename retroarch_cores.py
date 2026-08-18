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

# (console name shown in the picker, libretro core name, needs BIOS).
# Wider than the original 7-console starter list, but still a curated
# pick -- one core per mainstream system RetroArch/libretro actually
# ships a real, currently-published core build for (every name below
# confirmed against buildbot.libretro.com/nightly/linux/x86_64/latest/
# directly, not assumed), not the full ~150-entry libretro catalog
# (which includes homebrew/fantasy-console/engine-port cores this
# picker has no reason to list). BIOS flags are best-effort based on
# each core/system's own documented requirements, not individually
# live-tested the way PS1/mGBA were.
CONSOLES = [
    ("Atari 2600", "stella", False),
    ("Atari 7800", "prosystem", True),
    ("Nintendo (NES)", "nestopia", False),
    ("Super Nintendo", "snes9x", False),
    ("Nintendo 64", "mupen64plus_next", False),
    ("Game Boy", "gambatte", False),
    ("Game Boy Color", "gambatte", False),
    ("Game Boy Advance", "mgba", False),
    ("Nintendo DS", "melonds", False),
    ("Sega Master System", "genesis_plus_gx", False),
    ("Sega Game Gear", "genesis_plus_gx", False),
    ("Sega Genesis", "genesis_plus_gx", False),
    ("Sega CD", "genesis_plus_gx", True),
    ("Sega 32X", "picodrive", True),
    ("Sega Saturn", "mednafen_saturn", True),
    ("Sega Dreamcast", "flycast", True),
    ("PlayStation 1", "pcsx_rearmed", True),
    ("PlayStation 2", "pcsx2", True),
    ("PSP", "ppsspp", False),
    ("TurboGrafx-16 / PC Engine", "mednafen_pce", False),
    ("Neo Geo", "fbneo", True),
    ("Neo Geo Pocket", "mednafen_ngp", False),
    ("WonderSwan", "mednafen_wswan", False),
    ("Atari Lynx", "handy", True),
    ("ColecoVision", "gearcoleco", True),
    ("Intellivision", "freeintv", True),
    ("Vectrex", "vecx", False),
    ("MSX", "bluemsx", True),
    ("Commodore Amiga", "puae", True),
    ("Commodore 64", "vice_x64sc", False),
]
_CORE_FOR_CONSOLE = {name: core for name, core, _ in CONSOLES}
CONSOLES_NEEDING_BIOS = {name for name, _, needs in CONSOLES if needs}


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
_BIOS_FILENAMES = {
    "Atari 7800": ("any", ["7800 BIOS (U).rom"]),
    "Sega CD": ("any", ["bios_CD_U.bin", "bios_CD_E.bin", "bios_CD_J.bin"]),
    "Sega Saturn": ("any", ["sega_101.bin", "mpr-17933.bin"]),
    "Sega Dreamcast": ("any", ["dc/dc_boot.bin"]),
    "PlayStation 1": ("any", ["scph5501.bin", "scph1001.bin", "scph7001.bin", "scph101.bin", "PSXONPSP660.bin"]),
    "Atari Lynx": ("any", ["lynxboot.img"]),
    "ColecoVision": ("any", ["colecovision.rom", "coleco.rom"]),
    "Intellivision": ("all", ["exec.bin", "grom.bin"]),
    "Commodore Amiga": ("any", ["kick34005.A500", "kick37175.A500", "kick40063.A600", "kick39106.A1200", "kick40068.A1200", "kick33180.A500"]),
}


def bios_installed(console):
    """Real on-disk check, same idea as standalone_emulators.keys_installed/
    firmware_installed -- lets a second/third/... game for a console that
    already has its BIOS in place skip picking it again. Returns the
    real filename(s) actually found (comma-joined for "all"-mode
    consoles needing more than one), or None -- the picker shows this
    directly rather than a vague "installed" label."""
    entry = _BIOS_FILENAMES.get(console)
    if not entry:
        return None
    mode, filenames = entry
    system_dir = _system_dir()
    present = [fn for fn in filenames if os.path.isfile(os.path.join(system_dir, fn))]
    if mode == "all":
        return ", ".join(os.path.basename(fn) for fn in filenames) if len(present) == len(filenames) else None
    return os.path.basename(present[0]) if present else None


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
