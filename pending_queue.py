"""Shortcuts staged for the next Steam maintenance window, instead of
each "Create Steam Shortcut" click paying for its own Steam
stop/write/restart cycle. Lets a user queue up several shortcuts in
one sitting (across URL, and later Apps/RetroArch/Emulators once those
tabs are wired up) and commit them all at once via the header's "Save
Changes and Restart Steam OS" button.

Persisted to disk (data/gridge/pending_queue.json under XDG_CONFIG_HOME,
alongside config.json) rather than kept purely in memory: unlike
auth.py's session/code state, losing a queue silently to a server
restart would destroy real user work (artwork already downloaded,
forms already filled out and cleared), not just force a re-login.
"""
import json
import os
import threading
import time

import config

_QUEUE_FILE = os.path.join(config.CONFIG_DIR, "pending_queue.json")
_lock = threading.Lock()


def _load():
    if not os.path.exists(_QUEUE_FILE):
        return []
    with open(_QUEUE_FILE) as f:
        return json.load(f)


def _save(items):
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with open(_QUEUE_FILE, "w") as f:
        json.dump(items, f, indent=2)


def add(name, url, couch_mode, asset_paths, browser_app_id=None):
    with _lock:
        items = _load()
        items.append({
            "type": "add",
            "name": name,
            "url": url,
            "couch_mode": couch_mode,
            "asset_paths": asset_paths,
            "browser_app_id": browser_app_id,
            "queued_at": time.time(),
        })
        _save(items)


def add_removal(appid, name):
    # Removals are batchable the same way additions are -- someone
    # cleaning up several old shortcuts shouldn't need a separate Steam
    # restart per deletion any more than someone adding several new
    # ones should.
    with _lock:
        items = _load()
        items.append({
            "type": "remove",
            "appid": appid,
            "name": name,
            "queued_at": time.time(),
        })
        _save(items)


def all_items():
    with _lock:
        return _load()


def count():
    with _lock:
        return len(_load())


def remove(index):
    with _lock:
        items = _load()
        if 0 <= index < len(items):
            items.pop(index)
            _save(items)


def clear():
    with _lock:
        _save([])
