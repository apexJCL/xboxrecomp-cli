"""A game's symbol map ([pipeline] symbols; docs/symbols.md): the checks on
load, the names written into functions.json, the comments written into
gen/, and the generation key. A game without the key keeps its argv, key
and gen/ as they were.

  uv run pytest tests/test_symbols.py
"""

import json
import os

import pytest
from test_cli import fake_tree, raises, write

from xboxrecomp_cli import host, manifest, pipeline, symbols

MAP = """
[[symbol]]
addr = 0x00012000
name = "menu_title_main"
kind = "function"
note = "Title state machine; 27 states"
source = "a game's own key"

[[symbol]]
addr = 0x00013000
name = "lib_named_already"
kind = "function"

[[symbol]]
addr = 0x00014000
name = "sys_hle_override"
kind = "function"
in_gen = false

[[symbol]]
addr = 0x00015000
name = "ent_not_a_start"
kind = "function"

[[symbol]]
addr = 0x00016004
name = "ent_hit_site"
kind = "label"

[[symbol]]
addr = 0x00ADE76C
name = "sys_frame_counter"
kind = "global"

[[symbol]]
addr = 0x00ADE770
name = "sys_frame_counter_hi"
kind = "field"
struct = "sys_frame"
offset = 4

[[symbol]]
name = "ent_hp"
kind = "field"
struct = "ent_enemy"
offset = 0x10
"""


def load_text(tmp_path, text):
    p = tmp_path / "symbols.toml"
    p.write_text(text)
    return symbols.load(str(p))


def test_load_keeps_unknown_keys(tmp_path):
    rows = load_text(tmp_path, MAP)
    assert len(rows) == 8 and rows[0]["source"] == "a game's own key"
    assert sorted(symbols.functions(rows)) == [0x12000, 0x13000, 0x15000]


@pytest.mark.parametrize(
    "text, error",
    [
        ('[[symbol]]\naddr = 1\nname = "a-b"\nkind = "function"', "not a C identifier"),
        ('[[symbol]]\naddr = 1\nname = "int"\nkind = "function"', "int is reserved"),
        ('[[symbol]]\naddr = 1\nname = "esp"\nkind = "global"', "x86 register"),
        ('[[symbol]]\naddr = 1\nname = "_flags"\nkind = "function"', "prefix"),
        ('[[symbol]]\naddr = 1\nname = "g_ebp"\nkind = "global"', "prefix"),
        ('[[symbol]]\naddr = 1\nname = "recomp_fxam"\nkind = "function"', "prefix"),
        ('[[symbol]]\naddr = 1\nname = "sub_00012000"\nkind = "function"', "reserved"),
        ('[[symbol]]\naddr = 1\nname = "loc_00012000_x"\nkind = "label"', "reserved"),
        ('[[symbol]]\naddr = 1\nname = "a_b"\nkind = "thing"', "kind 'thing'"),
        ('[[symbol]]\nname = "a_b"\nkind = "global"', "no addr"),
        ('[[symbol]]\naddr = -1\nname = "a_b"\nkind = "global"', "not a 32-bit VA"),
        ('[[symbol]]\naddr = 1\nname = "a_b"\nkind = "function"\nin_gen = 0', "in_gen"),
        (
            '[[symbol]]\naddr = 1\nname = "a_b"\nkind = "global"\n'
            '[[symbol]]\naddr = 2\nname = "a_b"\nkind = "global"',
            "the name is also",
        ),
        (
            '[[symbol]]\naddr = 1\nname = "a_b"\nkind = "function"\n'
            '[[symbol]]\naddr = 1\nname = "a_c"\nkind = "function"',
            "0x00000001 is also",
        ),
        ("symbol = 1", "must be [[symbol]] tables"),
        ("[[symbol]\n", "symbol map"),
    ],
)
def test_load_refuses(tmp_path, text, error):
    p = tmp_path / "symbols.toml"
    p.write_text(text)
    raises(symbols.load, str(p), match=error)


