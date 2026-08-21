"""Persistent app settings (SGDB API key, etc.), stored as JSON under
XDG_CONFIG_HOME. This is the real, user-facing settings storage the
GUI reads/writes -- separate from the .env file, which stays a
dev-only convenience for running the CLI directly.
"""
import json
import os
import shutil

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


def save(**updates):
    data = load()
    data.update(updates)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


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
