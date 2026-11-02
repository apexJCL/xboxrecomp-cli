"""Frame dumps as lossless PNG: the toolkit's dumpers write BMP (a copy in
the render path, so the game's timing is untouched) and the CLI turns them
into PNG once the run is over. Standard library only.

  <game> frames compact [--dry-run] [--jobs N] [--pins FILE]... [--no-pins] DIR...
  xbr frames compact ...            the same outside a game

compact: every *.bmp under each DIR (links never followed, nothing outside
DIR touched) becomes a PNG of the same base name (flip_01139.bmp ->
flip_01139.png) with the same pixels: the PNG is written beside it, read
back from disk with a decoder of its own and compared with the BMP's
pixels, and only then is the BMP removed. A mismatch, a BMP this reader
does not take (only uncompressed 24 and 32 bpp with the 40-byte
BITMAPINFOHEADER: a V4 or V5 header's colour space, ICC profile or
resolution has no place in the PNG, so such a BMP is left) or a PNG of that name with
other pixels keeps the BMP, named on stderr (exit 1 for a mismatch or a
conflict). The PNG takes the BMP's mtime, so age rules that read it are
unchanged.

A 32 bpp dump's fourth byte is BI_RGB's reserved byte (the dumpers write
the render target's alpha, 0 in every dump so far). When it is 0 in every
pixel the PNG is RGB, as every reader reads the frame (the golden hash is
of the RGB pixels); otherwise (a constant 0xFF too) the PNG is RGBA, so no
byte of the dump is lost either way.

Pins: what the game's docs cite stays BMP, as benchdb would keep it.
- golden.json (game.toml golden.json) and each --pins FILE pin a file by
  its base name or its path under DIR: golden.json cites the runs its
  references came from, and `golden reference` reads those dumps by name.
- The reference text benchdb reads (golden.json, TASKS.md, RESUME*.md,
  openspec/, the seeds and game.toml bench.gc.refs, less refs_exclude; in
  the main checkout and in the worktree compact runs from) pins a file by
  a path that names it (runs/<topic>/shot.bmp), every file in a folder it
  names by path (runs/<topic>), and every file of a run whose stamp
  (YYYYMMDD-HHMMSS) it names.
--pins adds files, --no-pins drops the defaults.

--dry-run converts in memory, checks, and prints what each DIR would save
and each file left as pinned; it writes and removes nothing.

The commands that run the game and keep frames (bench golden, golden run,
hud-map capture and check) do the same to the frames they keep, once the
run is over and the check has read them. XBR_FRAMES_BMP=1 (the environment,
or scripts/bench.env for bench) or --bmp-frames keeps their BMPs.
"""

import os
import re
import struct
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from itertools import accumulate

PNG_SIG = b"\x89PNG\r\n\x1a\n"
OPT_OUT = "XBR_FRAMES_BMP"
STAMP_RE = re.compile(r"(?<!\d)(\d{8}-\d{6})(?!\d)")


class FrameError(Exception):
    pass


def keep_bmp(env=None, flag=False):
    """Whether a run's frames stay BMP: --bmp-frames, or XBR_FRAMES_BMP=1."""
    env = os.environ if env is None else env
    return bool(flag) or (env.get(OPT_OUT) or "0") == "1"


# ---- naming: a reader takes the PNG, else the BMP -----------------------


def existing(path):
    """The dump `path` names (either spelling), as it is on disk: its .png
    when there is one, else its .bmp, else path itself (so a message names
    what the dumper writes)."""
    stem, ext = os.path.splitext(path)
    if ext.lower() not in (".bmp", ".png"):
        return path
    for e in (".png", ".bmp"):
        if os.path.exists(stem + e):
            return stem + e
    return path


def with_png(name):
    return os.path.splitext(name)[0] + ".png"


# ---- BMP in: (w, h, bpp, pixels top-down, RGB or RGBA) ------------------


