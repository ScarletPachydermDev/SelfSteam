#!/usr/bin/env python3
"""Stage 1 CLI: search SGDB, confirm match, download the 5 asset categories,
and (for local testing without Steam) register a .desktop entry so the icon
shows up in the app menu.
"""
import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import edge_launcher
import host_exec
import sgdb_client as sgdb
import shortcuts_vdf
import steam_paths

# XDG_CACHE_HOME, not a path next to the source files -- the latter
# resolves to /app/share/gridge/assets once packaged as a Flatpak,
# which is the read-only app installation dir; writes there would fail
# entirely. Flatpak automatically redirects XDG_CACHE_HOME to this
# app's own private, writable cache dir, same pattern config.py already
# uses for XDG_CONFIG_HOME.
ASSET_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "gridge", "assets")
APPLICATIONS_DIR = os.path.expanduser("~/.local/share/applications")

# Where the launcher lives when Steam itself is natively installed --
# native Steam has full host filesystem access, so pointing straight at
# wherever Gridge itself is installed works fine.
LOCAL_LAUNCH_WRAPPER = os.path.join(os.path.dirname(__file__), "launch-browser.sh")

# Flatpak Steam's sandbox does NOT get general home/host filesystem
# access by default (confirmed: its stock permissions only grant a
# handful of narrow XDG dirs like music/pictures, nothing that would
# cover wherever Gridge happens to be installed) -- so exec'ing
# LOCAL_LAUNCH_WRAPPER silently fails (Steam shows "Launching..." then
# reverts to "Play" with no error, no window, no process). The one path
# Flatpak Steam is guaranteed full access to is its own persistent data
# dir, so for a Flatpak Steam install we copy the launcher (and its
# sync_gamescope_resolution.py + vendored Xlib dependency) there instead.
FLATPAK_STEAM_DATA_DIR = os.path.expanduser("~/.var/app/com.valvesoftware.Steam")
FLATPAK_LAUNCHER_DIR = os.path.join(FLATPAK_STEAM_DATA_DIR, "gridge-launcher")
FLATPAK_LAUNCH_WRAPPER = os.path.join(FLATPAK_LAUNCHER_DIR, "launch-browser.sh")
_LAUNCHER_COPY_ITEMS = ["sync_gamescope_resolution.py", "vendor"]

# Relocating the wrapper into Steam's own sandbox-visible dir only gets
# it exec'd -- the browser command inside it (e.g. "/usr/bin/flatpak
# run com.microsoft.Edge ...", or even a native Edge binary path) still
# fails once Steam's sandbox tries to run it, since that sandbox has its
# own self-contained /usr with no view of the host's binaries at all
# (confirmed via Steam's own logs: "/usr/bin/flatpak: No such file or
# directory" even though that path is valid on the real host). This
# isn't Edge-specific: nothing outside Steam's narrow granted
# permissions is reachable, native or Flatpak alike. flatpak-spawn
# --host is the standard, always-available escape hatch bundled in
# every Flatpak sandbox specifically for running a command on the real
# host regardless of filesystem permissions -- confirmed present in
# Steam's own sandbox.
_FLATPAK_STEAM_LAUNCH_SCRIPT = """#!/bin/sh
unset LD_PRELOAD
python3 "$(dirname "$0")/sync_gamescope_resolution.py" 2>/dev/null
sleep 0.3
# --env forwards DISPLAY/WAYLAND_DISPLAY explicitly rather than relying
# on flatpak-spawn's default environment propagation, which isn't
# guaranteed to carry them across every Flatpak version -- getting this
# wrong reproduces the exact silent "nothing happens" failure this
# whole wrapper exists to avoid.
exec flatpak-spawn --host --env=DISPLAY="$DISPLAY" --env=WAYLAND_DISPLAY="$WAYLAND_DISPLAY" "$@"
"""


def is_gridge_launch_wrapper(exe):
    """True if exe is one of the launch-browser.sh paths Gridge itself
    creates shortcuts with -- used by export/import to find only
    shortcuts this tool created, never a user's own unrelated non-Steam
    shortcuts. Covers the native-Steam-root-relative case (see
    get_launch_wrapper_path) by directory name rather than a fixed
    constant, since the exact path depends on which native root was
    detected (~/.local/share/Steam vs ~/.steam/steam)."""
    if exe in (LOCAL_LAUNCH_WRAPPER, FLATPAK_LAUNCH_WRAPPER):
        return True
    return os.path.basename(exe) == "launch-browser.sh" and os.path.basename(os.path.dirname(exe)) == "gridge-launcher"


