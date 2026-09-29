"""Unpack picked files that arrive compressed, when the emulator cannot
open them that way.

Games, BIOS dumps, keys and DLC are routinely shared as .zip, .7z or
.rar, and most emulators only open the file inside. Rather than make
someone unpack a multi-gigabyte archive on another machine and upload it
again, SelfSteam unpacks it at Create time and hands the emulator the
real file.

Not everything should be unpacked, and the callers decide which, because
it depends on where the file is going:

  * RetroArch opens .zip and .7z content itself, and arcade and Neo Geo
    ROM sets must stay zipped -- the zip *is* the ROM set.
  * The Neo Geo BIOS is neogeo.zip, and has to stay one.
  * Switch firmware is installed from a .zip; SelfSteam reads the
    firmware out of the zip itself.

Everything is unpacked with bsdtar, which reads zip, 7z, rar and tar
alike and is present both on SteamOS and inside SelfSteam's own Flatpak
runtime, so nothing extra needs to be bundled. Python's own zip and tar
support stands in if it is ever missing.

Each archive is unpacked once, into a folder keyed by the archive's
path, size and modification time, so making the same shortcut again, or
a second shortcut from the same archive, does not unpack it again.
"""

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

# Compound suffixes first, so ".tar.gz" is not read as ".gz".
_ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz2", ".txz",
)

# What a picked file is likely to be, once unpacked. An archive often
# carries readmes, cover art and .nfo files beside the one file that
# matters; anything with one of these extensions outranks them.
_CONTENT_EXTENSIONS = frozenset({
    # disc images and their descriptors
    ".iso", ".bin", ".img", ".cue", ".chd", ".gdi", ".cdi", ".ccd",
    ".m3u", ".mds", ".pbp", ".cso", ".ecm",
    # Nintendo
    ".z64", ".n64", ".v64", ".nds", ".3ds", ".cia", ".cci", ".cxi",
    ".rvz", ".wbfs", ".gcm", ".gcz", ".wia", ".ciso", ".dol", ".elf",
    ".wua", ".wud", ".wux", ".rpx", ".nsp", ".xci", ".nsz", ".xcz",
    # Sony, Microsoft, others
    ".pkg", ".vpk", ".pup", ".xiso", ".xex", ".jag", ".j64", ".rom",
    ".mx1", ".mx2", ".dsk", ".keys",
})

# When several content files come out together, the one that describes
# the others is what the emulator should be pointed at.
_DESCRIPTOR_PRIORITY = (".m3u", ".cue", ".gdi", ".ccd", ".mds")

# Files that are the game when they are present, whatever else is there:
# a PS3 folder dump boots from EBOOT.BIN, a Wii U one from its .rpx.
_ENTRY_POINT_NAMES = ("eboot.bin",)


def is_archive(path):
    return bool(path) and path.lower().endswith(_ARCHIVE_SUFFIXES)


def _cache_dir(root, archive):
    st = os.stat(archive)
    key = f"{os.path.abspath(archive)}\0{st.st_size}\0{int(st.st_mtime)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    base = os.path.basename(archive)
    for suffix in _ARCHIVE_SUFFIXES:
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return os.path.join(root, f"{base}-{digest}")


def is_extracted(root, archive):
    try:
        return os.path.isdir(_cache_dir(root, archive))
    except OSError:
        return False


def _unpack(archive, dest):
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        result = subprocess.run(
            [bsdtar, "-xf", archive, "-C", dest],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return
        raise RuntimeError(
            f"Could not decompress {os.path.basename(archive)}: "
            f"{(result.stderr or '').strip() or 'bsdtar failed'}"
        )
    lower = archive.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
        return
    raise RuntimeError(
        f"Could not decompress {os.path.basename(archive)}: "
        "no tool on this machine can read this archive format"
    )


def extract(root, archive):
    """Unpack archive under root, once, and return the folder it is in.

    Unpacked into a temporary folder and moved into place only when it
    succeeds, so an interrupted run never leaves a half-unpacked folder
    behind that a later run would mistake for a finished one.
    """
    dest = _cache_dir(root, archive)
    if os.path.isdir(dest):
        return dest
    os.makedirs(root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".unpacking-", dir=root)
    try:
        _unpack(archive, tmp)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def _all_files(folder):
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            out.append(os.path.join(dirpath, name))
    return sorted(out)


def main_file(folder, extensions=None):
    """The one file in an unpacked folder the emulator should be given.

    extensions narrows it to what a slot accepts (".nsp" for a DLC
    picker, say); otherwise any recognised game format will do. Within
    that: a known entry point, then a descriptor like .cue that names
    the other files, then simply the largest, which is how a BIOS or a
    ROM stands out from a readme sitting beside it.
    """
    files = _all_files(folder)
    if not files:
        return None
    wanted = extensions or _CONTENT_EXTENSIONS

    def ext(path):
        return os.path.splitext(path)[1].lower()

    for path in files:
        if os.path.basename(path).lower() in _ENTRY_POINT_NAMES:
            return path
    candidates = [p for p in files if ext(p) in wanted] or (
        [] if extensions else files
    )
    if not candidates:
        return None
    for descriptor in _DESCRIPTOR_PRIORITY:
        for path in candidates:
            if ext(path) == descriptor:
                return path
    return max(candidates, key=os.path.getsize)


def all_files(folder, extensions):
    """Every file in an unpacked folder with one of these extensions.
    For pickers that take many files at once, like DLC and updates."""
    return [p for p in _all_files(folder)
            if os.path.splitext(p)[1].lower() in extensions]


def as_zip(root, archive):
    """A .zip holding the same files, for consumers that read a zip
    directly. A .zip is returned as it is; anything else is unpacked and
    packed again as a zip, once, beside its unpacked folder."""
    if archive.lower().endswith(".zip"):
        return archive
    folder = extract(root, archive)
    zip_path = folder + ".zip"
    if not os.path.isfile(zip_path):
        tmp = shutil.make_archive(folder + ".repacking", "zip", folder)
        os.rename(tmp, zip_path)
    return zip_path
