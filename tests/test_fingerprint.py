"""fingerprint.py: the XBE's verdict from its sections, against synthetic
XBEs (test_xbe.image), never game data.

  uv run pytest tests/test_fingerprint.py
"""

import contextlib
import hashlib
import json
import os
import re
import shutil

import pytest
from test_xbe import image

from xboxrecomp_cli import doctor, fingerprint, main, manifest
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.manifest import ManifestError
from xboxrecomp_cli.package import lib as pl
from xboxrecomp_cli.package import plib

GAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "game")

X, RW = 0x4 | 0x1, 0x1  # executable, writable
STOCK = [
    (".text", X, b"\x55\x8b\xec" * 40),
    ("D3D", X, b"\xc3" * 64),
    ("M11", X, b"model data" * 8),  # executable, but not code (D2)
    (".rdata", 0, b"strings" * 10),
    (".data", RW, b"\0" * 32),
]


def m_for(stock=STOCK, listed=(), code=("D3D",), text_only=True):
    """A manifest's tables for a game whose reference is stock."""
    return {
        "xbe": {
            "sha256": list(listed),
            "sections": dict((s.name, h) for s, h in fingerprint.section_hashes(image(stock))),
        },
        "pipeline": {"disasm": {"text_only": text_only, "extra_sections": list(code)}},
    }


def changed(name, byte=b"\xff", sections=STOCK):
    return [(n, f, (byte + raw[1:]) if n == name else raw) for n, f, raw in sections]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_known_by_its_sha256():
    data = image(STOCK)
    v = fingerprint.classify(data, m_for(listed=[sha(data)]))
    assert v == (sha(data), "known", [], "")


def test_header_only():
    """Another pressing: the title differs, every section matches."""
    m = m_for(listed=[sha(image(STOCK))])
    other = image(STOCK, title="Same Game, Other Disc")
    v = fingerprint.classify(other, m)
    assert (v.verdict, v.sections) == ("header", [])


def test_modded_data():
    v = fingerprint.classify(image(changed(".rdata")), m_for())
    assert (v.verdict, v.sections) == ("data", [".rdata"])
    # Executable data the pipeline does not translate is data too.
    v = fingerprint.classify(image(changed("M11")), m_for())
    assert (v.verdict, v.sections) == ("data", ["M11"])


def test_modded_code():
    v = fingerprint.classify(image(changed(".rdata", sections=changed(".text"))), m_for())
    assert (v.verdict, v.sections) == ("code", [".text"])
    v = fingerprint.classify(image(changed("D3D")), m_for())
    assert (v.verdict, v.sections) == ("code", ["D3D"])


def test_without_text_only_the_flag_decides():
    m = m_for(text_only=False)
    assert fingerprint.classify(image(changed("M11")), m).verdict == "code"
    assert fingerprint.classify(image(changed(".rdata")), m).verdict == "data"


def test_added_and_missing_sections():
    m = m_for()
    # An added section is code by its own flags when no code set names it.
    added = fingerprint.classify(image(STOCK + [("PATCH", 0, b"x" * 16)]), m)
    assert (added.verdict, added.sections) == ("data", ["PATCH"])
    assert fingerprint.classify(image(STOCK + [("XPATCH", X, b"x")]), m_for(text_only=False)) == (
        sha(image(STOCK + [("XPATCH", X, b"x")])),
        "code",
        ["XPATCH"],
        "",
    )
    # With text_only too (D2): the code set cannot name a section the
    # reference lacks, so a new executable one is code by its flags.
    added = fingerprint.classify(image(STOCK + [("XPATCH", X, b"x")]), m)
    assert (added.verdict, added.sections) == ("code", ["XPATCH"])
    gone = fingerprint.classify(image([s for s in STOCK if s[0] != "D3D"]), m)
    assert (gone.verdict, gone.sections) == ("code", ["D3D"])
    gone = fingerprint.classify(image([s for s in STOCK if s[0] != ".data"]), m)
    assert (gone.verdict, gone.sections) == ("data", [".data"])


def test_a_truncated_dump_is_unknown_not_modded():
    data = image(STOCK)[:-48]
    v = fingerprint.classify(data, m_for())
    assert (v.verdict, v.why) == ("unknown", "truncated")
    assert v.sections == [".rdata", ".data"]
    assert fingerprint.note(v).startswith("truncated dump: XBE sha256 %s; " % sha(data)[:12])
    assert "run past the file's end" in fingerprint.note(v)