def _grant_steam_flatpak_spawn_permission():
    """flatpak-spawn --host needs D-Bus permission to talk to the
    org.freedesktop.Flatpak portal, which Steam's own Flathub manifest
    doesn't request by default (confirmed via Steam's own logs: "Portal
    call failed: ServiceUnknown ... --host only works when the Flatpak
    is allowed to talk to org.freedesktop.Flatpak" -- Valve never
    designed Steam's Flatpak build to spawn arbitrary host processes for
    non-Steam shortcuts). Gridge grants this itself (via flatpak-spawn
    --host if Gridge itself is sandboxed, direct otherwise) rather than
    requiring the user to run `flatpak override` manually. Idempotent;
    a no-op if already granted. Takes effect on Steam's next launch,
    not an already-running instance."""
    subprocess.run(
        host_exec.wrap(
            ["flatpak", "override", "--user", "com.valvesoftware.Steam", "--talk-name=org.freedesktop.Flatpak"]
        ),
        capture_output=True,
    )


def _copy_launcher(dest_dir, script_content=None):
    """Copy the wrapper + its runtime dependencies into dest_dir.
    script_content overrides launch-browser.sh's own content (used for
    the flatpak-spawn-wrapped Flatpak-Steam variant); None just copies
    the plain script unchanged (native Steam, which isn't sandboxed and
    so needs no escape hatch, regardless of whether Gridge itself is)."""
    os.makedirs(dest_dir, exist_ok=True)
    src_dir = os.path.dirname(__file__)
    for name in _LAUNCHER_COPY_ITEMS:
        src = os.path.join(src_dir, name)
        dest = os.path.join(dest_dir, name)
        if os.path.isdir(src):
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)

    wrapper_path = os.path.join(dest_dir, "launch-browser.sh")
    if script_content is not None:
        with open(wrapper_path, "w") as f:
            f.write(script_content)
    else:
        shutil.copy2(os.path.join(src_dir, "launch-browser.sh"), wrapper_path)
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


def get_launch_wrapper_path():
    """Return the launch-browser.sh path to use as this shortcut's exe.

    LOCAL_LAUNCH_WRAPPER (wherever Gridge's own source lives) only
    works when BOTH sides can see it: Gridge running unsandboxed
    (plain `python3 gui.py`, e.g. during development) AND Steam being
    native. Any other combination needs the wrapper relocated
    somewhere both sides can reach regardless of sandboxing:

    - Steam is Flatpak: its sandbox can't see anything outside its own
      persistent data dir (confirmed: narrow stock permissions, no
      general home access), so the wrapper goes there, wrapped with
      flatpak-spawn --host since that sandbox is what constrains the
      browser-launch command inside it.
    - Steam is native but Gridge itself is a packaged Flatpak: Gridge's
      own install location (e.g. /app/share/gridge) isn't a real host
      path at all once packaged -- it only exists inside Gridge's own
      mount namespace, so even native Steam's full host access can't
      see it (confirmed on real hardware: "No such file or directory"
      for a /app/share/gridge/launch-browser.sh exe). The wrapper goes
      inside Steam's own root instead, which Gridge can still write to
      (a plain home-relative path, covered by --filesystem=home) and
      native Steam already reads/writes freely with no sandbox of its
      own to route around -- so the plain, unwrapped script is enough.
    """
    try:
        steam_root = steam_paths.find_steam_root()
        using_flatpak_steam = steam_root == os.path.expanduser(steam_paths.FLATPAK_ROOT)
    except steam_paths.SteamNotFoundError:
        steam_root = None
        using_flatpak_steam = False

    if not host_exec.IN_FLATPAK and not using_flatpak_steam:
        return LOCAL_LAUNCH_WRAPPER

    if using_flatpak_steam:
        _grant_steam_flatpak_spawn_permission()
        return _copy_launcher(FLATPAK_LAUNCHER_DIR, script_content=_FLATPAK_STEAM_LAUNCH_SCRIPT)

    return _copy_launcher(os.path.join(steam_root, "gridge-launcher"))


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def clean_shortcut_name(name):
    """SGDB disambiguates streaming-site entries from unrelated games/shows
    with a trailing " (Website)" suffix -- strip it so the Steam shortcut
    just shows the plain app name. Case varies between entries (confirmed
    both "(Website)" and "(website)" in the wild), so match case-
    insensitively rather than assuming one casing."""
    suffix = " (website)"
    if name.lower().endswith(suffix):
        return name[: -len(suffix)]
    return name


