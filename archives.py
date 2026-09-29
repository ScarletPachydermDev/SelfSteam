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

That folder goes beside the archive, in a hidden ".selfsteam-extracted"
folder on the same drive. Someone who keeps games on an SD card or an
external drive does so on purpose, and unpacking a 6 GB disc image onto
a Deck's internal storage behind their back would undo that. Only a
drive that cannot be written to falls back to a folder the caller gives.
SelfSteam's own file pickers hide dot-folders, so it never shows up
there. Space is checked before anything is written, so a full drive is
reported as exactly that rather than as a failed unpack.
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


HIDDEN_DIR = ".selfsteam-extracted"

# Headroom beyond the unpacked size, so unpacking never leaves a drive
# completely full.
_SPACE_MARGIN = 256 * 1024 * 1024


def _writable(folder):
    return os.path.isdir(folder) and os.access(folder, os.W_OK | os.X_OK)


def _roots(archive, fallback_root):
    """Where this archive's folder may live, in order of preference."""
    beside = os.path.join(os.path.dirname(os.path.abspath(archive)), HIDDEN_DIR)
    return [beside, fallback_root]


def _root_for(archive, fallback_root):
    beside, fallback = _roots(archive, fallback_root)
    if _writable(beside) or _writable(os.path.dirname(beside)):
        return beside
    return fallback


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


def is_extracted(archive, fallback_root):
    try:
        return any(os.path.isdir(_cache_dir(root, archive))
                   for root in _roots(archive, fallback_root))
    except OSError:
        return False


def _unpacked_size(archive):
    """Total size of the archive's contents, or None if unknown."""
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        result = subprocess.run([bsdtar, "-tvf", archive], capture_output=True, text=True)
        if result.returncode == 0:
            total = 0
            for line in result.stdout.splitlines():
                fields = line.split()
                # ls -l style: mode links owner group SIZE month day time name
                if len(fields) >= 5 and fields[4].isdigit():
                    total += int(fields[4])
            return total
    try:
        if archive.lower().endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                return sum(i.file_size for i in z.infolist())
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                return sum(m.size for m in t.getmembers())
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        pass
    return None


def _mount_point(path):
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def _check_space(archive, root):
    needed = _unpacked_size(archive)
    if needed is None:
        return  # unknown; let the unpack itself report a problem
    probe = root if os.path.isdir(root) else os.path.dirname(root)
    st = os.statvfs(probe)
    free = st.f_bavail * st.f_frsize
    if free < needed + _SPACE_MARGIN:
        raise RuntimeError(
            f"Not enough space on {_mount_point(probe)} to decompress "
            f"{os.path.basename(archive)}: it needs {_size(needed)} "
            f"and {_size(free)} is free"
        )


def _size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{max(1, round(n / 1024 ** 2))} MB"


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


def extract(archive, fallback_root):
    """Unpack archive once and return the folder it is in: beside the
    archive where that drive can be written, otherwise under
    fallback_root.

    Unpacked into a temporary folder and moved into place only when it
    succeeds, so an interrupted run never leaves a half-unpacked folder
    behind that a later run would mistake for a finished one.
    """
    for root in _roots(archive, fallback_root):
        done = _cache_dir(root, archive)
        if os.path.isdir(done):
            return done
    root = _root_for(archive, fallback_root)
    dest = _cache_dir(root, archive)
    os.makedirs(root, exist_ok=True)
    _check_space(archive, root)
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


def as_zip(archive, fallback_root):
    """A .zip holding the same files, for consumers that read a zip
    directly. A .zip is returned as it is; anything else is unpacked and
    packed again as a zip, once, beside its unpacked folder."""
    if archive.lower().endswith(".zip"):
        return archive
    folder = extract(archive, fallback_root)
    zip_path = folder + ".zip"
    if not os.path.isfile(zip_path):
        tmp = shutil.make_archive(folder + ".repacking", "zip", folder)
        os.rename(tmp, zip_path)
    return zip_path
