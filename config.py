"""Persistent app settings (SGDB API key, etc.), stored as JSON under
XDG_CONFIG_HOME. This is the real, user-facing settings storage the
GUI reads/writes -- separate from the .env file, which stays a
dev-only convenience for running the CLI directly.
"""
import json
import os

_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(_CONFIG_HOME, "gridge")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Old name (steam-webapp-creator) before the app was renamed to Gridge --
# migrate a one-time copy so machines already testing don't lose their
# saved SGDB key.
_OLD_CONFIG_FILE = os.path.join(_CONFIG_HOME, "steam-webapp-creator", "config.json")


def _migrate_old_config():
    if os.path.exists(CONFIG_FILE) or not os.path.exists(_OLD_CONFIG_FILE):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(_OLD_CONFIG_FILE) as f:
        data = json.load(f)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load():
    if not os.path.exists(CONFIG_FILE):
        _migrate_old_config()
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
