#!/usr/bin/env python3
"""Stage 1 CLI: search SGDB, confirm match, download the 5 asset categories,
and (for local testing without Steam) register a .desktop entry so the icon
shows up in the app menu.
"""
import argparse
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import browser_launcher
import edge_launcher
import host_exec
import retroarch_cores
import sgdb_client as sgdb
import shortcuts_vdf
import appimage_apps
import standalone_emulators
import steam_paths

# Functions:
#   is_gridge_launch_wrapper(exe) -- True if exe is one of this tool's own launch-browser.sh paths.
#   _grant_steam_flatpak_spawn_permission() -- lets Steam's own flatpak-spawn talk to the host.
#   _copy_launcher(dest_dir) -- copies the launch wrapper + its runtime deps into dest_dir.
#   get_launch_wrapper_path() -- the launch-browser.sh path to use as a shortcut's exe.
#   slugify(name) -- filesystem-safe slug for a shortcut name.
#   clean_shortcut_name(name) -- strips SGDB's own category-tag suffix from a match name.
#   pick_match(matches, index) -- the chosen match dict, or a synthetic fallback.
#   _download_asset_url(url, dest) -- downloads one artwork URL to disk.
#   fetch_assets(game_id) -- downloads all 5 artwork categories for an SGDB game id.
#   download_selected_assets(slug, selections) -- downloads only the user-picked candidates.
#   register_steam_shortcut(name, url, asset_paths, ...) -- writes the shortcut + copies its artwork.
#   register_custom_shortcut(name, target, start_dir, launch_options, asset_paths, ...) -- same, for a raw Target/Start In/Launch Options shortcut.
#   _field(entry, *names) -- reads a shortcuts.vdf entry field trying several key casings.
#   _extract_launch_url(launch_options) -- pulls a URL-tab shortcut's target URL back out.
#   _extract_retroarch_info(launch_options) -- pulls (console, romfile) back out of an RA shortcut.
#   _extract_standalone_emulator_info(launch_options) -- pulls (emulator_name, romfile) back out.
#   find_grid_image_path(appid, grid_dir) -- the vertical-grid image file for appid.
#   find_grid_image_for_appid(appid) -- same, searching every Steam user's own grid dir.
#   thumbnail_for_appid(appid) -- a small cached webp copy of appid's vertical grid image.
#   list_gridge_shortcuts() -- every non-Steam shortcut across all Steam profiles, ours and foreign alike.
#   remove_gridge_shortcut(appid) -- removes a shortcut this tool created, and its grid assets.
#   register_test_desktop_entry() -- adds a .desktop file for local testing without Steam.
#   main() -- Stage 1 CLI entrypoint.

# XDG_CACHE_HOME, not a path next to the source files -- the latter
# resolves to /app/share/selfsteam/assets once packaged as a Flatpak,
# which is the read-only app installation dir; writes there would fail
# entirely. Flatpak automatically redirects XDG_CACHE_HOME to this
# app's own private, writable cache dir, same pattern config.py already
# uses for XDG_CONFIG_HOME. No migration from the old ~/.cache/gridge/
# dir this app used before its SelfSteam rename -- unlike config.py's
# own settings, everything here is a lossy, cheaply-regenerated
# derivative (SGDB artwork downloads, grid thumbnails), not real user
# data worth carrying forward.
ASSET_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "selfsteam", "assets")
APPLICATIONS_DIR = os.path.expanduser("~/.local/share/applications")

# Same cache root as ASSET_DIR, own subfolder -- these are small, lossy
# derivatives of Steam's own grid images (see thumbnail_for_appid),
# never the real artwork Steam itself displays, so they live entirely
# separate from anything Steam reads.
GRID_THUMB_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "selfsteam", "grid-thumbs")
# 2x the poster's own CSS display width (166px, see .poster-art) --
# sharp on HiDPI without hauling in SGDB's original, which routinely
# ships 600x900 (or larger "featured" variants well past that) for what
# the gallery only ever shows at postage-stamp size.
_GRID_THUMB_WIDTH = 332

# Where the launcher lives when Steam itself is natively installed --
# native Steam has full host filesystem access, so pointing straight at
# wherever SelfSteam itself is installed works fine.
LOCAL_LAUNCH_WRAPPER = os.path.join(os.path.dirname(__file__), "launch-browser.sh")

