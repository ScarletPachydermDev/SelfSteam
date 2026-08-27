"""Standalone (non-RetroArch) emulator catalog and launch helpers.

Unlike retroarch_cores.py -- one RetroArch app loading many interchangeable
cores via a single -L flag -- each entry here is its own separate, self-
contained application with its own CLI conventions. There's no shared
launch pattern across them, so each gets its own small args-builder
function rather than a generic one.

Two install_type values, matching the Emulators tab's own Flathub/
AppImage toggle:
  "flathub" -- a real Flatpak app, installed via `flatpak install`.
  "binary"  -- a portable build (AppImage or tarball) resolved fresh from
               the emulator project's own update API/release feed each
               time (same idea as retroarch_cores.py pulling cores from
               libretro's buildbot), not a fixed URL. Not implemented
               yet -- installed()/install() only handle "flathub" so
               far; the field exists now so the tab's dropdown-filtering
               logic has something real to filter on ahead of the first
               binary-type entry being added.
"""
import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

import gamescope_splash
import host_exec
import steamos_session

# Functions, grouped:
#
# Public catalog/dispatch API (used by selfsteam_server.py):
#   by_install_type(install_type) -- emulator names filtered to "flathub" or "binary".
#   installed(name) / install(name) -- whether/how to install an entry's own app.
#   flathub_app_id_installed(app_id) / install_flathub_app_id(app_id) -- same, for a bare Flathub app_id not in EMULATORS (the Apps tab).
#   installed_flathub_app_ids() -- every installed Flatpak app id in one call (the Apps tab's own browse grid, avoids a subprocess per card).
#   uninstall_flathub_app_id(app_id) -- flatpak uninstall for a bare app_id (the Apps tab's own Remove button).
#   grant_permissions(name) -- flatpak override for an entry's own permission gap, if any.
#   launch_args(name, romfile, zrif=None) -- argv for launching name against romfile (zrif only used for a Vita3K .pkg -- see install_vita3k_pkg).
#   configure_game_dir(name, game_dir) -- registers game_dir as a watched ROM folder, if supported.
#   bios_slots(name) / bios_slot_installed(name, prefix) / install_bios_slot(name, prefix, path) --
#       the multi-BIOS-file dispatch (xemu, PCSX2, RPCS3, Vita3K).
#   keys_installed(name) / install_keys(name, path) -- Switch prod.keys/title.keys handling.
#   firmware_installed(name) / install_firmware_zip(name, path) -- Switch firmware handling.
#   configure_renderer(name) -- sets an emulator's own renderer preference, if it has one (xemu -> Vulkan).
#   bootstrap_config(name) -- copies an emulator's own bundled config it needs but won't set up itself, if any (no current user).
#   binary_path(name) -- real AppImage path for a "binary" install_type entry.
#
# Per-emulator launch-arg builders (one per catalog entry, e.g. _dolphin_args, _pcsx2_args,
#   _xemu_args, _eden_args, _rpcs3_args, ...) -- each returns the real argv tail for that
#   emulator, confirmed against its own source/docs, not guessed.
#
# Per-emulator game-dir configurators (_dolphin_configure_game_dir, _ryubing_configure_game_dir,
#   _cemu_configure_game_dir, _flycast_configure_game_dir) -- registers a watched ROM folder in
#   that emulator's own config format.
#
# Switch-family (Ryubing/Eden) keys sharing:
#   _switch_keys_dirs() -- every real prod.keys/title.keys directory across the whole family.
#   _switch_keys_installed(entry) / _switch_install_keys(entry, path) -- shared keys dispatch.
#   _ryujinx_family_contents_dirs() -- every real bis/system/Contents dir across the Ryujinx family.
#   _firmware_marker_path/_old_firmware_marker_path(dir) -- sidecar file recording what was installed.
#
# Per-emulator one-off config helpers (Cemu keys, xemu's own TOML, PCSX2's own INI):
#   _cemu_keys_path/_cemu_keys_installed/_cemu_install_keys
#   _toml_get_in_section/_toml_set_in_section/_xemu_toml_path/xemu_bios_slot_installed/
#     install_xemu_bios_slot
#   _pcsx2_bios_dir/_pcsx2_ini_path/pcsx2_bios_slot_installed/install_pcsx2_bios_slot
#   _rpcs3_dev_flash_dir/rpcs3_firmware_installed/install_rpcs3_firmware -- PUP install via
#     RPCS3's own --installfw (needs its real GUI, not headless).
#   _vita3k_fs_dir/vita3k_firmware_installed/install_vita3k_firmware -- Vita3K's own --firmware flag.
#   install_vita3k_pkg(pkg_path, zrif) -- Vita3K's own --pkg/--zrif install (headless, unlike RPCS3's), returns the installed title id.
#
# Binary (AppImage) install machinery:
#   _xdg_data_dir/_xdg_config_dir -- real (unsandboxed) XDG dirs on the host.
#   _binary_dir_name(name)/_binary_dir(name)/_binary_path(name, entry) -- where an AppImage lives.
#   _ensure_flathub_remote() -- adds the Flathub remote if it's missing.
#   install_binary(name, entry) -- resolves the latest release fresh from the emulator's own
#       release feed (GitHub/Forgejo API) and downloads/chmods the AppImage.
#
# Shared path helpers:
#   _flatpak_config_dir/_flatpak_data_dir(app_id, *parts) -- a Flatpak app's own sandboxed dir.

# Official, stable URL for Flathub's own repo file -- same one Flathub's
# own "Quick Setup" instructions use. Needed because a fresh machine
# (never having installed anything from Flathub before) doesn't have
# this remote configured at all, and `flatpak install ... flathub <id>`
# fails outright with "specified remote not found" until it is.
FLATHUB_REPO_URL = "https://flathub.org/repo/flathub.flatpakrepo"


def _dolphin_args(romfile):
    # -b/--batch: exit Dolphin when the game closes instead of returning
    # to its own game list UI. -e/--exec: load and run this file
    # directly. No BIOS/IPL needed -- Dolphin ships its own built-in
    # GameCube IPL emulation (HLE) by default; a real console's IPL/font
    # file is an optional accuracy enhancement, not required to boot.
    #
    # -C Dolphin.Display.Fullscreen=True: Dolphin has no dedicated
    # --fullscreen switch (confirmed via its own --help), only this
    # generic -C <System>.<Section>.<Key>=<Value> config-override flag.
    # "Dolphin.Display.Fullscreen" was inferred from Dolphin.ini's own
    # [Display] section (not persisted anywhere -C overrides could be
    # read back from to confirm ahead of time) and confirmed live by
    # launching a real shortcut on X1 -- it actually opens fullscreen.
    return ["-b", "-C", "Dolphin.Display.Fullscreen=True", "-e", shlex.quote(romfile)]


def _dolphin_configure_game_dir(entry, game_dir):
    # Dolphin.ini, [General] section, count-then-indexed-keys pattern
    # (ISOPaths=<count>, ISOPath0=..., ISOPath1=..., ...) -- confirmed
    # via Dolphin's own real source (Source/Core/Core/Config/
    # MainSettings.cpp's GetIsoPaths()/SetIsoPaths()), not guessed.
    #
    # Only touches an existing ini file -- a fresh install (this
    # emulator has never actually run yet) has no Dolphin.ini at all,
    # and Dolphin creates its own on first launch with a full set of
    # its own defaults; writing a partial one ourselves ahead of that
    # isn't necessary for Dolphin specifically (its ini format is
    # tolerant of missing keys/sections), but staying consistent with
    # every other emulator's own same rule here keeps this one
    # predictable rather than a special case.
    ini_path = _flatpak_config_dir(entry["app_id"], "dolphin-emu", "Dolphin.ini")
    if not os.path.isfile(ini_path):
        return
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve exact key casing -- Dolphin's own keys are case-sensitive PascalCase, not the case-insensitive convention configparser assumes by default
    cp.read(ini_path)
    if not cp.has_section("General"):
        cp.add_section("General")
    count = int(cp.get("General", "ISOPaths", fallback="0") or "0")
    existing = {cp.get("General", f"ISOPath{i}", fallback="") for i in range(count)}
    if game_dir in existing:
        return
    cp.set("General", f"ISOPath{count}", game_dir)
    cp.set("General", "ISOPaths", str(count + 1))
    with open(ini_path, "w") as f:
        cp.write(f, space_around_delimiters=True)


def _ryubing_configure_game_dir(entry, game_dir):
    # Config.json, top-level "game_dirs" array -- the C# field is
    # GameDirs, but Ryubing serializes everything through a real
    # SnakeCaseNamingPolicy (confirmed in its own JsonHelper.cs), so
    # the actual on-disk key is game_dirs, not GameDirs. Confirmed via
    # source, not assumed the C# name and the JSON key would match.
    config_path = _flatpak_config_dir(entry["app_id"], "Ryujinx", "Config.json")
    if not os.path.isfile(config_path):
        # Same reasoning as Dolphin's own -- nothing to safely add to
        # yet. More load-bearing here than for Dolphin: Ryubing's own
        # ConfigurationFileFormat.TryLoad treats Version == 0 (or a
        # missing file) as invalid and silently regenerates its own
        # defaults, discarding anything written -- so even attempting
        # to bootstrap a minimal file here would just get thrown away
        # the moment Ryubing actually starts, not merely be unnecessary
        # the way it is for Dolphin.
        return
    try:
        with open(config_path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not data.get("version"):
        return
    game_dirs = data.get("game_dirs") or []
    if game_dir in game_dirs:
        return
    game_dirs.append(game_dir)
    data["game_dirs"] = game_dirs
    with open(config_path, "w") as f:
        json.dump(data, f, indent=2)


def _ryubing_args(romfile):
    # -f/--fullscreen and the bare romfile positional arg, both confirmed
    # real via Ryubing's own CommandLineState.cs (the actual CLI argument
    # parser in its source, not guessed/assumed): "-f"/"--fullscreen"
    # sets StartFullscreenArg, and anything not matching a known flag
    # falls through to LaunchPathArg, i.e. a bare positional path.
    return ["--fullscreen", shlex.quote(romfile)]


def _cemu_args(romfile):
    # -f/--fullscreen and -g/--game <path>, both confirmed real via
    # Cemu's own src/config/LaunchSettings.cpp (boost::program_options
    # definitions, not guessed): "fullscreen,f" takes an implicit true
    # value (bare -f is enough), "game,g" takes the path of the title to
    # launch directly, bypassing Cemu's own game-list UI.
    return ["-f", "-g", shlex.quote(romfile)]


def _cemu_configure_game_dir(entry, game_dir):
    # settings.xml, <content><GamePaths><Entry>...</Entry></GamePaths>,
    # confirmed via Cemu's own source (config/CemuConfig.cpp's Load/Save)
    # -- a flat list of directories, not per-game entries, same shape as
    # Dolphin's own ISOPaths.
    #
    # Unlike Dolphin/Ryubing's own configurators, this DOES bootstrap a
    # fresh settings.xml if one doesn't exist yet -- deliberately, not
    # an oversight. Confirmed via Cemu's own source (CemuApp.cpp): the
    # "Getting Started" first-run wizard (the "library path and game
    # mods" window) is gated purely on
    # `!fs::exists(GetConfigPath("settings.xml"))`, nothing more --
    # every field CemuConfig::Load reads back out of it falls back to a
    # built-in default if missing, and the on-first-run MLC setup
    # (CreateDefaultMLCFiles) runs identically whether settings.xml
    # existed beforehand or not. So a settings.xml existing before
    # Cemu's own first launch safely skips the wizard outright, which is
    # exactly what a shortcut meant to launch straight into a game
    # should do -- verified from source, not a guess. (Ryubing's own
    # config is version-gated and gets silently discarded if malformed,
    # which is why that one still stays read-only-if-missing.)
    settings_path = _flatpak_config_dir(entry["app_id"], "Cemu", "settings.xml")
    if os.path.isfile(settings_path):
        try:
            tree = ET.parse(settings_path)
            root = tree.getroot()
        except ET.ParseError:
            return
    else:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        root = ET.Element("content")
        tree = ET.ElementTree(root)
    game_paths = root.find("GamePaths")
    if game_paths is None:
        game_paths = ET.SubElement(root, "GamePaths")
    if any((e.text or "") == game_dir for e in game_paths.findall("Entry")):
        return
    entry_el = ET.SubElement(game_paths, "Entry")
    entry_el.text = game_dir
    tree.write(settings_path, encoding="utf-8", xml_declaration=True)


def _switch_keys_dirs():
    """Every real prod.keys/title.keys directory across the whole
    Switch-emulator family this catalog knows about: Flathub Ryubing
    (sandboxed), the AppImage Ryubing build (shared by stable and
    Canary -- confirmed via source, both are built from the same
    AppDataManager.DefaultBaseDir="Ryujinx" regardless of channel, so
    they were already going to land in the same real directory with no
    extra work), and Eden (shared by all four of its own CPU-target
    variants, which are likewise just different builds of one codebase
    reading from the same $XDG_DATA_HOME/eden/keys/).

    prod.keys/title.keys are a literal per-console dump with no
    emulator-specific format -- there's no reason picking them once for
    any one of these should still demand picking them again for any
    other, so install/installed-check below write to and read from all
    of these together rather than per-entry."""
    return [
        _flatpak_config_dir("io.github.ryubing.Ryujinx", "Ryujinx", "system"),
        _xdg_config_dir("Ryujinx", "system"),
        _xdg_data_dir("eden", "keys"),
    ]


def _switch_keys_installed(entry):
    dirs = _switch_keys_dirs()
    source_dir, source_files = None, None
    for keys_dir in dirs:
        if not os.path.isdir(keys_dir):
            continue
        found = sorted(f for f in os.listdir(keys_dir) if f.endswith(".keys"))
        if found:
            source_dir, source_files = keys_dir, found
            break
    if not source_dir:
        return None

    # Backfill any sibling dir that's missing what this one has --
    # closes a real gap confirmed live: keys/firmware installed before
    # this sharing mechanism existed (e.g. via Flathub Ryubing alone)
    # made this function report "already installed" for every family
    # member, but a member whose own directory was never actually
    # populated would still be missing the real files at launch --
    # Ryubing Canary AppImage hit exactly this, reporting installed
    # while its own ~/.config/Ryujinx/system stayed empty until a fresh
    # reinstall happened to propagate it. Checking here instead of only
    # propagating on a fresh install makes the "already installed"
    # claim actually true, not just true for one directory.
    for keys_dir in dirs:
        if keys_dir == source_dir:
            continue
        os.makedirs(keys_dir, exist_ok=True)
        for fname in source_files:
            dest = os.path.join(keys_dir, fname)
            if not os.path.isfile(dest):
                shutil.copy2(os.path.join(source_dir, fname), dest)

    return ", ".join(source_files)


def _switch_install_keys(entry, keys_path):
    """Copies picked keys into every real keys directory across the
    whole Switch-emulator family (see _switch_keys_dirs) at once, not
    just the one the current shortcut's emulator happens to use --
    confirmed real per-directory paths via each emulator's own source,
    same as the single-directory version this replaced.

    keys_path is a single picked file (prod.keys) -- a real Switch key
    dump typically has title.keys (per-game) sitting right alongside it
    too, so this also auto-picks up title.keys from that same folder if
    it's there, without the user needing to pick it separately (see
    _em_picker_section's own "keys" display for the matching UI side of
    this). Returns the list of files actually copied. No parsing/
    verification of the keys' own contents either way -- each emulator
    does that itself on next launch."""
    sibling = os.path.join(os.path.dirname(keys_path), "title.keys")
    has_sibling = os.path.basename(keys_path) != "title.keys" and os.path.isfile(sibling)

    copied = []
    for dest_dir in _switch_keys_dirs():
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(keys_path))
        shutil.copy2(keys_path, dest)
        copied.append(dest)
        if has_sibling:
            sibling_dest = os.path.join(dest_dir, "title.keys")
            shutil.copy2(sibling, sibling_dest)
            copied.append(sibling_dest)

    return copied


