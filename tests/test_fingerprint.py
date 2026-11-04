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

from xboxrecomp_cli import doctor, fingerprint, main, manifest, xbe
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


def m_for(stock=STOCK, listed=(), code=("D3D",), text_only=True, region=None, version=None):
    """A manifest's tables for a game whose reference is stock (of that
    region and version, when given)."""
    return {
        "xbe": {
            "sha256": list(listed),
            "sections": dict((s.name, h) for s, h in fingerprint.section_hashes(image(stock))),
            "region": region,
            "version": version,
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
    assert v[:4] == (sha(data), "known", [], "")
    assert (v.region, v.version, v.ref, v.other) == (None, None, "", False)


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
    assert fingerprint.classify(image(STOCK + [("XPATCH", X, b"x")]), m_for(text_only=False))[
        :4
    ] == (
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

    data = image(STOCK, title="Some Game", region=0x4, version=2, alt=(0x4D530099,))
    text = fingerprint.toml_table(data)
    # The sha256 is a comment: pasted under an [xbe] that has the list,
    # a second sha256 key would not parse.
    assert text.startswith('# Under [xbe]. Add this to the sha256 list:\n#   "%s"\n' % sha(data))
    assert "v0.20.0" in text
    assert "\nregion = 0x4  # PAL\nversion = 2\n" in text
    # Alternate IDs commented out, with the same-game line above them.
    assert (
        "# Keep only IDs of this same game (other regions), never another game's:\n"
        "# alt_title_ids = [0x4D530099]\n"
    ) in text
    doc = tomllib.loads(text)
    assert doc["xbe"]["sections"] == m_for()["xbe"]["sections"]
    assert (doc["region"], doc["version"], doc["title"]) == (4, 2, "Some Game")
    assert "sha256" not in doc and "alt_title_ids" not in doc
    # A certificate too short for the fields: no region or version lines.
    bare = tomllib.loads(fingerprint.toml_table(image(STOCK, title="")))
    assert sorted(bare) == ["xbe"]


# ── another release ───────────────────────────────────────────────────

NA6 = dict(region=0x1, version=6)
PAL2 = dict(region=0x4, version=2)


def test_another_release_with_other_code():
    """A player's dump of another region and version, other code. The
    verdict is still code; the note says another release first."""
    m = m_for(listed=[sha(image(STOCK, **NA6))], **NA6)
    v = fingerprint.classify(image(changed(".text"), **PAL2), m)
    assert (v.verdict, v.sections, v.other) == ("code", [".text"], True)
    assert (v.region, v.version, v.ref) == (4, 2, "NTSC-U, version 6")
    n = fingerprint.note(v)
    assert n.startswith(
        "another release: PAL, version 2, where this port was made from NTSC-U, version 6; "
        "the build may fail or the game misbehave: XBE sha256 "
    )
    assert n.endswith("; code sections differ: .text") and "modded" not in n
    d = fingerprint.classify(image(changed(".rdata"), **PAL2), m)
    assert fingerprint.note(d).endswith("; only data sections differ: .rdata")
    rec = fingerprint.plan_record(v)
    assert rec["verdict"] == "code" and rec["note"] == n
    assert (rec["release"], rec["region"], rec["version"]) == ("PAL, version 2", 4, 2)
    assert (rec["reference_release"], rec["other_release"]) == ("NTSC-U, version 6", True)
    assert rec["why"] == ""
    assert fingerprint.stamp(v) == "%s code .text release=PAL,v2" % v.sha
    # Only the version differs: another revision of the same region.
    r = fingerprint.classify(image(changed(".text"), region=1, version=7), m)
    assert r.other and fingerprint.note(r).startswith("another release: NTSC-U, version 7, ")


def test_the_same_release_modded_stays_modded():
    m = m_for(**NA6)
    v = fingerprint.classify(image(changed(".text"), **NA6), m)
    assert (v.verdict, v.other) == ("code", False)
    assert fingerprint.note(v).startswith("modded code, unsupported, may crash")
    assert fingerprint.plan_record(v)["release"] == "NTSC-U, version 6"
    assert fingerprint.plan_record(v)["other_release"] is False
    assert " release=" not in fingerprint.stamp(v)
    # Not recorded on either side: never another release.
    for ref, dump in ((m_for(), PAL2), (m_for(**NA6), {})):
        assert not fingerprint.classify(image(changed(".text"), **dump), ref).other


def test_another_release_when_the_check_is_off():
    """No sha256 list nor sections, but a recorded release: a dump of
    another one is unknown, one of the same release is known."""
    m = m_for(**NA6)
    m["xbe"]["sections"] = {}
    v = fingerprint.classify(image(STOCK, **PAL2), m)
    assert (v.verdict, v.other) == ("unknown", True)
    assert fingerprint.note(v) == (
        "another release: PAL, version 2, where this port was made from NTSC-U, version 6; "
        "the build may fail or the game misbehave: XBE sha256 %s" % v.sha[:12]
    )
    assert fingerprint.classify(image(STOCK, **NA6), m).verdict == "known"


def test_a_listed_dump_is_never_another_release():
    data = image(STOCK, **PAL2)
    v = fingerprint.classify(data, m_for(listed=[sha(data)], **NA6))
    assert (v.verdict, v.other) == ("known", False)


def test_another_release_with_the_same_sections():
    """Sections match: the port works, no warning; doctor names it."""
    m = m_for(listed=["0" * 64], **NA6)
    v = fingerprint.classify(image(STOCK, **PAL2), m)
    assert (v.verdict, v.other, fingerprint.note(v)) == ("header", True, "")
    assert fingerprint.doctor_text(v).endswith("sections match; another release, PAL, version 2)")


def test_truncated_beats_another_release():
    m = m_for(**NA6)
    v = fingerprint.classify(image(STOCK, **PAL2)[:-48], m)
    # A cut file's certificate may be garbage: no release is claimed, in
    # the plan record or the stamp either.
    assert (v.verdict, v.why, v.other, v.title_id) == ("unknown", "truncated", False, None)
    assert fingerprint.note(v).startswith("truncated dump: ")
    assert fingerprint.plan_record(v)["other_release"] is False
    assert "release=" not in fingerprint.stamp(v)


REF_ID, OTHER_ID = 0x4D530065, 0x4D530099


def ref_with_id(**kw):
    m = m_for(**kw)
    m["xbe"]["title_id"] = REF_ID
    return m


def test_another_title_id_is_another_release():
    """Import lets in another title ID that names this game: classify must
    not call it known (check off) or modded (same region and version)."""
    m = ref_with_id(**NA6)
    v = fingerprint.classify(image(changed(".text"), title_id=OTHER_ID, **NA6), m)
    assert (v.verdict, v.other, v.title_id) == ("code", True, OTHER_ID)
    # The two sides never read the same: the IDs lead.
    assert fingerprint.note(v).startswith(
        "another release: title ID 0x4D530099, NTSC-U, version 6, where this port "
        "was made from title ID 0x4D530065, NTSC-U, version 6; "
    )
    assert fingerprint.stamp(v).endswith(" release=NTSC-U,v6,id=0x4D530099")
    rec = fingerprint.plan_record(v)
    assert (rec["release"], rec["reference_release"], rec["other_release"]) == (
        "title ID 0x4D530099, NTSC-U, version 6",
        "title ID 0x4D530065, NTSC-U, version 6",
        True,
    )
    # The check off (no lists): unknown, not known.
    off = ref_with_id()
    off["xbe"]["sections"] = {}
    v = fingerprint.classify(image(STOCK, title_id=OTHER_ID), off)
    assert (v.verdict, v.other) == ("unknown", True)
    assert fingerprint.note(v).startswith(
        "another release: title ID 0x4D530099, where this port was made from title ID 0x4D530065"
    )
    # The same ID, the check off: known as before.
    assert fingerprint.classify(image(STOCK, title_id=REF_ID), off).verdict == "known"
    # A listed sha256 stays the reference whatever its ID.
    data = image(STOCK, title_id=OTHER_ID)
    listed = ref_with_id(listed=[sha(data)])
    assert fingerprint.classify(data, listed)[:2] == (sha(data), "known")


def test_alternate_ids_need_one_publisher():
    """A sequel that lists its prequel's ID is another game: the alternate
    ID rules hold only within one publisher (the ID's high half)."""
    m = {"game": {"name": "BLiNX: the time sweeper"}, "xbe": {"title_id": 0x4D530013}}
    m["xbe"].update(region=1, version=6, title="", alt_title_ids=[])
    sequel = image(STOCK, title="Sequel", title_id=0x4B4C0002, alt=(0x4D530013,), **PAL2)
    h, c = xbe.read_header(sequel), xbe.cert_release(sequel)
    assert fingerprint.title_match(h, c, m) == ""
    m["xbe"]["alt_title_ids"] = [0x4B4C0002]
    assert fingerprint.title_match(h, c, m) == ""


def test_title_match():
    from test_xbe import header

    from xboxrecomp_cli import xbe

    m = {"game": {"name": "BLiNX: the time sweeper"}, "xbe": {"title_id": 0x4D530013}}
    m["xbe"].update(region=1, version=6, title="", alt_title_ids=[])

    def why(**kw):
        h = header(**kw)
        return fingerprint.title_match(xbe.read_header(h), xbe.cert_release(h), m)

    assert why(title="Burnout 3", title_id=0x4541000A, **PAL2) == ""
    assert why(title="BLiNX - The Time Sweeper", title_id=0x4D530099, **PAL2) == (
        'its title is "BLiNX - The Time Sweeper"'
    )
    assert why(title="Other", title_id=0x4D530099, alt=(0x4D530013,), **PAL2) == (
        "its alternate title IDs name 0x4D530013"
    )
    m["xbe"]["alt_title_ids"] = [0x4D530099]
    assert why(title="Other", title_id=0x4D530099) == "game.toml's alternate title IDs name it"
    # [xbe] title, when set, is the name to match instead of the game's.
    m["xbe"].update(title="BLiNX", alt_title_ids=[])
    assert why(title="BLiNX: the time sweeper", title_id=0x4D530099) == ""
    h = header(title="blinx", title_id=0x4D530099, **PAL2)
    text = fingerprint.other_title_text(xbe.read_header(h), xbe.cert_release(h), m, "BLiNX 1")
    assert text == (
        'title ID 0x4D530099, not BLiNX 1\'s 0x4D530013, but its title is "blinx": another '
        "release (PAL, version 2; this port was made from NTSC-U, version 6). The build may "
        "fail or the game misbehave"
    )


@contextlib.contextmanager
def game_with(d, xbe_bytes, **tables):
    """testdata/game copied to d with xbe_bytes as its dump and the
    [xbe]/[data]/[package] keys given (table.key=value), current while
    it runs."""
    shutil.copy(os.path.join(GAME, "game.toml"), d)
    G = manifest.load(d)
    # The dump is this game unless a test says otherwise: another title
    # ID is another release (classify).
    try:
        G.m["xbe"]["title_id"] = xbe.title_id(xbe_bytes)
    except xbe.XbeError:
        pass
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
    base = re.sub(
        r"\n\[xbe\.sections\]\n(?:[^\n\[]*\n)*", "\n", open(path, encoding="utf-8").read()
    )
    base = re.sub(r"\nreference_files = \d+\n", "\n", base)
    assert "[xbe.sections]" not in base and "reference_files" not in base
    for extra, ok in (
        ('\n[xbe.sections]\n".text" = "%s"\n' % ("a" * 64), True),
        ('\n[xbe.sections]\n".text" = "abc"\n', False),
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(base + extra)
        if ok:
            assert manifest.load(d).m["xbe"]["sections"] == {".text": "a" * 64}
        else:
            with pytest.raises(ManifestError, match="sections"):
                manifest.load(d)
    for old, new, ok in (
        ("[xbe]\n", "[xbe]\nregion = 0x4\nversion = 2\n", True),
        ("[xbe]\n", '[xbe]\ntitle = "BLiNX2"\nalt_title_ids = [0x4D530099]\n', True),
        ("[xbe]\n", "[xbe]\nregion = -1\n", False),
        ("[xbe]\n", "[xbe]\nversion = 0x100000000\n", False),
        ("[xbe]\n", "[xbe]\nalt_title_ids = [0]\n", False),
        ("[xbe]\n", '[xbe]\nregion = "PAL"\n', False),
        ("[package]\n", '[package]\nstamp_env = "GAME_BUILD_XBE"\n', True),
        ("[package]\n", '[package]\nstamp_env = "1 bad"\n', False),
        ("[data]\n", "[data]\nreference_files = 485\n", True),
        ("[data]\n", "[data]\nreference_files = -1\n", False),
    ):
        assert old in base
        with open(path, "w", encoding="utf-8") as f:
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
            open(os.path.join(G.game_files, n), "w", encoding="utf-8").close()
        # A derived file the pipeline writes there is not the dump's.
        excl = G.m["data"]["game_files_exclude"][0]
        open(os.path.join(G.game_files, excl), "w", encoding="utf-8").close()
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
    with open(os.path.join(G.content, "launch.env.default.windows"), "w", encoding="utf-8") as f:
        f.write("LOG_KEEP=10\n")
    with open(os.path.join(G.content, "config.default.toml"), "w", encoding="utf-8") as f:
        f.write("# none\n")
    with open(G.path("LICENSE"), "w", encoding="utf-8") as f:
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


def test_doctor_and_fingerprint_name_another_release(d, capsys):
    with game_with(
        d,
        image(changed(".text"), **PAL2),
        xbe__sections=ref_sections(),
        xbe__region=1,
        xbe__version=6,
    ) as G:
        lines, _, _ = doctor.doctor_report()
        line = next(x for x in lines if x.startswith("game files: "))
        assert "another release: PAL, version 2, where this port was made from NTSC-U" in line
        main.register()
        ap, _ = main.make_parser()
        main.cmd_fingerprint(ap.parse_args(["fingerprint", G.xbe]))
        out = capsys.readouterr()
        text = out.out + out.err
        assert ": code (sha256 " in text and "; PAL, version 2)" in text
        assert "another release: PAL, version 2" in text


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


def test_failed_recomp_names_the_dump(d):
    """recomp's wrap error says the dump may be another release; the CLI
    adds what the fingerprint says and the command that shows it."""
    from xboxrecomp_cli import pipeline

    other = image(changed(".text"), **PAL2)
    with game_with(d, other, xbe__sections=ref_sections(), xbe__region=1, xbe__version=6) as G:
        hint = pipeline._dump_hint()
        assert hint.startswith("\nYour game's main file: another release: PAL, version 2, ")
        assert hint.endswith("fingerprint %s' says more." % G.rel(G.xbe))
    with game_with(d, image(STOCK, **NA6), xbe__sections=ref_sections(), xbe__region=1) as G:
        assert pipeline._dump_hint() == ""


def test_fingerprint_command_names_a_header_only_release(d, capsys):
    """Another title ID with the reference's sections: no warning (the
    code is the reference's), but the command still names both."""
    data = image(STOCK, title_id=OTHER_ID, **NA6)
    with game_with(
        d, data, xbe__sections=ref_sections(), xbe__title_id=REF_ID, xbe__region=1, xbe__version=6
    ) as G:
        main.register()
        ap, _ = main.make_parser()
        main.cmd_fingerprint(ap.parse_args(["fingerprint", G.xbe]))
        out = capsys.readouterr().out
    assert ": header (sha256 %s; title ID 0x4D530099, NTSC-U, version 6)\n" % sha(data) in out
    assert out.endswith(
        "another release than this port's (title ID 0x4D530065, NTSC-U, version 6), "
        "with the same sections\n"
    )
