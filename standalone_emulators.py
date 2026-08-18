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