def read_bmp_px(path):
    """The pixels of an uncompressed 24 or 32 bpp BMP, rows top-down, as
    (w, h, bpp, bytes) with bpp 3 (RGB) or 4 (RGBA, unless the fourth
    byte is 0 in every pixel). FrameError for anything else."""
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 54 or d[:2] != b"BM":
        raise FrameError("not a BMP")
    off = struct.unpack_from("<I", d, 10)[0]
    hsize = struct.unpack_from("<I", d, 14)[0]
    if hsize != 40:
        # A V4/V5 header's colour space, ICC profile and resolution would
        # be lost; the core header is another layout.
        raise FrameError("BMP with a %d-byte header not supported (40 only)" % hsize)
    w, h = struct.unpack_from("<ii", d, 18)
    planes, bits = struct.unpack_from("<HH", d, 26)
    comp = struct.unpack_from("<I", d, 30)[0]
    if planes != 1 or bits not in (24, 32) or comp != 0 or w <= 0 or h == 0:
        raise FrameError("%d bpp, compression %d BMP not supported" % (bits, comp))
    src = bits // 8
    rows = abs(h)
    stride = (w * src + 3) & ~3
    if off + stride * (rows - 1) + w * src > len(d):
        raise FrameError("BMP shorter than its header says")
    order = range(rows) if h < 0 else range(rows - 1, -1, -1)
    body = b"".join(d[off + y * stride : off + y * stride + w * src] for y in order)
    alpha = body[3::4] if src == 4 else b""
    bpp = 4 if alpha and alpha.count(0) != len(alpha) else 3
    out = bytearray(w * rows * bpp)
    out[0::bpp] = body[2::src]
    out[1::bpp] = body[1::src]
    out[2::bpp] = body[0::src]
    if bpp == 4:
        out[3::4] = alpha
    return w, rows, bpp, bytes(out)


# ---- PNG out: each row's filter chosen by the minimum sum ---------------
#
# The filters are computed for the whole image at once, on Python integers
# holding every byte (SWAR): an encoder's inputs are the original pixels,
# so no row waits on another. Paeth needs comparisons, so it runs on 16-bit
# lanes; the rest on byte lanes.


def _rep(byte, n):
    return int.from_bytes(bytes([byte]) * n, "little")


def _sub_bytes(x, y, n):
    """(x - y) mod 256 in each of n byte lanes."""
    hi = _rep(0x80, n)
    lo = _rep(0x7F, n)
    return ((x | hi) - (y & lo)) ^ ((x ^ y ^ hi) & hi)


def _avg_bytes(a, b, n):
    """floor((a + b) / 2) in each byte lane."""
    return (a & b) + (((a ^ b) & _rep(0xFE, n)) >> 1)


def _lanes16(bs):
    """bytes as 16-bit little-endian lanes, an int."""
    out = bytearray(2 * len(bs))
    out[0::2] = bs
    return int.from_bytes(out, "little")


def _paeth_pred(a, b, c, n):
    """The Paeth predictor of n bytes (a left, b up, c up-left: bytes)."""
    one = int.from_bytes(b"\x01\x00" * n, "little")
    bias = one << 10
    ffff = int.from_bytes(b"\xff\xff" * n, "little")
    A, B, C = _lanes16(a), _lanes16(b), _lanes16(c)

    def mask(bit):
        # A lane's flag (0 or 1 in bit 0) as 0x0000 or 0xFFFF.
        return (bit << 16) - bit

    def absdiff(x, y):
        # |x - y| per lane, for lanes holding 0..1023.
        d = x + bias - y
        m = mask((d >> 10) & one)
        return ((d & m) - (bias & m)) + ((bias & (ffff ^ m)) - (d & (ffff ^ m)))

    pa = absdiff(B, C)
    pb = absdiff(A, C)
    pc = absdiff(A + B, C + C)

    def le(x, y):
        # 1 per lane where x <= y (both 0..1023).
        return ((y + bias - x) >> 10) & one

    sa = le(pa, pb) & le(pa, pc)
    sb = (one ^ sa) & le(pb, pc)
    sc = one ^ sa ^ sb
    pred = (A & mask(sa)) | (B & mask(sb)) | (C & mask(sc))
    return pred.to_bytes(2 * n + 2, "little")[0 : 2 * n : 2]


# The minimum-sum heuristic reads each residual byte as signed.
_MAG = bytes(v if v < 128 else 256 - v for v in range(256))


