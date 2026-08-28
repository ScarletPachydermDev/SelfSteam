"""Read just enough of a Switch .nsp (PFS0-container) file to identify
which game it belongs to, without needing to fully mount/decrypt it the
way an emulator does.

Only the NCA *header* (first 0x400 bytes of the .cnmt.nca inside the
PFS0) is decrypted -- that's enough to read the content type and title
ID fields, both stored in plaintext-after-decryption in the header
itself, well before anything that needs a per-title key. The header is
encrypted with a single fixed AES-128-XTS key ("header_key") that's
identical on every real Switch and every NSP dump -- not a per-console
or per-title secret -- so this deliberately reads it out of the user's
own already-installed prod.keys (see standalone_emulators.py's
needs_keys/install_keys flow) rather than hardcoding Nintendo's crypto
constant directly in this repo's source.

Functions:
  read_header_key(prod_keys_path) -- prod.keys -> the 32-byte header key.
  _decrypt_nca_header(data, header_key) -- first 0x400 bytes of an NCA,
      encrypted -> decrypted (AES-128-XTS, Nintendo's big-endian tweak).
  _pfs0_entries(f) -- open PFS0 file -> [(name, offset, size), ...].
  read_title_id(nsp_path, header_key) -- .nsp path -> TitleIdInfo, by
      locating and decrypting the header of its first *.cnmt.nca entry.
"""
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_NCA_HEADER_SECTOR_SIZE = 0x200
_NCA_HEADER_SECTORS_NEEDED = 2  # 0x400 bytes covers magic/content-type/title-id.

# NCA header, offsets within the decrypted first 0x400 bytes -- stable,
# publicly documented format (switchbrew.org's NCA_Format page), used
# identically by every real NCA tool (hactool, LibHac, etc). Confirmed
# against Ryujinx's own real source (Ryubing-1.3.3 checkout,
# NcaExtensions.cs/DownloadableContentsHelper.cs) for which fields
# this feature actually needs -- Header.TitleId and Header.ContentType.
_OFF_MAGIC = 0x200
_OFF_CONTENT_TYPE = 0x205
_OFF_TITLE_ID = 0x210

# LibHac's NcaContentType enum ordering (Ryubing's own dependency,
# referenced by IsProgram/IsControl in NcaExtensions.cs).
CONTENT_TYPE_NAMES = {
    0: "Program",
    1: "Meta",
    2: "Control",
    3: "Manual",
    4: "Data",
    5: "PublicData",
}


class NspParseError(Exception):
    pass


@dataclass
class TitleIdInfo:
    title_id: int          # e.g. 0x01006FE013472000
    title_id_base: int      # title_id with the low 13 bits cleared -- the
                             # same masking DownloadableContentModel.cs and
                             # TitleUpdateModel.cs use to compare a DLC/
                             # update against its base game, since DLC and
                             # multi-program titles vary only those bits.
    content_type: str       # CONTENT_TYPE_NAMES value, expected "Meta" here.
    cnmt_nca_name: str       # PFS0 entry name this was read from, for logging.

    @property
    def title_id_str(self):
        return f"{self.title_id:016x}"


def read_header_key(prod_keys_path):
    """The fixed 32-byte AES-128-XTS header key out of a real prod.keys
    file. Not console-unique -- every real Switch and every real NSP
    dump uses this same key -- but still sourced from the user's own
    key dump rather than bundled here."""
    with open(prod_keys_path, encoding="utf-8") as f:
        for line in f:
            if line.strip().lower().startswith("header_key"):
                _, _, value = line.partition("=")
                hex_key = value.strip()
                if len(hex_key) != 64:
                    raise NspParseError(f"header_key in {prod_keys_path} is not 32 bytes")
                return bytes.fromhex(hex_key)
    raise NspParseError(f"No header_key line found in {prod_keys_path}")


