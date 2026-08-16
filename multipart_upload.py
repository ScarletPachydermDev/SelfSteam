"""Minimal streaming multipart/form-data parser for a single file field.

Deliberately narrow -- this exists for exactly one job (a plain
<input type=file> upload from a zero-JS <form enctype=multipart/
form-data>), not a general multipart library. The one thing that
matters for ROM/BIOS files (can be multi-GB) is never buffering the
whole upload in memory: do_POST's other handlers read the entire body
via self.rfile.read(length) up front, which is fine for small form
posts but not for this.
"""
import re

_CHUNK_SIZE = 65536


def _parse_boundary(content_type):
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise ValueError("no multipart boundary in Content-Type")
    return ("--" + match.group(1)).encode()


def _parse_filename(headers_text):
    for line in headers_text.split("\r\n"):
        if line.lower().startswith("content-disposition"):
            match = re.search(r'filename="([^"]*)"', line)
            if match:
                return match.group(1)
    return None


def save_uploaded_file(rfile, content_type, length, dest_path):
    """Reads a single-file multipart/form-data body from rfile
    (length bytes total) and streams that file's content straight to
    dest_path, never holding more than one chunk in memory. Returns
    the original filename the browser sent (the part's own
    Content-Disposition filename), or None if the part had none.

    Raises ValueError on a malformed body -- this is meant to fail
    loudly, not silently write a truncated/garbage file."""
    boundary = _parse_boundary(content_type)
    remaining = length
    buf = b""

    def read_more():
        nonlocal remaining, buf
        if remaining <= 0:
            raise ValueError("unexpected end of multipart body")
        chunk = rfile.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError("unexpected end of multipart body")
        remaining -= len(chunk)
        buf += chunk

    # Find the opening boundary line.
    while boundary not in buf:
        read_more()
    buf = buf[buf.index(boundary) + len(boundary):]
    while b"\r\n" not in buf[:2]:
        read_more()
    buf = buf[2:]

    # Read this part's headers (ends at the first blank line).
    while b"\r\n\r\n" not in buf:
        read_more()
    header_end = buf.index(b"\r\n\r\n")
    filename = _parse_filename(buf[:header_end].decode("utf-8", errors="replace"))
    buf = buf[header_end + 4:]

    # Stream everything up to the closing boundary straight to disk.
    # Only the trailing (len(closing)-1) bytes of buf are ever held
    # back between reads, so a boundary split across two chunk reads
    # still gets caught before being written out as file content.
    closing = b"\r\n" + boundary
    with open(dest_path, "wb") as out:
        while True:
            idx = buf.find(closing)
            if idx != -1:
                out.write(buf[:idx])
                break
            safe_len = max(0, len(buf) - (len(closing) - 1))
            out.write(buf[:safe_len])
            buf = buf[safe_len:]
            read_more()

    return filename
