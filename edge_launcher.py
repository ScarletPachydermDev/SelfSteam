"""Detect an installed Microsoft Edge, native or Flatpak.

Edge is the only Chromium-based browser with Dolby Digital Plus/Atmos audio
decoding built in on every platform it ships, including Linux -- Google
never licensed Dolby codecs into open-source Chromium, so no other
Chromium derivative (including a bundled Electron) can play that audio.
Rather than bundling a browser ourselves, we shell out to Edge if it's
already installed, and ask the user to install it otherwise.
"""
import json
import os
import subprocess

import host_exec

NATIVE_BINARY_NAMES = ["microsoft-edge-stable", "microsoft-edge", "microsoft-edge-beta", "microsoft-edge-dev"]
FLATPAK_APP_ID = "com.microsoft.Edge"

# Chromium/Edge only shows its first-run wizard (instead of the kiosk
# --app= page) when this sentinel file is missing from the profile dir.
# Pre-creating it (empty) lets a shortcut work correctly on its very
# first launch. Confirmed path on the Flatpak build; native installs
# manage their own profile/first-run outside our scope.
FLATPAK_FIRST_RUN_PATH = os.path.expanduser(
    "~/.var/app/com.microsoft.Edge/config/microsoft-edge/First Run"
)

# Edge layers its OWN onboarding (a fullscreen "Welcome to Edge" wizard,
# a sign-in nudge, then an auto-opened explore.microsoft.com/edge/welcome
# tour tab) on top of the base Chromium first-run flow the sentinel
# above suppresses -- confirmed via inspecting a real completed profile
# on real hardware: it's tracked in Local State (browser-level, not
# per-profile) under "fre"/"new_device_fre", a separate mechanism the
# sentinel file doesn't touch at all. There's no managed-policy route
# for the Flatpak build either (confirmed: its sandboxed /etc has no
# /opt, and host-etc access only exposes the host's /etc at
# /run/host/etc, which Edge's binary never looks at) -- pre-seeding
# these same keys before Edge's first-ever launch is the only lever
# available.
FLATPAK_LOCAL_STATE_PATH = os.path.expanduser(
    "~/.var/app/com.microsoft.Edge/config/microsoft-edge/Local State"
)
_FRE_SEED = {
    "fre": {
        "has_user_completed_fre": True,
        "has_user_seen_fre": True,
        "has_first_visible_browser_session_completed": True,
    },
    "new_device_fre": {"has_user_seen_new_fre": True},
}

# Confirmed on real hardware: even with FRE suppressed, a fresh profile
# still auto-opens an "Update complete!" tour tab the first time Edge
# runs post-install -- and critically, that tab opens as a REGULAR
# window, not through our --app= flag at all. Since we share one Edge
# profile across every shortcut (deliberately, so logins persist), that
# regular-mode window becomes the first Chromium singleton for this
# profile, and every subsequent --app= shortcut click just gets
# funneled into it as a plain tab instead of spawning its own kiosk
# window (matches what was observed: shortcuts opening Edge's normal
# home page, unaffected by closing and reopening). Pre-seeding these
# version-tracking keys to the currently-installed version prevents the
# tour from ever deciding a "what's new" page is owed.
def _get_flatpak_edge_version():
    result = subprocess.run(
        host_exec.wrap(["flatpak", "info", FLATPAK_APP_ID]),
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if line.strip().startswith("Version:"):
            return line.split(":", 1)[1].strip().split("-")[0]
    return None

INSTALL_INSTRUCTIONS = (
    "Microsoft Edge wasn't found. Install it from Flathub "
    "(flatpak install flathub com.microsoft.Edge) or from "
    "https://www.microsoft.com/en-us/edge/download?platform=linux "
    "(official .deb/.rpm), then try again."
)


class EdgeNotFoundError(RuntimeError):
    pass


def _flatpak_edge_installed():
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        return False
    result = subprocess.run(
        host_exec.wrap([flatpak, "info", FLATPAK_APP_ID]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def find_edge():
    """Returns (exe, prefix_args) for the installed Edge. prefix_args is
    empty for a native binary, or ["run", FLATPAK_APP_ID] when only the
    Flatpak is installed, so callers can just do
    [exe, *prefix_args, "--app=<url>", ...].

    No "--" separator before the app's own args: flatpak run doesn't
    need one here (no ambiguity, our args come after the app id), and
    Chromium/Edge treats a literal "--" in its own argv as "stop parsing
    flags, treat everything after as URLs" -- flatpak forwards it
    straight through, so adding one here silently breaks every flag
    after it (confirmed on real hardware: --app/--start-fullscreen/etc.
    all opened as literal tabs instead of being parsed)."""
    for name in NATIVE_BINARY_NAMES:
        path = host_exec.which(name)
        if path:
            return path, []

    if _flatpak_edge_installed():
        suppress_first_run()
        return host_exec.which("flatpak"), ["run", FLATPAK_APP_ID]

    raise EdgeNotFoundError(INSTALL_INSTRUCTIONS)


def suppress_first_run():
    """Pre-seed the Flatpak Edge profile's first-run sentinel and FRE
    completion state so a kiosk shortcut isn't hijacked by either
    Chromium's base first-run wizard or Edge's own onboarding on top of
    it. Safe to call whenever we know the Flatpak Edge is installed;
    a no-op wherever the profile already has state (e.g. the user
    already ran Edge directly) -- we don't want to fight Chromium's own
    management of these files once they exist."""
    if not os.path.exists(FLATPAK_FIRST_RUN_PATH):
        os.makedirs(os.path.dirname(FLATPAK_FIRST_RUN_PATH), exist_ok=True)
        open(FLATPAK_FIRST_RUN_PATH, "a").close()

    if not os.path.exists(FLATPAK_LOCAL_STATE_PATH):
        os.makedirs(os.path.dirname(FLATPAK_LOCAL_STATE_PATH), exist_ok=True)
        seed = dict(_FRE_SEED)
        version = _get_flatpak_edge_version()
        if version:
            seed["browser"] = {
                "last_seen_whats_new_page_version": version,
                "browser_version_of_last_seen_whats_new": version,
            }
        with open(FLATPAK_LOCAL_STATE_PATH, "w") as f:
            json.dump(seed, f)
