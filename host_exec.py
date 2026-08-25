"""Escape our own Flatpak sandbox to run/find things on the real host,
for when SelfSteam itself is running as a packaged Flatpak. This is the
same class of problem already solved once this project for Steam's own
Flatpak sandbox: a sandboxed process can't see host binaries (flatpak,
pidof, a native browser) at all -- flatpak-spawn --host is the standard
escape hatch, requiring --talk-name=org.freedesktop.Flatpak in this
app's own manifest. A no-op passthrough when running unsandboxed (e.g.
directly via `python3 selfsteam_server.py` during development).
"""
import os
import shutil
import subprocess

IN_FLATPAK = os.path.exists("/.flatpak-info")

# Functions:
#   wrap(argv) -- prefixes argv with flatpak-spawn --host if sandboxed, unchanged otherwise.
#   wrap_with_env(argv, env) -- same, but also forwards specific env vars to the host process.
#   which(name) -- shutil.which() that can see host binaries even when sandboxed.


def wrap(argv):
    """Prefix argv with flatpak-spawn --host if sandboxed, unchanged
    otherwise -- use this instead of calling subprocess.run/Popen with
    a bare host command directly."""
    return ["flatpak-spawn", "--host", *argv] if IN_FLATPAK else list(argv)


def wrap_with_env(argv, env):
    """Same escape hatch as wrap() above, but also forwards specific
    environment variables to the host-side process via flatpak-spawn's
    own --env= flag. Needed for host commands (xdotool, unlike xprop/
    xwininfo elsewhere in this codebase, which take DISPLAY as a plain
    CLI argument instead) that only ever read something like DISPLAY
    from their own process environment -- confirmed live (2026-08-25)
    that flatpak-spawn --host does NOT forward this sandbox's own
    environment to the host process at all by default, so a plain
    wrap() left such a command with no display to find, failing
    outright rather than picking one up from anywhere else."""
    if not IN_FLATPAK:
        return list(argv)
    env_args = [f"--env={key}={value}" for key, value in env.items()]
    return ["flatpak-spawn", "--host", *env_args, *argv]


def which(name):
    """shutil.which() only ever sees our own sandbox's PATH/filesystem --
    it will never find a host-installed binary once we're running as a
    Flatpak. Falls back to asking the host directly via flatpak-spawn
    --host when sandboxed."""
    if not IN_FLATPAK:
        return shutil.which(name)
    result = subprocess.run(["flatpak-spawn", "--host", "which", name], capture_output=True, text=True)
    return result.stdout.strip() or None