def encode_png(w, h, bpp, px, level=6):
    """PNG bytes of w x h pixels (bpp 3: RGB, 4: RGBA), rows top-down.
    Level 6, not 9: about 4x faster for some 4% more bytes on dumps."""
    stride = w * bpp
    n = stride * h
    if len(px) != n:
        raise FrameError("pixels are %d bytes, not %d" % (len(px), n))
    zl = bytes(bpp)
    left = b"".join(zl + px[y * stride : (y + 1) * stride - bpp] for y in range(h))
    up = bytes(stride) + px[: n - stride]
    upleft = bytes(stride) + left[: n - stride]
    X = int.from_bytes(px, "little")
    L = int.from_bytes(left, "little")
    U = int.from_bytes(up, "little")
    res = [px]
    res.append(_sub_bytes(X, L, n).to_bytes(n, "little"))
    res.append(_sub_bytes(X, U, n).to_bytes(n, "little"))
    res.append(_sub_bytes(X, _avg_bytes(L, U, n), n).to_bytes(n, "little"))
    P = int.from_bytes(_paeth_pred(left, up, upleft, n), "little")
    res.append(_sub_bytes(X, P, n).to_bytes(n, "little"))
    mags = [r.translate(_MAG) for r in res]
    rows = []
    for y in range(h):
        s, e = y * stride, (y + 1) * stride
        best = min(range(5), key=lambda f: (sum(mags[f][s:e]), f))
        rows.append(bytes((best,)) + res[best][s:e])
    raw = b"".join(rows)

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))

    ctype = 2 if bpp == 3 else 6
    return (
        PNG_SIG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, level))
        + chunk(b"IEND", b"")
    )


# ---- PNG in: a decoder of its own, so a check does not trust the encoder -


def decode_png(d, name="PNG"):
    """(w, h, bpp, pixels top-down) of an 8-bit RGB or RGBA PNG."""
    if d[:8] != PNG_SIG:
        raise FrameError("%s: not a PNG" % name)
    pos, idat, hdr = 8, [], None
    while pos + 8 <= len(d):
        ln, typ = struct.unpack_from(">I4s", d, pos)
        body = d[pos + 8 : pos + 8 + ln]
        if len(body) != ln or struct.unpack_from(">I", d, pos + 8 + ln)[0] != zlib.crc32(
            typ + body
        ):
            raise FrameError("%s: bad %s chunk" % (name, typ.decode("latin-1")))
        pos += 12 + ln
        if typ == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if hdr is None:
        raise FrameError("%s: no IHDR" % name)
    w, h, depth, ctype, _, _, inter = hdr
    if depth != 8 or ctype not in (2, 6) or inter:
        raise FrameError("%s: only 8-bit RGB/RGBA non-interlaced PNG" % name)
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    if len(raw) != h * (stride + 1):
        raise FrameError("%s: image data is %d bytes, not %d" % (name, len(raw), h * (stride + 1)))
    hi = _rep(0x80, stride)
    lo = _rep(0x7F, stride)
    and255 = (255).__and__
    prev = bytes(stride)
    out = []
    for y in range(h):
        i = y * (stride + 1)
        ft = raw[i]
        cur = raw[i + 1 : i + 1 + stride]
        if ft == 1:
            row = bytearray(cur)
            for ch in range(bpp):
                row[ch::bpp] = bytes(map(and255, accumulate(cur[ch::bpp])))
            cur = bytes(row)
        elif ft == 2:
            x, u = int.from_bytes(cur, "little"), int.from_bytes(prev, "little")
            cur = (((x & lo) + (u & lo)) ^ ((x ^ u) & hi)).to_bytes(stride, "little")
        elif ft == 3:
            row = bytearray(cur)
            for k in range(stride):
                left = row[k - bpp] if k >= bpp else 0
                row[k] = (row[k] + ((left + prev[k]) >> 1)) & 255
            cur = bytes(row)
        elif ft == 4:
            row = bytearray(cur)
            for k in range(bpp):
                row[k] = (row[k] + prev[k]) & 255
            for k in range(bpp, stride):
                a, b, c = row[k - bpp], prev[k], prev[k - bpp]
                pa = b - c if b > c else c - b
                pb = a - c if a > c else c - a
                p2 = a + b - c - c
                pc = p2 if p2 > 0 else -p2
                if pa <= pb and pa <= pc:
                    row[k] = (row[k] + a) & 255
                elif pb <= pc:
                    row[k] = (row[k] + b) & 255
                else:
                    row[k] = (row[k] + c) & 255
            cur = bytes(row)
        elif ft != 0:
            raise FrameError("%s: bad PNG filter %d" % (name, ft))
        out.append(cur)
        prev = cur
    return w, h, bpp, b"".join(out)


