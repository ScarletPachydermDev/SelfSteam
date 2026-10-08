#!/usr/bin/env python3
"""SelfSteam's pairing code screen: SDL2, fullscreen, controller-first.

Shows four controllers in the logo's colours, the machine's name, the address to open on another device, and the
current code. Any button on a controller or keyboard closes it, and so
does someone logging in with the code from another device.

Opened only by the "SelfSteam" Steam shortcut and by the desktop app --
never by the server on its own. From the shortcut it is the game Steam
is running, so Steam puts it in front and hands it the controller by
itself, with no window tricks.

Runs on the host, not in SelfSteam's Flatpak, through ctypes against the
system's own libSDL2 and libSDL2_ttf -- the same zero-dependency way
Preflight draws its screens. The server copies this file out of the
Flatpak to ~/.local/share/selfsteam/ at start, where the host can run it.

The code itself only exists in the server's memory, so this asks the
server for it (GET /code, answered for 127.0.0.1 only) and keeps asking
once a second: to pick up a fresh code when one expires, and to notice a
login, after which there is nothing left to show.
"""

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = os.environ.get("SELFSTEAM_SERVER_PORT", "8845")
CODE_URL = f"http://127.0.0.1:{PORT}/code"

# SDL2 constants (SDL.h, SDL_events.h, SDL_video.h).
SDL_INIT_VIDEO = 0x20
SDL_INIT_JOYSTICK = 0x200
SDL_INIT_GAMECONTROLLER = 0x2000
SDL_WINDOW_FULLSCREEN_DESKTOP = 0x1001
SDL_WINDOW_SHOWN = 0x4
SDL_WINDOWPOS_CENTERED = 0x2FFF0000
SDL_RENDERER_ACCELERATED = 0x2
SDL_RENDERER_PRESENTVSYNC = 0x4

SDL_QUIT = 0x100
SDL_KEYDOWN = 0x300
SDL_MOUSEBUTTONDOWN = 0x401
SDL_JOYBUTTONDOWN = 0x603
SDL_CONTROLLERBUTTONDOWN = 0x650
SDL_CONTROLLERDEVICEADDED = 0x653
_CLOSE_EVENTS = {SDL_KEYDOWN, SDL_MOUSEBUTTONDOWN, SDL_JOYBUTTONDOWN, SDL_CONTROLLERBUTTONDOWN}

# The button that launched the shortcut is often still held, or its
# release still queued, when this window opens. Input this early is
# ignored so the screen does not close the instant it appears.
_IGNORE_INPUT_SECONDS = 0.6

BG = (18, 20, 24)
TEXT = (235, 237, 240)
DIM = (150, 155, 165)
ACCENT = (0, 149, 255)  # SelfSteam's button blue


class SDL_Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8), ("b", ctypes.c_uint8), ("a", ctypes.c_uint8)]


class SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("w", ctypes.c_int), ("h", ctypes.c_int)]


class SDL_Surface(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("format", ctypes.c_void_p),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


class SDL_DisplayMode(ctypes.Structure):
    _fields_ = [("format", ctypes.c_uint32), ("w", ctypes.c_int), ("h", ctypes.c_int),
                ("refresh_rate", ctypes.c_int), ("driverdata", ctypes.c_void_p)]


def _load(names):
    for name in names:
        path = ctypes.util.find_library(name) or None
        for candidate in filter(None, [path, f"lib{name}.so", f"lib{name}-2.0.so.0", f"lib{name}-2.0.so"]):
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                continue
    raise OSError(f"could not load {names[0]}")


sdl = _load(["SDL2"])
ttf = _load(["SDL2_ttf"])

sdl.SDL_CreateWindow.restype = ctypes.c_void_p
sdl.SDL_CreateWindow.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
sdl.SDL_CreateRenderer.restype = ctypes.c_void_p
sdl.SDL_CreateRenderer.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
sdl.SDL_SetRenderDrawColor.argtypes = [ctypes.c_void_p] + [ctypes.c_uint8] * 4
sdl.SDL_RenderClear.argtypes = [ctypes.c_void_p]
sdl.SDL_RenderPresent.argtypes = [ctypes.c_void_p]
sdl.SDL_RenderFillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(SDL_Rect)]
sdl.SDL_CreateTextureFromSurface.restype = ctypes.c_void_p
sdl.SDL_CreateTextureFromSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
sdl.SDL_RenderCopy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(SDL_Rect)]
sdl.SDL_DestroyTexture.argtypes = [ctypes.c_void_p]
sdl.SDL_FreeSurface.argtypes = [ctypes.c_void_p]
sdl.SDL_GetRendererOutputSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
sdl.SDL_PollEvent.argtypes = [ctypes.c_void_p]
sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
sdl.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
sdl.SDL_JoystickOpen.restype = ctypes.c_void_p
sdl.SDL_JoystickOpen.argtypes = [ctypes.c_int]
sdl.SDL_IsGameController.argtypes = [ctypes.c_int]
sdl.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
sdl.SDL_DestroyRenderer.argtypes = [ctypes.c_void_p]
sdl.SDL_DestroyWindow.argtypes = [ctypes.c_void_p]
sdl.SDL_ShowCursor.argtypes = [ctypes.c_int]
sdl.SDL_CreateRGBSurfaceWithFormatFrom.restype = ctypes.c_void_p
sdl.SDL_CreateRGBSurfaceWithFormatFrom.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                   ctypes.c_int, ctypes.c_uint32]
SDL_PIXELFORMAT_ABGR8888 = 0x16762004  # bytes R, G, B, A in memory

