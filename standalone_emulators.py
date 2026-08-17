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
import os
import shlex
import shutil
import subprocess
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


def _ryubing_args(romfile):
    # -f/--fullscreen and the bare romfile positional arg, both confirmed
    # real via Ryubing's own CommandLineState.cs (the actual CLI argument
    # parser in its source, not guessed/assumed): "-f"/"--fullscreen"
    # sets StartFullscreenArg, and anything not matching a known flag
    # falls through to LaunchPathArg, i.e. a bare positional path.
    return ["--fullscreen", shlex.quote(romfile)]


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
# firmware but not a traditional "BIOS"), and args(romfile) -> argv
# (already shell-quoted where needed, same "ready to append after
# flatpak run <app_id>" contract as retroarch_cores.launch_args).
EMULATORS = {
    "Dolphin": {
        "install_type": "flathub",
        "app_id": "org.DolphinEmu.dolphin-emu",
        "consoles": "Nintendo GameCube / Wii",
        "needs_bios": False,
        "needs_keys": False,
        "needs_firmware": False,
        "args": _dolphin_args,
    },
    "Ryubing": {
        "install_type": "flathub",
        "app_id": "io.github.ryubing.Ryujinx",
        "consoles": "Nintendo Switch",
        "needs_bios": False,
        "needs_keys": True,
        "needs_firmware": True,
        "args": _ryubing_args,
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


def _flatpak_config_dir(app_id, *parts):
    # Same ~/.var/app/<app-id>/config/... layout every Flatpak app's
    # persistent data lives under (already confirmed live against
    # RetroArch's own real install -- see retroarch_cores._cores_dir's
    # own comment), not assumed fresh here.
    return os.path.join(os.path.expanduser(f"~/.var/app/{app_id}/config"), *parts)


def keys_installed(name):
    """Whether keys already exist for this emulator -- keys/firmware are
    a one-time install for the emulator itself, not per-shortcut state,
    so the Emulators tab's own Create-button gate (see render_page's
    em_ready) only requires freshly picking them when neither is
    present yet, letting an existing shortcut's own Edit link land back
    on the tab without permanently blocking Create just because the
    keys picker wasn't touched again."""
    entry = EMULATORS.get(name)
    if not entry:
        return False
    keys_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "system")
    return os.path.isdir(keys_dir) and any(f.endswith(".keys") for f in os.listdir(keys_dir))


def firmware_installed(name):
    """Same idea as keys_installed, for the registered firmware dir."""
    entry = EMULATORS.get(name)
    if not entry:
        return False
    registered_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "bis", "system", "Contents", "registered")
    return os.path.isdir(registered_dir) and bool(os.listdir(registered_dir))


def install_keys(name, keys_path):
    """Copies picked keys into Ryubing's own real keys directory --
    confirmed via its actual source (AppDataManager.KeysDirPath =
    BaseDirPath/system, i.e. ~/.config/Ryujinx/system on Linux,
    ~/.var/app/<id>/config/Ryujinx/system/ under the Flatpak sandbox),
    the same directory ContentManager.InstallKeys's own real caller
    (MainWindowViewModel.HandleKeysInstallation) uses.

    keys_path can be a single file (prod.keys alone) or a directory --
    a real Switch key dump typically has both prod.keys (console-wide)
    and title.keys (per-game) side by side, and ContentManager.
    InstallKeys itself supports both shapes: a single file gets copied
    as-is, a directory gets every *.keys file inside it copied. Ported
    directly from that same method, not guessed. Returns the list of
    files actually copied. No parsing/verification of the keys' own
    contents either way -- Ryubing does that itself on next launch."""
    entry = EMULATORS.get(name)
    if not entry:
        raise ValueError(f"No known standalone emulator: {name}")
    dest_dir = _flatpak_config_dir(entry["app_id"], "Ryujinx", "system")
    os.makedirs(dest_dir, exist_ok=True)

    if os.path.isdir(keys_path):
        copied = []
        for entry_name in sorted(os.listdir(keys_path)):
            if not entry_name.endswith(".keys"):
                continue
            src = os.path.join(keys_path, entry_name)
            if not os.path.isfile(src):
                continue
            dest = os.path.join(dest_dir, entry_name)
            shutil.copy2(src, dest)
            copied.append(dest)
        return copied

    dest = os.path.join(dest_dir, os.path.basename(keys_path))
    shutil.copy2(keys_path, dest)
    return [dest]


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
    return registered_dir
