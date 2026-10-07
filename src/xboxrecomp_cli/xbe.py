"""The XBE header, read with the standard library: the title, title ID,
base address and entry point that `new` writes into a scaffold and
`package` checks a dump against. The toolkit's xbe_parser reads the whole
file; this reads the few fields a game's manifest needs, before the tools
environment that runs xbe_parser exists."""

import struct

# The entry point at 0x128 is XORed with a key that says retail or debug.
ENTRY_RETAIL_XOR = 0xA8FC57AB
ENTRY_DEBUG_XOR = 0x94859D4B
# What read_header needs: the fixed header through the entry point.
HEADER_LEN = 0x12C
# The certificate: size, time, title ID, then the title in UTF-16LE (40
# wide characters).
CERT_LEN = 0xC + 80


class XbeError(ValueError):
    pass


def _check(data):
    if len(data) < HEADER_LEN or data[:4] != b"XBEH":
        raise XbeError("not an XBE (no XBEH magic)")


def title_id(data):
    """The certificate's title ID. Header: 'XBEH', base address at 0x104,
    certificate VA at 0x118; the certificate is size, time, then title ID."""
    if len(data) < 0x11C or data[:4] != b"XBEH":
        raise XbeError("not an XBE (no XBEH magic)")
    (base,) = struct.unpack_from("<I", data, 0x104)
    (cert,) = struct.unpack_from("<I", data, 0x118)
    off = cert - base
    if off < 0 or off + 12 > len(data):
        raise XbeError("certificate address 0x%X outside the header" % cert)
    (tid,) = struct.unpack_from("<I", data, off + 8)
    return tid


def read_header(data):
    """{title, title_id, base, entry, kind}: kind is 'retail' or 'debug',
    whichever key decodes an entry point inside the image (retail first).
    The title is '' when the certificate's name is empty or out of reach."""
    _check(data)
    base, _, image_size = struct.unpack_from("<3I", data, 0x104)
    (cert,) = struct.unpack_from("<I", data, 0x118)
    (entry_raw,) = struct.unpack_from("<I", data, 0x128)
    tid = title_id(data)
    entry, kind = entry_raw ^ ENTRY_RETAIL_XOR, "retail"
    if not base <= entry < base + image_size:
        alt = entry_raw ^ ENTRY_DEBUG_XOR
        if base <= alt < base + image_size:
            entry, kind = alt, "debug"
    off = cert - base
    title = ""
    if 0 <= off and off + CERT_LEN <= len(data):
        raw = data[off + 0xC : off + CERT_LEN]
        title = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0].strip()
    return {"title": title, "title_id": tid, "base": base, "entry": entry, "kind": kind}


def read_file(path):
    with open(path, "rb") as f:
        return read_header(f.read(0x10000))
