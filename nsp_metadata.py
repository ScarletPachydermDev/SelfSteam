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

Also reads a DLC's actual payload content NCA id (read_dlc_content_nca_id)
-- the one piece Ryubing's own dlc.json needs beyond a title ID, since it
points at a path *inside* the DLC's own PFS0, not just the container's
path (see standalone_emulators.py's own docstring on that). Getting that
id means actually decrypting NCA section 0 (AES-128-CTR, a per-title-
generation key unwrapped from the NCA's own key area) to reach the real
Cnmt content-entry table -- unlike the header-only title-ID read above,
which needs no section decryption at all. The exact byte offsets, key
derivation, and counter construction here are ported from LibHac's own
real source (git.ryujinx.app/projects/LibHac.git -- its GitHub original,
Thealexbarney/LibHac, is gone with no forks left, but the Ryujinx-hosted
Forgejo mirror is real and current), not reconstructed from memory of
the public NCA spec -- confirmed correct against two real DLC files: one
cross-checked against its own bundled .cnmt.xml sidecar (a common but
NOT universal scene convention -- confirmed NOT present on the other
file, which this still worked on without it), the other cross-checked
against its own outer PFS0's own file list.

Functions:
  read_header_key(prod_keys_path) -- prod.keys -> the 32-byte header key.
  read_named_key(prod_keys_path, name) -- prod.keys -> any one named key line.
  _decrypt_nca_header(data, header_key) -- an NCA's header bytes (any
      multiple of 0x200, from offset 0), encrypted -> decrypted
      (AES-128-XTS, Nintendo's big-endian tweak).
  _pfs0_entries(f) -- open PFS0 file/BytesIO -> [(name, offset, size), ...].
  read_title_id(nsp_path, header_key) -- .nsp path -> TitleIdInfo, by
      locating and decrypting the header of its first *.cnmt.nca entry.
  _nca_content_key(header_full, prod_keys_path) -- a non-RightsId NCA's
      real AesCtr content key (key-area unwrap).
  _nca_section0_pfs0(nsp_path, nca_data_offset, header_full, prod_keys_path)
      -- decrypts NCA section 0 and returns its real PFS0 bytes (past
      the HierarchicalSha256 integrity-info's own data-level offset).
  read_dlc_content_nca_id(nsp_path, header_key, prod_keys_path) -- a
      DLC .nsp -> the hex id (PFS0 filename, sans ".nca") of its actual
      payload content NCA.
"""
import io
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


def read_named_key(prod_keys_path, name):
    """Any single `<name> = <hex>` line out of a real prod.keys file
    (header_key, key_area_key_<application/ocean/system>_<XX>,
    titlekek_<XX>, ...). None if the line isn't there at all -- a
    missing key_area_key for a newer key generation than the user's
    own dump has is a real, expected case (an unrecognized-format
    error, not a parse bug), not something to raise on here."""
    with open(prod_keys_path, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.strip().partition("=")
            if key.strip().lower() == name.lower():
                hex_value = value.strip()
                return bytes.fromhex(hex_value) if hex_value else None
    return None


def read_header_key(prod_keys_path):
    """The fixed 32-byte AES-128-XTS header key out of a real prod.keys
    file. Not console-unique -- every real Switch and every real NSP
    dump uses this same key -- but still sourced from the user's own
    key dump rather than bundled here."""
    key = read_named_key(prod_keys_path, "header_key")
    if key is None:
        raise NspParseError(f"No header_key line found in {prod_keys_path}")
    if len(key) != 32:
        raise NspParseError(f"header_key in {prod_keys_path} is not 32 bytes")
    return key


# LibHac's own KakNames (Nca.cs) -- which of prod.keys' three
# key_area_key_<name>_<rev> line families a given NCA's own
# KeyAreaKeyIndex header byte (0/1/2) selects.
_KAK_NAMES = ("application", "ocean", "system")


def _master_key_revision(key_generation):
    """LibHac's own Utilities.GetMasterKeyRevision -- the real prod.keys
    line suffix (key_area_key_*_<rev:02x>) is one less than the NCA
    header's own KeyGeneration byte, except generation 0 which stays 0."""
    return 0 if key_generation == 0 else key_generation - 1


def _aes_ecb_decrypt(key, data):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _aes_ecb_encrypt(key, data):
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _nca_content_key(header_full, prod_keys_path):
    """The real AesCtr content key for section 0 of a non-RightsId NCA
    -- ported from LibHac's own Nca.GetDecryptedKey/GetContentKey: the
    key-area's slot 2 (NcaKeyType.AesCtr) is AES-ECB-decrypted with
    key_area_key_<name>_<rev> (name picked by the header's own
    KeyAreaKeyIndex byte, rev by KeyGeneration). header_full is the full
    decrypted 0xC00-byte NCA header (_decrypt_nca_header's own output
    when called with that many bytes).

    Raises NspParseError if this NCA uses RightsId/title-key crypto
    instead (a real ticket + titlekek, not this function's job) or if
    prod.keys is missing the specific key_area_key line needed --
    confirmed via real files that every DLC/update Meta content this
    feature deals with uses the plain key-area path (RightsId all-
    zero), never title-key crypto."""
    rights_id = header_full[0x230:0x240]
    if rights_id != b"\x00" * 16:
        raise NspParseError("This NCA uses title-key crypto (has a RightsId) -- not supported here")
    key_generation = max(header_full[0x206], header_full[0x220])
    key_area_key_index = header_full[0x207]
    if key_area_key_index >= len(_KAK_NAMES):
        raise NspParseError(f"Unrecognized KeyAreaKeyIndex {key_area_key_index}")
    key_name = f"key_area_key_{_KAK_NAMES[key_area_key_index]}_{_master_key_revision(key_generation):02x}"
    key_area_key = read_named_key(prod_keys_path, key_name)
    if key_area_key is None:
        raise NspParseError(f"prod.keys is missing {key_name} -- can't decrypt this NCA's content section")
    encrypted_key = header_full[0x300 + 0x10 * 2 : 0x300 + 0x10 * 3]
    return _aes_ecb_decrypt(key_area_key, encrypted_key)


def _ctr_decrypt(content_key, upper_counter, base_offset, data):
    """AES-128-CTR decrypt using Nintendo's own NCA counter convention
    -- ported from LibHac's own Aes128CtrStorage.CreateCounter/
    UpdateCounter: a big-endian 16-byte counter, the upper 8 bytes the
    FS-header's own "Counter" field (0 for every Meta content this
    feature deals with), the lower 8 bytes the *absolute* NCA-file
    block index (byte_offset // 0x10) -- not relative to the section's
    own start, which is why base_offset (the section's own absolute
    start within the NCA) has to be added in by the caller rather than
    always starting the counter at 0."""
    out = bytearray()
    for i in range(0, len(data), 16):
        block = data[i : i + 16]
        counter = upper_counter.to_bytes(8, "big") + ((base_offset + i) // 0x10).to_bytes(8, "big")
        keystream = _aes_ecb_encrypt(content_key, counter)
        out += bytes(a ^ b for a, b in zip(block, keystream[: len(block)]))
    return bytes(out)


def _nca_section0_pfs0(nsp_path, nca_data_offset, header_full, prod_keys_path):
    """Decrypts NCA section 0 and returns the real PFS0 bytes at its
    HierarchicalSha256 integrity info's own data level -- every Meta-
    content NCA this feature deals with uses exactly this shape
    (AesCtr encryption + a 2-level SHA256 hash layout, the second level
    being the real data), confirmed against two real DLC files, not
    assumed. Raises NspParseError if a given NCA's section 0 isn't
    AesCtr-encrypted -- an honest "can't handle this one" rather than
    silently misreading a different layout as PFS0 bytes."""
    fsh0 = header_full[0x400:0x600]
    encryption_type = fsh0[0x04]
    if encryption_type != 3:  # NcaEncryptionType.AesCtr
        raise NspParseError(f"Unsupported NCA section 0 encryption type {encryption_type} (expected AesCtr)")
    _block_size, level_count = struct.unpack("<ii", fsh0[0x28:0x30])
    levels = [struct.unpack("<qq", fsh0[0x30 + 0x10 * i : 0x40 + 0x10 * i]) for i in range(level_count)]
    if not levels:
        raise NspParseError("NCA section 0 has no integrity-info levels")

    content_key = _nca_content_key(header_full, prod_keys_path)
    start_block, end_block = struct.unpack("<ii", header_full[0x240:0x248])
    sect_start, sect_end = start_block * 0x200, end_block * 0x200
    upper_counter = struct.unpack("<Q", fsh0[0x140:0x148])[0]

    with open(nsp_path, "rb") as f:
        f.seek(nca_data_offset + sect_start)
        section_bytes = f.read(sect_end - sect_start)

    plaintext = _ctr_decrypt(content_key, upper_counter, sect_start, section_bytes)
    data_offset, data_size = levels[-1]
    return plaintext[data_offset : data_offset + data_size]


# Cnmt binary format (switchbrew's own "CNMT" page) content-entry
# "Type" byte -- LibHac's own Ncm.ContentType enum, confirmed via its
# real source. Only Data (the actual DLC payload NCA, as opposed to
# Meta/Control/HtmlDocument/LegalInformation/DeltaFragment) matters
# here.
_CNMT_CONTENT_TYPE_DATA = 2


def read_dlc_content_nca_id(nsp_path, header_key, prod_keys_path):
    """For a DLC .nsp, finds its actual payload content NCA's id (the
    PFS0 filename, sans ".nca") -- the piece Ryubing's own dlc.json
    needs beyond a title ID (see standalone_emulators.py's own
    docstring on why). Confirmed against two real DLC files (see this
    module's own docstring)."""
    with open(nsp_path, "rb") as f:
        entries = _pfs0_entries(f)
        cnmt_entry = next((e for e in entries if e[0].endswith(".cnmt.nca")), None)
        if cnmt_entry is None:
            raise NspParseError(f"No *.cnmt.nca entry found in {nsp_path}")
        name, data_offset, _size = cnmt_entry
        f.seek(data_offset)
        encrypted_header = f.read(0xC00)

    header_full = _decrypt_nca_header(encrypted_header, header_key)
    magic = header_full[_OFF_MAGIC : _OFF_MAGIC + 4]
    if magic != b"NCA3":
        raise NspParseError(f"{name} in {nsp_path} decrypted to magic {magic!r}, expected NCA3")

    pfs0_bytes = _nca_section0_pfs0(nsp_path, data_offset, header_full, prod_keys_path)
    inner = io.BytesIO(pfs0_bytes)
    inner_entries = _pfs0_entries(inner)
    cnmt_entry = next((e for e in inner_entries if e[0].endswith(".cnmt")), None)
    if cnmt_entry is None:
        raise NspParseError(f"No .cnmt entry inside {name}'s own content section")
    cnmt_name, cnmt_off, cnmt_size = cnmt_entry
    inner.seek(cnmt_off)
    cnmt = inner.read(cnmt_size)

    table_offset = struct.unpack("<H", cnmt[0x0E:0x10])[0]
    n_content = struct.unpack("<H", cnmt[0x10:0x12])[0]
    entries_start = 0x20 + table_offset
    for i in range(n_content):
        entry = cnmt[entries_start + i * 0x38 : entries_start + (i + 1) * 0x38]
        content_type = entry[0x36]
        if content_type == _CNMT_CONTENT_TYPE_DATA:
            return entry[0x20:0x30].hex()

    raise NspParseError(f"No Data content entry found in {cnmt_name}")


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
