#!/usr/bin/env python3
"""Full-screen "please wait" splash shown while Steam is down for a
shortcuts.vdf/artwork write from the SelfSteam server (maintenance.py).

Big on purpose: it covers a TV across the room for the seconds Steam is
gone, so it has to read as "SelfSteam is working on it" at a glance.
The four controllers hop one after another as the progress indicator.

SDL2, drawn with sdl_screen.py like the code screen, and run on the host
(see sdl_screen.py on why). It runs until maintenance.py ends it with
SIGTERM, which is handled so SDL still closes its window cleanly.
"""

import ctypes
import math
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import window_titles  # noqa: E402
from sdl_screen import DIM, SDL_QUIT, TEXT, Window, sdl  # noqa: E402

_FONTS = {
    "message": (True, lambda w, h: min(96 * h / 1080, 80 * w / 1920)),
    "sub": (False, 48),
}


def main():
    message = sys.argv[1] if len(sys.argv) > 1 else "Applying changes…"
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    screen = Window(window_titles.SPLASH_TITLE, _FONTS)
    event = ctypes.create_string_buffer(64)  # SDL_Event is a 56-byte union
    start = time.monotonic()
    try:
        while not stop:
            while sdl.SDL_PollEvent(event):
                if ctypes.c_uint32.from_buffer(event).value == SDL_QUIT:
                    stop.append(True)
            t = time.monotonic() - start
            # Each controller hops in turn: a half-sine bump, staggered.
            lift = [max(0.0, math.sin((t * 2.2 - i * 0.35) * math.pi)) ** 2 * 0.35
                    if ((t * 2.2 - i * 0.35) % 2) < 1 else 0.0 for i in range(4)]
            screen.clear()
            screen.controllers(screen.h * 0.36, size_ratio=0.15, lift=lift)
            screen.text("message", message, TEXT, screen.h * 0.6)
            screen.text("sub", "Steam will be back in a moment", DIM, screen.h * 0.7)
            screen.present()
            time.sleep(0.016)
    finally:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