def pick_match(name):
    matches = sgdb.search(name)
    if not matches:
        sys.exit(f"No SGDB matches found for '{name}'")

    print(f"\nSGDB matches for '{name}':")
    for i, m in enumerate(matches):
        tag = " (verified)" if m["verified"] else ""
        print(f"  [{i}] {m['name']}{tag} - id {m['id']}")

    if len(matches) == 1:
        choice = 0
    else:
        raw = input(f"Pick a match [0-{len(matches) - 1}] (default 0): ").strip()
        choice = int(raw) if raw else 0
    return matches[choice]


def _download_asset_url(url, basename, out_dir):
    """Download one artwork URL into out_dir/<basename><ext>, converting
    a .ico into a real .png first when possible (.ico isn't part of the
    freedesktop icon spec and renders blank in some app menus). Returns
    the local path."""
    ext = os.path.splitext(urlparse(url).path)[1] or ".png"
    filename = f"{basename}{ext}"
    dest = os.path.join(out_dir, filename)
    sgdb.download(url, dest)

    if ext == ".ico":
        with open(dest, "rb") as f:
            png_data = sgdb.extract_largest_png_from_ico(f.read())
        if png_data:
            os.remove(dest)
            filename = f"{basename}.png"
            dest = os.path.join(out_dir, filename)
            with open(dest, "wb") as f:
                f.write(png_data)

    print(f"  + {filename}  <-  {url}")
    return dest


def fetch_assets(game_id, slug):
    out_dir = os.path.join(ASSET_DIR, slug)
    os.makedirs(out_dir, exist_ok=True)

    fetchers = {
        "grid_vertical": sgdb.get_vertical_grid,
        "grid_horizontal": sgdb.get_horizontal_grid,
        "hero": sgdb.get_hero,
        "logo": sgdb.get_logo,
        "icon": sgdb.get_icon,
    }

    paths = {}
    for basename, fetch in fetchers.items():
        url = fetch(game_id)
        if not url:
            print(f"  ! no {basename} available on SGDB, skipping")
            continue
        paths[basename] = _download_asset_url(url, basename, out_dir)
    return paths


def download_selected_assets(slug, selections):
    """Download only the user-picked candidate per category from the
    artwork picker. selections is {basename: candidate_or_None}, each
    candidate being a raw SGDB entry dict with a "url" key. Categories
    left unpicked (or with no candidate at all) are simply skipped --
    same graceful degradation as fetch_assets(), a shortcut can always
    be created regardless of how much artwork was picked."""
    out_dir = os.path.join(ASSET_DIR, slug)
    os.makedirs(out_dir, exist_ok=True)

    paths = {}
    for basename, candidate in selections.items():
        if not candidate:
            continue
        paths[basename] = _download_asset_url(candidate["url"], basename, out_dir)
    return paths


GRID_FILENAMES = {
    "grid_vertical": "{appid}p{ext}",
    "grid_horizontal": "{appid}{ext}",
    "hero": "{appid}_hero{ext}",
    "logo": "{appid}_logo{ext}",
    "icon": "{appid}_icon{ext}",
}


# Google gates youtube.com/tv's D-pad-friendly "leanback" interface to
# requests that look like they're coming from a TV/console app, redirecting
# regular desktop browsers back to the normal site. Spoofing Edge's whole
# user-agent works because each shortcut gets its own dedicated Edge
# instance anyway -- there's no other traffic in that process that a
# TV-flavored UA could mess up. UA string matches the one used by
# github.com/angeloanan/youtube-tv-browser, a maintained extension doing
# the same spoof.
YOUTUBE_TV_URL = "https://www.youtube.com/tv"
YOUTUBE_TV_USER_AGENT = (
    "Mozilla/5.0 (SMART-TV; LINUX; Tizen 7.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) 94.0.4606.31/7.0 TV Safari/537.36"
)


