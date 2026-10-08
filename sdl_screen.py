"""SelfSteam's SDL drawing, shared by its full-screen windows: the
pairing code screen (code_screen.py) and the "please wait" splash shown
while Steam restarts (splash.py).

Both run on the host, not in SelfSteam's Flatpak, through ctypes against
the system's own libSDL2 and libSDL2_ttf -- the same zero-dependency way
Preflight draws its screens. SteamOS ships no SDL2_image, so PNG art is
decoded here with zlib. The server copies these files out of the Flatpak
to ~/.local/share/selfsteam/ at start, where the host can run them.
"""

import ctypes
import ctypes.util
import os
import subprocess

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
CLOSE_EVENTS = {SDL_KEYDOWN, SDL_MOUSEBUTTONDOWN, SDL_JOYBUTTONDOWN, SDL_CONTROLLERBUTTONDOWN}

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


def font_path(bold):
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


def load_png(path):
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


class Window:
    """A fullscreen SDL window with SelfSteam's look: renderer, size,
    the controller art, and centred text. fonts: {name: (bold, size at
    1080 lines)}; a size may also be a callable of (w, h) for one that
    must fit the width too."""

    def __init__(self, title, fonts, input_devices=False):
        sdl.SDL_SetHint(b"SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", b"0")
        flags = SDL_INIT_VIDEO | (SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER if input_devices else 0)
        if sdl.SDL_Init(flags) != 0:
            raise OSError("SDL_Init failed")
        if ttf.TTF_Init() != 0:
            raise OSError("TTF_Init failed")
        self.window = sdl.SDL_CreateWindow(
            title.encode(), SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, 1280, 720,
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
        paths = {True: font_path(True), False: font_path(False)}
        self.fonts = {}
        for name, (bold, size) in fonts.items():
            px = size(self.w, self.h) if callable(size) else size * self.h / 1080
            self.fonts[name] = ttf.TTF_OpenFont(paths[bold].encode(), max(10, int(px)))
        sdl.SDL_SetHint(b"SDL_RENDER_SCALE_QUALITY", b"1")
        self.art = []
        for name in CONTROLLERS:
            try:
                aw, ah, pixels = load_png(os.path.join(ART_DIR, f"controller-{name}.png"))
            except (OSError, ValueError):
                continue
            buf = ctypes.create_string_buffer(pixels, len(pixels))
            surface = sdl.SDL_CreateRGBSurfaceWithFormatFrom(buf, aw, ah, 32, aw * 4, SDL_PIXELFORMAT_ABGR8888)
            if surface:
                self.art.append(sdl.SDL_CreateTextureFromSurface(self.renderer, surface))
                sdl.SDL_FreeSurface(surface)

    def clear(self):
        sdl.SDL_SetRenderDrawColor(self.renderer, *BG, 255)
        sdl.SDL_RenderClear(self.renderer)

    def present(self):
        sdl.SDL_RenderPresent(self.renderer)

    def text(self, font, text, color, center_y):
        if not text:
            return
        surface = ttf.TTF_RenderUTF8_Blended(self.fonts[font], text.encode(), SDL_Color(*color, 255))
        if not surface:
            return
        texture = sdl.SDL_CreateTextureFromSurface(self.renderer, surface)
        tw, th = surface.contents.w, surface.contents.h
        sdl.SDL_FreeSurface(surface)
        rect = SDL_Rect((self.w - tw) // 2, int(center_y - th / 2), tw, th)
        sdl.SDL_RenderCopy(self.renderer, texture, None, ctypes.byref(rect))
        sdl.SDL_DestroyTexture(texture)

    def controllers(self, center_y, size_ratio=0.07):
        """The four controllers in a row."""
        size = int(self.h * size_ratio)
        gap = size // 4
        x = (self.w - (len(self.art) * size + (len(self.art) - 1) * gap)) // 2
        for texture in self.art:
            rect = SDL_Rect(x, int(center_y - size / 2), size, size)
            sdl.SDL_RenderCopy(self.renderer, texture, None, ctypes.byref(rect))
            x += size + gap

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
