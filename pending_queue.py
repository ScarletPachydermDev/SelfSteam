"""Shortcuts staged for the next Steam maintenance window, instead of
each "Create Steam Shortcut" click paying for its own Steam
stop/write/restart cycle. Lets a user queue up several shortcuts in
one sitting (across URL, and later Apps/RetroArch/Emulators once those
tabs are wired up) and commit them all at once via the header's "Save
Changes and Restart Steam OS" button.

Persisted to disk (pending_queue.json under XDG_CONFIG_HOME/selfsteam,
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

# Functions:
#   _load() / _save(items) -- read/write the whole queue file.
#   add(name, url, couch_mode, asset_paths, ...) -- queues a shortcut to add.
#   add_custom(name, target, start_dir, launch_options, asset_paths) -- queues a /custom shortcut to add.
#   add_removal(appid, name, romfile=None) -- queues a shortcut to remove.
#   all_items() -- every queued item.
#   count() -- number of queued items.
#   remove(index) -- drops one queued item by position.
#   clear() -- empties the queue (after a successful commit).


def _load():
    if not os.path.exists(_QUEUE_FILE):
        return []
    with open(_QUEUE_FILE) as f:
        return json.load(f)


def _save(items):
    # atomic_write_json, not a plain open(...)+json.dump -- see its own
    # docstring: this file is exactly as vulnerable as auth.py's
    # remembered-devices one to a kill-mid-write from the auto-restart-
    # on-update feature, which would silently discard a staged shortcut
    # someone hadn't committed yet.
    config.atomic_write_json(_QUEUE_FILE, items)


def add(name, url, couch_mode, asset_paths, browser_app_id=None, launch_args=None):
    with _lock:
        items = _load()
        items.append({
            "type": "add",
            "name": name,
            "url": url,
            "couch_mode": couch_mode,
            "asset_paths": asset_paths,
            "browser_app_id": browser_app_id,
            "launch_args": launch_args,
            "queued_at": time.time(),
        })
        _save(items)


def add_custom(name, target, start_dir, launch_options, asset_paths):
    # Separate item type from "add" (URL/RetroArch/Emulators) since it
    # commits through create_webapp.register_custom_shortcut instead --
    # that one writes target/start_dir/launch_options verbatim rather
    # than synthesizing them from a url/browser_app_id/launch_args.
    with _lock:
        items = _load()
        items.append({
            "type": "add_custom",
            "name": name,
            "target": target,
            "start_dir": start_dir,
            "launch_options": launch_options,
            "asset_paths": asset_paths,
            "queued_at": time.time(),
        })
        _save(items)


def add_removal(appid, name, romfile=None):
    # Removals are batchable the same way additions are -- someone
    # cleaning up several old shortcuts shouldn't need a separate Steam
    # restart per deletion any more than someone adding several new
    # ones should. romfile (RetroArch/Emulators-tab shortcuts only, see
    # create_webapp.list_gridge_shortcuts' own ra_romfile/em_romfile)
    # gets deleted from disk alongside the Steam shortcut itself once
    # this actually commits -- never BIOS/keys/firmware, which are
    # shared across every shortcut using that console/emulator, not
    # owned by this one shortcut the way its own ROM is.
    with _lock:
        items = _load()
        items.append({
            "type": "remove",
            "appid": appid,
            "name": name,
            "romfile": romfile,
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