def _azahar_args(romfile):
    # -f/--fullscreen plus a bare positional romfile -- both confirmed
    # real via Azahar's own source (src/citra_qt/citra_qt.cpp's
    # ParseArguments: explicit "-f"/"--fullscreen" check, and the last
    # non-flag argument becomes game_path).
    return ["-f", shlex.quote(romfile)]


def _play_args(romfile):
    # --disc <path> and --fullscreen, both confirmed real via Play!'s
    # own source (Source/ui_qt/main.cpp's QCommandLineParser setup:
    # explicit "disc" option feeding w.LoadCDROM()/w.BootCDROM(), and
    # "fullscreen" feeding w.showFullScreen()). Named option, not a bare
    # positional arg, unlike most other emulators here.
    return ["--fullscreen", "--disc", shlex.quote(romfile)]


def _pcsx2_args(romfile):
    # -batch (skip the game list UI, quit instead of returning to it on
    # close) and -fullscreen, both confirmed real via PCSX2's own source
    # (pcsx2-qt/QtHost.cpp's ParseCommandLineOptions) -- notably the
    # exact same flags PCSX2's own built-in "Create Shortcut" dialog
    # generates for a fullscreen shortcut (confirmed via
    # pcsx2-qt/ShortcutCreationDialog.cpp), not independently guessed.
    # Bare positional romfile -- any non-flag token accumulates into the
    # boot filename (same file, same ParseCommandLineOptions).
    return ["-batch", "-fullscreen", shlex.quote(romfile)]


def _ppsspp_args(romfile):
    # --fullscreen (forces fullscreen, ignoring saved config -- confirmed
    # real via PPSSPP's own command-line docs) plus a bare positional
    # romfile -- confirmed real via PPSSPP's own source (SDL/SDLMain.cpp
    # logs "Boot filename found in args" for the first non-flag arg it
    # sees).
    return ["--fullscreen", shlex.quote(romfile)]


def _xemu_args(romfile):
    # -full-screen (QEMU's own legacy option, still real and wired up --
    # confirmed via ui/xemu.c: `gui_fullscreen = o->has_full_screen &&
    # o->full_screen`) plus -dvd_path <path>, xemu's own added override
    # (confirmed via system/vl.c: an explicit argv scan for "-dvd_path"
    # that overrides the persisted g_config.sys.files.dvd_path setting
    # for just this one launch). No bare positional romfile convention
    # here unlike most other emulators -- this is a QEMU fork, and
    # that's QEMU's own CLI style, not something xemu simplified away.
    return ["-full-screen", "-dvd_path", shlex.quote(romfile)]


def _melonds_args(romfile):
    # -f/--fullscreen plus a bare positional "nds" romfile -- both
    # confirmed real via melonDS's own source (src/frontend/qt_sdl/
    # CLI.cpp: addOption({"f","fullscreen"}, ...), addPositionalArgument
    # ("nds", ...)).
    return ["-f", shlex.quote(romfile)]


def _m64py_args(romfile):
    # Bare positional romfile -- confirmed real via M64Py's own source
    # (src/m64py/opts.py: `usage = 'usage: %prog <romfile>'`, only other
    # option is -v/--verbose). No fullscreen flag or persisted fullscreen
    # setting exists at all (confirmed: grepped mainwindow.py/settings.py
    # for both -- isFullScreen()/setWindowState() are pure runtime Qt
    # window-state toggles, never read from or written to
    # QSettings/m64py.conf), so unlike every other emulator here this
    # always launches windowed -- a real, unavoidable gap for a
    # Steam/Big-Picture launch, not an oversight in this args builder.
    return [shlex.quote(romfile)]


def _rmg_args(romfile):
    # -f/-n/-q (fullscreen, hide menubar/toolbar/statusbar, quit once
    # emulation ends) plus a bare positional ROM path -- confirmed real
    # via RMG's own source (Source/RMG/main.cpp's QCommandLineParser
    # setup: fullscreenOption/noGuiOption/quitAfterEmulationOption,
    # addPositionalArgument("ROM", ...)). No configure_game_dir entry --
    # RMG does have a real watched-folder setting (RomBrowser_Directory,
    # confirmed via Source/RMG-Core/Settings.cpp), unlike gopher64's
    # capped recent-list-only approach, but its on-disk config file path
    # wasn't pinned down with the same confidence as Dolphin/Cemu/
    # Flycast's own configure_game_dir writers, so this skips writing to
    # it rather than guessing a path that could silently no-op or hit
    # the wrong file.
    return ["-f", "-n", "-q", shlex.quote(romfile)]


def _gopher64_args(romfile):
    # -f/--fullscreen (clap's derive macro auto-shortens "fullscreen" to
    # -f since no explicit short letter is given) plus a bare positional
    # rom path -- confirmed real via gopher64's own source
    # (src/lib.rs's Args struct: `pub game: Option<String>` with no
    # #[arg(...)] above it is clap's plain-positional convention,
    # `#[arg(short, long)] pub fullscreen: bool`). No configure_game_dir
    # entry for this one -- confirmed via source (src/ui/gui.rs) that
    # gopher64 only keeps a capped 5-item "recently played" list, no
    # watched-folder/library concept at all to register anything into.
    return ["-f", shlex.quote(romfile)]


# The single commented-out line Cemu's own KeyCache.cpp writes into a
# freshly-created keys.txt on first run ("541b9889519b27d363cd21604b97c67a
# # example key (can be deleted)") -- confirmed via source. keys.txt
# existing at all doesn't mean a usable key was ever added, since Cemu
# creates this file itself before the user has done anything.
_CEMU_EXAMPLE_KEY = "541b9889519b27d363cd21604b97c67a"


def _cemu_keys_path(entry):
    # keys.txt lives under Cemu's *data* path, not its config path --
    # confirmed via source (ActiveSettings::GetUserDataPath("keys.txt")
    # in KeyCache.cpp; CemuApp.cpp sets user_data_path from
    # XDG_DATA_HOME, separately from config_path's XDG_CONFIG_HOME).
    # Under Flatpak that's ~/.var/app/<id>/data/Cemu/keys.txt, a
    # genuinely different subtree than the ~/.var/app/<id>/config one
    # Ryubing's keys and Cemu's own settings.xml both live under.
    return _flatpak_data_dir(entry["app_id"], "Cemu", "keys.txt")


def _flycast_args(romfile):
    # A bare positional disc image path plus -config window:fullscreen=yes,
    # both confirmed real via Flycast's own source and man page (not
    # guessed): core/cfg/cl.cpp's parseCommandLine takes any non-flag
    # argument as the content path directly (no -g/--game flag exists),
    # and shell/linux/man/flycast.1 documents "-config
    # window:fullscreen=yes" as the real, working way to start in
    # fullscreen ("-config section:option=value" is a transient config
    # override, not persisted to emu.cfg).
    return ["-config", "window:fullscreen=yes", shlex.quote(romfile)]


def _flycast_configure_game_dir(entry, game_dir):
    # emu.cfg, [config] section, key "Dreamcast.ContentPath" -- a
    # semicolon-separated list of directories Flycast's own game browser
    # scans (core/ui/game_scanner.cpp reads config::ContentPath.get()).
    # Confirmed via source, including the exact on-disk section/key
    # split: Option's constructor defaults its own `section` argument to
    # "config" unless explicitly overridden (see core/cfg/option.cpp's
    # ContentPath("Dreamcast.ContentPath") call, only one arg passed),
    # so the dotted name is a literal key string under [config], not a
    # nested [Dreamcast] section the way it might look at a glance.
    #
    # Only touches an existing emu.cfg -- same "don't bootstrap a config
    # file the app hasn't initialized yet" rule as Dolphin's own
    # configurator (no evidence, unlike Cemu, that pre-creating one here
    # would suppress anything -- not verified either way, so this stays
    # on the conservative default).
    config_path = _flatpak_config_dir(entry["app_id"], "flycast", "emu.cfg")
    if not os.path.isfile(config_path):
        return
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(config_path)
    if not cp.has_section("config"):
        cp.add_section("config")
    existing = cp.get("config", "Dreamcast.ContentPath", fallback="")
    paths = [p for p in existing.split(";") if p]
    if game_dir in paths:
        return
    paths.append(game_dir)
    cp.set("config", "Dreamcast.ContentPath", ";".join(paths))
    with open(config_path, "w") as f:
        cp.write(f, space_around_delimiters=True)


def _cemu_keys_marker_path(entry):
    return os.path.join(os.path.dirname(_cemu_keys_path(entry)), ".selfsteam-keys-source")