def _decrypt_nca_header(data, header_key):
    """AES-128-XTS over the first `len(data)` bytes (must be a multiple
    of 0x200), using Nintendo's own sector-tweak convention: the sector
    index is encoded big-endian, not the little-endian the XTS standard
    (and every general-purpose crypto library's default) expects. This
    quirk is well documented across every homebrew NCA tool and is the
    one detail here not read straight out of Ryubing's own source (that
    lives in LibHac, an external dependency not vendored in this
    checkout) -- flagged so it's the first thing to double check if
    decryption ever produces a wrong-looking magic/title ID."""
    key1, key2 = header_key[:16], header_key[16:]
    cipher = Cipher(algorithms.AES(key1 + key2), modes.XTS(b"\x00" * 16))
    out = bytearray()
    for sector_index in range(len(data) // _NCA_HEADER_SECTOR_SIZE):
        tweak = sector_index.to_bytes(16, "big")
        decryptor = Cipher(algorithms.AES(key1 + key2), modes.XTS(tweak)).decryptor()
        chunk = data[sector_index * _NCA_HEADER_SECTOR_SIZE : (sector_index + 1) * _NCA_HEADER_SECTOR_SIZE]
        out += decryptor.update(chunk) + decryptor.finalize()
    return bytes(out)


def _pfs0_entries(f):
    """[(name, data_offset, size), ...] for a PFS0 (NSP) container
    already open in binary mode, positioned/seeked from 0. Format:
    magic(4)="PFS0", num_files(u32), string_table_size(u32), reserved(4),
    then num_files * 24-byte entries (offset u64, size u64,
    name_offset u32, reserved u32), then the string table, then file
    data starts right after (no extra alignment padding, unlike HFS0)."""
    f.seek(0)
    magic = f.read(4)
    if magic != b"PFS0":
        raise NspParseError(f"Not a PFS0 container (magic was {magic!r})")
    num_files, string_table_size = struct.unpack("<II", f.read(8))
    f.read(4)  # reserved
    entries_raw = []
    for _ in range(num_files):
        offset, size, name_offset, _reserved = struct.unpack("<QQII", f.read(24))
        entries_raw.append((offset, size, name_offset))
    string_table = f.read(string_table_size)
    header_size = 16 + num_files * 24 + string_table_size
    entries = []
    for offset, size, name_offset in entries_raw:
        name_end = string_table.index(b"\x00", name_offset)
        name = string_table[name_offset:name_end].decode("utf-8")
        entries.append((name, header_size + offset, size))
    return entries


def read_title_id(nsp_path, header_key):
    """Open `nsp_path`, find its first *.cnmt.nca entry, decrypt just
    that NCA's header, and return the TitleIdInfo it describes. This is
    the "Meta" content's own title ID, which is the same title ID the
    real content it describes belongs to -- confirmed via Ryubing's own
    NcaExtensions.GetCnmt, which opens the cnmt file at a path built
    from `{cnmtNca.Header.TitleId:x16}` -- so no need to parse the
    actual Cnmt content-entry table just to identify the title.
    Raises NspParseError on anything that isn't a well-formed NSP with
    a readable cnmt.nca (wrong container type, no cnmt.nca entry, wrong
    header key, unsupported NCA format version)."""
    with open(nsp_path, "rb") as f:
        entries = _pfs0_entries(f)
        cnmt_entry = next((e for e in entries if e[0].endswith(".cnmt.nca")), None)
        if cnmt_entry is None:
            raise NspParseError(f"No *.cnmt.nca entry found in {nsp_path}")
        name, data_offset, _size = cnmt_entry
        f.seek(data_offset)
        needed = _NCA_HEADER_SECTOR_SIZE * _NCA_HEADER_SECTORS_NEEDED
        encrypted_header = f.read(needed)
        if len(encrypted_header) != needed:
            raise NspParseError(f"{name} in {nsp_path} is truncated")

    header = _decrypt_nca_header(encrypted_header, header_key)
    magic = header[_OFF_MAGIC : _OFF_MAGIC + 4]
    if magic != b"NCA3":
        raise NspParseError(
            f"{name} in {nsp_path} decrypted to magic {magic!r}, expected NCA3 "
            "(wrong header_key, or an older NCA2/NCA0-format dump this parser doesn't support)"
        )
    content_type = CONTENT_TYPE_NAMES.get(header[_OFF_CONTENT_TYPE], "Unknown")
    title_id = struct.unpack("<Q", header[_OFF_TITLE_ID : _OFF_TITLE_ID + 8])[0]
    return TitleIdInfo(
        title_id=title_id,
        title_id_base=title_id & ~0x1FFF,
        content_type=content_type,
        cnmt_nca_name=name,
    )