def funcs_json(tmp_path, names):
    p = tmp_path / "functions.json"
    p.write_text(json.dumps([{"start": "0x%08X" % va, "name": n, "size": 4} for va, n in names]))
    return str(p)


def names_in(path):
    with open(path) as f:
        return {int(e["start"], 16): e["name"] for e in json.load(f)}


def test_apply_names_and_rerun(tmp_path):
    rows = load_text(tmp_path, MAP)
    fj = funcs_json(
        tmp_path,
        [
            (0x12000, "sub_00012000"),
            (0x13000, "atexit_00013000"),
            (0x14000, "sub_00014000"),
            (0x20000, "sub_00020000"),
        ],
    )
    renamed, kept, absent = symbols.apply_names(fj, rows)
    assert (renamed, kept, absent) == ([0x12000], [0x13000], [0x15000])
    got = names_in(fj)
    # A disasm or Ghidra name stays; in_gen = false leaves gen's name alone.
    assert got[0x12000] == "menu_title_main" and got[0x13000] == "atexit_00013000"
    assert got[0x14000] == "sub_00014000" and got[0x20000] == "sub_00020000"
    # Rerun with the name dropped from the map: sub_ comes back.
    assert symbols.apply_names(fj, rows[1:])[0] == []
    assert names_in(fj)[0x12000] == "sub_00012000"
    with open(fj) as f:
        assert all(symbols.MARK not in e for e in json.load(f))
    # A later Ghidra name over a renamed entry is kept on the rerun.
    symbols.apply_names(fj, rows)
    with open(fj) as f:
        data = json.load(f)
    data[0]["name"] = "ghidra_name"
    with open(fj, "w") as f:
        json.dump(data, f)
    symbols.apply_names(fj, rows)
    assert names_in(fj)[0x12000] == "ghidra_name"


def test_apply_names_refuses_a_taken_name(tmp_path):
    rows = load_text(tmp_path, MAP)
    fj = funcs_json(tmp_path, [(0x12000, "sub_00012000"), (0x30000, "menu_title_main")])
    raises(symbols.apply_names, fj, rows, match="0x00030000 is already called that")


GEN = """/**
 * menu_title_main
 * Original: 0x00012000 - 0x00012040 (64 bytes, 20 insns)
 * Frame: fpo_leaf
 */
void menu_title_main(void)
{
    eax = MEM32(0xADE76C);
    ecx = MEM32(0xADE770); /* done */
    /* MEM32(0xADE76C) in a comment */
    edx = 0xADE76Cu;
loc_00016004: ;
    eax = ebx; /* mov eax, [0xADE76C] */
    recomp_log("0xADE76C");
    eax = MEM32(0xADE76C); /* open
    still open */
}

/**
 * sub_00013000
 * Original: 0x00013000 - 0x00013004 (4 bytes, 1 insns)
 */
void sub_00013000(void)
{
}

/**
 * sub_00014000
 * Original: 0x00014000 - 0x00014004 (4 bytes, 1 insns)
 */
void sub_00014000(void)
{
}
"""


def test_annotate_text(tmp_path):
    tables = symbols.comment_tables(load_text(tmp_path, MAP))
    out = symbols.annotate_text(GEN, tables).split("\n")
    i = out.index(" * menu_title_main")
    assert out[i + 1] == (" * menu_title_main (sub_00012000): Title state machine; 27 states")
    assert out[i + 2].startswith(" * Original: 0x00012000")
    assert "    eax = MEM32(0xADE76C); /* sys_frame_counter */" in out
    assert "    ecx = MEM32(0xADE770); /* done */ /* sys_frame_counter_hi */" in out
    assert "    /* MEM32(0xADE76C) in a comment */" in out
    assert "    edx = 0xADE76Cu; /* sys_frame_counter */" in out
    assert "loc_00016004: ; /* ent_hit_site */" in out
    # A string, or a comment still open at the end of the line, gets no
    # trailing comment.
    assert '    recomp_log("0xADE76C");' in out
    assert "    eax = MEM32(0xADE76C); /* open" in out
    # recomp's instruction comment repeats the literal: no name for it.
    assert "    eax = ebx; /* mov eax, [0xADE76C] */" in out
    # A function with a name the map does not use still gets its line, and
    # so does one the map keeps out of gen (in_gen = false).
    j = out.index(" * sub_00013000")
    assert out[j + 1] == " * lib_named_already (sub_00013000)"
    j = out.index(" * sub_00014000")
    assert out[j + 1] == " * sys_hle_override (sub_00014000)"
    # Only comments were added: dropping them gives the input back.
    assert symbols.annotate_text(GEN.replace("\n", "\r\n"), tables).replace("\r\n", "\n") == (
        "\n".join(out)
    )


