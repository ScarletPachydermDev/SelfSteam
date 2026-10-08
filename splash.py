#!/usr/bin/env python3
"""Full-screen "please wait" splash shown while Steam is down for a
shortcuts.vdf/artwork write from the SelfSteam server (maintenance.py).

Just "Restarting Steam…", big: it covers a TV across the room for the
seconds Steam is gone, and that is the only thing worth reading there.

SDL2, drawn with sdl_screen.py like the code screen, and run on the host
(see sdl_screen.py on why). It runs until maintenance.py ends it with
SIGTERM, which is handled so SDL still closes its window cleanly.
"""

import ctypes
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import window_titles  # noqa: E402
from sdl_screen import SDL_QUIT, TEXT, Window, sdl  # noqa: E402

_FONTS = {
    "message": (True, lambda w, h: min(96 * h / 1080, 80 * w / 1920)),
}


def main():
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    screen = Window(window_titles.SPLASH_TITLE, _FONTS)
    event = ctypes.create_string_buffer(64)  # SDL_Event is a 56-byte union
    try:
        while not stop:
            while sdl.SDL_PollEvent(event):
                if ctypes.c_uint32.from_buffer(event).value == SDL_QUIT:
                    stop.append(True)
            screen.clear()
            screen.text("message", "Restarting Steam…", TEXT, screen.h / 2)
            screen.present()
            time.sleep(0.1)
    finally:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
