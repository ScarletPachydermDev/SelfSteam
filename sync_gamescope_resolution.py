"""Tell Gamescope to size this shortcut's nested Xwayland display to match
Gamescope's actual current output resolution (Deck screen, docked TV,
whatever's connected right now), instead of staying pinned to whatever
resolution it happened to start at.

Non-Steam shortcuts don't get Steam's automatic docked-resolution
handling that recognized library games get -- without this, a shortcut
keeps rendering at the resolution it was first launched at (e.g. the
Deck's own 1280x800) even after docking to a 1080p/4K TV, and Gamescope
just upscales that stale render instead of using the TV's native
resolution.

Technique and exact sentinel values (INT32_MAX signals "use Gamescope's
own output resolution" rather than forcing a specific size) taken from
https://github.com/loki-47-6F-64/gamescope-mode-change, which discovered
Gamescope listens for a custom X11 property
(GAMESCOPE_XWAYLAND_MODE_CONTROL) on the root window of the per-app
nested Xwayland it creates for each non-Steam shortcut. Uses vendored
python-xlib (see vendor/) since there's no pip on the Steam Deck's host
OS to install it with.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))

from Xlib import X, Xatom, display  # noqa: E402
from Xlib.protocol import event as xevent  # noqa: E402

ATOM_NAME = "GAMESCOPE_XWAYLAND_MODE_CONTROL"
INT32_MAX = 2**30 + (2**30 - 1)


def sync_to_native():
    dpy_name = os.environ.get("DISPLAY")
    if not dpy_name:
        return

    try:
        d = display.Display(dpy_name)
    except Exception:
        return

    atom = d.intern_atom(ATOM_NAME, only_if_exists=True)
    if atom == X.NONE:
        # Not running under Gamescope (e.g. Desktop Mode) -- nothing to do.
        return

    root = d.screen().root
    server_id = 0
    super_res = 0
    root.change_property(atom, Xatom.CARDINAL, 32, [server_id, INT32_MAX, INT32_MAX, super_res])

    notify = xevent.PropertyNotify(
        window=root.id,
        display=d,
        atom=atom,
        time=0,
        state=X.PropertyNewValue,
    )
    root.send_event(notify)
    d.flush()


if __name__ == "__main__":
    sync_to_native()
