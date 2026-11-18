"""Curated apps for the Apps tab's "Flatpaks" source: apps that ship a
.flatpak bundle on their own GitHub releases instead of being on
Flathub. Same hand-picked shape as appimage_apps.APPS.

A bundle installs as an ordinary user flatpak (`flatpak install --user
<file>`), pulling its runtime from Flathub through the runtime-repo the
bundle names, so once installed it is checked, launched (`flatpak run
<app_id>`) and removed exactly like a Flathub app. Each key is the
bundle's real app ID, read from the bundle itself.

Functions:
  installed(app_id) -- whether this app is installed as a flatpak.
  install(app_id) -- downloads the newest release's bundle and installs it.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import host_exec
import standalone_emulators

_DVP_ART = "https://raw.githubusercontent.com/nicocarbone/DeckVideoPlayer/main/packaging/steam/"

APPS = {
    "com.monodivision.archivereader": {
        "name": "Archivist",
        # Its repo has no description of its own beyond "Repo for Linux
        # flatpak"; this says what the app does (an archive.org browser
        # and player, per its own screenshots).
        "summary": "Browse, search and play archive.org videos, audio and texts",
        # The repo hosts no icon of its own; this is the 128px icon from
        # its Linux release, kept in vendor/.
        "icon": "/vendor/app-icons/com.monodivision.archivereader.png",
        "homepage": "https://github.com/boblothian/archivist-browser",
        "release_api": "https://api.github.com/repos/boblothian/archivist-browser/releases?per_page=5",
        "bundle_asset_re": re.compile(r"^archivist-browser-[\d.]+\.flatpak$"),
    },
    "io.github.nicocarbone.DeckVideoPlayer": {
        "name": "Deck Video Player",
        "summary": "Video player for the Steam Deck",
        "icon": "https://raw.githubusercontent.com/nicocarbone/DeckVideoPlayer/main/packaging/icons/io.github.nicocarbone.DeckVideoPlayer-256.png",
        "homepage": "https://github.com/nicocarbone/DeckVideoPlayer",
        "release_api": "https://api.github.com/repos/nicocarbone/DeckVideoPlayer/releases?per_page=5",
        "bundle_asset_re": re.compile(r"^DeckVideoPlayer\.flatpak$"),
        # The project ships its own Steam library artwork; used ahead of
        # SteamGridDB's (see selfsteam_server's apps artwork lookup).
        "steam_artwork": {
            "grid_vertical": _DVP_ART + "capsule.png",
            "grid_horizontal": _DVP_ART + "header.png",
            "hero": _DVP_ART + "hero.png",
            "logo": _DVP_ART + "logo.png",
            "icon": _DVP_ART + "icon.png",
        },
    },
}

_INSTALL_TIMEOUT_SECONDS = 900


def installed(app_id):
    return app_id in APPS and standalone_emulators.flathub_app_id_installed(app_id)


def _newest_bundle_url(entry):
    req = urllib.request.Request(entry["release_api"], headers={"User-Agent": "SelfSteam"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.load(resp)
    if isinstance(releases, dict):
        releases = [releases]
    for release in releases:
        if release.get("prerelease") or release.get("draft"):
            continue
        for asset in release.get("assets", ()):
            if entry["bundle_asset_re"].match(asset["name"]):
                return asset["browser_download_url"]
    raise RuntimeError(f"{entry['name']}: no release has a .flatpak bundle")


def install(app_id):
    entry = APPS.get(app_id)
    if not entry:
        raise ValueError(f"No known Flatpak bundle app: {app_id}")
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        raise RuntimeError("flatpak isn't available on this host")
    # The runtime comes from Flathub, so make sure that remote exists.
    standalone_emulators._ensure_flathub_remote(flatpak)
    try:
        url = _newest_bundle_url(entry)
        # Downloaded into the real home (not /tmp), since the host's
        # flatpak, run outside this sandbox, has to read the file.
        bundle_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "selfsteam", "flatpak-bundles")
        os.makedirs(bundle_dir, exist_ok=True)
        fd, bundle = tempfile.mkstemp(suffix=".flatpak", dir=bundle_dir)
        try:
            dl = urllib.request.Request(url, headers={"User-Agent": "SelfSteam"})
            with urllib.request.urlopen(dl, timeout=300) as resp, os.fdopen(fd, "wb") as f:
                shutil.copyfileobj(resp, f)
            result = subprocess.run(
                host_exec.wrap([flatpak, "install", "--user", "-y", "--noninteractive", "--reinstall", bundle]),
                capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
            )
        finally:
            if os.path.exists(bundle):
                os.remove(bundle)
    except (urllib.error.URLError, OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{entry['name']}: install failed: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(f"{entry['name']}: flatpak install failed: "
                           f"{result.stderr.strip() or result.stdout.strip()}")
