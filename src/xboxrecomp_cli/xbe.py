"""The XBE header, read with the standard library: the title, title ID,
base address and entry point that `new` writes into a scaffold and
`package` checks a dump against, the section and library tables, and D3D's
fence wait, which `new` looks for to fill in the game's fence-mirror line.
The toolkit's xbe_parser reads the whole file; this reads the few fields a
new game needs, before the tools environment that runs xbe_parser exists."""

import re
import struct
from collections import namedtuple

# The entry point at 0x128 is XORed with a key that says retail or debug.
ENTRY_RETAIL_XOR = 0xA8FC57AB
ENTRY_DEBUG_XOR = 0x94859D4B
# What read_header needs: the fixed header through the entry point.
HEADER_LEN = 0x12C
# The certificate: size, time, title ID, then the title in UTF-16LE (40
# wide characters).
CERT_LEN = 0xC + 80
# A section header: flags, VA, virtual size, raw offset, raw size, name VA,
# then reference counts and the digest.
SECTION_LEN = 0x38
SECTION_EXECUTABLE = 0x4
# A library version: an 8-byte name, major, minor, build, flags.
LIBRARY_LEN = 16
# What the table readers need: the fixed header through the library
# table's VA at 0x164.
TABLES_LEN = 0x168


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


def header_size(data):
    """The size of the headers (0x108): the section and library tables and
    their names lie inside it. On every XBE seen it is under 64 KiB, the
    part `new` reads first."""
    _check(data)
    return struct.unpack_from("<I", data, 0x108)[0]


def _table(data, count_at, entry_len, what):
    if len(data) < TABLES_LEN:
        raise XbeError("header too short for the %s table (%d bytes)" % (what, len(data)))
    (base,) = struct.unpack_from("<I", data, 0x104)
    count, va = struct.unpack_from("<2I", data, count_at)
    off = va - base
    if count and (count > 0x1000 or off < 0 or off + count * entry_len > len(data)):
        raise XbeError("%s table at 0x%X (%d entries) outside the header" % (what, va, count))
    return base, count, off


def _name(data, base, va, n=64):
    off = va - base
    if off < 0 or off >= len(data):
        return ""
    return data[off : off + n].split(b"\0", 1)[0].decode("ascii", errors="replace")


Section = namedtuple("Section", "name flags va vsize raw_off raw_size")


def sections(data):
    """[Section]: the section table (count at 0x11C, table VA at 0x120).
    data holds the headers (header_size bytes)."""
    _check(data)
    base, count, off = _table(data, 0x11C, SECTION_LEN, "section")
    out = []
    for i in range(count):
        flags, va, vsize, raw_off, raw_size, name_va = struct.unpack_from(
            "<6I", data, off + i * SECTION_LEN
        )
        out.append(Section(_name(data, base, name_va), flags, va, vsize, raw_off, raw_size))
    return out


def libraries(data):
    """[(name, 'major.minor.build')]: the library version table (count at
    0x160, table VA at 0x164), the XDK libraries the title links and their
    builds."""
    _check(data)
    _, count, off = _table(data, 0x160, LIBRARY_LEN, "library")
    out = []
    for i in range(count):
        raw, major, minor, build = struct.unpack_from("<8s3H", data, off + i * LIBRARY_LEN)
        name = raw.split(b"\0", 1)[0].decode("ascii", errors="replace")
        out.append((name, "%d.%d.%d" % (major, minor, build)))
    return out


# D3D's fence wait, XDK 5849 (D3D8 and D3D8LTCG): mov esi, [D3D__pDevice];
# (LTCG: push edi; mov edi, eax;) mov eax, [esi+get]; mov ecx, [eax];
# mov eax, [esi+put]; mov edx, eax; sub edx, ecx. The device pointer is the
# one wildcard; the two displacements are read from the match, so a build
# that moved the fields reports its own.
FENCE_WAIT = re.compile(
    rb"\x8B\x35(.{4})(?:\x57\x8B\xF8)?\x8B\x46(.)\x8B\x08\x8B\x46(.)\x8B\xD0\x2B\xD1",
    re.DOTALL,
)
PUSH_ESI = 0x56

Fence = namedtuple("Fence", "wait device put get")


def find_d3d_fences(code, va):
    """Every match of D3D's fence wait in code (a section's raw bytes,
    loaded at va), as Fence(wait VA, device pointer, put offset, get-pointer
    offset). The wait VA is the function start, the push esi before the
    pointer load, when that byte is there."""
    out = []
    for m in FENCE_WAIT.finditer(code):
        at = m.start()
        if at and code[at - 1] == PUSH_ESI:
            at -= 1
        (device,) = struct.unpack("<I", m.group(1))
        out.append(Fence(va + at, device, m.group(3)[0], m.group(2)[0]))
    return out


def find_d3d_fence(code, va):
    """The one Fence in code, or None for none or several: two matches
    mean the signature is not specific enough to trust either."""
    found = find_d3d_fences(code, va)
    return found[0] if len(found) == 1 else None