def test_annotate_is_idempotent(tmp_path):
    tables = symbols.comment_tables(load_text(tmp_path, MAP))
    for text in (GEN, GEN.replace("\n", "\r\n")):
        once = symbols.annotate_text(text, tables)
        assert once != text and symbols.annotate_text(once, tables) == once


def test_doc_lines_wrap_and_close():
    lines = symbols.doc_lines({"addr": 0x10, "name": "a_b", "note": "x */ y " + "word " * 40})
    assert all(len(line) <= 79 for line in lines) and len(lines) > 2
    assert "*/" not in "".join(lines)
    assert lines[1].startswith(" *   ")


def test_annotate_gen_keeps_mtime(tmp_path):
    rows = load_text(tmp_path, MAP)
    gen = tmp_path / "gen"
    gen.mkdir()
    a, b = gen / "recomp_0000.c", gen / "recomp_0001.c"
    a.write_text(GEN)
    b.write_text("int x;\n")
    assert symbols.annotate_gen(str(gen), rows, symbols.snapshot(str(gen))) == 1
    os.utime(a, ns=(0, 1_000_000_000))
    os.utime(b, ns=(0, 1_000_000_000))
    before = symbols.snapshot(str(gen))
    # recomp rewrites its own (unannotated) text: the same bytes after the
    # comments, so the old mtime comes back.
    a.write_text(GEN)
    assert symbols.annotate_gen(str(gen), rows, before) == 0
    assert os.stat(a).st_mtime_ns == 1_000_000_000
    assert os.stat(b).st_mtime_ns == 1_000_000_000
    a.write_text(GEN.replace("0x00012040", "0x00012044"))
    assert symbols.annotate_gen(str(gen), rows, before) == 1
    assert os.stat(a).st_mtime_ns != 1_000_000_000


def with_map(root):
    write(os.path.join(root, "config", "symbols.toml"), MAP)
    m = host.g().m
    m["pipeline"]["symbols"] = "config/symbols.toml"
    manifest.use(manifest.Game(root, m))


def test_manifest_key():
    assert (
        manifest.parse_text(
            open(os.path.join(os.path.dirname(__file__), "testdata", "game", "game.toml")).read()
        )["pipeline"]["symbols"]
        == ""
    )


def test_gen_key_only_with_a_map(d):
    with fake_tree(d) as (root, _tk):
        argv, inputs = pipeline.stage_argv(), pipeline.gen_inputs()
        assert pipeline.symbols_cmds() == [] and "symbols" not in argv
        pipeline.write_gen_key()
        with_map(root)
        assert pipeline.gen_inputs()["$ROOT/config/symbols.toml"] != "absent"
        assert set(pipeline.gen_inputs()) - set(inputs) == {"$ROOT/config/symbols.toml"}
        assert (
            pipeline.stage_argv() != argv and "$ROOT/config/symbols.toml" in pipeline.stage_argv()
        )
        why = pipeline.gen_stale_reasons()
        assert "config/symbols.toml changed" in why and "stage commands changed" in why
        pipeline.write_gen_key()
        write(os.path.join(root, "config", "symbols.toml"), MAP + "\n# edit\n")
        assert pipeline.gen_stale_reasons() == ["config/symbols.toml changed"]