# ---- one file -----------------------------------------------------------


def convert(path, dry_run=False, encode=None):
    """path (a .bmp) as a PNG beside it, checked, the BMP then removed.
    Returns (status, bmp bytes, png bytes, note): status "done" (or "would"
    on a dry run), "same" (a PNG of that name with these pixels was there:
    the BMP removed), "unsupported" (kept), "mismatch" or "conflict"
    (kept: the PNG read back differs, or one of that name with other
    pixels exists)."""
    size = os.path.getsize(path)
    try:
        img = read_bmp_px(path)
    except FrameError as e:
        return "unsupported", size, 0, str(e)
    except OSError as e:
        return "unsupported", size, 0, e.strerror or str(e)
    png = with_png(path)
    if os.path.lexists(png):
        try:
            with open(png, "rb") as f:
                there = decode_png(f.read(), png)
        except (OSError, FrameError, zlib.error) as e:
            return "conflict", size, 0, "%s exists and is not readable (%s)" % (png, e)
        if not same(there, img):
            return "conflict", size, 0, "%s exists with other pixels" % png
        if not dry_run:
            os.remove(path)
        return "same", size, 0, ""
    data = (encode or encode_png)(*img)
    try:
        back = decode_png(data, png)
    except (FrameError, zlib.error) as e:
        return "mismatch", size, len(data), str(e)
    if not same(back, img):
        return "mismatch", size, len(data), "the PNG's pixels differ from the BMP's"
    if dry_run:
        return "would", size, len(data), ""
    tmp = "%s.tmp-%d" % (png, os.getpid())
    st = os.stat(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        # The check reads what is on disk, not what was meant to be written.
        with open(tmp, "rb") as f:
            back = decode_png(f.read(), png)
        if not same(back, img):
            os.remove(tmp)
            return "mismatch", size, len(data), "the PNG on disk differs from the BMP"
        # A link fails on an existing name, so a PNG made meanwhile is never
        # overwritten (a check then a rename would leave a window).
        try:
            os.link(tmp, png)
        except FileExistsError:
            os.remove(tmp)
            return "conflict", size, 0, "%s appeared while converting" % png
        os.remove(tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    # The PNG's name is durable before the BMP's goes.
    _fsync_dir(os.path.dirname(os.path.abspath(png)))
    os.remove(path)
    return "done", size, len(data), ""


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def same(a, b):
    """Two decoded images hold the same pixels. An RGB one and an RGBA one
    never do here: the BMP side is RGBA unless its alpha is all 0."""
    return a[:3] == b[:3] and a[3] == b[3]


def _convert_job(args):
    path, dry = args
    try:
        return (path,) + convert(path, dry)
    except OSError as e:
        return path, "error", 0, 0, e.strerror or str(e)


# ---- many files ---------------------------------------------------------


def find_bmps(top):
    """Every regular *.bmp under top (top itself when it is one), no link
    followed, each under top's real path."""
    if os.path.isfile(top):
        return [top] if top.lower().endswith(".bmp") and not os.path.islink(top) else []
    real = os.path.realpath(top)
    out = []
    for dp, dirs, files in os.walk(top, followlinks=False):
        dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(dp, x)))
        for n in sorted(files):
            p = os.path.join(dp, n)
            if not n.lower().endswith(".bmp") or os.path.islink(p) or not os.path.isfile(p):
                continue
            rp = os.path.realpath(p)
            if os.path.commonpath([real, rp]) != real:
                continue
            out.append(p)
    return out


def pin_text(paths):
    parts = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                parts.append(f.read())
        except OSError as e:
            raise FrameError("pins file %s: %s" % (p, e.strerror)) from None
    return "\n".join(parts)