def register_steam_shortcut(name, url, asset_paths, user_id=None, couch_mode=False):
    """Copy fetched assets into Steam's grid folder and add/update a
    non-Steam shortcut entry in shortcuts.vdf. Returns the appid."""
    if couch_mode:
        url = YOUTUBE_TV_URL

    edge_exe, edge_prefix_args = edge_launcher.find_edge()

    userdata_dir = steam_paths.find_userdata_dir(user_id)
    grid_dir = os.path.join(userdata_dir, "config", "grid")
    os.makedirs(grid_dir, exist_ok=True)

    launch_wrapper = get_launch_wrapper_path()
    appid = shortcuts_vdf.generate_appid(launch_wrapper, name)

    icon_dest = None
    for basename, src in asset_paths.items():
        if basename not in GRID_FILENAMES:
            continue
        ext = os.path.splitext(src)[1]
        dest = os.path.join(grid_dir, GRID_FILENAMES[basename].format(appid=appid, ext=ext))
        shutil.copy2(src, dest)
        print(f"  + {os.path.basename(dest)}  <-  {src}")
        if basename == "icon":
            icon_dest = dest

    # No --profile-directory/--user-data-dir: use Edge's own default
    # profile, shared with the user's regular Edge browsing, so logins
    # already saved there (Netflix, Disney+, etc.) just work without a
    # separate sign-in per shortcut.
    edge_args = [
        edge_exe,
        *edge_prefix_args,
        f"--app={url}",
        "--kiosk",
        "--start-fullscreen",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if couch_mode:
        # LaunchOptions is stored/parsed as one shell-like string, and the
        # TV user-agent has spaces/parens/semicolons in it -- unquoted, it
        # gets word-split into several bogus arguments (confirmed: Edge
        # then fails to start at all, so Steam's Play button just resets
        # with nothing visibly happening).
        edge_args.append(shlex.quote(f"--user-agent={YOUTUBE_TV_USER_AGENT}"))

    vdf_path = os.path.join(userdata_dir, "config", "shortcuts.vdf")
    written_appid, stale_appids = shortcuts_vdf.add_shortcut(
        vdf_path,
        appname=name,
        exe=launch_wrapper,
        start_dir=os.path.dirname(launch_wrapper) + "/",
        icon=icon_dest or "",
        launch_options=" ".join(edge_args),
        allow_overlay=False,
    )
    assert written_appid == appid

    for stale_appid in stale_appids:
        for f in os.listdir(grid_dir):
            if f.startswith(str(stale_appid)):
                os.remove(os.path.join(grid_dir, f))
                print(f"  - removed stale {f}")

    print(f"\nAdded/updated Steam shortcut '{name}' (appid {appid}) in {vdf_path}")
    print("Restart Steam (fully quit, not just close the window) to see it.")
    return appid


def register_test_desktop_entry(name, slug, url, icon_path):
    """Add a .desktop file to the app menu so we can visually confirm the
    icon/artwork pipeline without needing Steam installed."""
    os.makedirs(APPLICATIONS_DIR, exist_ok=True)
    desktop_path = os.path.join(APPLICATIONS_DIR, f"webapp-test-{slug}.desktop")
    icon_field = icon_path or ""
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name} (webapp test)\n"
        f"Comment=Steam webapp creator test entry for {url}\n"
        f"Icon={icon_field}\n"
        f'Exec=xdg-open "{url}"\n'
        "Terminal=false\n"
        "Categories=Network;\n"
    )
    with open(desktop_path, "w") as f:
        f.write(content)
    os.chmod(desktop_path, 0o755)
    print(f"\nRegistered test app menu entry: {desktop_path}")

    update_db = "/usr/bin/update-desktop-database"
    if os.path.exists(update_db):
        os.system(f'"{update_db}" "{APPLICATIONS_DIR}" >/dev/null 2>&1')


def main():
    parser = argparse.ArgumentParser(description="Search SGDB, fetch assets, add a Steam shortcut")
    parser.add_argument("name", help="App name to search on SteamGridDB, e.g. Netflix")
    parser.add_argument("url", help="URL the webapp should open, e.g. https://netflix.com")
    parser.add_argument("--steam-user", help="Steam user id, only needed if you have more than one")
    parser.add_argument(
        "--desktop-only", action="store_true",
        help="Skip Steam integration and just register a test .desktop entry",
    )
    args = parser.parse_args()

    match = pick_match(args.name)
    match["name"] = clean_shortcut_name(match["name"])
    slug = slugify(match["name"])
    print(f"\nFetching assets for '{match['name']}' (SGDB id {match['id']})...")
    paths = fetch_assets(match["id"], slug)

    if not args.desktop_only:
        try:
            register_steam_shortcut(match["name"], args.url, paths, args.steam_user)
            return
        except steam_paths.SteamNotFoundError as e:
            print(f"\n! Steam not found ({e}), falling back to test .desktop entry")
        except edge_launcher.EdgeNotFoundError as e:
            print(f"\n! {e}\nFalling back to test .desktop entry")

    icon_path = paths.get("icon") or paths.get("grid_vertical")
    register_test_desktop_entry(match["name"], slug, args.url, icon_path)


if __name__ == "__main__":
    main()