# Flatpak Steam's sandbox does NOT get general home/host filesystem
# access by default (confirmed: its stock permissions only grant a
# handful of narrow XDG dirs like music/pictures, nothing that would
# cover wherever SelfSteam happens to be installed) -- so exec'ing
# LOCAL_LAUNCH_WRAPPER silently fails (Steam shows "Launching..." then
# reverts to "Play" with no error, no window, no process). The one path
# Flatpak Steam is guaranteed full access to is its own persistent data
# dir, so for a Flatpak Steam install we copy the launcher (and its
# sync_gamescope_resolution.py + vendored Xlib dependency) there instead.
FLATPAK_STEAM_DATA_DIR = os.path.expanduser("~/.var/app/com.valvesoftware.Steam")
# "selfsteam-launcher" going forward -- but is_gridge_launch_wrapper below
# still has to recognize the old "gridge-launcher" name too. Shortcuts
# created before this app's SelfSteam rename have that literal old path
# baked into their exe field in Steam's own shortcuts.vdf; nothing here
# rewrites those, so the old directory has to keep existing and keep
# being recognized as "ours" for as long as any such shortcut is still
# around, or listing/removing them in the UI would silently stop working.
FLATPAK_LAUNCHER_DIR = os.path.join(FLATPAK_STEAM_DATA_DIR, "selfsteam-launcher")
_OLD_FLATPAK_LAUNCHER_DIRNAME = "gridge-launcher"
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
#
# WAYLAND_DISPLAY only forwarded when actually set -- confirmed live
# (2026-08-27) that Flatpak Steam's own process has DISPLAY set but no
# WAYLAND_DISPLAY at all, and forwarding it anyway as a literal empty
# string (not simply omitting it) makes a Chromium-based browser's own
# ozone-platform auto-detection think Wayland IS available, try to
# connect, fail ("Connection refused"), and exit outright instead of
# falling back to X11 -- "The platform failed to initialize. Exiting.",
# no window, no error visible to the user, exactly the silent failure
# this wrapper exists to avoid. Firefox's own GTK backend tolerates the
# empty value fine, which is why this only ever showed up on Chromium-
# family browsers (Edge, Opera), not Firefox.
if [ -n "$WAYLAND_DISPLAY" ]; then
    exec flatpak-spawn --host --env=DISPLAY="$DISPLAY" --env=WAYLAND_DISPLAY="$WAYLAND_DISPLAY" "$@"
else
    exec flatpak-spawn --host --env=DISPLAY="$DISPLAY" "$@"
