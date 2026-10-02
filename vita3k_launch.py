#!/usr/bin/env python3
"""Start a PS Vita game in Vita3K the way Vita3K can actually start it.

Vita3K's launch path accepts only three things: a .vpk or .zip, a
folder of content, or a license file (vita3k/main.cpp). A folder dump's
eboot.bin is none of them, so a shortcut pointing at one only ever
reached "is not a supported content type" and Vita3K's own game list.

Handing it the folder works, but Vita3K then installs it -- deleting the
installed copy and copying the whole game again (interface.cpp,
install_content) -- on every single launch. A dump with a license file
(sce_sys/package/work.bin) is also decrypted during that install, so
SelfSteam cannot install it ahead of time by copying files itself.

So this decides at launch time:

  * not installed yet: give Vita3K the folder, through a link whose path
    has no spaces, and it installs the game and starts it;
  * installed: start it by title ID (--installed-path), with no path and
    no copying at all.

The path is passed *relative*, from inside the links folder: Vita3K
parses its command line with Windows-style options allowed (config.cpp,
allow_windows_style_options), which reads any argument starting with
"/" as an option -- so every absolute Linux path was swallowed before
it was ever seen as content. On a Steam Machine, an absolute path to
the link produced no "input-content-path" line in Vita3K's log at all,
and one with spaces in it came out as a single word of the folder name
("2011").

Run by the launcher in place of the shortcut's own command, for Vita3K
shortcuts only; everything else is passed through untouched.
"""

import os
import struct
import sys


def _vita_fs():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data, "Vita3K", "Vita3K")


def _links_dir():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data, "selfsteam", "vita3k-content")


def title_id(folder):
    """TITLE_ID from a game's sce_sys/param.sfo, or None."""
    try:
        with open(os.path.join(folder, "sce_sys", "param.sfo"), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:4] != b"\x00PSF" or len(data) < 20:
        return None
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    for i in range(count):
        key_off, _fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 20 + i * 16)
        key_end = data.index(b"\x00", key_start + key_off)
        if data[key_start + key_off:key_end] == b"TITLE_ID":
            raw = data[data_start + data_off:data_start + data_off + length]
            return raw.rstrip(b"\x00").decode("ascii", "replace") or None
    return None


def _installed(tid):
    path = os.path.join(_vita_fs(), "ux0", "app", tid)
    return os.path.isdir(path) and bool(os.listdir(path))


def _relative_link(folder, tid):
    """A link to folder named after its title ID, returned as a path
    relative to the links folder -- which becomes the working folder, so
    Vita3K resolves it -- since an absolute one never reaches it."""
    os.makedirs(_links_dir(), exist_ok=True)
    link = os.path.join(_links_dir(), tid)
    if os.path.islink(link) and os.readlink(link) != folder:
        os.remove(link)
    if not os.path.lexists(link):
        os.symlink(folder, link)
    os.chdir(_links_dir())
    return os.path.join(".", tid)


def rewrite(args):
    """The command to run instead, or args unchanged if this is not a
    Vita3K launch of a game folder."""
    exe = next((i for i, a in enumerate(args) if os.path.basename(a).startswith("Vita3K")), None)
    if exe is None:
        return args
    for i in range(len(args) - 1, exe, -1):
        arg = args[i]
        folder = os.path.dirname(arg) if os.path.basename(arg).lower() == "eboot.bin" else arg
        if not os.path.isdir(folder):
            continue
        tid = title_id(folder)
        if not tid:
            return args
        if _installed(tid):
            return args[:i] + ["--installed-path", tid] + args[i + 1:]
        return args[:i] + [_relative_link(folder, tid)] + args[i + 1:]
    return args


def main(argv):
    args = argv[1:]
    if not args:
        return 1
    try:
        args = rewrite(args)
    except Exception:
        pass  # launch as given rather than not at all
    os.execvp(args[0], args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
