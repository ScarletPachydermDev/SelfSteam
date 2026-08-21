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
import subprocess

import host_exec

# Functions:
#   _installed_app_ids() -- app ids of every installed Flatpak app.
#   _parse_desktop_file(path) -- (name, categories, mimetypes) parsed out of a .desktop file.
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


def _parse_desktop_file(path):
    name = None
    categories = ""
    mimetypes = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            in_main_entry = False
            for line in f:
                line = line.rstrip("\n")
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
    except OSError:
        return None
    return name, categories, mimetypes


def _is_browser(categories, mimetypes):
    return "WebBrowser" in categories.split(";") or "x-scheme-handler/https" in mimetypes.split(";")


def list_installed_browsers():
    """Returns [(app_id, display_name), ...] for installed Flatpak apps
    that self-identify as web browsers, sorted by display name."""
    app_ids = set(_installed_app_ids())
    browsers = []
    for export_dir in _EXPORT_DIRS:
        for path in glob.glob(os.path.join(export_dir, "*.desktop")):
            app_id = os.path.splitext(os.path.basename(path))[0]
            if app_id not in app_ids:
                continue
            parsed = _parse_desktop_file(path)
            if not parsed:
                continue
            name, categories, mimetypes = parsed
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
