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

The unpacked files go where the archive was, on the same drive: a
single file straight beside it, several (a .cue and its .bin tracks) in
a folder named after the archive. Someone who keeps games on an SD card
or an external drive does so on purpose, and unpacking a 6 GB disc image
onto a Deck's internal storage behind their back would undo that. Once
the shortcut is made, the archive itself goes to the rubbish bin -- the
drive's own bin, per the freedesktop Trash spec, so it can be restored
from a file manager -- leaving just the playable files. Only a drive that
cannot be written to keeps its archive, and unpacks to a folder the
caller gives instead.

Space is checked before anything is written, so a full drive is
reported as exactly that rather than as a failed unpack. An archive
whose files are already there is not unpacked again.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
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


# Headroom beyond the unpacked size, so unpacking never leaves a drive
# completely full.
_SPACE_MARGIN = 256 * 1024 * 1024


def _writable(folder):
    return os.path.isdir(folder) and os.access(folder, os.W_OK | os.X_OK)


def _stem(archive):
    base = os.path.basename(archive)
    for suffix in _ARCHIVE_SUFFIXES:
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base


def _members(archive):
    """The files inside an archive (not folders), as stored."""
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        result = subprocess.run([bsdtar, "-tf", archive], capture_output=True, text=True)
        if result.returncode == 0:
            return [m for m in result.stdout.splitlines() if m and not m.endswith("/")]
    try:
        if archive.lower().endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                return [i.filename for i in z.infolist() if not i.is_dir()]
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                return [m.name for m in t.getmembers() if m.isfile()]
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        pass
    return []


def target(archive, fallback_root):
    """Where this archive's files go: a single file beside the archive,
    or a folder named after it, on the archive's own drive if that can
    be written to and under fallback_root if not."""
    folder = os.path.dirname(os.path.abspath(archive))
    if not _writable(folder):
        folder = fallback_root
    members = _members(archive)
    if len(members) == 1:
        return os.path.join(folder, os.path.basename(members[0]))
    return os.path.join(folder, _stem(archive))


def is_extracted(archive, fallback_root):
    try:
        return os.path.exists(target(archive, fallback_root))
    except OSError:
        return False


def went_beside(archive, fallback_root):
    """True if the archive's files went next to it, which is when the
    archive itself is no longer needed and can go to the bin."""
    return os.path.dirname(target(archive, fallback_root)) == \
        os.path.dirname(os.path.abspath(archive))


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
    """Unpack archive, once, and return where its files are: the single
    file itself, or the folder holding several.

    Unpacked into a temporary folder on the same drive and moved into
    place only when it succeeds, so an interrupted run never leaves a
    half-unpacked result that a later run would mistake for a finished
    one.
    """
    dest = target(archive, fallback_root)
    if os.path.exists(dest):
        return dest
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    _check_space(archive, parent)
    tmp = tempfile.mkdtemp(prefix=".unpacking-", dir=parent)
    try:
        _unpack(archive, tmp)
        files = _all_files(tmp)
        if len(files) == 1:
            os.rename(files[0], dest)
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def _all_files(folder):
    if os.path.isfile(folder):
        return [folder]
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            out.append(os.path.join(dirpath, name))
    return sorted(out)


def main_file(folder, extensions=None):
    """The one file among what an archive unpacked to (a single file, or
    a folder) that the emulator should be given.

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
    """Every unpacked file with one of these extensions.
    For pickers that take many files at once, like DLC and updates."""
    return [p for p in _all_files(folder)
            if os.path.splitext(p)[1].lower() in extensions]


def as_zip(archive, fallback_root):
    """A .zip holding the same files, for consumers that read a zip
    directly. A .zip is returned as it is; anything else is repacked as
    a .zip of the same name, once, in the same place its files would
    otherwise have been unpacked to."""
    if archive.lower().endswith(".zip"):
        return archive
    zip_path = os.path.join(os.path.dirname(target(archive, fallback_root)),
                            _stem(archive) + ".zip")
    if os.path.isfile(zip_path):
        return zip_path
    parent = os.path.dirname(zip_path)
    os.makedirs(parent, exist_ok=True)
    _check_space(archive, parent)
    tmp = tempfile.mkdtemp(prefix=".unpacking-", dir=parent)
    try:
        _unpack(archive, tmp)
        packed = shutil.make_archive(os.path.join(tmp, "repacked"), "zip", tmp)
        os.rename(packed, zip_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return zip_path


# --- the rubbish bin ------------------------------------------------------


def _mount_top(path):
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def trash(path, home):
    """Move a file to the rubbish bin rather than deleting it.

    Per the freedesktop Trash spec, which is what file managers read: a
    file on the same drive as home goes to ~/.local/share/Trash, and a
    file on any other drive to that drive's own .Trash-<uid> folder, so
    it is moved rather than copied across drives. Each entry gets a
    .trashinfo beside it recording where it came from, so it can be
    restored. The space it uses is only freed once the bin is emptied.
    """
    path = os.path.abspath(path)
    if os.stat(path).st_dev == os.stat(home).st_dev:
        bin_dir = os.path.join(home, ".local", "share", "Trash")
        recorded = urllib.parse.quote(path)
    else:
        top = _mount_top(path)
        bin_dir = os.path.join(top, f".Trash-{os.getuid()}")
        recorded = urllib.parse.quote(os.path.relpath(path, top))
    files_dir = os.path.join(bin_dir, "files")
    info_dir = os.path.join(bin_dir, "info")
    os.makedirs(files_dir, mode=0o700, exist_ok=True)
    os.makedirs(info_dir, mode=0o700, exist_ok=True)

    base = os.path.basename(path)
    name, n = base, 1
    while True:
        info_path = os.path.join(info_dir, name + ".trashinfo")
        try:
            fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            name = f"{base}.{n}"
            continue
        if os.path.exists(os.path.join(files_dir, name)):
            os.close(fd)
            os.remove(info_path)
            n += 1
            name = f"{base}.{n}"
            continue
        break
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("[Trash Info]\n"
                 f"Path={recorded}\n"
                 f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        os.rename(path, os.path.join(files_dir, name))
    except OSError:
        os.remove(info_path)
        raise