def test_unknown_without_a_table_or_a_section_table():
    m = m_for(listed=["0" * 64])
    m["xbe"]["sections"] = {}
    assert fingerprint.classify(image(STOCK), m).verdict == "unknown"
    assert fingerprint.classify(b"not an xbe", m_for(listed=["0" * 64])).verdict == "unknown"
    # Neither list: the check is off.
    m["xbe"]["sha256"] = []
    assert fingerprint.classify(b"anything", m).verdict == "known"


def test_messages_name_the_sections():
    many = [("S%02d" % i, X, bytes([i]) * 4) for i in range(12)]
    m = m_for(stock=many, code=[n for n, _, _ in many])
    v = fingerprint.classify(image([(n, f, b"\xee" * 4) for n, f, _ in many]), m)
    assert fingerprint.note(v).startswith("modded code, unsupported, may crash: XBE sha256 ")
    assert "S00, S01, S02, S03, S04, S05, S06, S07 and 4 more" in fingerprint.note(v)
    d = fingerprint.classify(image(changed(".rdata")), m_for())
    assert fingerprint.note(d).startswith("modded data, code stock: ")
    assert fingerprint.note(d).endswith("data sections differ: .rdata")
    assert fingerprint.note(fingerprint.Verdict("a" * 64, "header", [])) == ""
    assert fingerprint.stamp(v).split(" ")[1:] == ["code", ",".join(n for n, _, _ in many)]
    assert fingerprint.stamp(fingerprint.Verdict("a" * 64, "known", [])) == "a" * 64 + " known"


def test_toml_table_round_trips(d):
    import tomllib

    text = fingerprint.toml_table(image(STOCK))
    assert text.startswith("[xbe.sections]\n")
    table = tomllib.loads(text)["xbe"]["sections"]
    assert table == m_for()["xbe"]["sections"]


@contextlib.contextmanager
def game_with(d, xbe_bytes, **tables):
    """testdata/game copied to d with xbe_bytes as its dump and the
    [xbe]/[data]/[package] keys given (table.key=value), current while
    it runs."""
    shutil.copy(os.path.join(GAME, "game.toml"), d)
    G = manifest.load(d)
    for k, v in tables.items():
        t, key = k.split("__")
        G.m[t][key] = v
    os.makedirs(os.path.dirname(G.xbe), exist_ok=True)
    with open(G.xbe, "wb") as f:
        f.write(xbe_bytes)
    saved = manifest.current()
    manifest.use(G)
    try:
        yield G
    finally:
        manifest.use(saved)
        plib()


def ref_sections():
    return m_for()["xbe"]["sections"]


def test_manifest_validates_the_new_keys(d):
    shutil.copy(os.path.join(GAME, "game.toml"), d)
    path = os.path.join(d, "game.toml")
    # The fixture sets both: start from a game.toml without them.
    base = re.sub(r"\n\[xbe\.sections\]\n(?:[^\n\[]*\n)*", "\n", open(path).read())
    base = re.sub(r"\nreference_files = \d+\n", "\n", base)
    assert "[xbe.sections]" not in base and "reference_files" not in base
    for extra, ok in (
        ('\n[xbe.sections]\n".text" = "%s"\n' % ("a" * 64), True),
        ('\n[xbe.sections]\n".text" = "abc"\n', False),
    ):
        with open(path, "w") as f:
            f.write(base + extra)
        if ok:
            assert manifest.load(d).m["xbe"]["sections"] == {".text": "a" * 64}
        else:
            with pytest.raises(ManifestError, match="sections"):
                manifest.load(d)
    for old, new, ok in (
        ("[package]\n", '[package]\nstamp_env = "GAME_BUILD_XBE"\n', True),
        ("[package]\n", '[package]\nstamp_env = "1 bad"\n', False),
        ("[data]\n", "[data]\nreference_files = 485\n", True),
        ("[data]\n", "[data]\nreference_files = -1\n", False),
    ):
        assert old in base
        with open(path, "w") as f:
            f.write(base.replace(old, new, 1))
        if ok:
            manifest.load(d)
        else:
            with pytest.raises(ManifestError):
                manifest.load(d)


def test_doctor_line_and_dump_state(d):
    stock = image(STOCK)
    with game_with(
        d, image(changed(".text")), xbe__sha256=[sha(stock)], xbe__sections=ref_sections()
    ):
        lines, _, _ = doctor.doctor_report()
        line = next(x for x in lines if x.startswith("game files: "))
        assert "modded code, unsupported, may crash" in line and ".text" in line
        assert doctor.dump_state()[1] is False
    with game_with(
        d, image(STOCK, title="Other Disc"), xbe__sha256=[sha(stock)], xbe__sections=ref_sections()
    ):
        assert doctor.dump_state()[1] is True
        lines, _, _ = doctor.doctor_report()
        line = next(x for x in lines if x.startswith("game files: "))
        assert "header differs from the known dumps, sections match" in line


