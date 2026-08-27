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
# Same official, stable Flathub repo URL as standalone_emulators.py's own
# FLATHUB_REPO_URL -- duplicated rather than imported, matching this
# project's existing preference for small self-contained modules over a
# new inter-module dependency for two lines of code.
_FLATHUB_REPO_URL = "https://flathub.org/repo/flathub.flatpakrepo"

# Functions:
#   _cores_dir() -- RetroArch's own real cores directory.
#   core_path(console) -- the .so file path for a given console/core entry.
#   core_installed(console) -- whether that core's .so is already on disk.
#   retroarch_installed() -- whether the RetroArch Flatpak itself is installed.
#   _ensure_flathub_remote(flatpak) -- adds the Flathub remote if it's missing.
#   install_retroarch() -- flatpak installs RetroArch.
#   install_core(console) -- downloads the real prebuilt core .so from libretro's buildbot.
#   _system_dir() -- RetroArch's own real BIOS/system directory.
#   bios_installed(console) -- real on-disk check for that console's required BIOS file(s).
#   install_bios(console, file_path) -- copies a picked BIOS file into RetroArch's system dir.
#   launch_args(console, romfile) -- argv for launching console's core against romfile.
#   default_label_for_group(group) -- the full "<group> - <core>" label for group's own recommended core.
#   console_icon_url(group) -- the /vendor/console-icons/ URL for group's own icon.