ttf.TTF_OpenFont.restype = ctypes.c_void_p
ttf.TTF_OpenFont.argtypes = [ctypes.c_char_p, ctypes.c_int]
ttf.TTF_CloseFont.argtypes = [ctypes.c_void_p]
ttf.TTF_RenderUTF8_Blended.restype = ctypes.POINTER(SDL_Surface)
ttf.TTF_RenderUTF8_Blended.argtypes = [ctypes.c_void_p, ctypes.c_char_p, SDL_Color]


def _font_path(bold):
    """A real font file for SDL_ttf, which cannot ask fontconfig itself."""
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out and os.path.isfile(out):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    for path in ("/usr/share/fonts/noto/NotoSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/noto/NotoSans-Regular.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"):
        if os.path.isfile(path):
            return path
    raise OSError("no font found")


ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "code-screen")
# The logo's four tiles, as controllers, in the logo's order.
CONTROLLERS = ("yellow", "blue", "green", "red")


def _load_png(path):
    """(width, height, RGBA bytes) for an 8-bit RGBA, non-interlaced PNG.
    SteamOS ships no SDL2_image, so like Preflight this decodes its own
    art with zlib -- only the one format its art is exported in."""
    import struct
    import zlib
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, []
    width = height = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or ctype != 6 or interlace:
                raise ValueError("only 8-bit RGBA, non-interlaced")
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    stride = width * 4
    out = bytearray(height * stride)
    prev = bytearray(stride)
    i = 0
    for y in range(height):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if ftype == 1:
            for x in range(4, stride):
                line[x] = (line[x] + line[x - 4]) & 0xFF
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(stride):
                left = line[x - 4] if x >= 4 else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                b = prev[x]
                c = prev[x - 4] if x >= 4 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bytes(out)