def test_doctor_assets_line(d):
    with game_with(d, image(STOCK), data__reference_files=5) as G:
        for n in ("a.bin", "b/c.bin"):
            os.makedirs(os.path.dirname(os.path.join(G.game_files, n)), exist_ok=True)
            open(os.path.join(G.game_files, n), "w").close()
        # A derived file the pipeline writes there is not the dump's.
        excl = G.m["data"]["game_files_exclude"][0]
        open(os.path.join(G.game_files, excl), "w").close()
        assert doctor.assets_line() == "3 files (the reference dump has 5, 2 fewer)"
        lines, _, blocked = doctor.doctor_report()
        assert "assets:     3 files (the reference dump has 5, 2 fewer)" in lines
        G.m["data"]["reference_files"] = 0
        assert doctor.doctor_report()[2] == blocked  # information only
        assert doctor.assets_line() == ""
        G.m["data"]["reference_files"] = 3
        assert doctor.assets_line() == "3 files (as the reference dump)"


def stage_common(G, monkeypatch, d):
    G.content = G.path("packaging")
    os.makedirs(G.content, exist_ok=True)
    with open(os.path.join(G.content, "launch.env.default.windows"), "w") as f:
        f.write("LOG_KEEP=10\n")
    with open(os.path.join(G.content, "config.default.toml"), "w") as f:
        f.write("# none\n")
    with open(G.path("LICENSE"), "w") as f:
        f.write("MIT\n")
    monkeypatch.setattr(pkg, "readme", lambda *a: None)
    monkeypatch.setattr(pl, "stage_game_files", lambda src, dst: 0)
    payload = os.path.join(d, "payload")
    os.makedirs(payload, exist_ok=True)
    pkg.write_common(payload, "windows", "v", pl)
    with open(os.path.join(payload, "launch.env.default"), "rb") as f:
        return f.read().decode()


def test_stamp_line(d, monkeypatch):
    data = image(changed(".rdata"))
    with game_with(d, data, xbe__sections=ref_sections(), package__stamp_env="GAME_BUILD_XBE") as G:
        text = stage_common(G, monkeypatch, d)
    assert text == (
        "LOG_KEEP=10\n# The XBE this build was made from (xbr fingerprint).\n"
        "GAME_BUILD_XBE=%s data .rdata\n" % sha(data)
    )


def test_no_stamp_line_without_stamp_env(d, monkeypatch):
    with game_with(d, image(STOCK), xbe__sections=ref_sections(), package__stamp_env="") as G:
        assert stage_common(G, monkeypatch, d) == "LOG_KEEP=10\n"


def test_reads_a_folder_source_at_the_xbe_size(d, monkeypatch):
    """A folder (a dump not yet imported) is read in place, exactly the
    XBE's size: a 4 GiB read(n) would allocate it first."""
    from xboxrecomp_cli import dump

    data = image(changed(".rdata"))
    asked = []
    read = dump.Source.xbe_read

    def spy(self, off, n, name=None):
        asked.append(n)
        return read(self, off, n, name)

    monkeypatch.setattr(dump.Source, "xbe_read", spy)
    with game_with(d, data, xbe__sections=ref_sections()) as G:
        v = fingerprint.of_path(G.game_files)
    assert (v.sha, v.verdict, v.sections) == (sha(data), "data", [".rdata"])
    assert asked == [len(data)]


def test_warn_never_refuses(d, capsys):
    with game_with(d, image(changed(".text")), xbe__sections=ref_sections()):
        fingerprint.warn(fingerprint.current(), "; packaging it anyway")
    err = capsys.readouterr()
    out = err.out + err.err
    assert "WARNING: modded code, unsupported, may crash" in out
    assert out.rstrip().endswith("; packaging it anyway")


def test_fingerprint_command(d, capsys):
    data = image(changed(".rdata"))
    with game_with(d, data, xbe__sections=ref_sections()) as G:
        main.register()
        ap, _ = main.make_parser()
        a = ap.parse_args(["fingerprint", "--json"])
        main.cmd_fingerprint(a)
        rec = json.loads(capsys.readouterr().out)
        assert rec["verdict"] == "data" and rec["sections"] == [".rdata"]
        assert rec["note"].startswith("modded data, code stock")
        a = ap.parse_args(["fingerprint", G.xbe, "--toml"])
        main.cmd_fingerprint(a)
        assert capsys.readouterr().out == fingerprint.toml_table(data)