# (console group, libretro core name, needs BIOS, core's own real display
# name, is_default). Wider than the original 7-console starter list, but
# still a curated pick -- one or more cores per mainstream system
# RetroArch/libretro actually ships a real, currently-published core
# build for (every entry below confirmed against buildbot.libretro.com/
# nightly/linux/x86_64/latest/ for the .so.zip itself, and against
# libretro's own libretro-core-info repo for its corename field, not
# guessed), not the full ~200-entry libretro catalog (which includes
# homebrew/fantasy-console/engine-port cores this picker has no reason to
# list). BIOS flags are best-effort based on each core/system's own
# documented requirements, not individually live-tested the way PS1/mGBA
# were.
#
# More than one core per console group is deliberate and, as of
# 2026-08-25, the norm rather than the exception (most groups below have
# 2-3) -- every core actually available gets its own picker entry rather
# than this module picking a single "best" one on the user's behalf, so
# the group name alone is NOT a unique identifier; the picker shows (and
# state stores) "<group> - <corename>" as the real unique value instead.
# Exactly one entry per group is marked is_default=True -- the Emulators
# tab's own two-dropdown picker (console, then that console's own cores)
# pre-selects it the moment a console is chosen, based on general
# community consensus for "most accurate/most recommended" at the time
# this list was put together (RetroHandheldHQ's own core-recommendation
# guide, cross-checked against libretro-core-info), not a hard rule --
# any entry in the same group can still be picked instead.
_CONSOLE_ENTRIES = [
    ("Atari 2600", "stella", False, "Stella", True),
    ("Atari 7800", "prosystem", True, "ProSystem", True),
    ("Nintendo (NES)", "nestopia", False, "Nestopia", False),
    # corename confirmed via libretro-core-info's own fceumm_libretro.info
    # ("FCEUmm"), mesen_libretro.info ("Mesen"), and quicknes_libretro.info
    # ("QuickNES").
    ("Nintendo (NES)", "fceumm", False, "FCEUmm", False),
    ("Nintendo (NES)", "mesen", False, "Mesen", True),
    ("Nintendo (NES)", "quicknes", False, "QuickNES", False),
    # Confirmed real and non-experimental via libretro's own core-info
    # entry (citra_libretro.info: is_experimental="false") -- needs
    # decrypted ROMs to function per that same entry's description, not
    # a system-file/BIOS requirement this module's own needs_bios flag
    # is meant to gate on. Azahar is the actively-maintained continuation
    # of Citra (confirmed via its own azahar_libretro.info: corename
    # "Azahar", systemname "3DS", same no-BIOS-needed shape) -- the
    # default here now that Citra's own original team has moved on,
    # Citra kept as a real alternative for anyone already set up with it.
    ("Nintendo 3DS", "citra", False, "Citra", False),
    ("Nintendo 3DS", "azahar", False, "Azahar", True),
    ("Super Nintendo", "snes9x", False, "Snes9x", False),
    # corename confirmed via libretro-core-info's own bsnes_libretro.info
    # ("bsnes") and mesen-s_libretro.info ("Mesen-S").
    ("Super Nintendo", "bsnes", False, "bsnes", True),
    ("Super Nintendo", "mesen-s", False, "Mesen-S", False),
    ("Nintendo 64", "mupen64plus_next", False, "Mupen64Plus-Next", True),
    ("Nintendo 64", "parallel_n64", False, "ParaLLEl N64", False),
    ("Game Boy", "gambatte", False, "Gambatte", True),
    ("Game Boy Color", "gambatte", False, "Gambatte", True),
    # corename confirmed via libretro-core-info's own sameboy_libretro.info
    # ("SameBoy") -- its two boot ROMs (dmg_boot.bin/cgb_boot.bin) are
    # listed there as optional, not required, same as Gambatte's own
    # needs_bios=False.
    ("Game Boy", "sameboy", False, "SameBoy", False),
    ("Game Boy Color", "sameboy", False, "SameBoy", False),
    ("Game Boy Advance", "mgba", False, "mGBA", True),
    # VBA-M, confirmed real via vbam_libretro.so.zip on the buildbot --
    # same no-BIOS-needed shape as mGBA (GBA's own boot ROM is optional,
    # not required, for either core).
    ("Game Boy Advance", "vbam", False, "VBA-M", False),
    ("Nintendo DS", "melonds", False, "melonDS", True),
    # DeSmuME, the long-standing alternative -- real via desmume_libretro.
    # so.zip on the buildbot.
    ("Nintendo DS", "desmume", False, "DeSmuME", False),
    ("Sega Master System", "genesis_plus_gx", False, "Genesis Plus GX", True),
    ("Sega Game Gear", "genesis_plus_gx", False, "Genesis Plus GX", True),
    ("Sega Genesis", "genesis_plus_gx", False, "Genesis Plus GX", True),
    # BlastEm, confirmed real via blastem_libretro.so.zip on the buildbot --
    # Genesis/Mega Drive specifically, not the wider Master System/Game
    # Gear scope Genesis Plus GX also covers.
    ("Sega Genesis", "blastem", False, "BlastEm", False),
    ("Sega CD", "genesis_plus_gx", True, "Genesis Plus GX", True),
    ("Sega 32X", "picodrive", True, "PicoDrive", True),
    ("Sega Saturn", "mednafen_saturn", True, "Beetle Saturn", True),
    # Kronos, confirmed real via libretro-core-info's own kronos_libretro.
    # info (corename "Kronos", systemname "Saturn", needs the same
    # mandatory Saturn BIOS + ST-V BIOS as Beetle Saturn) -- an actively-
    # developed Yabause fork, kept as an alternative rather than the
    # default since Beetle Saturn is what this app already shipped with.
    ("Sega Saturn", "kronos", True, "Kronos", False),
    ("Sega Dreamcast", "flycast", True, "Flycast", True),
    ("PlayStation 1", "pcsx_rearmed", True, "PCSX-ReARMed", False),
    # Beetle PSX HW, confirmed real via libretro-core-info's own
    # mednafen_psx_hw_libretro.info (corename "Beetle PSX HW", needs one
    # of the standard scph550x.bin BIOS files) -- the more accuracy-
    # focused, hardware-accelerated pick, default here over PCSX-ReARMed
    # (built for ARM/mobile, not this app's own target hardware).
    # SwanStation, confirmed real via its own swanstation_libretro.info
    # (corename "SwanStation", same BIOS requirement) -- a modern, fast
    # alternative.
    ("PlayStation 1", "mednafen_psx_hw", True, "Beetle PSX HW", True),
    ("PlayStation 1", "swanstation", True, "SwanStation", False),
    ("PlayStation 2", "pcsx2", True, "LRPS2", True),
    ("PSP", "ppsspp", False, "PPSSPP", True),
    ("TurboGrafx-16 / PC Engine", "mednafen_pce", False, "Beetle PCE", True),
    # Beetle PCE Fast, confirmed real via mednafen_pce_fast_libretro.so.zip
    # on the buildbot -- a lighter/faster variant of the same core family.
    ("TurboGrafx-16 / PC Engine", "mednafen_pce_fast", False, "Beetle PCE Fast", False),
    ("Neo Geo", "fbneo", True, "FinalBurn Neo", True),
    ("Neo Geo Pocket", "mednafen_ngp", False, "Beetle NeoPop", True),
    ("WonderSwan", "mednafen_wswan", False, "Beetle WonderSwan", True),
    # Beetle Lynx, confirmed real via mednafen_lynx_libretro.so.zip on the
    # buildbot -- generally considered the more accurate of the two, made
    # the default here; Handy (already shipped) kept as the lighter
    # alternative.
    ("Atari Lynx", "mednafen_lynx", True, "Beetle Lynx", True),
    ("Atari Lynx", "handy", True, "Handy", False),
    ("ColecoVision", "gearcoleco", True, "Gearcoleco", True),
    ("Intellivision", "freeintv", True, "FreeIntv", True),
    ("Vectrex", "vecx", False, "vecx", True),
    ("MSX", "bluemsx", True, "blueMSX", True),
    # fMSX, confirmed real via fmsx_libretro.so.zip on the buildbot.
    ("MSX", "fmsx", True, "fMSX", False),
    ("Commodore Amiga", "puae", True, "PUAE", True),
    ("Commodore 64", "vice_x64sc", False, "VICE x64sc", True),
    # VICE x64, confirmed real via vice_x64_libretro.so.zip on the
    # buildbot -- the older, non-cycle-exact (faster, less accurate)
    # sibling of x64sc.
    ("Commodore 64", "vice_x64", False, "VICE x64", False),
]

