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
# Deliberately narrow list -- systems confirmed to have a real,
# currently-published core build, not an exhaustive libretro catalog.
CONSOLES = [
    ("Nintendo 64", "mupen64plus_next", False),
    ("Super Nintendo", "snes9x", False),
    ("Game Boy Advance", "mgba", False),
    ("PlayStation 1", "pcsx_rearmed", True),
    ("Sega Genesis", "genesis_plus_gx", False),
    ("Nintendo DS", "melonds", False),
    ("PSP", "ppsspp", False),
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
    return [flatpak, "run", RETROARCH_APP_ID, "-L", shlex.quote(core), shlex.quote(romfile)]
