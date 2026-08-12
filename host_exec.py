"""Escape our own Flatpak sandbox to run/find things on the real host,
for when Gridge itself is running as a packaged Flatpak. This is the
same class of problem already solved once this project for Steam's own
Flatpak sandbox: a sandboxed process can't see host binaries (flatpak,
pidof, a native browser) at all -- flatpak-spawn --host is the standard
escape hatch, requiring --talk-name=org.freedesktop.Flatpak in this
app's own manifest. A no-op passthrough when running unsandboxed (e.g.
directly via `python3 gui.py` during development).
"""
import os
import shutil
import subprocess

IN_FLATPAK = os.path.exists("/.flatpak-info")


def wrap(argv):
    """Prefix argv with flatpak-spawn --host if sandboxed, unchanged
    otherwise -- use this instead of calling subprocess.run/Popen with
    a bare host command directly."""
    return ["flatpak-spawn", "--host", *argv] if IN_FLATPAK else list(argv)


def which(name):
    """shutil.which() only ever sees our own sandbox's PATH/filesystem --
    it will never find a host-installed binary once we're running as a
    Flatpak. Falls back to asking the host directly via flatpak-spawn
    --host when sandboxed."""
    if not IN_FLATPAK:
        return shutil.which(name)
    result = subprocess.run(["flatpak-spawn", "--host", "which", name], capture_output=True, text=True)
    return result.stdout.strip() or None