# (label, core, needs_bios), sorted alphabetically by the combined
# "<group> - <corename>" label -- single source of truth for ordering,
# so every consumer (the picker dropdown, BIOS lookups, install/launch)
# sees the same alphabetical order with no separate sort step of its own.
CONSOLES = sorted(
    (
        (f"{group} - {core_display}", core, needs_bios)
        for group, core, needs_bios, core_display, _is_default in _CONSOLE_ENTRIES
    ),
    key=lambda entry: entry[0].lower(),
)
_CORE_FOR_CONSOLE = {label: core for label, core, _ in CONSOLES}
CONSOLES_NEEDING_BIOS = {label for label, _, needs in CONSOLES if needs}

# Console groups (the RetroArch tab's own first dropdown), sorted --
# and, per group, its own real cores (the second dropdown, populated
# once a group is picked) as (core_display, full "<group> - <core>"
# label, is_default), sorted by core_display. Kept separate from
# CONSOLES above (which stays the flat, already-widely-used "one
# picker, one combined label" shape every existing consumer --
# core_path/install_core/bios_installed/launch_args, none of which
# needed to change) rather than replacing it -- these two only exist
# for the picker's own two-dropdown rendering.
CONSOLE_GROUPS = sorted({group for group, *_rest in _CONSOLE_ENTRIES})
CORES_BY_GROUP = {}
for _group, _core, _needs_bios, _core_display, _is_default in _CONSOLE_ENTRIES:
    CORES_BY_GROUP.setdefault(_group, []).append((_core_display, f"{_group} - {_core_display}", _is_default))
for _group in CORES_BY_GROUP:
    CORES_BY_GROUP[_group].sort(key=lambda entry: entry[0].lower())