# A path as prose writes it: words joined by "/" (runs/x/shot.bmp,
# /abs/runs/x/, ../notes/y). Spaces, quotes, backticks and brackets end it.
PATH_RE = re.compile(r"[\w.@+~-]+(?:/[\w.@+~-]+)+/?|/[\w.@+~-]+")
# Folders that hold every run: naming one (runs/, an absolute bench-logs
# path) is not a citation of what is in it.
STORE_DIRS = ("runs", "bench-logs")


def path_words(text):
    """Every path text names, as a tuple of its parts, ".", ".." and "~"
    dropped (a citation counts by its tail: runs/x names <ws>/runs/x)."""
    out = set()
    for m in PATH_RE.finditer(text):
        parts = tuple(x for x in m.group(0).split("/") if x not in ("", ".", "..", "~"))
        if parts:
            out.add(parts)
    return out


class Pins:
    """What the pins sources cite. names: text that pins by a base name
    or a path under DIR (golden.json, --pins files). refs: text that pins
    by a path of two parts or more, or by a run's stamp (benchdb's
    reference text). keep: real paths of folders a citation never covers
    whole (the game root and every folder above it)."""

    def __init__(self, names="", refs="", keep=()):
        self.stamps = set(STAMP_RE.findall(names)) | set(STAMP_RE.findall(refs))
        self.name_paths = path_words(names)
        self.names = {p[-1] for p in self.name_paths}
        self.names |= {m.group(0) for m in re.finditer(r"[\w.-]+\.(?:bmp|png)", names)}
        self.paths = {p for p in path_words(refs) | self.name_paths if len(p) >= 2}
        self.keep = {os.path.realpath(k) for k in keep}
        self.empty = not (self.stamps or self.names or self.paths)

    def why(self, path, top):
        """Why path (a file under top) is pinned, or None."""
        if self.empty:
            return None
        name = os.path.basename(path)
        stem = os.path.splitext(name)[0]
        if name in self.names or stem + ".png" in self.names:
            return "named (%s)" % name
        if os.path.isdir(top):
            rel = tuple(os.path.relpath(path, top).split(os.sep))
            if rel in self.name_paths:
                return "named (%s)" % "/".join(rel)
        parts = tuple(x for x in os.path.abspath(path).split(os.sep) if x)
        for k in range(2, len(parts) + 1):
            if parts[-k:] in self.paths:
                return "named (%s)" % "/".join(parts[-k:])
        for j in range(len(parts) - 1, 0, -1):
            d = parts[:j]
            if d[-1] in STORE_DIRS or os.path.realpath(os.sep + os.sep.join(d)) in self.keep:
                continue
            for k in range(2, j + 1):
                if d[-k:] in self.paths:
                    return "folder cited (%s)" % "/".join(d[-k:])
        for part in parts[:-1]:
            m = STAMP_RE.search(part)
            if m and m.group(1) in self.stamps:
                return "run %s cited" % m.group(1)
        return None


def pinned(path, top, text):
    """Why text (a pins file's) pins path (a file under top), or None."""
    return Pins(names=text).why(path, top)


def ancestors(d):
    out = []
    d = os.path.realpath(d)
    while True:
        out.append(d)
        up = os.path.dirname(d)
        if up == d:
            return out
        d = up


def game_refs(root):
    """(reference text, [sources missing]) of the game at root: benchdb's
    (golden.json, TASKS.md, RESUME*.md, openspec/, the seeds, game.toml
    bench.gc.refs less refs_exclude), over the main checkout and root.
    Never refuses: compact keeps every pixel, so a gap only converts a
    cited dump that a reader still finds as PNG."""
    from . import manifest
    from .benchdb import refs as db_refs
    from .benchdb.store import DbError

    g = manifest.load(root)
    try:
        wts = db_refs.worktrees(root)
    except DbError:
        wts = [root]
    main_root = wts[0]
    others = [root] if os.path.realpath(root) != os.path.realpath(main_root) else []
    try:
        r = db_refs.read_refs(g, main_root, others)
    except DbError as e:
        return "", [str(e)]
    # A file too big to read is generated output, not a citation: named,
    # never a gap that matters here (benchdb refuses on it instead).
    big = tuple(r.skipped_big)
    gaps = ["missing %s" % m for m in r.missing if not (big and m.startswith(big))]
    gaps += ["not read (over %d MB): %s" % (db_refs.MAX_REF_FILE >> 20, b) for b in big]
    return r.text(), gaps


