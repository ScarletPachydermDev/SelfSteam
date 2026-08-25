"""Detects installed Flatpak web browsers so the shortcut form can offer
a real picker instead of a hardcoded browser list.

Flatpak apps export a standard .desktop file (freedesktop.org menu
spec) declaring Categories=...;WebBrowser;... -- confirmed live against
real installed apps: both com.microsoft.Edge and the LibreWolf Flatpak
declare this correctly. MimeType's x-scheme-handler/https is checked
too as a second, complementary signal (literally "can this app open a
URL"), in case a browser's Categories field is ever incomplete.
"""
import glob
import os
import shlex
import subprocess

import host_exec

# Functions:
#   _installed_app_ids() -- app ids of every installed Flatpak app.
#   _desktop_entries() -- yields (app_id, raw .desktop file content) for every export.
#   _parse_desktop_entry(content) -- (name, categories, mimetypes) parsed out of .desktop content.
#   _is_browser(categories, mimetypes) -- True if those fields self-identify as a web browser.
#   list_installed_browsers() -- [(app_id, display_name), ...] for installed browsers, sorted.

_EXPORT_DIRS = [
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
]


def _installed_app_ids():
    result = subprocess.run(
        host_exec.wrap(["flatpak", "list", "--app", "--columns=application"]),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _desktop_entries():
    """Yields (app_id, raw .desktop content) for every export in
    _EXPORT_DIRS. When sandboxed, this can't just glob/open() the
    paths directly -- confirmed live (2026-08-25) that /var/lib itself
    (not just /var/lib/flatpak) is invisible inside this Flatpak's own
    sandbox even with --filesystem=host granted (the manifest's own
    ~/.var/app comment already documents a similar exclusion for a
    different directory; /var turns out to have the same gap for a
    *system*-wide Flatpak install's own exports, unlike a *user* one
    under ~/.local/share which the sandbox can see fine). A browser
    installed system-wide (flatpak install --system, not --user) was
    silently invisible to the picker because of this -- the Browser
    field disappeared from the URL tab entirely rather than just
    missing one option, since list_installed_browsers() returning
    empty is exactly what makes _browser_select_html render nothing at
    all.

    One host-spawned shell walks both export dirs and prints each
    file's own app_id + content, delimited by \\x01/\\x02 (bytes that
    can't appear in a normal file's own basename or a .desktop file's
    own text), so N desktop files only costs one subprocess round trip
    via flatpak-spawn --host instead of N separate ones."""
    if not host_exec.IN_FLATPAK:
        for export_dir in _EXPORT_DIRS:
            for path in glob.glob(os.path.join(export_dir, "*.desktop")):
                app_id = os.path.splitext(os.path.basename(path))[0]
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        yield app_id, f.read()
                except OSError:
                    continue
        return
    cmd = (
        "for dir in " + " ".join(shlex.quote(d) for d in _EXPORT_DIRS) + "; do "
        '[ -d "$dir" ] || continue; '
        'for f in "$dir"/*.desktop; do '
        '[ -f "$f" ] || continue; '
        'printf "\\x01%s\\x02" "$(basename "$f" .desktop)"; cat "$f"; '
        "done; done"
    )
    result = subprocess.run(host_exec.wrap(["sh", "-c", cmd]), capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout:
        return
    for chunk in result.stdout.split("\x01")[1:]:
        app_id, _, content = chunk.partition("\x02")
        yield app_id, content


def _parse_desktop_entry(content):
    name = None
    categories = ""
    mimetypes = ""
    in_main_entry = False
    for line in content.splitlines():
        if line == "[Desktop Entry]":
            in_main_entry = True
            continue
        if line.startswith("[") and line != "[Desktop Entry]":
            break  # left the main entry (e.g. into a [Desktop Action ...])
        if not in_main_entry:
            continue
        if line.startswith("Name=") and name is None:
            name = line[len("Name="):]
        elif line.startswith("Categories="):
            categories = line[len("Categories="):]
        elif line.startswith("MimeType="):
            mimetypes = line[len("MimeType="):]
    return name, categories, mimetypes


def _is_browser(categories, mimetypes):
    return "WebBrowser" in categories.split(";") or "x-scheme-handler/https" in mimetypes.split(";")


def list_installed_browsers():
    """Returns [(app_id, display_name), ...] for installed Flatpak apps
    that self-identify as web browsers, sorted by display name."""
    app_ids = set(_installed_app_ids())
    browsers = []
    for app_id, content in _desktop_entries():
        if app_id not in app_ids:
            continue
        name, categories, mimetypes = _parse_desktop_entry(content)
        if _is_browser(categories, mimetypes):
            browsers.append((app_id, name or app_id))
    # De-dupe (user + system exports can both list the same app_id).
    seen = set()
    unique = []
    for app_id, name in sorted(browsers, key=lambda b: b[1].lower()):
        if app_id in seen:
            continue
        seen.add(app_id)
        unique.append((app_id, name))
    return unique
