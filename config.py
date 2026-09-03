"""Persistent app settings (SGDB API key, etc.), stored as JSON under
XDG_CONFIG_HOME. This is the real, user-facing settings storage the
GUI reads/writes -- separate from the .env file, which stays a
dev-only convenience for running the CLI directly.
"""
import json
import os
import shutil
import tempfile

_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(_CONFIG_HOME, "selfsteam")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Old names before the app was renamed, in order: steam-webapp-creator ->
# Gridge -> SelfSteam. Migrating the *whole* old directory (not just
# config.json) on first run under the new name carries pending_queue.json
# and remembered_devices.json along with it too -- those live in the same
# dir (see pending_queue.py/auth.py), and losing either one silently would
# mean a staged-but-uncommitted shortcut queue disappearing, or every
# "remember this device" pairing needing to be redone.
_OLD_CONFIG_DIRS = [
    os.path.join(_CONFIG_HOME, "gridge"),
    os.path.join(_CONFIG_HOME, "steam-webapp-creator"),
]


# Functions:
#   _migrate_old_config() -- copies an old gridge/steam-webapp-creator config dir forward once.
#   load() / save(**updates) -- read/merge-write the whole config.json.
#   get_sgdb_api_key() / set_sgdb_api_key(key) / clear_sgdb_api_key() -- the SGDB key setting.
#   get_last_browser() / set_last_browser(app_id) -- the last-used browser setting.
def _migrate_old_config():
    if os.path.exists(CONFIG_DIR):
        return
    for old_dir in _OLD_CONFIG_DIRS:
        if os.path.isdir(old_dir):
            shutil.copytree(old_dir, CONFIG_DIR)
            return


# Run once at import time, not lazily from load() -- pending_queue.py and
# auth.py both call os.makedirs(config.CONFIG_DIR, ...) directly in their
# own save paths without going through load() first. If either of those
# ran before this did, it would create an empty CONFIG_DIR and permanently
# block the real migration (this function's own guard is "does CONFIG_DIR
# already exist"). Every module here does `import config`, so this runs
# once, before anything else, regardless of which module happens first.
_migrate_old_config()


def load():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def atomic_write_json(path, data):
    """Writes JSON to path atomically -- a temp file in the same
    directory, flushed and fsynced, then renamed over the real path --
    instead of a plain open(path, "w") + json.dump, which leaves a
    truncated/corrupt file behind if the process dies mid-write.
    Confirmed live as a real bug, not a hypothetical: selfsteam_server.
    py's own auto-restart-on-update calls `flatpak kill` on this app
    (a hard kill, not a graceful shutdown -- see its own docstring for
    why a graceful one doesn't reliably work here), and auth.py's own
    remembered-device file gets rewritten on every authenticated
    request (a sliding-expiry renewal) -- landing that kill mid-write
    silently discarded every remembered device, forcing a fresh pairing-
    code login the next time, even though "remember this device" had
    been used. Shared here (not duplicated per file) since
    pending_queue.json and this module's own config.json are written
    just as often and are exactly as vulnerable to the same kill."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def save(**updates):
    data = load()
    data.update(updates)
    atomic_write_json(CONFIG_FILE, data)


def get_sgdb_api_key():
    return load().get("sgdb_api_key")


def set_sgdb_api_key(key):
    save(sgdb_api_key=key)


def clear_sgdb_api_key():
    data = load()
    if data.pop("sgdb_api_key", None) is not None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)


def get_last_browser():
    return load().get("server_last_browser")


def set_last_browser(app_id):
    save(server_last_browser=app_id)


def get_pending_first_show():
    # Set once by the installer (install.sh, or a Flatpak's own first-run
    # setup later) right after it finishes setting up the persistent
    # background service. Lets the server show the pairing screen exactly
    # once on its own -- the first time it notices a real Game Mode/
    # gamescope session after a fresh install -- without ever doing so
    # again on a later reboot. Defaults to False for an existing config
    # that predates this flag, not True -- an upgrade shouldn't suddenly
    # start popping the auth screen unprompted for someone who's been
    # running this for a while already.
    return bool(load().get("pending_first_show"))


def set_pending_first_show(value):
    save(pending_first_show=bool(value))


def get_last_seen_selfsteam_version():
    # None the first time this ever runs (a fresh install, or an
    # existing config that predates this flag) -- selfsteam_server.py's
    # own startup check treats that the same as "different from
    # current," so a Preflight update-check still runs once rather than
    # silently never firing for anyone upgrading from before this
    # existed.
    return load().get("last_seen_selfsteam_version")


def set_last_seen_selfsteam_version(version):
    save(last_seen_selfsteam_version=version)