def test_names_stage_without_ghidra(d, monkeypatch):
    with fake_tree(d) as (root, _tk):
        G = host.g()
        # No map and no Ghidra export: names is skipped in analyze.
        ran = []
        monkeypatch.setattr(pipeline, "stage_names", lambda: ran.append(1))
        pipeline.maybe_names()
        assert ran == []
        monkeypatch.undo()
        with_map(root)
        G = host.g()
        fj = os.path.join(G.out, "disasm", "functions.json")
        write(fj, json.dumps([{"start": "0x00012000", "name": "sub_00012000"}]))
        cmds = []
        monkeypatch.setattr(pipeline, "run_cmds", cmds.extend)
        pipeline.maybe_names()
        assert cmds == []  # no Ghidra export: no merge_names, no hooks
        assert names_in(fj)[0x12000] == "menu_title_main"
        raises(pipeline.stage_names, ["--x"], match="needs a Ghidra export")


def test_missing_map(d):
    with fake_tree(d) as (root, _tk):
        with_map(root)
        os.remove(os.path.join(root, "config", "symbols.toml"))
        raises(pipeline.symbol_entries, match="config/symbols.toml, which is not there")


def test_recomp_annotates_and_applies_names(d, monkeypatch):
    """recomp applies the map first (an edit then recomp alone keeps names
    and comments together), then writes the comments; a rerun with the
    same output changes no file and no mtime."""
    with fake_tree(d) as (root, _tk):
        with_map(root)
        G = host.g()
        fj = os.path.join(G.out, "disasm", "functions.json")
        write(fj, json.dumps([{"start": "0x00012000", "name": "sub_00012000"}]))
        write(os.path.join(G.out, "abi", "abi_functions.json"), "{}")

        def fake_recomp(cmds):
            # What recomp writes: the name functions.json holds by now.
            name = names_in(fj)[0x12000]
            write(os.path.join(G.gen, "recomp_0000.c"), GEN.replace("menu_title_main", name))
            write(os.path.join(G.gen, "recomp_funcs.h"), "")

        monkeypatch.setattr(pipeline, "run_cmds", fake_recomp)
        pipeline.stage_recomp()
        c = os.path.join(G.gen, "recomp_0000.c")
        with open(c) as f:
            text = f.read()
        assert "void menu_title_main(void)" in text
        assert " * menu_title_main (sub_00012000): Title state machine; 27 states" in text
        os.utime(c, ns=(0, 1_000_000_000))
        pipeline.stage_recomp()
        assert os.stat(c).st_mtime_ns == 1_000_000_000
        with open(c) as f:
            assert f.read() == text
        assert pipeline.gen_stale_reasons() == []


def test_no_map_leaves_gen_and_key_alone(d, monkeypatch):
    """Without the key: no symbols step, recomp's output untouched, and the
    gen key byte-identical whether the key is absent or empty."""
    with fake_tree(d) as (root, _tk):
        G = host.g()
        write(os.path.join(G.out, "abi", "abi_functions.json"), "{}")

        def fake_recomp(cmds):
            write(os.path.join(G.gen, "recomp_0000.c"), GEN)
            write(os.path.join(G.gen, "recomp_funcs.h"), "")

        monkeypatch.setattr(pipeline, "run_cmds", fake_recomp)
        pipeline.stage_recomp()
        with open(os.path.join(G.gen, "recomp_0000.c")) as f:
            assert f.read() == GEN
        with open(G.gen_key, "rb") as f:
            key = f.read()
        assert b"symbols" not in key
        m = host.g().m
        m["pipeline"]["symbols"] = ""
        manifest.use(manifest.Game(root, m))
        pipeline.stage_recomp()
        with open(G.gen_key, "rb") as f:
            assert f.read() == key


def test_kept_names_are_listed(d, capsys):
    with fake_tree(d) as (root, _tk):
        with_map(root)
        G = host.g()
        fj = os.path.join(G.out, "disasm", "functions.json")
        write(fj, json.dumps([{"start": "0x00013000", "name": "atexit_00013000"}]))
        pipeline.apply_symbols()
        out = capsys.readouterr()
        said = out.out + out.err
        assert "keeps its name: 0x00013000" in said
        assert "is no function start: 0x00012000 0x00015000" in said
