"""Curated AppImage apps for the Apps tab's own AppImage source toggle
(see selfsteam_server.py's _apps_tab_panel_html) -- unlike Flathub
browsing (a real catalog with its own API, see flathub_browse.py),
there's no equivalent "browse every AppImage app" source, so this is a
small, hand-picked list, same spirit as standalone_emulators.py's own
EMULATORS dict but for general apps instead of emulators (no romfile,
just installed -> launch with no arguments).

Two real, different install mechanisms among the three entries here:
  "github" -- a real GitHub releases API + asset regex, same mechanism
    standalone_emulators.install_binary already uses for its own
    binary-install emulators (240-MP, Minigalaxy).
  "fixed_zip" -- a single fixed URL (itch.io's own "broth" auto-updater
    channel, confirmed real and always-latest via a live redirect) that
    serves a .zip containing the real AppImage inside, not the AppImage
    directly -- needs extracting after download (itch.io).

Functions:
  installed(app_id) -- whether this app's AppImage is on disk and executable.
  install(app_id) -- installs via whichever mechanism this entry declares.
  binary_path(app_id) -- where this app's AppImage lives once installed.
"""
import io
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile

APPS = {
    "io.itch.itch": {
        "name": "itch.io",
        # Real description, confirmed via itch.io/app's own <meta
        # name="description"> tag.
        "summary": "Download and play indie games, manage your library",
        "icon": "https://itch.io/favicon.ico",
        "homepage": "https://itch.io/app",
        "install_type": "fixed_zip",
        # Itch's own "broth" auto-updater channel -- confirmed live via
        # a real HTTP redirect (307 to a signed, short-lived download
        # URL) that this always resolves to whatever's currently
        # latest, same "no fixed version pinned in code" property
        # install_binary's own release-API approach has, just without
        # a GitHub releases API behind it.
        "download_url": "https://broth.itch.zone/install-itch/linux-appimage-amd64/LATEST/archive.zip",
    },
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


def _install_fixed_zip(app_id, entry):
    """Downloads entry's own fixed download_url (a .zip, not a bare
    AppImage) and extracts the one *.AppImage file inside it -- itch.io's
    own "broth" channel ships it this way, unlike a plain GitHub release
    asset."""
    req = urllib.request.Request(entry["download_url"], headers={"User-Agent": "SelfSteam"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        zip_bytes = resp.read()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        appimage_name = next((n for n in zf.namelist() if n.lower().endswith(".appimage")), None)
        if not appimage_name:
            raise RuntimeError(f"{entry['name']}: downloaded archive has no .AppImage inside it")
        dest_path = binary_path(app_id)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with zf.open(appimage_name) as src, open(dest_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
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
        elif entry["install_type"] == "fixed_zip":
            _install_fixed_zip(app_id, entry)
        else:
            raise NotImplementedError(f"install_type {entry['install_type']!r} not implemented")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"{entry['name']}: install failed: {e}")
