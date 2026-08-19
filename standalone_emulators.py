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
import xml.etree.ElementTree as ET
import zipfile

import host_exec

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


def _ryubing_keys_installed(entry):
    keys_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "system")
    if not os.path.isdir(keys_dir):
        return None
    found = sorted(f for f in os.listdir(keys_dir) if f.endswith(".keys"))
    return ", ".join(found) if found else None


def _ryubing_install_keys(entry, keys_path):
    """Copies picked keys into Ryubing's own real keys directory --
    confirmed via its actual source (AppDataManager.KeysDirPath =
    BaseDirPath/system, i.e. ~/.config/Ryujinx/system on Linux,
    ~/.var/app/<id>/config/Ryujinx/system/ under the Flatpak sandbox),
    the same directory ContentManager.InstallKeys's own real caller
    (MainWindowViewModel.HandleKeysInstallation) uses.

    keys_path is a single picked file (prod.keys) -- a real Switch key
    dump typically has title.keys (per-game) sitting right alongside it
    too, so this also auto-picks up title.keys from that same folder if
    it's there, without the user needing to pick it separately (see
    _em_picker_section's own "keys" display for the matching UI side of
    this). Returns the list of files actually copied. No parsing/
    verification of the keys' own contents either way -- Ryubing does
    that itself on next launch."""
    dest_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "system")
    os.makedirs(dest_dir, exist_ok=True)

    copied = []
    dest = os.path.join(dest_dir, os.path.basename(keys_path))
    shutil.copy2(keys_path, dest)
    copied.append(dest)

    sibling = os.path.join(os.path.dirname(keys_path), "title.keys")
    if os.path.basename(keys_path) != "title.keys" and os.path.isfile(sibling):
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


def _cemu_keys_installed(entry):
    keys_path = _cemu_keys_path(entry)
    if not os.path.isfile(keys_path):
        return None
    with open(keys_path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and line.lower() != _CEMU_EXAMPLE_KEY.lower():
                return "keys.txt"
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
# (label, xemu's own real TOML key under [sys.files], confirmed via its
# own config_spec.yml) -- xemu is the first (and so far only) emulator
# here with more than one required BIOS-type file, since original Xbox
# emulation has no HLE fallback the way GameCube/DS/N64 do (confirmed:
# no "enable HLE"-style toggle exists anywhere in its settings).
XEMU_BIOS_SLOTS = [
    ("bios", "Select MCPX Boot ROM", "bootrom_path"),
    ("bios2", "Select Xbox BIOS", "flashrom_path"),
    ("bios3", "Select EEPROM", "eeprom_path"),
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
    toml_key = next(k for p, _label, k in XEMU_BIOS_SLOTS if p == slot_prefix)
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
    toml_key = next(k for p, _label, k in XEMU_BIOS_SLOTS if p == slot_prefix)
    toml_path = _xemu_toml_path(entry)
    os.makedirs(os.path.dirname(toml_path), exist_ok=True)
    content = ""
    if os.path.isfile(toml_path):
        with open(toml_path) as f:
            content = f.read()
    content = _toml_set_in_section(content, "sys.files", toml_key, file_path)
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


def rpcs3_firmware_installed(entry, slot_prefix):
    # vsh/etc/version.txt only exists once a real firmware install has
    # actually completed -- confirmed via source (util/sysinfo.cpp's
    # get_firmware_version(), the same file RPCS3 itself reads to know
    # its own installed firmware version).
    version_path = os.path.join(_rpcs3_dev_flash_dir(entry), "vsh", "etc", "version.txt")
    if not os.path.isfile(version_path):
        return None
    with open(version_path) as f:
        version = f.read().strip()
    return f"firmware {version}" if version else "firmware installed"


def install_rpcs3_firmware(entry, slot_prefix, file_path):
    """Firmware install isn't a plain file copy the way keys/BIOS are
    elsewhere in this module -- a PS3 PUP is a signed, encrypted
    package RPCS3 has to decrypt and unpack itself, no shortcut around
    that. RPCS3's own --installfw <path> flag does exactly this
    (confirmed via source: rpcs3.cpp calls main_window::InstallPup for
    it) -- run for real here rather than reimplementing PUP decryption
    ourselves. Real caveat, not swept under the rug: --installfw can't
    run in --no-gui mode (confirmed via source: report_fatal_error
    otherwise), so this genuinely pops up RPCS3's own install dialog
    rather than staying silent, and this call blocks until that
    process exits (the user closing/finishing the dialog) rather than
    us polling for completion some other way."""
    flatpak = host_exec.which("flatpak")
    subprocess.run(
        host_exec.wrap([flatpak, "run", entry["app_id"], "--installfw", file_path]),
    )


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
        "keys_installed": _ryubing_keys_installed,
        "install_keys": _ryubing_install_keys,
        "keys_tooltip": "Pick prod.keys -- if title.keys is sitting in the same folder, it'll be picked up automatically too.",
        # .nsp omitted from the ROM picker -- not supported by Ryubing
        # (or Eden, its other fork) per real user testing, not guessed.
        "rom_exclude_extensions": {".nsp"},
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
        # its own config_spec.yml, [sys.files] requires a real MCPX
        # bootrom, Xbox BIOS (flashrom), and EEPROM dump, no toggle
        # anywhere to run without them. Three separate files, not one,
        # so needs_bios/bios_slots both point at XEMU_BIOS_SLOTS rather
        # than the single-file bios picker every other emulator here
        # uses.
        "needs_bios": True,
        "bios_slots": XEMU_BIOS_SLOTS,
        "bios_slot_installed": xemu_bios_slot_installed,
        "install_bios_slot": install_xemu_bios_slot,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _xemu_args,
        # No grant_permissions needed -- confirmed via its own Flathub
        # manifest, it already ships --filesystem=host:ro.
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
}


def by_install_type(install_type):
    """Emulator names filtered to one install_type -- what the Emulators
    tab's Flathub/AppImage toggle actually switches between."""
    return [name for name, entry in EMULATORS.items() if entry["install_type"] == install_type]


def installed(name):
    # Callers (see _add_standalone_emulator_shortcut) check this first
    # and only call install() when it's False -- a real Flatpak app,
    # once present, is never reinstalled on a later shortcut for the
    # same emulator.
    entry = EMULATORS.get(name)
    if not entry or entry["install_type"] != "flathub":
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


def install(name):
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    if entry["install_type"] != "flathub":
        raise NotImplementedError(f"install_type {entry['install_type']!r} not implemented yet")
    flatpak = host_exec.which("flatpak")
    _ensure_flathub_remote(flatpak)

    last_result = None
    for attempt in range(1, _INSTALL_ATTEMPTS + 1):
        # capture_output (not check=True) -- CalledProcessError's own
        # .stderr is None without this, which is exactly why the actual
        # flatpak error ("[56] Failure when receiving data from the
        # peer", "specified remote not found", etc.) was getting lost
        # behind a useless bare "returned non-zero exit status 1".
        last_result = subprocess.run(
            host_exec.wrap([flatpak, "install", "--user", "-y", "flathub", entry["app_id"]]),
            capture_output=True, text=True,
        )
        if last_result.returncode == 0:
            return
    raise RuntimeError(
        f"flatpak install failed after {_INSTALL_ATTEMPTS} attempts: "
        f"{last_result.stderr.strip() or last_result.stdout.strip()}"
    )


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


def launch_args(name, romfile):
    entry = EMULATORS.get(name)
    if not entry or entry["install_type"] != "flathub":
        return None
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return None
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


def _firmware_marker_path(entry):
    contents_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "bis", "system", "Contents")
    return os.path.join(contents_dir, ".selfsteam-firmware-source")


