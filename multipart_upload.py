"""Minimal streaming multipart/form-data parser for file field(s).

Deliberately narrow -- this exists for exactly one job (a plain
<input type=file[ multiple]> upload from a zero-JS <form enctype=
multipart/form-data>), not a general multipart library. The one thing
that matters for ROM/BIOS/DLC files (can be multi-GB, or many-at-once
for the DLC+updates picker) is never buffering a whole upload in
memory: do_POST's other handlers read the entire body via
self.rfile.read(length) up front, which is fine for small form posts
but not for this.
"""
import re

_CHUNK_SIZE = 65536

# Functions:
#   _parse_boundary(content_type) -- the multipart boundary bytes from a Content-Type header.
#   _parse_filename(headers_text) -- the uploaded filename from a part's own headers.
#   _stream_parts(rfile, content_type, length, dest_path_for) -- the shared
#       part-by-part streaming loop both functions below are built from.
#   save_uploaded_file(rfile, content_type, length, dest_path) -- streams one uploaded file to disk.
#   save_uploaded_files(rfile, content_type, length, make_dest_path) -- streams every
#       file part in the body (a `multiple` file input) to disk.


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


def _stream_parts(rfile, content_type, length, dest_path_for):
    """Shared streaming loop: walks every part in a multipart/form-data
    body, calling dest_path_for(filename) once per part to decide where
    (if anywhere) to write it. dest_path_for returning None skips that
    part (still consumes its bytes -- has to, there's no seeking a
    request body) without writing it anywhere or including it in the
    returned list. Stops at the real terminal boundary ("--boundary--"),
    unlike the old single-file version this replaces (which stopped
    after its one expected part and left the rest of the body unread) --
    safe here specifically because every call site already reads a
    fresh HTTP/1.0 request per connection (see selfsteam_server.py's own
    Handler), so nothing downstream ever depends on a partially-drained
    socket lining back up with a next request.

    Returns [(filename, dest_path), ...] for every part that got a
    non-None dest_path_for(...). Raises ValueError on a malformed body
    -- this is meant to fail loudly, not silently write a truncated/
    garbage file."""
    boundary = _parse_boundary(content_type)
    remaining = length
    buf = b""
    results = []

    def read_more():
        nonlocal remaining, buf
        if remaining <= 0:
            raise ValueError("unexpected end of multipart body")
        chunk = rfile.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError("unexpected end of multipart body")
        remaining -= len(chunk)
        buf += chunk

    # Consume the preamble up through the very first boundary marker --
    # after this, buf is positioned exactly where it ends up after every
    # subsequent part too (see the `closing` consumption below), so the
    # loop itself never needs to search for a boundary a second time.
    while boundary not in buf:
        read_more()
    buf = buf[buf.index(boundary) + len(boundary):]

    while True:
        while len(buf) < 2:
            read_more()
        if buf[:2] == b"--":
            # The real terminal boundary -- no more parts.
            return results
        while b"\r\n" not in buf[:2]:
            read_more()
        buf = buf[2:]

        # Read this part's headers (ends at the first blank line).
        while b"\r\n\r\n" not in buf:
            read_more()
        header_end = buf.index(b"\r\n\r\n")
        filename = _parse_filename(buf[:header_end].decode("utf-8", errors="replace"))
        buf = buf[header_end + 4:]

        dest_path = dest_path_for(filename)

        # Stream everything up to the closing boundary straight to disk
        # (or just discard it, if this part isn't wanted). Only the
        # trailing (len(closing)-1) bytes of buf are ever held back
        # between reads, so a boundary split across two chunk reads
        # still gets caught before being written out as file content.
        closing = b"\r\n" + boundary
        out = open(dest_path, "wb") if dest_path else None
        try:
            while True:
                idx = buf.find(closing)
                if idx != -1:
                    if out:
                        out.write(buf[:idx])
                    buf = buf[idx + len(closing):]
                    break
                safe_len = max(0, len(buf) - (len(closing) - 1))
                if out:
                    out.write(buf[:safe_len])
                buf = buf[safe_len:]
                read_more()
        finally:
            if out:
                out.close()

        if dest_path:
            results.append((filename, dest_path))


def save_uploaded_file(rfile, content_type, length, dest_path):
    """Reads a single-file multipart/form-data body from rfile
    (length bytes total) and streams that file's content straight to
    dest_path, never holding more than one chunk in memory. Returns
    the original filename the browser sent (the part's own
    Content-Disposition filename), or None if the part had none.

    Only ever looks at the first part -- fine for every existing
    single-<input type=file> call site, which never sends more than
    one."""
    first = []

    def dest_path_for(filename):
        if first:
            return None
        first.append(filename)
        return dest_path

    _stream_parts(rfile, content_type, length, dest_path_for)
    if not first:
        raise ValueError("multipart body had no file part")
    return first[0]


def save_uploaded_files(rfile, content_type, length, make_dest_path):
    """Like save_uploaded_file, but for a `multiple` file input: streams
    every file part in the body to its own destination. make_dest_path
    is called once per part with that part's own filename, and must
    return a real path to stream it to -- there's no skip-this-one
    option here, unlike _stream_parts' own dest_path_for, since every
    real caller (the DLC+updates picker) wants every selected file.
    Returns [(filename, dest_path), ...] in the order the browser sent
    them."""
    return _stream_parts(rfile, content_type, length, make_dest_path)