def _cemu_keys_installed(entry):
    keys_path = _cemu_keys_path(entry)
    if not os.path.isfile(keys_path):
        return None
    with open(keys_path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and line.lower() != _CEMU_EXAMPLE_KEY.lower():
                return _read_source_filename_marker(_cemu_keys_marker_path(entry)) or "keys.txt"
    return None


def _cemu_install_keys(entry, keys_path):
    """Copies a picked keys.txt into Cemu's own real data directory --
    same file KeyCache.cpp itself reads from (one AES-128 key per line,
    hex, '#'/';' start a comment) and creates with just the example key
    commented out if missing entirely. Overwrites whatever's there,
    same "last pick wins" behavior as every other keys/firmware picker
    in this app."""
    dest = _cemu_keys_path(entry)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(keys_path, dest)
    _write_source_filename_marker(_cemu_keys_marker_path(entry), keys_path)
    return [dest]


# Keyed by the emulator's own name (not "<consoles> (<emulator>)") --
# the dropdown shows "<name> - <consoles>" built from these two fields
# directly (native <option> elements can't mix two text colors/weights
# inside one option, so this is plain text, not a styled label+hint
# pairing the way e.g. the browser picker's own label works).
#
# Each entry: install_type ("flathub" for now), app_id (real Flathub
# id), consoles (display string for the hint line), needs_bios/
# needs_keys/needs_firmware (whether the picker should show those extra
# fields at all -- Dolphin needs none of them, Ryubing needs keys and
# firmware but not a traditional "BIOS"), args(romfile) -> argv (already
# shell-quoted where needed, same "ready to append after flatpak run
# <app_id>" contract as retroarch_cores.launch_args), and
# configure_game_dir(entry, game_dir) -- optional (None if an emulator
# doesn't have one yet), called once right after a fresh install() (see
# configure_game_dir's own docstring below) to register SelfSteam's own
# upload folder as a watched game directory in the emulator's own
# settings, so uploaded games show up in its own game list too, not
# just as Steam shortcuts.
def _toml_get_in_section(content, section, key):
    """Read key = "value" back from inside [section] of a TOML file's
    raw text. Hand-rolled instead of a real TOML parser -- Python's
    stdlib only ships a *reader* (tomllib, no writer) and this only ever
    needs to touch one flat table of plain string values (xemu's own
    [sys.files]), not anything with arrays/nesting/other value types a
    real parser would be needed to not mangle."""
    section_re = re.compile(r"^\[" + re.escape(section) + r"\]\s*$", re.MULTILINE)
    m = section_re.search(content)
    if not m:
        return None
    body_start = m.end()
    next_section = re.search(r"^\[", content[body_start:], re.MULTILINE)
    body_end = body_start + next_section.start() if next_section else len(content)
    body = content[body_start:body_end]
    key_re = re.compile(r'^' + re.escape(key) + r'\s*=\s*"(.*)"\s*$', re.MULTILINE)
    km = key_re.search(body)
    return km.group(1) if km else None


def _toml_set_in_section(content, section, key, value):
    """Write key = "value" into [section] of a TOML file's raw text,
    replacing an existing key = ... line in place if there is one,
    appending one to the section if not, or appending the whole section
    if it doesn't exist yet -- same "targeted line patch, not a full
    rewrite" approach as Dolphin/Cemu's own ini/xml editors, just for
    TOML's simpler [section]/key = "value" shape. See
    _toml_get_in_section's own docstring for why this isn't a real TOML
    library."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    line = f'{key} = "{escaped}"'
    section_re = re.compile(r"^\[" + re.escape(section) + r"\]\s*$", re.MULTILINE)
    m = section_re.search(content)
    if not m:
        sep = "\n" if content and not content.endswith("\n") else ""
        return content + f"{sep}\n[{section}]\n{line}\n"
    body_start = m.end()
    next_section = re.search(r"^\[", content[body_start:], re.MULTILINE)
    body_end = body_start + next_section.start() if next_section else len(content)
    body = content[body_start:body_end]
    key_re = re.compile(r"^" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
    if key_re.search(body):
        new_body = key_re.sub(line, body, count=1)
    else:
        new_body = body.rstrip("\n") + f"\n{line}\n"
    return content[:body_start] + new_body + content[body_end:]


# slot prefix (matches the em_<prefix>file/path/source state keys) ->
# (label, xemu's own real TOML key under [sys.files], required) --
# confirmed via xemu's own required-files docs (xemu.app/docs/required-
# files) and config_spec.yml: MCPX bootrom, flash BIOS, and a hard disk
# image are all genuinely required (no HLE fallback the way GameCube/
# DS/N64 have -- no "enable HLE"-style toggle exists anywhere in xemu's
# settings, and unlike EEPROM below, nothing auto-generates a usable
# HDD image). EEPROM is the one exception -- xemu's own docs confirm it
# auto-generates a default if none is configured -- so it's last in
# this list and marked optional (4th tuple element, defaults True when
# omitted -- see render_page's own em_prereqs_ready), not gated on the
# way the first three are. Kept as its own slot regardless of being
# optional, rather than dropped entirely, since a user migrating an
# existing profile still needs a way to point at their real EEPROM
# (serial/MAC/HDD key/region) instead of always getting xemu's freshly
# generated one.
XEMU_BIOS_SLOTS = [
    ("bios", "Select MCPX Boot ROM", "bootrom_path"),
    ("bios2", "Select Xbox BIOS", "flashrom_path"),
    ("bios3", "Select Hard Disk Image", "hdd_path"),
    ("bios4", "Select EEPROM (optional)", "eeprom_path", False),
]


def _xemu_toml_path(entry):
    # SDL_GetPrefPath("xemu", "xemu") -- confirmed via xemu's own source
    # (ui/xemu-settings.cc) -- resolves to $XDG_DATA_HOME/xemu/xemu/ on
    # Linux (SDL's own GetPrefPath reads XDG_DATA_HOME, not
    # XDG_CONFIG_HOME), so this is a bare os.path.expanduser join, not
    # _flatpak_config_dir like most other emulators here.
    return os.path.join(os.path.expanduser(f"~/.var/app/{entry['app_id']}/data"), "xemu", "xemu", "xemu.toml")


def xemu_bios_slot_installed(entry, slot_prefix):
    """Real filename already configured for this slot, or None. Doesn't
    copy files anywhere the way Ryubing/Cemu's keys installers do --
    xemu's own Flathub manifest already grants --filesystem=host:ro, so
    xemu.toml just points straight at wherever the user's real file
    already lives on disk, same as Dolphin's own ISOPaths referencing
    real host paths rather than copies."""
    toml_key = next(k for p, _label, k, *_r in XEMU_BIOS_SLOTS if p == slot_prefix)
    toml_path = _xemu_toml_path(entry)
    if not os.path.isfile(toml_path):
        return None
    with open(toml_path) as f:
        content = f.read()
    value = _toml_get_in_section(content, "sys.files", toml_key)
    return os.path.basename(value) if value and os.path.isfile(value) else None


def install_xemu_bios_slot(entry, slot_prefix, file_path):
    """Points xemu.toml's real [sys.files] key at file_path -- bootstraps
    a fresh xemu.toml if one doesn't exist yet (unlike Dolphin/Ryubing's
    own configurators, which only touch an existing file), since xemu is
    installed here via a bare `flatpak install`, never an actual first
    launch, so nothing would ever create one otherwise -- every real
    BIOS write would silently no-op on a fresh install without this."""
    toml_key = next(k for p, _label, k, *_r in XEMU_BIOS_SLOTS if p == slot_prefix)
    toml_path = _xemu_toml_path(entry)
    os.makedirs(os.path.dirname(toml_path), exist_ok=True)
    content = ""
    if os.path.isfile(toml_path):
        with open(toml_path) as f:
            content = f.read()
    content = _toml_set_in_section(content, "sys.files", toml_key, file_path)
    with open(toml_path, "w") as f:
        f.write(content)


def _xemu_configure_vulkan(entry):
    """Sets xemu's own [display] renderer to Vulkan (Machine > Settings
    > Display > Backend in xemu's own UI, confirmed via its docs) --
    same xemu.toml this file's BIOS-slot writes already touch, just a
    different section/key ([display] renderer = "VULKAN" rather than
    [sys.files]). OpenGL is xemu's own default; Vulkan is applied
    unconditionally here since it's a real emulator-wide preference
    (generally the better-performing/more broadly compatible backend on
    modern GPUs), not something that varies per shortcut/game the way
    BIOS files do."""
    toml_path = _xemu_toml_path(entry)
    os.makedirs(os.path.dirname(toml_path), exist_ok=True)
    content = ""
    if os.path.isfile(toml_path):
        with open(toml_path) as f:
            content = f.read()
    content = _toml_set_in_section(content, "display", "renderer", "VULKAN")
    with open(toml_path, "w") as f:
        f.write(content)


def _pcsx2_bios_dir(entry):
    # [Folders] Bios = "bios" (relative to DataRoot), confirmed via
    # source (Pcsx2Config.cpp's EmuFolders::SetDataDirectory) --
    # DataRoot itself is $XDG_CONFIG_HOME/PCSX2 on Linux, so under
    # Flatpak that's ~/.var/app/<id>/config/PCSX2/bios. Unlike xemu,
    # PCSX2 scans this folder for BIOS files rather than accepting an
    # arbitrary path, so the picked file has to actually be copied here,
    # not just referenced.
    return _flatpak_config_dir(entry["app_id"], "PCSX2", "bios")


def _pcsx2_ini_path(entry):
    # DataRoot/inis/PCSX2.ini -- confirmed via source
    # (EmuFolders::Settings = Path::Combine(DataRoot, "inis")).
    return _flatpak_config_dir(entry["app_id"], "PCSX2", "inis", "PCSX2.ini")


def pcsx2_bios_slot_installed(entry, slot_prefix):
    # [Filenames] BIOS = <basename>, confirmed via source
    # (Pcsx2Config.cpp: SettingsWrapSection("Filenames") immediately
    # precedes wrap.Entry(..., "BIOS", Bios, Bios)) -- just the
    # filename, matched against what's actually sitting in
    # _pcsx2_bios_dir, same real-on-disk-check convention as every
    # other *_installed helper here.
    ini_path = _pcsx2_ini_path(entry)
    if not os.path.isfile(ini_path):
        return None
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve exact key casing -- PCSX2's own "BIOS" key is case-sensitive, not configparser's default lowercasing convention
    cp.read(ini_path)
    filename = cp.get("Filenames", "BIOS", fallback="") if cp.has_section("Filenames") else ""
    if filename and os.path.isfile(os.path.join(_pcsx2_bios_dir(entry), filename)):
        return filename
    return None


def install_pcsx2_bios_slot(entry, slot_prefix, file_path):
    """Copies the picked BIOS file into PCSX2's own bios/ folder (unlike
    xemu's install_xemu_bios_slot, which just points at the original
    path -- PCSX2 scans its own folder rather than accepting an
    arbitrary one) and points [Filenames] BIOS at its basename.
    Bootstraps a fresh PCSX2.ini if one doesn't exist yet, same
    reasoning as install_xemu_bios_slot's own docstring -- installed()
    here only ever runs a bare `flatpak install`, never a real first
    launch."""
    bios_dir = _pcsx2_bios_dir(entry)
    os.makedirs(bios_dir, exist_ok=True)
    dest = os.path.join(bios_dir, os.path.basename(file_path))
    shutil.copy2(file_path, dest)

    ini_path = _pcsx2_ini_path(entry)
    os.makedirs(os.path.dirname(ini_path), exist_ok=True)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve exact key casing -- see pcsx2_bios_slot_installed's own comment
    if os.path.isfile(ini_path):
        cp.read(ini_path)
    if not cp.has_section("Filenames"):
        cp.add_section("Filenames")
    cp.set("Filenames", "BIOS", os.path.basename(file_path))
    # PCSX2 checks "UI"/"SettingsVersion" against its own hardcoded
    # SETTINGS_VERSION constant (confirmed via source: VMManager.cpp's
    # CheckSettingsVersion/SetDefaultSettings, SETTINGS_VERSION = 1) --
    # a fresh ini bootstrapped by us that's missing this key fails that
    # check on PCSX2's own first real launch, which makes it silently
    # discard everything (including the BIOS line just written above)
    # and reset to full defaults, setting "UI"/"SetupWizardIncomplete" =
    # true in the process (confirmed via source: QtHost.cpp's
    # InitializeConfig) -- forcing PCSX2's own onboarding wizard on
    # every launch instead of booting straight into -batch -fullscreen
    # isopath. Stamping both keys ourselves up front (only if not
    # already present, so a wizard the user genuinely ran later isn't
    # clobbered back to "incomplete") makes PCSX2 treat this ini as a
    # real, already-configured one from its very first real launch.
    if not cp.has_section("UI"):
        cp.add_section("UI")
    if not cp.has_option("UI", "SettingsVersion"):
        cp.set("UI", "SettingsVersion", "1")
    if not cp.has_option("UI", "SetupWizardIncomplete"):
        cp.set("UI", "SetupWizardIncomplete", "false")
    with open(ini_path, "w") as f:
        cp.write(f, space_around_delimiters=True)


def _duckstation_bios_dir():
    # $XDG_DATA_HOME/duckstation/bios (~/.local/share/duckstation/bios
    # unsandboxed) -- confirmed via DuckStation's own README ("User
    # Directories": "place your BIOS images" there; "if you were using
    # Linux, you would place your BIOS images in
    # ~/.local/share/duckstation/bios"). Not a Flatpak install, so no
    # _flatpak_data_dir redirect -- this is the real, plain host path,
    # same as the AppImage's own binary_path.
    return os.path.expanduser("~/.local/share/duckstation/bios")


def duckstation_bios_slot_installed(entry, slot_prefix):
    # Unlike PCSX2, DuckStation needs no ini pointer at all -- confirmed
    # via its own README, it scans every file already sitting in
    # bios/ itself rather than requiring one to be selected by name.
    # So the copied-in file's own presence (any file, since a fresh
    # install has none yet) is already the real "installed" signal.
    bios_dir = _duckstation_bios_dir()
    if not os.path.isdir(bios_dir):
        return None
    names = sorted(n for n in os.listdir(bios_dir) if os.path.isfile(os.path.join(bios_dir, n)))
    return names[0] if names else None


def install_duckstation_bios_slot(entry, slot_prefix, file_path):
    """Copies the picked BIOS file into DuckStation's own bios/ folder,
    keeping its original filename -- same "must scan its own folder"
    reasoning as install_pcsx2_bios_slot, but with no ini/version-
    bootstrap step needed since DuckStation has nothing else that
    gates a fresh config on a first real launch."""
    bios_dir = _duckstation_bios_dir()
    os.makedirs(bios_dir, exist_ok=True)
    shutil.copy2(file_path, os.path.join(bios_dir, os.path.basename(file_path)))


def _duckstation_args(romfile):
    # "-fullscreen -- <path>" -- confirmed real via DuckStation's own
    # source (qthost.cpp's PrintCommandLineHelp: "-fullscreen: Enters
    # fullscreen mode immediately after starting."; "--: Signals that
    # no more arguments will follow and the remaining parameters make
    # up the filename. Use when the filename contains spaces or starts
    # with a dash.") -- the "--" guards against a romfile SelfSteam
    # itself may have moved into a path DuckStation's own parser could
    # otherwise misread.
    return ["-fullscreen", "--", shlex.quote(romfile)]


def _duckstation_settings_path():
    # $XDG_DATA_HOME/duckstation/settings.ini -- same DataRoot as
    # _duckstation_bios_dir's own bios/ subfolder (confirmed via
    # DuckStation's own source: Core::GetBaseSettingsPath() is
    # Path::Combine(EmuFolders::DataRoot, "settings.ini")).
    return os.path.join(_xdg_data_dir("duckstation"), "settings.ini")


def _duckstation_bootstrap_config(entry):
    """Pre-stamps DuckStation's own settings.ini so its first-run Setup
    Wizard (language, BIOS search directory, optional ROM search
    directories, resolution scale, preferred UI) never shows at all --
    confirmed via its own source: qthost.cpp's InitializeConfig sets
    run_setup_wizard whenever [Main]/SetupWizardIncomplete is true (the
    default it stamps on a genuinely fresh settings.ini) or absent
    alongside a missing [Main]/SettingsVersion, and the wizard itself,
    on completion, only ever clears that one flag -- nothing else it
    collects (BIOS dir, ROM dirs, scale, UI mode) is required to boot a
    game, only offered. Unlike PCSX2's own ini bootstrap, no
    SettingsVersion stamp is needed alongside it -- confirmed via
    source, DuckStation has no settings-version-mismatch reset
    mechanism to trip. Additive only: an existing settings.ini (already
    configured, wizard already dismissed for real) is left untouched
    beyond adding this one key if it's somehow missing."""
    settings_path = _duckstation_settings_path()
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if os.path.isfile(settings_path):
        cp.read(settings_path)
    if not cp.has_section("Main"):
        cp.add_section("Main")
    if not cp.has_option("Main", "SetupWizardIncomplete"):
        cp.set("Main", "SetupWizardIncomplete", "false")
    with open(settings_path, "w") as f:
        cp.write(f, space_around_delimiters=True)


def _bigpemu_args(romfile):
    # Bare positional romfile -- confirmed real via BigPEmu's own user
    # manual ("BigPEmu always expects the first command line argument
    # to be a software (e.g. cartridge) image path"). No documented
    # fullscreen flag exists anywhere in that manual (only -forcewidth/
    # -forceheight resolution overrides) -- fullscreen has to be toggled
    # once inside the app, after which BigPEmu persists it in its own
    # config for later launches, same as any other setting.
    return [shlex.quote(romfile)]


def _openmsx_args(romfile):
    # -cart <path> to insert the ROM as a cartridge, plus
    # -command "set fullscreen on" -- both confirmed real via openMSX's
    # own manual/source (Ubuntu man page's -cart entry; the -command
    # startup-Tcl mechanism and "set fullscreen on" documented on
    # msx.org's own forum, openMSX's maintainer-run support channel).
    # No -machine override -- the unspecified default is C-BIOS_MSX2+,
    # openMSX's own bundled open-source BIOS replacement, confirmed via
    # its own Contrib/README.cbios ("we can ship them with openMSX"),
    # so no external system ROM is required for cartridge-based games.
    return ["-cart", shlex.quote(romfile), "-command", shlex.quote("set fullscreen on")]


def _shadps4_args(romfile):
    # NOT the raw shadps4 binary's own CLI (src/main.cpp's "guest_arg"
    # positional + "-f,--fullscreen") -- that's what a from-source build
    # accepts, but the real Flathub app_id (net.shadps4.shadPS4) wraps
    # it in a separate launcher/version-manager binary with its own,
    # different CLI (confirmed live on real hardware: the raw form
    # above fails outright with "Unknown argument: --fullscreen, see
    # --help for info", which is exactly why a real shadPS4 shortcut
    # never actually launched a game before this fix -- the wrapper
    # silently ate the launch attempt). Its own --help: "-d" is short
    # for "-e default" (use the config's selected emulator version),
    # "-g,--game <ID|path>" is the game, and "-- ..." forwards
    # everything after it verbatim to the real core binary ("Needs to
    # be at the end of the line") -- confirmed live, the exact
    # "-d -g <path> -- --fullscreen true" invocation below actually
    # boots a real game (verified against a genuine Bloodborne PKG on
    # the Steam Machine, past its FromSoftware/PS logo splash and into
    # real asset loading).
    return ["-d", "-g", shlex.quote(romfile), "--", "--fullscreen", "true"]


