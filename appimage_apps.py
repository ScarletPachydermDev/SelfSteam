"""Curated AppImage apps for the Apps tab's own AppImage source toggle
(see selfsteam_server.py's _apps_tab_panel_html) -- unlike Flathub
browsing (a real catalog with its own API, see flathub_browse.py),
there's no equivalent "browse every AppImage app" source, so this is a
small, hand-picked list, same spirit as standalone_emulators.py's own
EMULATORS dict but for general apps instead of emulators (no romfile,
just installed -> launch with no arguments). itch.io was removed from
here (2026-08-25) once it turned out to already be a real, installable
Flathub app in its own right (io.itch.itch on Flathub) -- no reason to
offer it a second time through a separate, less-standard AppImage
install path when Flathub already covers it.

Only one install mechanism among the two entries here: "github" -- a
real GitHub releases API + asset regex, same mechanism
standalone_emulators.install_binary already uses for its own
binary-install emulators.

Functions:
  installed(app_id) -- whether this app's AppImage is on disk and executable.
  install(app_id) -- installs via whichever mechanism this entry declares.
  binary_path(app_id) -- where this app's AppImage lives once installed.
"""
import json
import os
import re
import shutil
import urllib.error
import urllib.request

APPS = {
    "com.anthonycaccese.240MP": {
        "name": "240-MP",
        # Real description, confirmed via the repo's own GitHub
        # "description" field (not guessed) -- a media/CRT frontend,
        # not a generic game launcher.
        "summary": "Retro VCR-styled media frontend for CRT displays, SteamOS and Raspberry Pi",
        "icon": "https://raw.githubusercontent.com/anthonycaccese/240-MP/main/assets/images/logo.svg",
        "homepage": "https://github.com/anthonycaccese/240-MP",
        "install_type": "github",
        "release_api": "https://api.github.com/repos/anthonycaccese/240-MP/releases?per_page=1",
        "binary_asset_re": re.compile(r"^240-MP-linux-x86_64\.AppImage$"),
    },
    "io.github.sharkwouter.Minigalaxy": {
        "name": "Minigalaxy",
        # Real description, confirmed via the repo's own GitHub
        # "description" field.
        "summary": "A simple GOG client for Linux",
        "icon": "https://raw.githubusercontent.com/sharkwouter/minigalaxy/master/data/icons/128x128/io.github.sharkwouter.Minigalaxy.png",
        "homepage": "https://github.com/sharkwouter/minigalaxy",
        "install_type": "github",
        "release_api": "https://api.github.com/repos/sharkwouter/minigalaxy/releases?per_page=1",
        "binary_asset_re": re.compile(r"^Minigalaxy-.*-x86_64\.AppImage$"),
    },
}

# Same layout convention as standalone_emulators._binary_dir -- a
# per-app subdirectory under the shared appimages root, one real
# AppImage file inside it.
_APPIMAGE_APPS_DIR = os.path.join(
    os.path.expanduser("~"), ".local", "share", "selfsteam", "appimage-apps",
)


def binary_path(app_id):
    return os.path.join(_APPIMAGE_APPS_DIR, app_id, f"{app_id}.AppImage")


def installed(app_id):
    if app_id not in APPS:
        return False
    path = binary_path(app_id)
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _install_github(app_id, entry):
    """Same mechanism as standalone_emulators.install_binary -- fetch
    the latest release fresh from the entry's own release API, match
    the one asset meant for this machine, download it, chmod +x."""
    req = urllib.request.Request(entry["release_api"], headers={"User-Agent": "SelfSteam"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.load(resp)
    if not releases:
        raise RuntimeError(f"{entry['name']}: release API returned no releases")
    release = releases[0]
    asset_re = entry["binary_asset_re"]
    match = next((a for a in release["assets"] if asset_re.match(a["name"])), None)
    if not match:
        raise RuntimeError(f"{entry['name']} {release.get('tag_name', '?')}: no asset matching {asset_re.pattern!r}")
    dest_path = binary_path(app_id)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    dl_req = urllib.request.Request(match["browser_download_url"], headers={"User-Agent": "SelfSteam"})
    with urllib.request.urlopen(dl_req, timeout=300) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    os.chmod(dest_path, 0o755)


def uninstall(app_id):
    """Removes this app's own AppImage (and its containing per-app
    directory, same layout install() writes into) -- the Apps tab's own
    Remove button, same idea as standalone_emulators.uninstall_flathub_app_id
    but for a bare on-disk file instead of a real flatpak uninstall."""
    path = binary_path(app_id)
    if os.path.isfile(path):
        os.remove(path)
    app_dir = os.path.dirname(path)
    if os.path.isdir(app_dir) and not os.listdir(app_dir):
        os.rmdir(app_dir)


def install(app_id):
    entry = APPS.get(app_id)
    if not entry:
        raise ValueError(f"No known AppImage app: {app_id}")
    try:
        if entry["install_type"] == "github":
            _install_github(app_id, entry)
        else:
            raise NotImplementedError(f"install_type {entry['install_type']!r} not implemented")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"{entry['name']}: install failed: {e}")