def _fetch():
    """{code, hostname, ip, port, logins} from the server, or None."""
    try:
        with urllib.request.urlopen(CODE_URL, timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


class Screen:
    def __init__(self):
        sdl.SDL_SetHint(b"SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", b"0")
        if sdl.SDL_Init(SDL_INIT_VIDEO | SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER) != 0:
            raise OSError("SDL_Init failed")
        if ttf.TTF_Init() != 0:
            raise OSError("TTF_Init failed")
        self.window = sdl.SDL_CreateWindow(
            b"SelfSteam", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, 1280, 720,
            SDL_WINDOW_FULLSCREEN_DESKTOP | SDL_WINDOW_SHOWN)
        if not self.window:
            raise OSError("SDL_CreateWindow failed")
        self.renderer = sdl.SDL_CreateRenderer(self.window, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC)
        if not self.renderer:
            self.renderer = sdl.SDL_CreateRenderer(self.window, -1, 0)
        sdl.SDL_ShowCursor(0)
        w, h = ctypes.c_int(), ctypes.c_int()
        sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(w), ctypes.byref(h))
        self.w, self.h = w.value, h.value
        bold, regular = _font_path(True), _font_path(False)
        unit = self.h / 1080
        self.fonts = {
            "title": ttf.TTF_OpenFont(bold.encode(), max(12, int(64 * unit))),
            # Capped by width too: on a 16:10 screen (Steam Deck, 1280x800)
            # a size from the height alone ran the code into both edges.
            "code": ttf.TTF_OpenFont(bold.encode(), max(24, int(min(300 * unit, 270 * self.w / 1920)))),
            "body": ttf.TTF_OpenFont(regular.encode(), max(12, int(44 * unit))),
            "small": ttf.TTF_OpenFont(regular.encode(), max(10, int(34 * unit))),
        }
        self.controllers = []
        sdl.SDL_SetHint(b"SDL_RENDER_SCALE_QUALITY", b"1")
        self.art = []
        for name in CONTROLLERS:
            try:
                w, h, pixels = _load_png(os.path.join(ART_DIR, f"controller-{name}.png"))
            except (OSError, ValueError):
                continue
            buf = ctypes.create_string_buffer(pixels, len(pixels))
            surface = sdl.SDL_CreateRGBSurfaceWithFormatFrom(buf, w, h, 32, w * 4, SDL_PIXELFORMAT_ABGR8888)
            if surface:
                self.art.append(sdl.SDL_CreateTextureFromSurface(self.renderer, surface))
                sdl.SDL_FreeSurface(surface)

    def _open_pad(self, index):
        pad = sdl.SDL_GameControllerOpen(index) if sdl.SDL_IsGameController(index) else sdl.SDL_JoystickOpen(index)
        if pad:
            self.controllers.append(pad)

    def _text(self, font, text, color, center_y, letter_gap=0):
        surface = ttf.TTF_RenderUTF8_Blended(self.fonts[font], text.encode(), SDL_Color(*color, 255))
        if not surface:
            return
        texture = sdl.SDL_CreateTextureFromSurface(self.renderer, surface)
        tw, th = surface.contents.w, surface.contents.h
        sdl.SDL_FreeSurface(surface)
        rect = SDL_Rect((self.w - tw) // 2, int(center_y - th / 2), tw, th)
        sdl.SDL_RenderCopy(self.renderer, texture, None, ctypes.byref(rect))
        sdl.SDL_DestroyTexture(texture)

    def _controllers(self, center_y):
        size = int(self.h * 0.07)
        gap = size // 4
        x = (self.w - (len(self.art) * size + (len(self.art) - 1) * gap)) // 2
        for texture in self.art:
            rect = SDL_Rect(x, int(center_y - size / 2), size, size)
            sdl.SDL_RenderCopy(self.renderer, texture, None, ctypes.byref(rect))
            x += size + gap

    def draw(self, info):
        sdl.SDL_SetRenderDrawColor(self.renderer, *BG, 255)
        sdl.SDL_RenderClear(self.renderer)
        h = self.h
        if info is None:
            self._controllers(h * 0.3)
            self._text("body", "SelfSteam's background service isn't running.", DIM, h * 0.5)
            self._text("body", "Restart the Steam Machine, or open SelfSteam from the desktop.", DIM, h * 0.57)
        else:
            self._controllers(h * 0.135)
            self._text("small", info.get("hostname") or "", DIM, h * 0.19)
            self._text("body", "Open this address on your phone or computer:", DIM, h * 0.27)
            self._text("title", f"http://{info.get('ip')}:{info.get('port')}", ACCENT, h * 0.35)
            self._text("body", "and enter this code:", DIM, h * 0.45)
            self._text("code", " ".join(info.get("code") or "------"), TEXT, h * 0.62)
        self._text("small", "Press any button to close", DIM, h * 0.92)
        sdl.SDL_RenderPresent(self.renderer)

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
                if etype in _CLOSE_EVENTS and time.monotonic() - opened > _IGNORE_INPUT_SECONDS:
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

    def close(self):
        for texture in self.art:
            sdl.SDL_DestroyTexture(texture)
        for font in self.fonts.values():
            if font:
                ttf.TTF_CloseFont(font)
        sdl.SDL_DestroyRenderer(self.renderer)
        sdl.SDL_DestroyWindow(self.window)
        ttf.TTF_Quit()
        sdl.SDL_Quit()


def main():
    screen = Screen()
    try:
        screen.run()
    finally:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