def _old_firmware_marker_path(entry):
    # Read-only fallback for a marker written before this app's SelfSteam
    # rename -- a real one already exists from prior firmware installs,
    # and losing it would silently regress firmware_installed()'s real-
    # filename display back to the generic "N titles" count-based label.
    contents_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "bis", "system", "Contents")
    return os.path.join(contents_dir, ".gridge-firmware-source")


def firmware_installed(name):
    """Same idea as keys_installed, for the registered firmware dir --
    but a firmware .zip install (see install_firmware_zip) explodes into
    a directory of NCA-id-named content folders with no real user-facing
    filename of its own, so this instead reads the real filename back
    from the sidecar marker install_firmware_zip writes next to it
    (works whether that zip was uploaded or picked locally, and
    naturally reflects whatever was installed *last*) -- falling back to
    a count-based label ("148 titles") only for firmware installed
    before that marker existed."""
    entry = EMULATORS.get(name)
    if not entry:
        return None
    registered_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "bis", "system", "Contents", "registered")
    if not os.path.isdir(registered_dir):
        return None
    count = len(os.listdir(registered_dir))
    if not count:
        return None
    marker_path = _firmware_marker_path(entry)
    if not os.path.isfile(marker_path):
        marker_path = _old_firmware_marker_path(entry)
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
    look like "<nca-id>.nca/00" or "<nca-id>.nca/<file>"), extracted to
    a temp "<nca-id>.nca/00" layout, then atomically swapped in as the
    real "registered" directory
    (~/.var/app/<id>/config/Ryujinx/bis/system/Contents/registered).
    Deliberately skips ContentManager.VerifyFirmwarePackage -- that's
    real NCA decryption (needs Ryubing's own loaded keyset/LibHac, not
    safely portable here) used only to show a nicer confirmation-dialog
    message in the GUI flow, not required for the install itself to be
    correct. An invalid zip here just means Ryubing finds no valid
    firmware at next launch, same failure mode as picking the wrong
    file in the GUI's own installer."""
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    contents_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "bis", "system", "Contents")
    registered_dir = os.path.join(contents_dir, "registered")
    temp_dir = os.path.join(contents_dir, "temp")

    if os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)

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
            # literal "00" part-file name (same check InstallFromZip's
            # own C# does: ncaId.Equals("00") -> use the previous
            # path component instead).
            if nca_id == "00" and len(parts) >= 2:
                nca_id = parts[-2]
            if ".nca" not in nca_id:
                continue
            dest_dir = os.path.join(temp_dir, nca_id)
            os.makedirs(dest_dir, exist_ok=True)
            with z.open(info) as src, open(os.path.join(dest_dir, "00"), "wb") as dst:
                shutil.copyfileobj(src, dst)

    if os.path.isdir(registered_dir):
        shutil.rmtree(registered_dir)
    shutil.move(temp_dir, registered_dir)
    # A sidecar marker, not anything Ryujinx itself reads -- registered/
    # only ever holds NCA-id-named content folders with no filename of
    # their own, so this is the one place the real, human-readable name
    # of whatever zip was actually installed survives, for
    # firmware_installed's own display. Overwritten on every real
    # install, so picking a different firmware later naturally replaces
    # it -- there's nothing else to "reset" separately.
    with open(_firmware_marker_path(entry), "w") as f:
        f.write(os.path.basename(zip_path))
    return registered_dir
