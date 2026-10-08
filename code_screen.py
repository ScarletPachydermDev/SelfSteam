#!/usr/bin/env python3
"""SelfSteam's pairing code screen: SDL2, fullscreen, controller-first.

Shows four controllers in the logo's colours, the machine's name, the
address to open on another device, and the current code. Any button on a
controller or keyboard closes it, and so does someone logging in with
the code from another device.

Opened only by the "SelfSteam" Steam shortcut and by the desktop app --
never by the server on its own. From the shortcut it is the game Steam
is running, so Steam puts it in front and hands it the controller by
itself, with no window tricks. Drawn with sdl_screen.py.

The code itself only exists in the server's memory, so this asks the
server for it (GET /code, answered for 127.0.0.1 only) and keeps asking
once a second: to pick up a fresh code when one expires, and to notice a
login, after which there is nothing left to show.
"""

import ctypes
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sdl_screen import (ACCENT, CLOSE_EVENTS, DIM, SDL_CONTROLLERDEVICEADDED, SDL_KEYDOWN,  # noqa: E402
                        SDL_QUIT, TEXT, Window, sdl)

PORT = os.environ.get("SELFSTEAM_SERVER_PORT", "8845")
CODE_URL = f"http://127.0.0.1:{PORT}/code"

# The button that launched the shortcut is often still held, or its
# release still queued, when this window opens. Input this early is
# ignored so the screen does not close the instant it appears.
_IGNORE_INPUT_SECONDS = 0.6

_FONTS = {
    "title": (True, 64),
    # Capped by width too: on a 16:10 screen (Steam Deck, 1280x800) a
    # size from the height alone ran the code into both edges.
    "code": (True, lambda w, h: min(300 * h / 1080, 270 * w / 1920)),
    "body": (False, 44),
    "small": (False, 34),
}


def _fetch():
    """{code, hostname, ip, port, logins} from the server, or None."""
    try:
        with urllib.request.urlopen(CODE_URL, timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


class Screen(Window):
    def __init__(self):
        super().__init__("SelfSteam", _FONTS, input_devices=True)
        self.pads = []

    def _open_pad(self, index):
        pad = sdl.SDL_GameControllerOpen(index) if sdl.SDL_IsGameController(index) else sdl.SDL_JoystickOpen(index)
        if pad:
            self.pads.append(pad)

    def draw(self, info):
        self.clear()
        h = self.h
        if info is None:
            self.controllers(h * 0.3)
            self.text("body", "SelfSteam's background service isn't running.", DIM, h * 0.5)
            self.text("body", "Restart the Steam Machine, or open SelfSteam from the desktop.", DIM, h * 0.57)
        else:
            self.controllers(h * 0.135)
            self.text("small", info.get("hostname") or "", TEXT, h * 0.19)
            self.text("body", "Open this address on your phone or computer:", DIM, h * 0.27)
            self.text("title", f"http://{info.get('ip')}:{info.get('port')}", ACCENT, h * 0.35)
            self.text("body", "and enter this code:", DIM, h * 0.45)
            self.text("code", " ".join(info.get("code") or "------"), TEXT, h * 0.62)
        self.text("small", "Press any button to close", DIM, h * 0.92)
        self.present()

    def run(self):
        info = _fetch()
        logins = info.get("logins") if info else None
        self.draw(info)
        opened = time.monotonic()
        next_poll = opened + 1.0
        event = ctypes.create_string_buffer(64)  # SDL_Event is a 56-byte union
        while True:
            while sdl.SDL_PollEvent(event):
                etype = ctypes.c_uint32.from_buffer(event).value
                if etype == SDL_QUIT:
                    return
                if etype == SDL_CONTROLLERDEVICEADDED:
                    self._open_pad(ctypes.c_int32.from_buffer(event, 8).value)
                    continue
                if etype == SDL_KEYDOWN and event.raw[13] != 0:
                    continue  # key repeat
                if etype in CLOSE_EVENTS and time.monotonic() - opened > _IGNORE_INPUT_SECONDS:
                    return
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + 1.0
                fresh = _fetch()
                if fresh and logins is not None and fresh.get("logins") != logins:
                    return  # someone logged in with the code: done
                if fresh and logins is None:
                    logins = fresh.get("logins")
                info = fresh
                self.draw(info)  # every second, changed or not, so nothing stale stays up
            time.sleep(0.016)


def main():
    screen = Screen()
    try:
        screen.run()
    finally:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