# PS4 .pkg extraction for shadPS4 (real-repo release, downloaded and
# cached the same on-demand way as every AppImage emulator's own
# install_binary): shadPS4's CLI has no PKG-install path at all
# (confirmed via its own source -- src/main.cpp's gamePath resolution
# only ever accepts an existing eboot-like file/path, or a Game ID
# looked up against directories registered with --add-game-folder,
# never a raw .pkg -- the only place shadPS4 itself understands PKG
# files is its Qt GUI's own installer, which SelfSteam's headless
# Create flow never runs). paulomanrique/ps4-pkg-extractor's own
# "pkgextract" CLI is a thin wrapper around the orbis-pkg-util crate,
# whose own README documents real PFS/AES decryption (not just outer-
# metadata extraction) -- benchmarked there against a genuine ~30GB
# Bloodborne PKG (CUSA03173_01), and built with shadPS4's own (since-
# removed) PKG-install code as its initial reference. Public domain
# (Unlicense).
_PKGEXTRACT_RELEASE_API = "https://api.github.com/repos/paulomanrique/ps4-pkg-extractor/releases?per_page=1"
_PKGEXTRACT_ASSET_RE = re.compile(r"^pkgextract-x86_64-unknown-linux-gnu\.zip$")


def _pkgextract_dir():
    return _xdg_data_dir("selfsteam", "tools", "pkgextract")


def _pkgextract_bin_path():
    return os.path.join(_pkgextract_dir(), "pkgextract")