def logs_warning(root):
    """A warning when root is a git worktree whose bench-logs/ is a real
    folder, not the link to the workspace's shared store: runs written
    there go when the worktree does, and benchdb records them under it.
    None otherwise (a main checkout, a link, no bench-logs yet)."""
    logs = os.path.join(root, "bench-logs")
    if not os.path.isdir(logs) or os.path.islink(logs):
        return None
    import subprocess

    r = subprocess.run(
        ["git", "-C", root, "rev-parse", "--absolute-git-dir", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    lines = r.stdout.split()
    if len(lines) != 2:
        return None
    git_dir = os.path.realpath(lines[0])
    common = os.path.realpath(os.path.join(root, lines[1]))
    if git_dir == common:
        return None
    main = os.path.dirname(common)
    return (
        "WARNING: %s is a folder in a worktree, not a link to the shared store: runs "
        "written there go with the worktree. Move them to the store (mv -n) and make it a "
        "link, e.g. to %s" % (logs, os.path.join(main, "bench-logs"))
    )


def default_jobs():
    return max(1, min(8, (os.cpu_count() or 2) - 1))


class Totals:
    def __init__(self):
        self.n = self.bmp = self.png = 0
        self.same = self.same_bytes = self.pinned = 0
        self.kept = []  # (path, status, note)

    def add(self, status, bsize, psize, path, note):
        if status in ("done", "would"):
            self.n += 1
            self.bmp += bsize
            self.png += psize
        elif status == "same":
            # Its PNG was there already: the BMP's whole size is saved.
            self.same += 1
            self.same_bytes += bsize
        else:
            self.kept.append((path, status, note))

    def failed(self):
        return any(s in ("mismatch", "conflict", "error") for _, s, _ in self.kept)


def run_many(paths, dry_run=False, jobs=None):
    """[(path, status, bmp bytes, png bytes, note)] for paths, converted in
    worker processes (each frame's encode and check is CPU work)."""
    jobs = jobs or default_jobs()
    work = [(p, dry_run) for p in paths]
    if jobs <= 1 or len(work) < 2:
        return [_convert_job(w) for w in work]
    with ProcessPoolExecutor(max_workers=min(jobs, len(work))) as ex:
        return list(ex.map(_convert_job, work, chunksize=4))


def mb(n):
    return "%.1f MB" % (n / 1e6)


def summary(label, t, dry_run=False):
    saved = t.bmp - t.png + t.same_bytes
    verb = "would convert" if dry_run else "converted"
    s = "%s: %s %d BMPs, %s -> %s PNG (%s %s, %d bytes)" % (
        label,
        verb,
        t.n,
        mb(t.bmp),
        mb(t.png),
        "would save" if dry_run else "saved",
        mb(saved),
        saved,
    )
    extra = []
    if t.same:
        extra.append("%d already PNG, BMP %s" % (t.same, "would go" if dry_run else "removed"))
    if t.pinned:
        extra.append("%d pinned, left" % t.pinned)
    if t.kept:
        extra.append("%d kept as BMP" % len(t.kept))
    return s + ("; " + ", ".join(extra) if extra else "")


def after_run(dirs, say=print, bmp=False, env=None, jobs=None, match=None):
    """A run's kept frames (each of dirs, recursively) to PNG, once the run
    and its check are over; one line per dir. bmp/XBR_FRAMES_BMP=1 keeps
    them. match: a compiled name pattern a file must match (hud-map's
    capture dir holds other files). Returns 1 when a frame failed its
    check (its BMP kept), else 0. Never raises for a frame: the run's
    verdict does not depend on this."""
    dirs = [d for d in dirs if d and os.path.isdir(d)]
    if not dirs:
        return 0
    if keep_bmp(env, bmp):
        say("frames: kept as BMP (%s=1 or --bmp-frames)" % OPT_OUT)
        return 0
    rc = 0
    for d in dirs:
        paths = [p for p in find_bmps(d) if match is None or match.fullmatch(os.path.basename(p))]
        if not paths:
            continue
        t = Totals()
        for path, status, bsize, psize, note in run_many(paths, jobs=jobs):
            t.add(status, bsize, psize, path, note)
        say(summary("frames: %s" % d, t))
        for path, status, note in t.kept:
            say("frames: %s kept: %s%s" % (path, status, (" (%s)" % note) if note else ""))
        if t.failed():
            rc = 1
    return rc


# ---- the verb -----------------------------------------------------------

USAGE = "usage: %s frames compact [--dry-run] [--jobs N] [--pins FILE]... [--no-pins] DIR...\n"


def parse(argv):
    o = {"dry": False, "jobs": None, "pins": [], "no_pins": False, "dirs": []}
    it = iter(argv)
    for a in it:
        if a == "--dry-run":
            o["dry"] = True
        elif a == "--no-pins":
            o["no_pins"] = True
        elif a in ("--jobs", "--pins"):
            v = next(it, None)
            if v is None:
                raise FrameError("%s takes a value" % a)
            if a == "--jobs":
                try:
                    o["jobs"] = int(v)
                except ValueError:
                    raise FrameError("--jobs takes a number, not %r" % v) from None
                if o["jobs"] < 1:
                    raise FrameError("--jobs takes a number of at least 1")
            else:
                o["pins"].append(os.path.abspath(v))
        elif a.startswith("-"):
            raise FrameError("unknown option %s" % a)
        else:
            o["dirs"].append(a)
    if not o["dirs"]:
        raise FrameError("name at least one DIR")
    for d in o["dirs"]:
        if not os.path.exists(d):
            raise FrameError("no %s" % d)
        if os.path.islink(d):
            raise FrameError("%s is a link: name what it points to" % d)
    return o


def cmd_compact(argv, default_pins=(), prog="xbr", root=None):
    o = parse(argv)
    # A game without a golden.json (or a fresh one) pins nothing by default.
    pins = ([] if o["no_pins"] else [p for p in default_pins if p and os.path.isfile(p)]) + o[
        "pins"
    ]
    refs, missing = ("", [])
    if root and not o["no_pins"]:
        refs, missing = game_refs(root)
    index = Pins(pin_text(pins), refs, ancestors(root) if root else ())
    grand = Totals()
    per = []
    todo = []
    seen = set()
    left = []
    for d in o["dirs"]:
        t = Totals()
        per.append((d, t))
        for p in find_bmps(d):
            # DIRs that overlap name a file once: two workers on one file
            # would race on its PNG.
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            seen.add(rp)
            why = index.why(p, d)
            if why:
                t.pinned += 1
                left.append((p, why))
                continue
            todo.append((p, t))
    owner = {p: t for p, t in todo}
    for path, status, bsize, psize, note in run_many(
        [p for p, _ in todo], dry_run=o["dry"], jobs=o["jobs"]
    ):
        owner[path].add(status, bsize, psize, path, note)
        grand.add(status, bsize, psize, path, note)
    for d, t in per:
        grand.pinned += t.pinned
        print(summary(d, t, o["dry"]))
    if o["dry"]:
        for path, why in left:
            print("pinned, left as BMP: %s (%s)" % (path, why))
    for path, status, note in grand.kept:
        print("%s: kept: %s%s" % (path, status, (" (%s)" % note) if note else ""), file=sys.stderr)
    if len(per) > 1:
        print(summary("total", grand, o["dry"]))
    if pins:
        print("pins: %s" % ", ".join(pins))
    if refs:
        print("pins: the game's reference text (benchdb's sources)")
    for m in missing:
        print("pins: %s" % m, file=sys.stderr)
    return 1 if grand.failed() else 0


def main(argv, root=None, prog="xbr"):
    """`frames compact ...`; root: the game root when there is a game.toml
    (its golden.json and reference text are the default pins)."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE % prog + __doc__.split("\n\n", 1)[1])
        return 0 if argv else 2
    if argv[0] != "compact":
        sys.stderr.write(USAGE % prog)
        return 2
    pins = []
    if root:
        from . import manifest

        try:
            g = manifest.load(root)
            if g.golden_json:
                pins.append(g.golden_json)
        except manifest.ManifestError:
            pass
    try:
        return cmd_compact(argv[1:], pins, prog, root)
    except FrameError as e:
        print("%s frames: %s" % (prog, e), file=sys.stderr)
        return 2