# Real per-console icons for the RA tab's own console picker (see
# selfsteam_server._console_picker_html) -- libretro's own
# retroarch-assets repo, xmb/retrosystem theme (real, colorful per-
# platform icons used in RetroArch's own XMB menu, not a generic/
# guessed icon set), downloaded once and bundled under
# vendor/console-icons/<slug>.png rather than hotlinked from GitHub's
# raw content host on every page load. Every CONSOLE_GROUPS entry has
# a real match here -- confirmed by cross-checking the full file
# listing at github.com/libretro/retroarch-assets/tree/master/xmb/
# retrosystem/png, not guessed.
CONSOLE_ICON_SLUGS = {
    "Atari 2600": "atari-2600",
    "Atari 7800": "atari-7800",
    "Atari Lynx": "atari-lynx",
    "ColecoVision": "colecovision",
    "Commodore 64": "commodore-64",
    "Commodore Amiga": "commodore-amiga",
    "Game Boy": "game-boy",
    "Game Boy Advance": "game-boy-advance",
    "Game Boy Color": "game-boy-color",
    "Intellivision": "intellivision",
    "MSX": "msx",
    "Neo Geo": "neo-geo",
    "Neo Geo Pocket": "neo-geo-pocket",
    "Nintendo (NES)": "nintendo-nes",
    "Nintendo 3DS": "nintendo-3ds",
    "Nintendo 64": "nintendo-64",
    "Nintendo DS": "nintendo-ds",
    "PSP": "psp",
    "PlayStation 1": "playstation-1",
    "PlayStation 2": "playstation-2",
    "Sega 32X": "sega-32x",
    "Sega CD": "sega-cd",
    "Sega Dreamcast": "sega-dreamcast",
    "Sega Game Gear": "sega-game-gear",
    "Sega Genesis": "sega-genesis",
    "Sega Master System": "sega-master-system",
    "Sega Saturn": "sega-saturn",
    "Super Nintendo": "super-nintendo",
    "TurboGrafx-16 / PC Engine": "turbografx-16-pc-engine",
    "Vectrex": "vectrex",
    "WonderSwan": "wonderswan",
}


def console_icon_url(group):
    """The /vendor/console-icons/ URL for group's own icon, or "" for an
    unknown group (the placeholder state) rather than a broken <img>
    src."""
    slug = CONSOLE_ICON_SLUGS.get(group)
    return f"/vendor/console-icons/{slug}.png" if slug else ""


def default_label_for_group(group):
    """The full "<group> - <core>" label for group's own is_default
    entry -- what the RetroArch tab's core dropdown pre-selects the
    moment its console dropdown picks this group. Falls back to
    group's own first (alphabetical) entry if somehow none is marked
    default, rather than raising -- every real group here does have
    exactly one, but an unknown/empty group (the placeholder "pick a
    console" option) has to resolve to something rather than crash."""
    entries = CORES_BY_GROUP.get(group)
    if not entries:
        return ""
    for _core_display, label, is_default in entries:
        if is_default:
            return label
    return entries[0][1]


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


def _ensure_flathub_remote(flatpak):
    # --if-not-exists makes this safe to run every install unconditionally
    # -- a no-op on the common case (Flathub's already configured), a
    # real remote-add on a genuinely fresh machine that's never
    # installed anything from Flathub before. Without this, a machine
    # that had never touched Flathub and added a RetroArch shortcut
    # *before* ever using the Emulators tab (the only place this check
    # used to run, in standalone_emulators.py's own install()) would
    # fail with "specified remote not found" instead of just working.
    subprocess.run(
        host_exec.wrap([flatpak, "remote-add", "--if-not-exists", "--user", "flathub", _FLATHUB_REPO_URL]),
        check=True,
    )


def install_retroarch():
    flatpak = host_exec.which("flatpak")
    _ensure_flathub_remote(flatpak)
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