def _ensure_pkgextract():
    """Downloads and unpacks the pkgextract CLI the first time a .pkg
    needs extracting, caching it at _pkgextract_bin_path() afterward --
    same "cheap once already done" convention as every other install_*
    helper here. Its own GitHub Releases ship one .zip per platform
    (not a bare executable, unlike every AppImage emulator's own single-
    file release asset), so this can't just reuse install_binary."""
    bin_path = _pkgextract_bin_path()
    if os.path.isfile(bin_path) and os.access(bin_path, os.X_OK):
        return bin_path

    req = urllib.request.Request(_PKGEXTRACT_RELEASE_API, headers={"User-Agent": "SelfSteam"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.load(resp)
    if not releases:
        raise RuntimeError("pkgextract: release API returned no releases")
    release = releases[0]
    match = next((a for a in release["assets"] if _PKGEXTRACT_ASSET_RE.match(a["name"])), None)
    if not match:
        raise RuntimeError(f"pkgextract {release.get('tag_name', '?')}: no asset matching {_PKGEXTRACT_ASSET_RE.pattern!r}")

    os.makedirs(_pkgextract_dir(), exist_ok=True)
    dl_req = urllib.request.Request(match["browser_download_url"], headers={"User-Agent": "SelfSteam"})
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "pkgextract.zip")
        with urllib.request.urlopen(dl_req, timeout=120) as resp, open(zip_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp_dir)
        extracted = next(
            (os.path.join(root, fname) for root, _dirs, files in os.walk(tmp_dir) for fname in files if fname == "pkgextract"),
            None,
        )
        if not extracted:
            raise RuntimeError("pkgextract: downloaded zip didn't contain a 'pkgextract' binary")
        shutil.copy2(extracted, bin_path)
    os.chmod(bin_path, 0o755)
    return bin_path


def _shadps4_pkg_dir():
    # Own persistent extraction dir, unrelated to shadPS4's own --add-
    # game-folder mechanism -- SelfSteam always launches by passing the
    # extracted eboot.bin's own direct path, so shadPS4 never needs to
    # be told this directory exists at all.
    return _xdg_data_dir("selfsteam", "shadps4-pkgs")


def extract_shadps4_pkg(pkg_path):
    """Extracts a PS4 .pkg into its own folder under _shadps4_pkg_dir()
    via pkgextract, returning the extracted eboot.bin's path -- a real
    install directory always has eboot.bin sitting directly at its own
    root next to sce_sys/param.sfo, confirmed via shadPS4's own source
    (common/path_util.cpp's FindGameByID). Skips re-extracting a pkg
    that's already been extracted, same "cheap once already done"
    convention as install_keys/install_firmware_zip elsewhere in this
    file."""
    pkgextract = _ensure_pkgextract()
    out_dir = os.path.join(_shadps4_pkg_dir(), os.path.splitext(os.path.basename(pkg_path))[0])
    eboot_path = os.path.join(out_dir, "eboot.bin")
    if os.path.isfile(eboot_path):
        return eboot_path
    os.makedirs(_shadps4_pkg_dir(), exist_ok=True)
    result = subprocess.run(
        [pkgextract, pkg_path, "-o", out_dir, "-f"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.isfile(eboot_path):
        raise RuntimeError(
            f"PS4 .pkg extraction failed: {result.stderr.strip() or result.stdout.strip() or 'no eboot.bin in extracted output'}"
        )
    return eboot_path


def shadps4_pkg_extraction_needed(romfile):
    """True if Create would actually run extract_shadps4_pkg's own
    extraction step for this romfile -- lets the UI show "Extracting
    PKG" ahead of time (a real, sometimes-multi-minute blocking wait
    for a large game) without duplicating extract_shadps4_pkg's own
    eboot.bin-already-there short-circuit logic here."""
    if not romfile.lower().endswith(".pkg"):
        return False
    out_dir = os.path.join(_shadps4_pkg_dir(), os.path.splitext(os.path.basename(romfile))[0])
    return not os.path.isfile(os.path.join(out_dir, "eboot.bin"))


def _rpcs3_args(romfile):
    # --no-gui --fullscreen plus a bare positional romfile -- confirmed
    # real via RPCS3's own source (rpcs3/rpcs3.cpp: arg_no_gui/
    # arg_fullscreen QCommandLineOptions, "fullscreen ... Only used when
    # no-gui is set"; positionalArguments()[0] becomes the boot path).
    return ["--no-gui", "--fullscreen", shlex.quote(romfile)]


def _rpcs3_dev_flash_dir(entry):
    # $(EmulatorDir)dev_flash/ where EmulatorDir defaults to
    # fs::get_config_dir() -- confirmed via source (Emu/vfs_config.h,
    # Utilities/File.cpp's Linux branch: $XDG_CONFIG_HOME/rpcs3/, i.e.
    # ~/.var/app/<id>/config/rpcs3/ under Flatpak).
    return _flatpak_config_dir(entry["app_id"], "rpcs3", "dev_flash")


def _rpcs3_firmware_marker_path(entry):
    # Sibling of dev_flash itself, not inside it -- dev_flash is RPCS3's
    # own real, entirely-managed content, not somewhere this app should
    # be leaving extra files it might trip over.
    return os.path.join(os.path.dirname(_rpcs3_dev_flash_dir(entry)), ".selfsteam-firmware-source")


def rpcs3_firmware_installed(entry, slot_prefix):
    # vsh/etc/version.txt only exists once a real firmware install has
    # actually completed -- confirmed via source (util/sysinfo.cpp's
    # get_firmware_version(), the same file RPCS3 itself reads to know
    # its own installed firmware version).
    version_path = os.path.join(_rpcs3_dev_flash_dir(entry), "vsh", "etc", "version.txt")
    if not os.path.isfile(version_path):
        return None
    return _read_source_filename_marker(_rpcs3_firmware_marker_path(entry)) or "firmware installed"


# RPCS3's own fixed window titles for this whole flow, in the order
# they actually appear -- confirmed live (2026-08-25), not documented
# anywhere in its own source/docs, so these could in principle drift if
# a future RPCS3 release renames any of them (the foregrounding below
# would then just silently no-op for that step, same as any other
# "window not found" case -- not fatal, just back to invisible). Three
# separate windows, not one: "Welcome to RPCS3" is its first-run wizard
# (Quickstart guide/Show at startup checkboxes, Continue); once that's
# dismissed, "RPCS3 Firmware Installer" actually runs the install; once
# that finishes, a third, much smaller "Success!" window confirms it.
# Each one was a real, separate gap from the previous ones' own
# foregrounding fix -- a single one-time check at launch never looked
# for anything past whatever it first found, leaving every later window
# in the sequence just as invisible as the very first bug this was
# written to fix.
_RPCS3_INSTALLFW_WINDOW_TITLES = ["Welcome to RPCS3", "RPCS3 Firmware Installer", "Success!"]
_RPCS3_SUCCESS_WINDOW_TITLE = "Success!"

# Fully automates both real dialogs in the flow (nothing needs sending
# to "Success!" -- its mere appearance is the completion signal
# is_done already watches for) -- confirmed live (2026-08-25) on a
# real Steam Machine, both individually and back-to-back start to
# finish:
#   "Welcome to RPCS3": real gamepad button presses did nothing at all
#     on this window (gamescope apparently doesn't route them to
#     whatever it considers "active" the way it does for keyboard
#     input here), but this exact Tab/Space sequence reliably dismisses
#     it -- its own checkboxes already default to the wanted state
#     ("I have read the Quickstart guide" checked, "Show at startup"
#     unchecked), so nothing needs toggling, just landing on and
#     activating "Continue". Window closes partway through once that
#     fires -- see send_keys' own docstring on why the remaining
#     harmless X errors from that are expected, not a bug.
#   "RPCS3 Firmware Installer": alt+y is Qt's own standard accelerator
#     for a message box's "Yes" button -- confirmed instant.
_RPCS3_INSTALLFW_AUTO_KEYS = {
    "Welcome to RPCS3": [
        "Tab", "Tab", "Tab", "Tab", "space", "Tab", "space", "Tab", "Tab", "Tab", "Tab", "space",
    ],
    "RPCS3 Firmware Installer": ["alt+y"],
}


def install_rpcs3_firmware(entry, slot_prefix, file_path):
    """Firmware install isn't a plain file copy the way keys/BIOS are
    elsewhere in this module -- a PS3 PUP is a signed, encrypted
    package RPCS3 has to decrypt and unpack itself, no shortcut around
    that. RPCS3's own --installfw <path> flag does exactly this
    (confirmed via source: rpcs3.cpp calls main_window::InstallPup for
    it) -- run for real here rather than reimplementing PUP decryption
    ourselves. Real caveat, not swept under the rug: --installfw can't
    run in --no-gui mode (confirmed via source: report_fatal_error
    otherwise), so this genuinely pops up RPCS3's own install dialogs
    rather than staying silent.

    Completion is detected by the "Success!" window actually appearing
    on screen, NOT by polling for the real firmware version file this
    same install writes (rpcs3_firmware_installed) -- confirmed live
    (2026-08-25, real Steam Machine) that this file gets written
    *before* all of the firmware's actual content finishes extracting,
    so an earlier version of this function that treated the file's
    mere existence as "done" and killed RPCS3 right then left a real,
    genuinely invalid, partially-installed firmware behind (RPCS3's own
    log: "PS3 firmware is not installed or the installed firmware is
    invalid" on the very next game launch, despite the version file
    being right there). "Success!" is RPCS3's own explicit, final
    confirmation that the whole install actually finished -- killing it
    the moment that appears (not waiting for it to be dismissed) is
    safe precisely because RPCS3 itself doesn't show it until there's
    nothing left to do.

    Also not just waiting for the process to exit on its own -- RPCS3
    keeps its full main window open indefinitely once a firmware
    install finishes, rather than quitting, so that approach hung
    forever with nothing left for anyone to click even once the
    install had genuinely (and, before this fix, sometimes only
    partially) succeeded. RPCS3 is killed outright (flatpak kill, not
    a plain terminate() -- see gamescope_splash.
    launch_foregrounded_until's own docstring on why) once "Success!"
    is seen, since there's nothing left for it to do and leaving it
    running only blocks whatever queued behind this call (the actual
    shortcut creation).

    On a gamescope session, each dialog in the install flow
    ("Welcome to RPCS3", then "RPCS3 Firmware Installer", then
    "Success!") is foregrounded the same way auth_display.py's own
    pairing screen is (gamescope never auto-focuses a new X11 client
    the way a normal WM would) -- confirmed live as a real, separate
    bug from the auth screen's own crash: each one opened and
    genuinely sat there waiting for a click, just invisible behind
    Steam's own UI, which from the web UI's own perspective looked
    identical to the install being stuck forever (see
    _RPCS3_INSTALLFW_WINDOW_TITLES' own comment). The first two are
    then fully automated too (see _RPCS3_INSTALLFW_AUTO_KEYS' own
    comment) -- confirmed live nobody needs to touch a controller,
    keyboard, or mouse at all for a fresh RPCS3 firmware install to go
    from launch to a real, valid, fully-extracted firmware on disk.
    Plain desktop sessions skip both the foregrounding (a real WM
    already handles focus) and the auto-keys (a human can just click
    normally there) but still poll for the same completion condition."""
    flatpak = host_exec.which("flatpak")
    argv = host_exec.wrap([flatpak, "run", entry["app_id"], "--installfw", file_path])
    is_done = lambda: gamescope_splash.window_exists(_RPCS3_SUCCESS_WINDOW_TITLE)
    if steamos_session.is_gamescope_session():
        proc = gamescope_splash.launch_foregrounded_until(
            argv, _RPCS3_INSTALLFW_WINDOW_TITLES, is_done, auto_keys=_RPCS3_INSTALLFW_AUTO_KEYS,
        )
    else:
        proc = subprocess.Popen(argv)
        while not is_done() and proc.poll() is None:
            time.sleep(1)
    # Captured before the kill below -- "Success!" is RPCS3's own
    # window, so it vanishes the instant RPCS3 itself is killed, and
    # checking is_done() again afterward would always read as false.
    succeeded = is_done()
    if proc.poll() is None:
        subprocess.run(host_exec.wrap(["flatpak", "kill", entry["app_id"]]))
    if succeeded:
        _write_source_filename_marker(_rpcs3_firmware_marker_path(entry), file_path)


def _xenia_canary_args(romfile):
    # --fullscreen=true plus a bare positional romfile -- confirmed real
    # via xenia-canary's own source (src/xenia/app/emulator_window.cc:
    # DEFINE_bool(fullscreen, false, "Whether to launch the emulator in
    # fullscreen.", "Display") is a real cvar, overridable on the
    # command line the same way its own wiki documents
    # "xenia.exe path/to/game/default.xex --vsync=false" -- and that
    # same wiki page confirms the game path itself comes before any
    # flags as a bare positional argument).
    return [shlex.quote(romfile), "--fullscreen=true"]


def _vita3k_args(romfile):
    # --fullscreen,-F plus a positional content-path, both confirmed
    # real via Vita3K's own source (vita3k/config/src/config.cpp's
    # CLI11 setup: "content-path" is a positional option taking ".vpk/
    # .zip extension or folder of content to install & run",
    # "--fullscreen,-F" is a flag).
    return ["--fullscreen", shlex.quote(romfile)]


def _vita3k_fs_dir():
    # $XDG_DATA_HOME/Vita3K/Vita3K -- confirmed via Vita3K's own docs
    # (follows the XDG Base Directory spec; vita_fs defaults to
    # ~/.local/share/Vita3K/Vita3K on Linux).
    return _xdg_data_dir("Vita3K", "Vita3K")


def _vita3k_firmware_marker_path(slot_prefix):
    suffix = "-bios2" if slot_prefix == "bios2" else ""
    return os.path.join(_vita3k_fs_dir(), f".selfsteam-firmware-source{suffix}")


def vita3k_firmware_installed(entry, slot_prefix):
    # vs0/ is the Vita OS system partition -- only populated once a real
    # firmware .pup has actually been installed, same "real content
    # exists" check RPCS3's own firmware-installed check uses (though
    # unlike RPCS3, no single sentinel file with a version string inside
    # it is confirmed here, so this only reports a count, not a real
    # version number, as a fallback for firmware installed before the
    # marker below existed). sa0/ is the separate font-package partition
    # (see the Vita3K EMULATORS entry's own comment on bios2) -- checked
    # the same way, independently of vs0/, matching Vita3K's own source
    # (app.cpp's get_firmware_state checks each partition separately).
    subdir = "sa0" if slot_prefix == "bios2" else "vs0"
    label = "font package" if slot_prefix == "bios2" else "firmware"
    part_dir = os.path.join(_vita3k_fs_dir(), subdir)
    if not os.path.isdir(part_dir):
        return None
    count = len(os.listdir(part_dir))
    if not count:
        return None
    return _read_source_filename_marker(_vita3k_firmware_marker_path(slot_prefix)) or f"{label} ({count} items)"


def install_vita3k_firmware(entry, slot_prefix, file_path):
    """Unlike RPCS3, Vita3K has a real CLI flag for this --
    "--firmware <path>" (confirmed via source: config.cpp's own
    --firmware option feeds command_line.pup_path, which main.cpp
    checks for and calls install_pup() with, logging progress via a
    callback -- not gated behind opening a GUI dialog first the way
    RPCS3's --installfw is). Still a real, separate binary launch that
    blocks until it's done, same shape as RPCS3's own install call.
    Shared verbatim by both bios_slots ("bios" and "bios2") -- Vita3K's
    own install_pup() extracts whichever of vs0/sa0 the given PUP file
    actually contains (see pup.cpp), so a main-firmware PUP and a
    font-pack PUP both just go through --firmware, only the file
    differs."""
    path = _binary_path("Vita3K", EMULATORS["Vita3K"])
    subprocess.run(host_exec.wrap([path, "--firmware", file_path]))
    _write_source_filename_marker(_vita3k_firmware_marker_path(slot_prefix), file_path)


def install_vita3k_pkg(pkg_path, zrif):
    """Installs a .pkg via Vita3K's own --pkg/--zrif CLI flags (confirmed
    real via source: config.cpp's command-line handler calls
    install_pkg(pkg_path, pref_path, zrif) directly and returns
    QuitRequested -- genuinely headless like the firmware install
    above, unlike RPCS3's own --installfw which refuses to run at all
    without a GUI). --pkg only installs; it never launches, and the
    later launch needs a different flag entirely (--installed-path,
    the installed title's own folder name under ux0/app/, confirmed
    via source: CLI11's own IsMember check validates it against
    exactly that directory listing) -- Vita3K's CLI doesn't print or
    return that id anywhere, so this diffs ux0/app/ before and after
    the install to find it. Raises RuntimeError if no new title shows
    up (a bad zrif/pkg pair just fails the install silently from the
    CLI's own point of view, no distinct error exit code to check
    instead).

    Vita3K's own --pkg argument parsing silently truncates the path at
    its first space character -- confirmed live (2026-08-23) even when
    the full path is passed as a single, already-split argv element
    (no shell re-parsing involved), so this isn't a quoting issue on
    our end. Uploaded ROM filenames routinely contain spaces, so this
    hard-links pkg_path to a space-free scratch path first (same
    filesystem as the uploads dir, so a hard link is instant and free,
    unlike a real copy of a multi-GB file) and installs from that
    instead."""
    path = _binary_path("Vita3K", EMULATORS["Vita3K"])
    app_dir = os.path.join(_vita3k_fs_dir(), "ux0", "app")
    before = set(os.listdir(app_dir)) if os.path.isdir(app_dir) else set()
    scratch_path = os.path.join(tempfile.gettempdir(), f"selfsteam-vita3k-pkg-{uuid.uuid4().hex}.pkg")
    try:
        os.link(pkg_path, scratch_path)
    except OSError:
        shutil.copyfile(pkg_path, scratch_path)
    try:
        subprocess.run(host_exec.wrap([path, "--pkg", scratch_path, "--zrif", zrif]))
    finally:
        os.remove(scratch_path)
    after = set(os.listdir(app_dir)) if os.path.isdir(app_dir) else set()
    new_ids = after - before
    if not new_ids:
        raise RuntimeError("Vita3K didn't install anything -- check the pkg file and zRIF key and try again")
    return sorted(new_ids)[0]


def _xdg_data_dir(*parts):
    # Eden runs as a plain AppImage on the host, not inside a Flatpak
    # sandbox -- no ~/.var/app/<id> redirect the way _flatpak_data_dir
    # gives every other emulator here, just real $XDG_DATA_HOME (falling
    # back to ~/.local/share per the XDG basedir spec's own default,
    # same fallback Eden's own GetDataDirectory() uses).
    #
    # NOT os.environ.get("XDG_DATA_HOME") -- confirmed live (2026-08-23)
    # Flatpak automatically redirects that env var to SelfSteam's own
    # private ~/.var/app/<selfsteam-app-id>/data when SelfSteam itself
    # is running sandboxed, which is nowhere the real, unsandboxed Eden
    # AppImage would ever look. $HOME itself isn't redirected the same
    # way (confirmed live: os.path.expanduser("~") still resolves to
    # the real host home even inside the sandbox), so anchoring on that
    # instead reaches Eden's real data dir every time, sandboxed or
    # not -- this was a real bug, not a hardening measure: keys/
    # firmware installs for every AppImage-based emulator here were
    # silently landing in SelfSteam's own isolated sandbox storage
    # instead of where the actual unsandboxed emulator process reads
    # from, the whole time SelfSteam itself has been a packaged
    # Flatpak.
    base = os.path.expanduser("~/.local/share")
    return os.path.join(base, *parts)


def _eden_args(romfile):
    # -f (fullscreen) and -g <path> (launch game at path), both
    # confirmed real via Eden's own source (src/yuzu/main_window.cpp's
    # command-line arg loop -- inherited near-verbatim from yuzu, which
    # this is forked from).
    return ["-f", "-g", shlex.quote(romfile)]


def _xdg_config_dir(*parts):
    # Same real-host-path reasoning as _xdg_data_dir's own comment --
    # not os.environ.get("XDG_CONFIG_HOME"), confirmed live (2026-08-23)
    # to be Flatpak's own sandbox redirect to SelfSteam's private config
    # dir, not the real ~/.config the AppImage-based Ryujinx builds
    # (running unsandboxed) actually read from. This was the real,
    # confirmed cause of an AppImage Ryujinx build still asking for
    # firmware after SelfSteam reported it as installed -- the install
    # itself was silently landing in SelfSteam's own sandbox storage,
    # never reaching ~/.config/Ryujinx at all.
    base = os.path.expanduser("~/.config")
    return os.path.join(base, *parts)


EMULATORS = {
    "Dolphin": {
        "install_type": "flathub",
        "app_id": "org.DolphinEmu.dolphin-emu",
        "consoles": "Nintendo GameCube / Wii",
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _dolphin_args,
        "configure_game_dir": _dolphin_configure_game_dir,
    },
    "Ryubing": {
        "install_type": "flathub",
        "app_id": "io.github.ryubing.Ryujinx",
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": True,
        "args": _ryubing_args,
        "configure_game_dir": _ryubing_configure_game_dir,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        # .nsz omitted from the ROM picker -- not supported by Ryubing
        # (or Eden, its other fork) per real user testing, not guessed.
        "rom_exclude_extensions": {".nsz"},
    },
    "Cemu": {
        "install_type": "flathub",
        "app_id": "info.cemu.Cemu",
        "consoles": "Nintendo Wii U",
        # No traditional BIOS and no separate firmware dump needed --
        # unlike Ryubing/Switch, Cemu boots and runs games without any
        # official Wii U system files at all. keys.txt is needed for
        # most real commercial dumps (WUD/WUX are typically encrypted),
        # but is a single small text file, not a firmware install.
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": False,
        "args": _cemu_args,
        "configure_game_dir": _cemu_configure_game_dir,
        "keys_installed": _cemu_keys_installed,
        "install_keys": _cemu_install_keys,
        "keys_tooltip": "Pick your keys.txt -- most real Wii U game dumps (WUD/WUX) are encrypted and need this to decrypt.",
    },
    "Flycast": {
        "install_type": "flathub",
        "app_id": "org.flycast.Flycast",
        "consoles": "Sega Dreamcast",
        # No BIOS gate -- confirmed via Flycast's own man page: "The
        # Dreamcast BIOS isn't needed in most cases but is recommended."
        # Genuinely optional (unlike Ryubing/Cemu's keys), same category
        # as Dolphin's own built-in GameCube/Wii IPL HLE, so this
        # doesn't block Create the way needs_bios=True would.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _flycast_args,
        "configure_game_dir": _flycast_configure_game_dir,
    },
    "gopher64": {
        "install_type": "flathub",
        "app_id": "io.github.gopher64.gopher64",
        "consoles": "Nintendo 64",
        # No BIOS/PIF ROM needed -- N64 emulators HLE-emulate the boot
        # ROM's function (same reason retroarch_cores.py's own N64
        # console entry, mupen64plus_next, needs_bios=False too), and
        # gopher64's own README makes no mention of one at all.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _gopher64_args,
        # Confirmed live (see grant_permissions' own docstring): unlike
        # every other emulator here, gopher64's own Flathub manifest
        # grants zero filesystem access, so it can't read a ROM from
        # anywhere outside its own sandbox at all until this is granted.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "Rosalie's Mupen GUI": {
        "install_type": "flathub",
        "app_id": "com.github.Rosalie241.RMG",
        "consoles": "Nintendo 64",
        # Same reasoning as gopher64/mupen64plus_next -- N64 emulators
        # HLE-emulate the PIF boot ROM's function, no real BIOS dump
        # needed.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _rmg_args,
        # Confirmed via its own Flathub manifest: same gap as gopher64,
        # zero general filesystem access (only its own
        # xdg-pictures/RMG:rw screenshot folder is granted), so a ROM
        # picked from anywhere else crashes the same way until this is
        # granted.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "M64Py": {
        "install_type": "flathub",
        "app_id": "net.sourceforge.m64py.M64Py",
        "consoles": "Nintendo 64",
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _m64py_args,
        # Confirmed via its own Flathub manifest: same gap as gopher64/
        # RMG, zero filesystem access granted at all (only ipc/device/
        # pulseaudio/x11/wayland sockets), so a ROM picked from anywhere
        # crashes the same way until this is granted.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "melonDS": {
        "install_type": "flathub",
        "app_id": "net.kuribo64.melonDS",
        "consoles": "Nintendo DS",
        # Confirmed via source (Config.cpp): ExternalBIOSEnable defaults
        # to false -- melonDS's own built-in HLE BIOS is what a fresh
        # install actually boots with, same category as Dolphin's own
        # GameCube IPL HLE. Real BIOS/firmware dumps are an optional
        # accuracy upgrade the user can add later via melonDS's own
        # settings, not something Create needs to gate on.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _melonds_args,
        # Its own Flathub manifest only grants --filesystem=home, not
        # host -- upgraded to host:ro so a ROM picked from outside home
        # (the file picker's root was widened to the real filesystem
        # root) doesn't hit the same NotFound crash gopher64/RMG/M64Py
        # did before they got this same fix.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "xemu": {
        "install_type": "flathub",
        "app_id": "app.xemu.xemu",
        "consoles": "Original Xbox",
        # Original Xbox emulation has no HLE fallback -- confirmed via
        # xemu's own required-files docs and config_spec.yml, [sys.files]
        # requires a real MCPX bootrom, Xbox BIOS (flashrom), and a hard
        # disk image, no toggle anywhere to run without them (EEPROM is
        # the one exception -- see XEMU_BIOS_SLOTS' own comment). Four
        # separate files, not one, so needs_bios/bios_slots both point
        # at XEMU_BIOS_SLOTS rather than the single-file bios picker
        # every other emulator here uses.
        "needs_bios": True,
        "bios_slots": XEMU_BIOS_SLOTS,
        "bios_slot_installed": xemu_bios_slot_installed,
        "install_bios_slot": install_xemu_bios_slot,
        # xemu provides its own free, legal, copyright-clean HDD image
        # (an unsigned dashboard with no official Xbox software on it) --
        # same "here's where to legally get this" pattern as PS3/Vita's
        # own bios_slot_links, just linking xemu's own docs instead of
        # Sony's.
        "bios_slot_links": {"bios3": ("https://xemu.app/docs/required-files/", "Download from xemu")},
        "needs_keys": False,
        "needs_firmware": False,
        "args": _xemu_args,
        "configure_renderer": _xemu_configure_vulkan,
        # xemu's own Flathub manifest ships --filesystem=host:ro, which
        # is fine for the ROM/bootrom/flashrom/eeprom files (xemu only
        # ever reads those) but NOT for the hard disk image -- xemu
        # writes real save/partition data to it at runtime. Confirmed
        # live (2026-08-21): a real game failed with xemu's own
        # "could not open [...] read only filesystem" error against the
        # qcow2 HDD image with only host:ro granted; upgrading to a real
        # read-write --filesystem=host (verified live: a direct write-
        # mode open of the same qcow2 file from inside xemu's own
        # sandbox succeeds after this) fixes it.
        "grant_permissions": ["--filesystem=host"],
    },
    "PCSX2": {
        "install_type": "flathub",
        "app_id": "net.pcsx2.PCSX2",
        "consoles": "PlayStation 2",
        # PS2 emulation has no HLE fallback either -- confirmed via
        # source (Pcsx2Config.cpp expects a real BIOS file matched
        # against its own bios/ folder, no toggle to skip it). Single
        # slot (unlike xemu's three), but still routed through
        # bios_slots rather than the bare needs_bios flag so it
        # actually gets installed -- see install_pcsx2_bios_slot's own
        # docstring for why PCSX2 needs a real copy, not just a
        # referenced path the way xemu's does.
        "needs_bios": True,
        "bios_slots": [("bios", "Select BIOS", "BIOS")],
        "bios_slot_installed": pcsx2_bios_slot_installed,
        "install_bios_slot": install_pcsx2_bios_slot,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _pcsx2_args,
        # Confirmed live on X1 (not just from the manifest, which was
        # misread earlier as already granting this): PCSX2's real
        # Flathub manifest grants NO general filesystem access at all
        # (`flatpak info --show-permissions` shows only
        # xdg-config/kdeglobals:ro and xdg-run/gamescope-0:ro, no home
        # or host) -- same "file not found" crash class as gopher64/
        # RMG/M64Py before they got this same fix.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "DuckStation": {
        "install_type": "binary",
        # Real GitHub, single x64 AppImage per release (also -x64-SSE2/
        # -arm64/-armhf builds for other CPUs, not applicable here) --
        # confirmed live via its own releases API. Ships a rolling
        # "latest" tag rather than numbered releases, same "newest-
        # first list still works the same way" case as Vita3K's own
        # "continuous" tag.
        "release_api": "https://api.github.com/repos/stenzek/duckstation/releases?per_page=1",
        "binary_asset_re": re.compile(r"^DuckStation-x64\.AppImage$"),
        "consoles": "PlayStation",
        # PS1 emulation has no HLE fallback -- confirmed via
        # DuckStation's own README ("A PS1 or PS2 'BIOS' ROM image is
        # required to start the emulator and to play games ... A ROM
        # image is not provided with the emulator for legal reasons").
        # Single slot, same "needs a real copy in its own folder"
        # reasoning as PCSX2's own entry.
        "needs_bios": True,
        "bios_slots": [("bios", "Select BIOS", "BIOS")],
        "bios_slot_installed": duckstation_bios_slot_installed,
        "install_bios_slot": install_duckstation_bios_slot,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _duckstation_args,
        # Skips its own first-run Setup Wizard entirely -- see
        # _duckstation_bootstrap_config's own docstring.
        "bootstrap_config": _duckstation_bootstrap_config,
    },
    "Azahar": {
        "install_type": "flathub",
        "app_id": "org.azahar_emu.Azahar",
        "consoles": "Nintendo 3DS",
        # Citra/Azahar can boot many games without real system files
        # via HLE, same "optional accuracy upgrade, not required to
        # boot" category as Dolphin's GameCube IPL -- unlike PS2/Xbox/
        # PS3, which all hard-require real dumps.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _azahar_args,
        # No grant_permissions needed -- confirmed via its own Flathub
        # manifest, it already ships --filesystem=host:ro.
    },
    "RPCS3": {
        "install_type": "flathub",
        "app_id": "net.rpcs3.RPCS3",
        "consoles": "PlayStation 3",
        # PS3 emulation has no HLE fallback -- a real firmware (PUP)
        # install is mandatory. Routed through bios_slots (a single
        # slot) so the label reads "Select PS3 Firmware" rather than
        # the generic "Select BIOS" every needs_bios-only emulator
        # shows -- see install_rpcs3_firmware's own docstring for why
        # this can't be a plain file copy the way PCSX2's is.
        "needs_bios": True,
        "bios_slots": [("bios", "Select PS3 Firmware (PUP)", "firmware")],
        # Sony's own official download for the PUP -- linked directly
        # next to the picker since it's not obtainable any other
        # legitimate way (unlike PS1/PS2 BIOS dumps, which are commonly
        # sourced from a user's own console).
        "bios_slot_links": {"bios": ("https://www.playstation.com/en-gb/support/hardware/ps3/system-software/", "Get it from Sony")},
        "bios_slot_installed": rpcs3_firmware_installed,
        "install_bios_slot": install_rpcs3_firmware,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _rpcs3_args,
        # Upgraded from its own Flathub manifest's --filesystem=home:ro
        # (+/media, /run/media) to host:ro -- same reasoning as
        # melonDS's own fix, the file picker's root was widened to the
        # real filesystem root, not just home/removable-media mounts.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "PPSSPP": {
        "install_type": "flathub",
        "app_id": "org.ppsspp.PPSSPP",
        "consoles": "PlayStation Portable",
        # PPSSPP's HLE covers PSP system calls without a real firmware
        # dump -- same "not required to boot" category as Azahar/Citra.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _ppsspp_args,
        # No grant_permissions needed -- confirmed LIVE via
        # `flatpak info --show-permissions org.ppsspp.PPSSPP` on X1
        # (filesystems=host:ro;xdg-run/gamescope-0:ro), not just read
        # from the manifest text.
    },
    "Play!": {
        "install_type": "flathub",
        "app_id": "org.purei.Play",
        "consoles": "PlayStation 2",
        # Play! ships its own built-in HLE BIOS -- confirmed via its own
        # README: "Play! uses a built-in high-level emulation BIOS.
        # Using an external BIOS file is not necessary or possible."
        # Unlike PCSX2/LRPS2, no real PS2 BIOS dump is needed at all.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _play_args,
        # Confirmed live on X1: its Flathub manifest only grants
        # home:ro (+ /media, /mnt, /run/media, all :ro) -- same gap as
        # melonDS/RPCS3 before their own fixes. Upgraded to host:ro so
        # the widened (real filesystem root) file picker works.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "shadPS4": {
        "install_type": "flathub",
        "app_id": "net.shadps4.shadPS4",
        "consoles": "PlayStation 4",
        # HLE of the PS4 system libraries -- confirmed via its own
        # README: real firmware .sprx modules are only optional (for
        # better compatibility on games that lean on them), never
        # required to boot, unlike RPCS3's mandatory real PUP.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _shadps4_args,
        # Confirmed live on X1: its manifest only grants home (full
        # read-write, not even :ro) + /media, /run/media -- no host:ro.
        # Same gap as melonDS/RPCS3/Play! before their own fixes.
        "grant_permissions": ["--filesystem=host:ro"],
    },
    "BigPEmu": {
        "install_type": "flathub",
        "app_id": "com.richwhitehouse.BigPEmu",
        "consoles": "Atari Jaguar",
        # Standard Jaguar cartridge games boot from their own on-cart
        # code -- no BIOS file is mentioned anywhere in BigPEmu's own
        # user manual (only Jaguar CD titles would need a CD BIOS, not
        # something this catalog entry covers).
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _bigpemu_args,
        # No grant_permissions needed -- confirmed LIVE via
        # `flatpak info --show-permissions com.richwhitehouse.BigPEmu`
        # on X1 (filesystems=home;host:ro), not just read from the
        # manifest text.
    },
    "Eden (amd64 — Intel/AMD desktop)": {
        "install_type": "binary",
        # Gitea REST API (self-hosted, not GitHub -- Eden's own GitHub
        # mirror is DMCA-blocked by Nintendo as of 2026-02-12, confirmed
        # live via `gh api` returning a 451 with that exact takedown
        # notice). Confirmed reachable directly from X1's own Python
        # (urllib + a User-Agent header) at the time this was added, not
        # just from a browser.
        "release_api": "https://git.eden-emu.dev/api/v1/repos/eden-emu/eden/releases?limit=1",
        # amd64 (Intel/AMD desktop) + PGO build (~10-30% faster per
        # Eden's own release notes, "generally recommended for all
        # users") -- NOT the "legacy" (pre-Ryzen/Haswell), "steamdeck"
        # (Zen 2), "rog-ally"/Zen4, or "aarch64" variants also published
        # in every release, and not the bare .zsync delta-update files
        # sitting alongside each real .AppImage.
        "binary_asset_re": re.compile(r"^Eden-Linux-v[\d.]+-amd64-clang-pgo\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        # Unlike Ryubing, not confirmed to hard-require a firmware
        # install to boot at all (yuzu-lineage emulators have
        # historically run many games keys-only, with system services
        # stubbed) -- leaving this False rather than asserting either
        # way without checking Eden's own NAND/firmware directory layout
        # first (it's a different codebase from Ryujinx's own
        # bis/system/Contents convention that install_firmware_zip is
        # built against).
        "needs_firmware": False,
        "args": _eden_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        # .nsz omitted from the ROM picker -- not supported by Eden (or
        # Ryubing, its other fork) per real user testing, not guessed.
        "rom_exclude_extensions": {".nsz"},
    },
    "Eden (Legacy amd64 — pre-Ryzen/pre-Haswell CPUs)": {
        "install_type": "binary",
        "release_api": "https://git.eden-emu.dev/api/v1/repos/eden-emu/eden/releases?limit=1",
        # "legacy" target -- pre-Ryzen/pre-Haswell CPUs, per Eden's own
        # release page ("Pre-Ryzen or Haswell CPUs (expect sadness)").
        "binary_asset_re": re.compile(r"^Eden-Linux-v[\d.]+-legacy-clang-pgo\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": False,
        "args": _eden_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        "rom_exclude_extensions": {".nsz"},
    },
    "Eden (Zen 2 — Steam Deck)": {
        "install_type": "binary",
        "release_api": "https://git.eden-emu.dev/api/v1/repos/eden-emu/eden/releases?limit=1",
        # "steamdeck" target -- Zen 2, per Eden's own release page.
        "binary_asset_re": re.compile(r"^Eden-Linux-v[\d.]+-steamdeck-clang-pgo\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": False,
        "args": _eden_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        "rom_exclude_extensions": {".nsz"},
    },
    "Eden (Zen 4 — AMD Z1/Z2, ROG Ally X, Legion Go S, Steam Machine)": {
        "install_type": "binary",
        "release_api": "https://git.eden-emu.dev/api/v1/repos/eden-emu/eden/releases?limit=1",
        # "rog-ally" target in Eden's own release naming -- Zen 4, per
        # its release page ("AMD Z1/Z2, ROG Ally X, Legion Go S").
        "binary_asset_re": re.compile(r"^Eden-Linux-v[\d.]+-rog-ally-clang-pgo\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": False,
        "args": _eden_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        "rom_exclude_extensions": {".nsz"},
    },
    "Ryubing (AppImage)": {
        "install_type": "binary",
        # Dropped from the dropdown's own display -- the AppImage/
        # Flathub toggle already disambiguates, so repeating it in the
        # name is just clutter (Eden's own entries don't repeat "Eden
        # (AppImage)" either).
        "display_name": "Ryubing",
        # Forgejo REST API, self-hosted (git.ryujinx.app) -- confirmed
        # live: the org's own repo listing (/api/v1/orgs/Ryubing/repos)
        # doesn't include the stable release repo at all; it actually
        # lives under a differently-cased owner ("projects", not
        # "Ryubing"), found via the real Flathub manifest's own source
        # link (git.ryujinx.app/ryubing/ryujinx) redirecting there, not
        # guessed.
        "release_api": "https://git.ryujinx.app/api/v1/repos/projects/Ryubing/releases?limit=1",
        # Single x64 AppImage per release -- no separate CPU-target/PGO
        # variants the way Eden publishes.
        "binary_asset_re": re.compile(r"^ryujinx-[\d.]+-x64\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        # Same real firmware requirement as Flathub Ryubing -- was left
        # False here originally (a real bug, not a deliberate skip):
        # firmware_installed()/install_firmware_zip() used to be
        # hardcoded to entry["app_id"], which this install_type doesn't
        # have, so this entry could never have safely asked for it
        # until that got generalized (see _ryujinx_family_contents_dirs).
        "needs_firmware": True,
        "args": _ryubing_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        "rom_exclude_extensions": {".nsz"},
    },
    "Ryubing Canary (AppImage)": {
        "install_type": "binary",
        "display_name": "Ryubing Canary",
        # Separate org/repo from stable -- confirmed via its own org
        # listing (/api/v1/orgs/Ryubing/repos includes "Canary").
        "release_api": "https://git.ryujinx.app/api/v1/repos/Ryubing/Canary/releases?limit=1",
        "binary_asset_re": re.compile(r"^ryujinx-canary-[\d.]+-x64\.AppImage$"),
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": True,
        "args": _ryubing_args,
        "keys_installed": _switch_keys_installed,
        "install_keys": _switch_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        "rom_exclude_extensions": {".nsz"},
    },
    "Vita3K": {
        "install_type": "binary",
        # Real GitHub, not DMCA-blocked -- confirmed live (unlike Eden's
        # own GitHub mirror). Ships continuous CI builds under a fixed
        # "continuous" tag rather than numbered releases, but the same
        # /releases list (newest first) still works the same way.
        "release_api": "https://api.github.com/repos/Vita3K/Vita3K/releases?per_page=1",
        "binary_asset_re": re.compile(r"^Vita3K-x86_64\.AppImage$"),
        "consoles": "PlayStation Vita",
        # No HLE -- a real PS Vita firmware (.pup) is mandatory, same as
        # RPCS3's PS3 firmware. Routed through bios_slots for the same
        # reason RPCS3's own entry is: the label reads "Select PS Vita
        # Firmware" instead of the generic "Select BIOS" every
        # needs_bios-only emulator shows.
        # Second slot is a real, separate thing -- confirmed via
        # Vita3K's own source (app.cpp's get_firmware_state checks sa0/
        # for font_package completely independently of vs0/ for
        # main_firmware, and has_firmware_installed -- what gates a
        # real launch -- only ever checks vs0/, never sa0/). Installed
        # via the exact same --firmware/install_pup() mechanism as the
        # main slot, just fed a different PUP file: Sony's main
        # firmware download bundles sa0 (font) data into the same PUP
        # in most modern versions, but not always, so this is a real
        # fallback for whoever's PUP doesn't -- optional (4th tuple
        # element False, same convention as xemu's own EEPROM slot)
        # since a missing font pack doesn't block a game from booting,
        # just non-Latin text rendering within it.
        "needs_bios": True,
        "bios_slots": [
            ("bios", "Select PS Vita Firmware (PUP)", "firmware"),
            ("bios2", "Select Font Package (optional)", "font", False),
        ],
        # Sony's own official download -- same reasoning as RPCS3's own
        # entry: not obtainable any other legitimate way.
        "bios_slot_links": {"bios": ("https://www.playstation.com/en-gb/support/hardware/psvita/system-software/", "Get it from Sony")},
        "needs_keys": False,
        "needs_firmware": False,
        "bios_slot_installed": vita3k_firmware_installed,
        "install_bios_slot": install_vita3k_firmware,
        "args": _vita3k_args,
    },
    "Xenia Canary (AppImage)": {
        "install_type": "binary",
        # Official xenia-canary-releases only ships Linux as a
        # .tar.xz/.tar.gz archive, NOT an AppImage -- confirmed live via
        # its own releases API. This is the unofficial, actively-updated
        # (weekly, tracking upstream canary commits) AppImage repackaging
        # by pkgforge-dev/Samueru-sama instead, per explicit user choice
        # over building new tarball-extraction handling for the official
        # one. "(AppImage)" kept in the display name specifically to
        # keep that distinction visible, not just buried in a comment.
        "release_api": "https://api.github.com/repos/pkgforge-dev/xenia-canary-AppImage/releases?per_page=1",
        "binary_asset_re": re.compile(r"^Xenia_Canary-.+-anylinux-x86_64\.AppImage$"),
        "consoles": "Xbox 360",
        # No HLE gap to fill with a BIOS -- Xenia reimplements the Xbox
        # 360 kernel (Xboxkrnl/XAM) itself rather than requiring a real
        # firmware dump, confirmed via its own project site.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _xenia_canary_args,
    },
    "openMSX": {
        "install_type": "flathub",
        "app_id": "org.openmsx.openMSX",
        "consoles": "MSX",
        # Defaults to C-BIOS_MSX2+, openMSX's own bundled open-source
        # BIOS replacement -- confirmed via its own Contrib/README.cbios.
        # Real proprietary MSX system ROMs are an optional accuracy/
        # compatibility upgrade (disk-based software needs them, C-BIOS
        # has no disk-drive support), not required to boot cartridges.
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _openmsx_args,
        # Confirmed live on X1: its manifest only grants home (full
        # read-write, not even :ro) -- no host:ro at all. Same gap as
        # melonDS/RPCS3/Play!/shadPS4 before their own fixes.
        "grant_permissions": ["--filesystem=host:ro"],
    },
}


def by_install_type(install_type):
    """Emulator names filtered to one install_type -- what the Emulators
    tab's Flathub/AppImage toggle actually switches between."""
    return [name for name, entry in EMULATORS.items() if entry["install_type"] == install_type]


# Real per-emulator icons for the Emulators tab's own picker (see
# selfsteam_server._emulator_picker_html) -- Flathub's own appstream
# icon for every flathub-install entry (flathub.org/api/v2/appstream/
# <app_id>), and each AppImage-install project's own real packaging
# icon for the rest (confirmed via each project's own repo, not
# guessed: DuckStation ships on Flathub too even though it's installed
# here as an AppImage, so its icon comes from there same as any other
# Flathub entry; Eden's dist/dev.eden_emu.eden.svg and Vita3K's own
# dist/image/org.vita3k.vita3k.svg are each project's own .desktop
# icon; Xenia Canary's assets/icon/128.png is its own packaged app
# icon). Every Eden/Ryubing-family variant here is the same underlying
# project under a different CPU-target/channel name, so they
# deliberately share one slug rather than needing a separate icon per
# variant.
EMULATOR_ICON_SLUGS = {
    "Dolphin": "dolphin",
    "Ryubing": "ryujinx",
    "Cemu": "cemu",
    "Flycast": "flycast",
    "gopher64": "gopher64",
    "Rosalie's Mupen GUI": "rmg",
    "M64Py": "m64py",
    "melonDS": "melonds",
    "xemu": "xemu",
    "PCSX2": "pcsx2",
    "DuckStation": "duckstation",
    "Azahar": "azahar",
    "RPCS3": "rpcs3",
    "PPSSPP": "ppsspp",
    "Play!": "play",
    "shadPS4": "shadps4",
    "BigPEmu": "bigpemu",
    "Eden (amd64 — Intel/AMD desktop)": "eden",
    "Eden (Legacy amd64 — pre-Ryzen/pre-Haswell CPUs)": "eden",
    "Eden (Zen 2 — Steam Deck)": "eden",
    "Eden (Zen 4 — AMD Z1/Z2, ROG Ally X, Legion Go S, Steam Machine)": "eden",
    "Ryubing (AppImage)": "ryujinx",
    "Ryubing Canary (AppImage)": "ryujinx",
    "Vita3K": "vita3k",
    "Xenia Canary (AppImage)": "xenia",
    "openMSX": "openmsx",
}


def emulator_icon_url(name):
    """The /vendor/emulator-icons/ URL for name's own icon, or "" for an
    unknown/future entry rather than a broken <img> src."""
    slug = EMULATOR_ICON_SLUGS.get(name)
    return f"/vendor/emulator-icons/{slug}.png" if slug else ""


def _binary_dir_name(name):
    # Catalog names are free-form display text (e.g. "Eden (Steam
    # Machine/Zen4)") -- "/" would otherwise be read as a real path
    # separator, splitting one emulator's folder into two nested ones.
    return name.replace("/", "-")


def _binary_dir(name):
    # One folder per binary-installed emulator, kept entirely separate
    # from anything Flatpak-related -- an AppImage here is just a file
    # SelfSteam downloaded and chmod +x'd, not a sandboxed app with its
    # own ~/.var/app/<id> tree.
    return os.path.join(_xdg_data_dir("selfsteam", "appimages"), _binary_dir_name(name))


def _binary_path(name, entry):
    return os.path.join(_binary_dir(name), f"{_binary_dir_name(name)}.AppImage")


def binary_path(name):
    """Public wrapper around _binary_path -- used by
    create_webapp._extract_standalone_emulator_info to reverse-map a
    binary-install shortcut's own LaunchOptions (which starts with the
    AppImage's real path directly, not "flatpak run <app_id>" the way
    every flathub-install entry's does) back to which catalog entry it
    is. Returns None for a flathub-install (or unknown) name."""
    entry = EMULATORS.get(name)
    if not entry or entry["install_type"] != "binary":
        return None
    return _binary_path(name, entry)


def installed(name):
    # Callers (see _add_standalone_emulator_shortcut) check this first
    # and only call install() when it's False -- a real Flatpak app,
    # once present, is never reinstalled on a later shortcut for the
    # same emulator. Binary installs follow the same rule -- once the
    # AppImage is on disk and executable, later shortcuts for the same
    # emulator don't re-download it (no update-check here; re-running
    # install() manually is how you'd force a refresh).
    entry = EMULATORS.get(name)
    if not entry:
        return False
    if entry["install_type"] == "binary":
        path = _binary_path(name, entry)
        return os.path.isfile(path) and os.access(path, os.X_OK)
    if entry["install_type"] != "flathub":
        return False
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return False
    result = subprocess.run(
        host_exec.wrap([flatpak, "info", entry["app_id"]]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _ensure_flathub_remote(flatpak):
    # --if-not-exists makes this safe to run every install unconditionally
    # -- a no-op on the common case (flathub's already configured, e.g.
    # SteamOS and most desktop distros ship it by default), a real
    # remote-add on a genuinely fresh machine that's never installed
    # anything from Flathub before.
    subprocess.run(
        host_exec.wrap([flatpak, "remote-add", "--if-not-exists", "--user", "flathub", FLATHUB_REPO_URL]),
        check=True,
    )


# A large Flatpak download failing partway through on a real "peer
# reset"/dropped-packet blip is common enough (confirmed live: "[56]
# Failure when receiving data from the peer" on X1, on an otherwise
# fine connection) to just retry rather than surface as a one-shot
# failure -- flatpak install itself is naturally resumable/idempotent
# (re-running it after a partial failure doesn't redownload objects it
# already has), so a retry here is cheap when it does help.
_INSTALL_ATTEMPTS = 3


def install_binary(name):
    """Resolves the latest release fresh from the emulator's own release
    API (same idea as retroarch_cores.py pulling cores from libretro's
    buildbot -- no fixed download URL, no update-checking logic either,
    just always fetch whatever's current right now), matches the one
    asset that's actually meant for this machine via the entry's own
    "binary_asset_re", downloads it, and chmod +x's it. Raises
    RuntimeError with the release API's own tag_name if no asset
    matches, so a naming-scheme change upstream fails loudly instead of
    silently downloading the wrong architecture's build."""
    entry = EMULATORS.get(name)
    if not entry or entry["install_type"] != "binary":
        raise ValueError(f"{name} is not a binary-install emulator")

    req = urllib.request.Request(entry["release_api"], headers={"User-Agent": "SelfSteam"})
    # Same transient-network retry as the download below -- confirmed
    # live: a fresh TLS handshake to git.ryujinx.app timed out once,
    # then succeeded immediately on retry, no different from the
    # "Connection reset by peer" case the download loop already handles.
    releases = None
    last_meta_error = None
    for attempt in range(1, _INSTALL_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                releases = json.load(resp)
            break
        except (urllib.error.URLError, OSError) as e:
            last_meta_error = e
    if releases is None:
        raise RuntimeError(f"{name}: release API failed after {_INSTALL_ATTEMPTS} attempts: {last_meta_error}")
    if not releases:
        raise RuntimeError(f"{name}: release API returned no releases")
    release = releases[0]

    asset_re = entry["binary_asset_re"]
    match = next((a for a in release["assets"] if asset_re.match(a["name"])), None)
    if not match:
        raise RuntimeError(f"{name} {release.get('tag_name', '?')}: no asset matching {asset_re.pattern!r}")

    dest_dir = _binary_dir(name)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = _binary_path(name, entry)
    dl_req = urllib.request.Request(match["browser_download_url"], headers={"User-Agent": "SelfSteam"})

    # Same reasoning as flatpak install's own _INSTALL_ATTEMPTS: a large
    # download failing partway through on a real transient network blip
    # is common enough (confirmed live: "[Errno 104] Connection reset by
    # peer" downloading a ~100MB AppImage from Eden's own release CDN,
    # on an otherwise fine connection, succeeding immediately on retry)
    # to just retry rather than surface as a one-shot failure.
    last_error = None
    for attempt in range(1, _INSTALL_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(dl_req, timeout=300) as resp, open(dest_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            os.chmod(dest_path, 0o755)
            return
        except (urllib.error.URLError, OSError) as e:
            last_error = e
    raise RuntimeError(f"{name}: download failed after {_INSTALL_ATTEMPTS} attempts: {last_error}")


_INSTALL_TIMEOUT_SECONDS = 300


def install_flathub_app_id(app_id):
    """The actual retry-loop `flatpak install` -- factored out of
    install() below so a generic Flathub app (not one of the curated
    EMULATORS entries -- see the Apps tab's flathub_browse.py) can
    install through the exact same, already-proven mechanism instead
    of a second copy of this logic.

    Timeout (not just retries) matters here -- confirmed live
    (2026-08-24, X1): a stalled connection to Flathub's own CDN left
    `flatpak install` sitting at "Fetching summary index file" forever,
    genuinely zero bytes/sec (checked via /proc/<pid>/io), not just
    slow. Without a timeout, subprocess.run waits on that forever too,
    which is exactly what made the Apps tab's own "Installing…" button
    (and the whole loading round-trip behind it) look permanently
    stuck with no error, no retry, nothing -- the retry loop below
    never even got a chance to run a second attempt."""
    flatpak = host_exec.which("flatpak")
    _ensure_flathub_remote(flatpak)

    last_result = None
    last_error = None
    for attempt in range(1, _INSTALL_ATTEMPTS + 1):
        try:
            # capture_output (not check=True) -- CalledProcessError's
            # own .stderr is None without this, which is exactly why
            # the actual flatpak error ("[56] Failure when receiving
            # data from the peer", "specified remote not found", etc.)
            # was getting lost behind a useless bare "returned non-zero
            # exit status 1".
            last_result = subprocess.run(
                host_exec.wrap([flatpak, "install", "--user", "-y", "flathub", app_id]),
                capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            last_error = None
        except subprocess.TimeoutExpired:
            last_result = None
            last_error = f"timed out after {_INSTALL_TIMEOUT_SECONDS}s (stalled network to Flathub's CDN?)"
            continue
        if last_result.returncode == 0:
            return
    raise RuntimeError(
        f"flatpak install failed after {_INSTALL_ATTEMPTS} attempts: "
        f"{last_error or last_result.stderr.strip() or last_result.stdout.strip()}"
    )


def flathub_app_id_installed(app_id):
    """Same real `flatpak info` check as installed() above, just for a
    bare app_id instead of a curated EMULATORS entry -- see
    install_flathub_app_id's own docstring for why this is split out."""
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return False
    result = subprocess.run(
        host_exec.wrap([flatpak, "info", app_id]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def installed_flathub_app_ids():
    """Every installed Flatpak app id, in one `flatpak list` call --
    the Apps tab's own browse grid (flathub_browse.py) needs an
    installed/not-installed check per card, and a real subprocess spawn
    (flathub_app_id_installed's own `flatpak info`) per card on a
    24-app page would be 24 round trips through host_exec.wrap just to
    render one page. Empty set (not an exception) if flatpak isn't
    available at all -- same "just render as if nothing's installed"
    fallback flathub_app_id_installed's own `if not flatpak` branch
    already uses."""
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return set()
    result = subprocess.run(
        host_exec.wrap([flatpak, "list", "--app", "--columns=application"]),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def uninstall_flathub_app_id(app_id):
    """`flatpak uninstall` for a bare app_id -- the Apps tab's own
    Remove button. No retry loop the way install_flathub_app_id has
    one: uninstall doesn't hit the network at all (nothing to download,
    nothing to retry after a transient blip), so a real failure here is
    a real, immediate problem (e.g. another app still depends on a
    shared runtime it's part of) worth surfacing as-is rather than
    silently retried."""
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        raise RuntimeError("flatpak isn't available on this host")
    result = subprocess.run(
        host_exec.wrap([flatpak, "uninstall", "--user", "-y", app_id]),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"flatpak uninstall failed: {result.stderr.strip() or result.stdout.strip()}")


def install(name):
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    if entry["install_type"] == "binary":
        install_binary(name)
        return
    if entry["install_type"] != "flathub":
        raise NotImplementedError(f"install_type {entry['install_type']!r} not implemented yet")
    install_flathub_app_id(entry["app_id"])


def grant_permissions(name):
    """flatpak override --user <app_id> <perm>... for an entry's own
    "grant_permissions" list -- most emulators here (Dolphin, Cemu,
    Flycast, Ryubing) already ship --filesystem=host:ro or home:ro in
    their own Flathub manifest, so this is a no-op for them (empty/
    absent list). gopher64's own manifest grants no filesystem access
    at all (confirmed live: a ROM picked from anywhere outside its own
    Flatpak sandbox -- which is anywhere, since it declares zero
    filesystem permissions -- crashes with
    `std::fs::File::open(file_path).unwrap()` -> NotFound the instant
    it tries to actually read the file), so its own entry lists
    ["--filesystem=host:ro"] to grant read access the same way those
    other emulators' own manifests already do by default.

    Idempotent (flatpak override just rewrites a permissions file) and
    run every time a shortcut is created for this emulator, not just on
    a fresh install -- unlike install() itself, this needs to reach an
    emulator that was already installed (by SelfSteam or otherwise)
    before this permission gap was noticed, not just future installs."""
    entry = EMULATORS.get(name)
    perms = entry.get("grant_permissions") if entry else None
    if not perms:
        return
    flatpak = host_exec.which("flatpak")
    subprocess.run(
        host_exec.wrap([flatpak, "override", "--user", entry["app_id"], *perms]),
        capture_output=True,
    )


def launch_args(name, romfile, zrif=None):
    entry = EMULATORS.get(name)
    if not entry:
        return None
    if entry["install_type"] == "binary":
        path = _binary_path(name, entry)
        if not os.path.isfile(path):
            return None
        # Vita3K .pkg is a real, separate case, not just another
        # content-path -- see install_vita3k_pkg's own docstring for
        # why: --pkg/--zrif only installs, the actual launch afterward
        # needs --installed-path <title id> instead of the original
        # .pkg path at all. zrif is None for every other emulator/file
        # type, so this never affects them.
        if name == "Vita3K" and romfile.lower().endswith(".pkg"):
            if not zrif:
                return None
            title_id = install_vita3k_pkg(romfile, zrif)
            return [shlex.quote(path), "--installed-path", shlex.quote(title_id), "--fullscreen"]
        # shlex.quote the AppImage path itself, not just the romfile --
        # LaunchOptions is stored/joined as one shell-like string with a
        # plain " ".join (see register_steam_shortcut), so every element
        # needs to already be shell-safe going in. Confirmed live: an
        # unquoted path with spaces/parens/an em-dash in it (e.g. "Eden
        # (Legacy amd64 — pre-Ryzen/pre-Haswell CPUs)") broke the
        # wrapper script's own shell parsing with a real "syntax error
        # near unexpected token" -- the flathub branch below never hit
        # this because `flatpak` and an app_id are never anything but
        # plain, space-free tokens.
        return [shlex.quote(path), *entry["args"](romfile)]
    if entry["install_type"] != "flathub":
        return None
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return None
    # shadPS4 .pkg -- see extract_shadps4_pkg's own docstring for why a
    # raw .pkg is never a valid boot path on its own; this swaps it for
    # the real extracted eboot.bin before building the launch args,
    # same "real content-path substitution" shape as Vita3K's own .pkg
    # case above, just without a title-id round trip.
    if name == "shadPS4" and romfile.lower().endswith(".pkg"):
        romfile = extract_shadps4_pkg(romfile)
    return [flatpak, "run", entry["app_id"], *entry["args"](romfile)]


def configure_game_dir(name, game_dir):
    """Registers game_dir as a watched ROM folder in the emulator's own
    settings, so games SelfSteam uploaded show up in the emulator's own
    game list too, not just as a Steam shortcut -- additive only (never
    removes/replaces anything already there), and each per-emulator
    configurator only ever touches an EXISTING config file (see their
    own docstrings for why bootstrapping one from scratch isn't safe in
    general, especially for a version-gated format like Ryubing's).

    Callers should only call this once, right after a fresh install()
    -- not on every shortcut creation -- so an emulator the user
    already had installed (and already configured however they like)
    never gets touched. No-op for an emulator with no
    configure_game_dir entry yet (not every emulator needs one, and
    none is required for the tab to otherwise work)."""
    entry = EMULATORS.get(name)
    configurator = entry.get("configure_game_dir") if entry else None
    if configurator:
        configurator(entry, game_dir)


def _flatpak_config_dir(app_id, *parts):
    # Same ~/.var/app/<app-id>/config/... layout every Flatpak app's
    # persistent data lives under (already confirmed live against
    # RetroArch's own real install -- see retroarch_cores._cores_dir's
    # own comment), not assumed fresh here.
    return os.path.join(os.path.expanduser(f"~/.var/app/{app_id}/config"), *parts)


def _flatpak_data_dir(app_id, *parts):
    # Flatpak's own separate redirect for XDG_DATA_HOME (~/.local/share
    # unsandboxed), distinct from _flatpak_config_dir's XDG_CONFIG_HOME
    # one -- needed because not every app keeps everything under config
    # the way RetroArch/Ryubing do (see Cemu's keys.txt, confirmed via
    # its own source to live under XDG_DATA_HOME specifically).
    return os.path.join(os.path.expanduser(f"~/.var/app/{app_id}/data"), *parts)


def bios_slots(name):
    """The (prefix, label, ...) list an entry's own "bios_slots" field
    declares, or None for an emulator with no BIOS requirement (or the
    single-file needs_bios case every other emulator here uses)."""
    entry = EMULATORS.get(name)
    return entry.get("bios_slots") if entry else None


def bios_slot_installed(name, slot_prefix):
    """Dispatches to the entry's own "bios_slot_installed" handler --
    see keys_installed's own comment for why there's no shared
    implementation across emulators."""
    entry = EMULATORS.get(name)
    handler = entry.get("bios_slot_installed") if entry else None
    return handler(entry, slot_prefix) if handler else None


def install_bios_slot(name, slot_prefix, file_path):
    """Dispatches to the entry's own "install_bios_slot" handler."""
    entry = EMULATORS.get(name)
    handler = entry.get("install_bios_slot") if entry else None
    return handler(entry, slot_prefix, file_path) if handler else None


def configure_renderer(name):
    """Dispatches to the entry's own "configure_renderer" handler, if it
    has one -- a no-op for every emulator that doesn't (xemu so far is
    the only one SelfSteam has any reason to set a renderer preference
    for; see _xemu_configure_vulkan's own docstring)."""
    entry = EMULATORS.get(name)
    handler = entry.get("configure_renderer") if entry else None
    if handler:
        handler(entry)


def bootstrap_config(name):
    """Dispatches to the entry's own "bootstrap_config" handler, if it
    has one -- a no-op for every emulator that doesn't need this. No
    current user (Supermodel, the original reason this existed, was
    removed -- see its own removal for why); kept as a real, generic
    hook for whatever needs it next, same pattern as grant_permissions/
    configure_renderer."""
    entry = EMULATORS.get(name)
    handler = entry.get("bootstrap_config") if entry else None
    if handler:
        handler(entry)


def keys_installed(name):
    """Whether keys already exist for this emulator -- keys/firmware are
    a one-time install for the emulator itself, not per-shortcut state,
    so the Emulators tab's own Create-button gate (see render_page's
    em_ready) only requires freshly picking them when neither is
    present yet, letting an existing shortcut's own Edit link land back
    on the tab without permanently blocking Create just because the
    keys picker wasn't touched again. Returns a real, user-meaningful
    label (filename(s) for Ryubing, "keys.txt" for Cemu) or None -- the
    picker shows this directly rather than a vague "installed" label.

    Dispatches to each entry's own "keys_installed" handler (see
    EMULATORS) rather than a single shared implementation -- Ryubing's
    keys are a folder of *.keys files under its config dir, Cemu's is
    one keys.txt under its data dir with a real-key-vs-example-line
    distinction to make; there's no shared layout to generalize over."""
    entry = EMULATORS.get(name)
    handler = entry.get("keys_installed") if entry else None
    return handler(entry) if handler else None


def _ryujinx_family_contents_dirs():
    """Every real bis/system/Contents dir across the whole Ryujinx
    family: Flathub Ryubing (sandboxed) and the AppImage build (shared
    by stable and Canary -- see _switch_keys_dirs' own comment on why
    that's one directory, not two). Firmware installed via any one of
    these should already satisfy the other two, same reasoning as
    _switch_keys_dirs. Not Eden -- its own NAND/firmware directory
    layout isn't confirmed compatible with this NCA-zip-explosion
    format, unlike prod.keys/title.keys, which are a universal format
    regardless of emulator."""
    return [
        _flatpak_config_dir("io.github.ryubing.Ryujinx", "Ryujinx", "bis", "system", "Contents"),
        _xdg_config_dir("Ryujinx", "bis", "system", "Contents"),
    ]


def _firmware_marker_path(contents_dir):
    return os.path.join(contents_dir, ".selfsteam-firmware-source")


def _old_firmware_marker_path(contents_dir):
    # Read-only fallback for a marker written before this app's SelfSteam
    # rename -- a real one already exists from prior firmware installs,
    # and losing it would silently regress firmware_installed()'s real-
    # filename display back to the generic "N titles" count-based label.
    return os.path.join(contents_dir, ".gridge-firmware-source")


def _write_source_filename_marker(marker_path, original_path):
    """Generic version of the Ryujinx-family firmware marker above, for
    every other keys/firmware/BIOS install that extracts or copies into
    a directory/config layout with no filename of its own to show back
    (RPCS3's own dev_flash, Vita3K's vs0/sa0 partitions, Cemu's fixed
    keys.txt) -- confirmed live (2026-08-25) that showing each of these
    already-installed pickers' own synthetic description ("firmware
    release:04.9300:...", "font package (3 items)") instead of the real
    file someone actually picked read as wrong/confusing, not just less
    specific. Overwritten on every real install, so picking a different
    file later naturally replaces it."""
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    with open(marker_path, "w") as f:
        f.write(os.path.basename(original_path))


def _read_source_filename_marker(marker_path):
    if not os.path.isfile(marker_path):
        return None
    with open(marker_path) as f:
        name = f.read().strip()
    return name or None


def firmware_installed(name):
    """Same idea as keys_installed, for the registered firmware dir --
    but a firmware .zip install (see install_firmware_zip) explodes into
    a directory of NCA-id-named content folders with no real user-facing
    filename of its own, so this instead reads the real filename back
    from the sidecar marker install_firmware_zip writes next to it
    (works whether that zip was uploaded or picked locally, and
    naturally reflects whatever was installed *last*) -- falling back to
    a count-based label ("148 titles") only for firmware installed
    before that marker existed.

    Checks every dir in _ryujinx_family_contents_dirs(), not just
    whichever one this specific entry would itself use -- firmware
    installed via any one of the Ryujinx-family entries should already
    read as installed for the other two. Also backfills any sibling dir
    that's missing the real registered/ content -- see
    _switch_keys_installed's own comment for why this needs to actually
    copy the files here, not just report true: confirmed live, firmware
    installed before this sharing mechanism existed left Ryubing Canary
    AppImage's own directory empty despite this function reporting
    "installed" for it, so Ryujinx Canary itself still complained about
    missing firmware at launch."""
    entry = EMULATORS.get(name)
    if not entry:
        return None

    dirs = _ryujinx_family_contents_dirs()
    source_contents_dir = None
    for contents_dir in dirs:
        registered_dir = os.path.join(contents_dir, "registered")
        if os.path.isdir(registered_dir) and os.listdir(registered_dir):
            source_contents_dir = contents_dir
            break
    if not source_contents_dir:
        return None

    for contents_dir in dirs:
        if contents_dir == source_contents_dir:
            continue
        dest_registered = os.path.join(contents_dir, "registered")
        if not os.path.isdir(dest_registered):
            os.makedirs(contents_dir, exist_ok=True)
            shutil.copytree(os.path.join(source_contents_dir, "registered"), dest_registered)
            src_marker = _firmware_marker_path(source_contents_dir)
            if os.path.isfile(src_marker):
                shutil.copy2(src_marker, _firmware_marker_path(contents_dir))

    registered_dir = os.path.join(source_contents_dir, "registered")
    count = len(os.listdir(registered_dir))
    marker_path = _firmware_marker_path(source_contents_dir)
    if not os.path.isfile(marker_path):
        marker_path = _old_firmware_marker_path(source_contents_dir)
    if os.path.isfile(marker_path):
        with open(marker_path) as f:
            marker = f.read().strip()
        if marker:
            return marker
    return f"{count} titles"


def install_keys(name, keys_path):
    """Copies a picked keys file into wherever this emulator actually
    keeps them -- dispatches to each entry's own "install_keys" handler
    (see EMULATORS and keys_installed's own comment for why there's no
    shared implementation). Returns whatever that handler returns (the
    list of files actually copied)."""
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    handler = entry.get("install_keys")
    if not handler:
        raise NotImplementedError(f"{name} has no install_keys handler")
    return handler(entry, keys_path)


def install_firmware_zip(name, zip_path):
    """Installs a Switch firmware .zip the same way Ryubing's own
    ContentManager.InstallFirmware/InstallFromZip do for a .zip package
    specifically (confirmed via its actual source, not guessed): each
    zip entry's own path already encodes its NCA content id (entries
    look like "<nca-id>.nca/00" or "<nca-id>.nca/<file>"), extracted
    once to a shared temp "<nca-id>.nca/00" layout, then propagated as
    the real "registered" directory into every dir in
    _ryujinx_family_contents_dirs() -- firmware installed via any one
    of the Ryujinx-family entries ends up satisfying all of them, not
    just the one used. Deliberately skips
    ContentManager.VerifyFirmwarePackage -- that's real NCA decryption
    (needs Ryubing's own loaded keyset/LibHac, not safely portable here)
    used only to show a nicer confirmation-dialog message in the GUI
    flow, not required for the install itself to be correct. An invalid
    zip here just means Ryubing finds no valid firmware at next launch,
    same failure mode as picking the wrong file in the GUI's own
    installer."""
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")

    extract_root = tempfile.mkdtemp(prefix="selfsteam-firmware-")
    try:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                cleaned = info.filename.replace(".cnmt", "")
                parts = [p for p in cleaned.split("/") if p]
                if not parts:
                    continue
                nca_id = parts[-1]
                # Fragmented NCA: the real file is one level up from a
                # literal "00" part-file name (same check
                # InstallFromZip's own C# does: ncaId.Equals("00") ->
                # use the previous path component instead).
                if nca_id == "00" and len(parts) >= 2:
                    nca_id = parts[-2]
                if ".nca" not in nca_id:
                    continue
                dest_dir = os.path.join(extract_root, nca_id)
                os.makedirs(dest_dir, exist_ok=True)
                with z.open(info) as src, open(os.path.join(dest_dir, "00"), "wb") as dst:
                    shutil.copyfileobj(src, dst)

        last_registered_dir = None
        for contents_dir in _ryujinx_family_contents_dirs():
            os.makedirs(contents_dir, exist_ok=True)
            registered_dir = os.path.join(contents_dir, "registered")
            if os.path.isdir(registered_dir):
                shutil.rmtree(registered_dir)
            shutil.copytree(extract_root, registered_dir)
            # A sidecar marker, not anything Ryujinx itself reads --
            # registered/ only ever holds NCA-id-named content folders
            # with no filename of their own, so this is the one place
            # the real, human-readable name of whatever zip was
            # actually installed survives, for firmware_installed's own
            # display. Overwritten on every real install, so picking a
            # different firmware later naturally replaces it -- there's
            # nothing else to "reset" separately.
            with open(_firmware_marker_path(contents_dir), "w") as f:
                f.write(os.path.basename(zip_path))
            last_registered_dir = registered_dir
        return last_registered_dir
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)
    return registered_dir