fi
"""


def is_gridge_launch_wrapper(exe):
    """True if exe is one of the launch-browser.sh paths this tool
    creates shortcuts with -- used by export/import to find only
    shortcuts this tool created, never a user's own unrelated non-Steam
    shortcuts. Covers the native-Steam-root-relative case (see
    get_launch_wrapper_path) by directory name rather than a fixed
    constant, since the exact path depends on which native root was
    detected (~/.local/share/Steam vs ~/.steam/steam). Matches the old
    "gridge-launcher" dirname alongside the current "selfsteam-launcher"
    one -- see FLATPAK_LAUNCHER_DIR's own comment for why that has to
    stay recognized indefinitely, not just during a transition window."""
    if exe in (LOCAL_LAUNCH_WRAPPER, FLATPAK_LAUNCH_WRAPPER):
        return True
    if os.path.basename(exe) != "launch-browser.sh":
        return False
    dirname = os.path.basename(os.path.dirname(exe))
    return dirname in ("selfsteam-launcher", _OLD_FLATPAK_LAUNCHER_DIRNAME)


def _grant_steam_flatpak_spawn_permission():
    """flatpak-spawn --host needs D-Bus permission to talk to the
    org.freedesktop.Flatpak portal, which Steam's own Flathub manifest
    doesn't request by default (confirmed via Steam's own logs: "Portal
    call failed: ServiceUnknown ... --host only works when the Flatpak
    is allowed to talk to org.freedesktop.Flatpak" -- Valve never
    designed Steam's Flatpak build to spawn arbitrary host processes for
    non-Steam shortcuts). SelfSteam grants this itself (via flatpak-spawn
    --host if SelfSteam itself is sandboxed, direct otherwise) rather than
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
    so needs no escape hatch, regardless of whether SelfSteam itself is)."""
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

    LOCAL_LAUNCH_WRAPPER (wherever SelfSteam's own source lives) only
    works when BOTH sides can see it: SelfSteam running unsandboxed
    (plain `python3 selfsteam_server.py`, e.g. during development) AND
    Steam being native. Any other combination needs the wrapper
    relocated somewhere both sides can reach regardless of sandboxing:

    - Steam is Flatpak: its sandbox can't see anything outside its own
      persistent data dir (confirmed: narrow stock permissions, no
      general home access), so the wrapper goes there, wrapped with
      flatpak-spawn --host since that sandbox is what constrains the
      browser-launch command inside it.
    - Steam is native but SelfSteam itself is a packaged Flatpak:
      SelfSteam's own install location (e.g. /app/share/selfsteam) isn't
      a real host path at all once packaged -- it only exists inside
      SelfSteam's own mount namespace, so even native Steam's full host
      access can't see it (confirmed on real hardware: "No such file or
      directory" for a /app/share/gridge/launch-browser.sh exe, the
      equivalent path under this project's old name). The wrapper goes
      inside Steam's own root instead, which SelfSteam can still write
      to (a plain home-relative path, covered by --filesystem=home) and
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

    return _copy_launcher(os.path.join(steam_root, "selfsteam-launcher"))


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


def build_browser_launch_args(url, couch_mode, browser_app_id=None):
    """The real argv for launching browser_app_id in kiosk mode against
    url -- factored out of register_steam_shortcut so a caller that
    needs to resolve/validate this BEFORE queuing a shortcut (the URL
    tab's own /add handler, so a browser that needs installing -- or
    fails to install/launch-build -- is caught right at Create time
    instead of silently deferring to the next "Save Changes and Restart
    Steam" commit) can reuse the exact same dispatch logic.

    browser_app_id picks which installed Flatpak browser the shortcut
    launches -- None or Edge's own id keeps using edge_launcher.py
    (proven, has its own extra first-run/onboarding suppression beyond
    plain kiosk flags); anything else goes through browser_launcher.py,
    which only covers browsers confirmed to actually work in kiosk mode
    (see its own docstring) rather than guessing flags for an untested
    one."""
    if couch_mode:
        url = YOUTUBE_TV_URL

    if not browser_app_id or browser_app_id == edge_launcher.FLATPAK_APP_ID:
        edge_exe, edge_prefix_args = edge_launcher.find_edge()
        # No --profile-directory/--user-data-dir: use Edge's own default
        # profile, shared with the user's regular Edge browsing, so
        # logins already saved there (Netflix, Disney+, etc.) just work
        # without a separate sign-in per shortcut.
        args = [
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
            # LaunchOptions is stored/parsed as one shell-like string,
            # and the TV user-agent has spaces/parens/semicolons in it --
            # unquoted, it gets word-split into several bogus arguments
            # (confirmed: Edge then fails to start at all, so Steam's
            # Play button just resets with nothing visibly happening).
            args.append(shlex.quote(f"--user-agent={YOUTUBE_TV_USER_AGENT}"))
        return args

    # Already shell-quoted where needed -- browser_launcher.py owns that
    # decision since it knows which element (if any) needs it per
    # browser family, unlike here.
    return browser_launcher.kiosk_launch_args(browser_app_id, url, couch_mode, YOUTUBE_TV_USER_AGENT)


def register_steam_shortcut(name, url, asset_paths, user_id=None, couch_mode=False, browser_app_id=None,
                             launch_args=None, steam_input_enabled=None):
    """Copy fetched assets into Steam's grid folder and add/update a
    non-Steam shortcut entry in shortcuts.vdf. Returns the appid.

    launch_args bypasses url/browser_app_id/build_browser_launch_args
    entirely when given -- an already-built, already-quoted argv (e.g.
    from retroarch_cores.launch_args, or the URL tab's own /add handler
    calling build_browser_launch_args itself ahead of time) for
    shortcuts that aren't resolved fresh here. get_launch_wrapper_path()'s
    own wrapper script is generic (just re-execs whatever LaunchOptions
    it's given on the host), so this needs no separate wrapper of its
    own.

    steam_input_enabled (True/False, or None to leave it alone) sets
    localconfig.vdf's own per-app Steam Input override for this exact
    appid right after it's assigned -- see set_steam_input_enabled's
    own docstring. Currently only Ryubing-preflight shortcuts ever pass
    this (preflight needs Steam Input ON to tell same-model controllers
    apart -- see ryu-preflight's own README)."""
    browser_args = launch_args if launch_args is not None else build_browser_launch_args(url, couch_mode, browser_app_id)

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

    vdf_path = os.path.join(userdata_dir, "config", "shortcuts.vdf")
    written_appid, stale_appids = shortcuts_vdf.add_shortcut(
        vdf_path,
        appname=name,
        exe=launch_wrapper,
        start_dir=os.path.dirname(launch_wrapper) + "/",
        icon=icon_dest or "",
        launch_options=" ".join(browser_args),
        allow_overlay=False,
    )
    assert written_appid == appid

    for stale_appid in stale_appids:
        for f in os.listdir(grid_dir):
            if f.startswith(str(stale_appid)):
                os.remove(os.path.join(grid_dir, f))
                print(f"  - removed stale {f}")

    if steam_input_enabled is not None:
        set_steam_input_enabled(appid, steam_input_enabled, user_id=user_id)

    print(f"\nAdded/updated Steam shortcut '{name}' (appid {appid}) in {vdf_path}")
    print("Restart Steam (fully quit, not just close the window) to see it.")
    return appid


def set_steam_input_enabled(appid, enabled, user_id=None):
    """Sets Steam's own per-shortcut "Enable Steam Input" override --
    UserLocalConfigStore/apps/<appid>/UseSteamControllerConfig in
    localconfig.vdf's real TEXT-VDF format (Valve's older plain-text
    key-value format -- a different, unrelated file/format from
    shortcuts.vdf's own binary one, and one this project has never
    touched before). Confirmed live (2026-09-02) by diffing a real
    localconfig.vdf before and after manually toggling this exact
    setting on a real shortcut in Steam's own UI: the value is "2" for
    enabled and "0" for disabled -- not a plain 0/1 boolean, there's a
    third state (the key simply being absent) for "never touched, use
    Steam's own default."

    A targeted line-level patch, not a full parse/serialize round trip
    -- same "hand-rolled targeted edit, not a full parser" reasoning as
    standalone_emulators.py's own _toml_set_in_section: this file is
    large, actively rewritten by Steam itself, and full of escaped JSON
    blobs as string values a naive generic text-VDF writer could easily
    reformat or corrupt. localconfig.vdf actually has *two* "apps"
    sections -- this one, a direct child of the file's own root
    UserLocalConfigStore map (one leading tab), and an unrelated one
    four tabs deep under Software/Valve/Steam holding badge/playtime
    data -- told apart here by indentation depth alone (^\\t"apps"$,
    anchored, MULTILINE), confirmed against a real 1973-line copy of
    this exact file rather than assumed from a text-VDF spec. Backs up
    to .bak first, same safety net shortcuts_vdf.save() already gives
    shortcuts.vdf. No-ops (returns False) if localconfig.vdf doesn't
    exist yet for this user."""
    userdata_dir = steam_paths.find_userdata_dir(user_id)
    path = os.path.join(userdata_dir, "config", "localconfig.vdf")
    if not os.path.isfile(path):
        return False

    with open(path, encoding="utf-8", newline="") as f:
        content = f.read()

    value = "2" if enabled else "0"
    appid = str(appid)
    key_line = f'\t\t\t"UseSteamControllerConfig"\t\t"{value}"\n'

    apps_re = re.compile(r'^\t"apps"\n\t\{\n(.*?)^\t\}\n', re.DOTALL | re.MULTILINE)
    apps_match = apps_re.search(content)

    if not apps_match:
        # No top-level "apps" section at all yet (no shortcut's own
        # controller settings have ever been touched on this machine) --
        # insert one right before the file's own final closing brace,
        # the one that closes UserLocalConfigStore itself.
        new_section = f'\t"apps"\n\t{{\n\t\t"{appid}"\n\t\t{{\n{key_line}\t\t}}\n\t}}\n'
        idx = content.rstrip().rfind("}")
        new_content = content[:idx] + new_section + content[idx:]
    else:
        apps_body = apps_match.group(1)
        appid_re = re.compile(r'^\t\t"' + re.escape(appid) + r'"\n\t\t\{\n(.*?)^\t\t\}\n', re.DOTALL | re.MULTILINE)
        appid_match = appid_re.search(apps_body)
        if appid_match:
            appid_body = appid_match.group(1)
            key_re = re.compile(r'^\t\t\t"UseSteamControllerConfig"\s*"[^"]*"\n', re.MULTILINE)
            if key_re.search(appid_body):
                new_appid_body = key_re.sub(key_line, appid_body, count=1)
            else:
                new_appid_body = appid_body + key_line
            new_apps_body = apps_body[:appid_match.start(1)] + new_appid_body + apps_body[appid_match.end(1):]
        else:
            new_apps_body = apps_body + f'\t\t"{appid}"\n\t\t{{\n{key_line}\t\t}}\n'
        new_apps_block = f'\t"apps"\n\t{{\n{new_apps_body}\t}}\n'
        new_content = content[:apps_match.start()] + new_apps_block + content[apps_match.end():]

    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_content)
    return True


def register_custom_shortcut(name, target, start_dir, launch_options, asset_paths, user_id=None):
    """Like register_steam_shortcut, but for the /custom page: writes
    target/start_dir/launch_options straight into shortcuts.vdf as
    given, rather than synthesizing them from a URL/browser-wrapper or
    RetroArch/emulator launch_args. This is the path that lets a
    foreign shortcut (one the user made in Steam directly, or a
    pre-rename SelfSteam build's exe path this version's
    is_gridge_launch_wrapper doesn't recognize) get edited here at all
    -- see list_gridge_shortcuts' own docstring. allow_overlay=True
    (Steam's own default, unlike the browser/emulator tabs which force
    it off for their own kiosk/fullscreen reasons) since this is
    arbitrary user software with no reason to assume otherwise."""
    appid = shortcuts_vdf.generate_appid(target, name)

    userdata_dir = steam_paths.find_userdata_dir(user_id)
    grid_dir = os.path.join(userdata_dir, "config", "grid")
    os.makedirs(grid_dir, exist_ok=True)

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

    vdf_path = os.path.join(userdata_dir, "config", "shortcuts.vdf")
    written_appid, stale_appids = shortcuts_vdf.add_shortcut(
        vdf_path,
        appname=name,
        exe=target,
        start_dir=start_dir,
        icon=icon_dest or "",
        launch_options=launch_options,
        allow_overlay=True,
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


def _field(entry, *names):
    """Reads a shortcuts.vdf entry field trying several key casings --
    Steam rewrites the whole file with its own casing (confirmed:
    "appname" becomes "AppName") every time it starts, and add_shortcut's
    own dedup logic already has to account for exactly this. Applying
    the same defensive lookup to every field read here, not just
    appname, since which fields Steam's own rewrite touches isn't
    something to assume without seeing it happen to each one."""
    for name in names:
        if name in entry:
            return entry[name]
    return None


def _extract_launch_url(launch_options):
    """Pulls the target URL back out of a shortcut's own LaunchOptions
    string -- Chromium-family shortcuts carry it as --app=<url>;
    Firefox-family ones (see browser_launcher.py) have no --app=
    equivalent, so it's just the last bare http(s) token instead."""
    try:
        tokens = shlex.split(launch_options)
    except ValueError:
        tokens = launch_options.split()
    for tok in tokens:
        if tok.startswith("--app="):
            return tok[len("--app="):]
    for tok in reversed(tokens):
        if tok.startswith(("http://", "https://")):
            return tok
    return None


def _extract_retroarch_info(launch_options):
    """Pulls (console, romfile) back out of a RetroArch shortcut's own
    LaunchOptions (see retroarch_cores.launch_args) so the RetroArch
    tab's Edit link can jump straight back into it, same as
    _extract_launch_url already does for URL-tab shortcuts. The core
    path is reverse-mapped to a console name via retroarch_cores' own
    table -- several consoles share one core (Game Boy/Game Boy Color
    both use gambatte), so this is best-effort and picks whichever
    console using that core comes first in retroarch_cores.CONSOLES,
    same "not a perfect restore, just a head start" contract the URL
    tab's own edit flow already has (it re-resolves from scratch too,
    rather than recalling the exact name/artwork picked originally).
    Returns (None, None) for anything that isn't a RetroArch shortcut."""
    try:
        tokens = shlex.split(launch_options)
    except ValueError:
        return None, None
    if retroarch_cores.RETROARCH_APP_ID not in tokens or "-L" not in tokens:
        return None, None
    idx = tokens.index("-L")
    if idx + 2 >= len(tokens):
        return None, None
    core_path, romfile = tokens[idx + 1], tokens[idx + 2]
    core_name = os.path.basename(core_path)
    if core_name.endswith("_libretro.so"):
        core_name = core_name[: -len("_libretro.so")]
    for console, core, _needs_bios in retroarch_cores.CONSOLES:
        if core == core_name:
            return console, romfile
    return None, None


# Where the romfile actually sits within an entry's own args() output
# (standalone_emulators.py) -- not always the last token the way most
# entries put it. Xenia Canary puts it first, ahead of its flags
# (confirmed via its own CLI docs). shadPS4 buries it in the middle --
# its own args() is ["-d", "-g", romfile, "--", "--fullscreen", "true"]
# (see _shadps4_args' own docstring on why "true" has to trail it), so
# blindly taking the last token grabbed that literal "true" as the
# romfile instead. Confirmed live: a real shadPS4 shortcut's Edit link
# showed "True" as the guessed game name and a bogus romfile, even
# though the shortcut itself launches its real game fine -- this only
# ever affected reconstructing Edit's own state, never actual launching.
# Every other entry's romfile is simply its args() output's last token
# (index -1), the default when not listed here.
_ROMFILE_ARGS_INDEX = {"Xenia Canary (AppImage)": 0, "shadPS4": 2}


def _extract_standalone_emulator_info(launch_options):
    """Pulls (emulator_name, romfile, preflight) back out of a
    standalone-emulator shortcut's own LaunchOptions (see standalone_
    emulators.launch_args) so the Emulators tab's own Edit link can
    jump straight back into it, same contract as _extract_retroarch_
    info above. The romfile's position within the args() portion of
    argv is looked up per-entry via _ROMFILE_ARGS_INDEX (default: its
    last token). Returns (None, None, False) for anything else.

    Three shapes to reverse: a Preflight-launched shortcut's own
    "<preflight.sh> <romfile>" (always "Ryubing" -- see standalone_
    emulators.PREFLIGHT_EMULATORS, checked first since it's the most
    specific/narrowest shape), a flathub entry's own "<flatpak> run
    <app_id> ..." (argv[2] is the app id, reverse-mapped to its
    emulator name via standalone_emulators.EMULATORS, with its own
    args() starting at argv[3]), and a binary (AppImage) entry's own
    "<real AppImage path> ..." -- no "flatpak run" prefix at all, so
    argv[0] is compared directly against each binary-install entry's own
    resolved path instead, with its own args() starting at argv[1].
    <flatpak> itself is host_exec.which("flatpak")'s own resolved
    absolute path (e.g. "/usr/bin/flatpak"), never the bare "flatpak"
    launch_args() actually writes it as -- matched by basename here, not
    by an exact literal comparison. Confirmed live as a real gap: before
    this fix, every flathub-installed shortcut's own Edit link silently
    fell through to the URL tab instead of actually restoring it, since
    the exact-string check never matched the real, absolute-path value."""
    try:
        tokens = shlex.split(launch_options)
    except ValueError:
        return None, None, False
    if not tokens:
        return None, None, False

    def _romfile_at(name, args_start):
        index = _ROMFILE_ARGS_INDEX.get(name)
        return tokens[args_start + index] if index is not None and index >= 0 else tokens[-1]

    if os.path.basename(tokens[0]) == "preflight.sh" and len(tokens) >= 2:
        return "Ryubing", tokens[1], True
    if os.path.basename(tokens[0]) == "flatpak" and len(tokens) >= 3 and tokens[1] == "run":
        app_id = tokens[2]
        for name, entry in standalone_emulators.EMULATORS.items():
            if entry.get("app_id") == app_id:
                return name, _romfile_at(name, 3), False
        return None, None, False
    for name, entry in standalone_emulators.EMULATORS.items():
        if entry.get("install_type") == "binary" and standalone_emulators.binary_path(name) == tokens[0]:
            return name, _romfile_at(name, 1), False
    return None, None, False


def _extract_apps_info(launch_options):
    """Pulls (app_id, source) back out of an Apps-tab shortcut's own
    LaunchOptions -- two shapes, matching the Apps tab's own two
    sources (see selfsteam_server.py's _apps_source_launch_args):
    "<flatpak> run <app_id>" for a real Flathub app_id (same shape
    _extract_standalone_emulator_info reverses for a curated EMULATORS
    entry -- see its own docstring on the real absolute-path-vs-bare-
    "flatpak" gotcha -- but for an *uncurated* app_id instead), or a
    bare AppImage path matching one of appimage_apps.APPS' own
    binary_path()s. Neither ever has extra argv after it the way an
    emulator's own args() does (no romfile to also recover here).
    Checked only after _extract_standalone_emulator_info's own
    EMULATORS match fails (see list_gridge_shortcuts' call order) so a
    real curated emulator's shortcut is never double-claimed as a
    generic Apps one instead. Returns (None, None) for anything else."""
    try:
        tokens = shlex.split(launch_options)
    except ValueError:
        return None, None
    if len(tokens) == 3 and os.path.basename(tokens[0]) == "flatpak" and tokens[1] == "run":
        return tokens[2], "flathub"
    if len(tokens) == 1:
        for app_id in appimage_apps.APPS:
            if appimage_apps.binary_path(app_id) == tokens[0]:
                return app_id, "appimage"
    return None, None


def find_grid_image_path(grid_dir, appid):
    """The vertical-grid image file for `appid` in `grid_dir`, whatever
    its extension actually is (SGDB candidates can be .png/.jpg/.webp) --
    GRID_FILENAMES only gives the pattern, not a real filename to open
    directly."""
    matches = glob.glob(os.path.join(grid_dir, GRID_FILENAMES["grid_vertical"].format(appid=appid, ext=".*")))
    return matches[0] if matches else None


def find_grid_image_for_appid(appid):
    """Same as find_grid_image_path, but searches every Steam user's
    grid dir instead of requiring the caller to already know which one --
    used by the gallery's own image-serving route, which only has an
    appid to go on."""
    root = steam_paths.find_steam_root()
    userdata = os.path.join(root, "userdata")
    if not os.path.isdir(userdata):
        return None
    for uid in os.listdir(userdata):
        if not uid.isdigit():
            continue
        path = find_grid_image_path(os.path.join(userdata, uid, "config", "grid"), appid)
        if path:
            return path
    return None


def thumbnail_for_appid(appid):
    """A small, cached webp copy of appid's own vertical grid image, for
    the gallery to serve instead of Steam's real (often multi-MB, full
    "featured"-resolution) original -- with 6 shortcuts already
    noticeably slow to load, and Steam users routinely running into the
    tens or hundreds, re-shipping the original to every browser on every
    home page load doesn't scale. Regenerated automatically whenever the
    source file actually changes (mtime-compared, not just "does a
    thumbnail exist") -- e.g. re-picking artwork on an Edit -- but never
    touches the original itself, since that's Steam's own file, not
    SelfSteam's to modify. Returns the original path unchanged if Pillow
    isn't available or the source can't be decoded, so a thumbnail
    failure degrades to the pre-thumbnail behavior rather than a broken
    image."""
    source = find_grid_image_for_appid(appid)
    if not source:
        return None
    os.makedirs(GRID_THUMB_DIR, exist_ok=True)
    thumb_path = os.path.join(GRID_THUMB_DIR, f"{appid}.webp")
    if os.path.exists(thumb_path) and os.path.getmtime(thumb_path) >= os.path.getmtime(source):
        return thumb_path
    try:
        from PIL import Image
        with Image.open(source) as img:
            img = img.convert("RGB")
            ratio = _GRID_THUMB_WIDTH / img.width
            target_size = (_GRID_THUMB_WIDTH, round(img.height * ratio))
            img = img.resize(target_size, Image.LANCZOS)
            img.save(thumb_path, "WEBP", quality=80)
        return thumb_path
    except Exception:  # noqa: BLE001 -- any decode/encode failure just falls back to the original
        return source


def list_gridge_shortcuts():
    """Every non-Steam shortcut across every Steam user profile --
    ones this tool itself created (matched via is_gridge_launch_wrapper
    on the exe field) as well as shortcuts the user added some other
    way (Steam's own "Add a Non-Steam Game", another tool, or a
    pre-rename SelfSteam build whose exe path this version's
    is_gridge_launch_wrapper doesn't happen to recognize). Confirmed
    live (2026-08-21) that filtering to only recognized-wrapper
    shortcuts hid entries a user reasonably expects to see and manage
    from here. Returns dicts with appid/name/url/ra_console/ra_romfile/
    em_emulator/em_romfile/em_preflight/apps_app_id/user_id/managed --
    ra_console/ra_romfile are None for anything but a RetroArch shortcut
    (see _extract_retroarch_info), em_emulator/em_romfile are None for
    anything but a standalone-emulator shortcut (em_preflight is always
    False alongside them, real only for a Ryubing shortcut routed
    through Preflight -- see _extract_standalone_emulator_info),
    apps_app_id is None for
    anything but an Apps-tab shortcut (see _extract_apps_info), url is
    None when LaunchOptions doesn't look like a browser launch at all --
    all of which _poster_card_html already renders a sane generic
    card/edit-link
    for. managed is True only for shortcuts is_gridge_launch_wrapper
    recognizes; foreign entries still list and can be edited/removed,
    editing one just adopts it as a SelfSteam-managed shortcut going
    forward, same as always happens when Edit's Create/Save button is
    used. Callers needing the grid image should call
    find_grid_image_path themselves with the right per-user grid_dir,
    since two different users could each have their own art for a
    same-named shortcut."""
    root = steam_paths.find_steam_root()
    userdata = os.path.join(root, "userdata")
    if not os.path.isdir(userdata):
        return []
    results = []
    for uid in os.listdir(userdata):
        if not uid.isdigit():
            continue
        vdf_path = os.path.join(userdata, uid, "config", "shortcuts.vdf")
        data = shortcuts_vdf.load(vdf_path)
        for entry in data.get("shortcuts", {}).values():
            exe = _field(entry, "exe", "Exe") or ""
            launch_options = _field(entry, "LaunchOptions", "launchoptions") or ""
            ra_console, ra_romfile = _extract_retroarch_info(launch_options)
            em_emulator, em_romfile, em_preflight = _extract_standalone_emulator_info(launch_options)
            # Only checked once em_emulator's own curated-EMULATORS match
            # fails -- see _extract_apps_info's own docstring on why
            # that's the right order (a real emulator shortcut should
            # never fall through and get double-claimed as a generic
            # Apps one instead).
            apps_app_id, apps_source = _extract_apps_info(launch_options) if em_emulator is None else (None, None)
            results.append({
                "appid": _field(entry, "appid", "AppID"),
                "name": _field(entry, "appname", "AppName") or "",
                "url": _extract_launch_url(launch_options),
                "ra_console": ra_console,
                "ra_romfile": ra_romfile,
                "em_emulator": em_emulator,
                "em_romfile": em_romfile,
                "em_preflight": em_preflight,
                "apps_app_id": apps_app_id,
                "apps_source": apps_source,
                "user_id": uid,
                "managed": is_gridge_launch_wrapper(exe),
                # Raw fields, used to pre-fill /custom's Target/Start In/
                # Launch Options for anything that isn't a recognized
                # URL/RetroArch/Emulators shortcut -- see _poster_card_html's
                # edit_href.
                "exe": exe,
                "start_dir": _field(entry, "StartDir", "startdir") or "",
                "launch_options": launch_options,
            })
    results.sort(key=lambda r: r["name"].lower())
    return results


def remove_gridge_shortcut(appid):
    """Removes a shortcut this tool created (and its stale grid assets) by
    appid, across every Steam user profile. No-ops (not an error) if
    already gone some other way -- deleting is inherently idempotent
    from the caller's point of view."""
    root = steam_paths.find_steam_root()
    userdata = os.path.join(root, "userdata")
    if not os.path.isdir(userdata):
        return
    for uid in os.listdir(userdata):
        if not uid.isdigit():
            continue
        vdf_path = os.path.join(userdata, uid, "config", "shortcuts.vdf")
        grid_dir = os.path.join(userdata, uid, "config", "grid")
        data = shortcuts_vdf.load(vdf_path)
        shortcuts = data.get("shortcuts", {})
        stale_keys = [k for k, e in shortcuts.items() if str(_field(e, "appid", "AppID")) == str(appid)]
        if not stale_keys:
            continue
        for k in stale_keys:
            del shortcuts[k]
        shortcuts_vdf.save(vdf_path, data)
        if os.path.isdir(grid_dir):
            for f in os.listdir(grid_dir):
                if f.startswith(str(appid)):
                    os.remove(os.path.join(grid_dir, f))


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
