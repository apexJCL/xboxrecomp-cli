"""The Windows applier (D8): apply_plan.c built with this host's cc and
checked against a Python model of the same plan, the patch folder and
patch.lst the CLI writes, and apply.exe, the stub and the launcher built
with llvm-mingw when it is installed (LLVM_MINGW_ROOT or PATH).

There is no Windows here: apply.c's Win32 calls are exercised on the PC
(tasks 7.5); what it decides is apply_plan.c, tested here."""

import hashlib
import json
import os
import random
import re
import shutil
import subprocess

import pytest

from xboxrecomp_cli import manifest
from xboxrecomp_cli.package import patch, plib, windows
from xboxrecomp_cli.package import template as pkg_template

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "testdata", "windows", "plan_driver.c")
TEMPLATES = os.path.dirname(pkg_template("windows", "apply.c"))
GAME2 = os.path.join(HERE, "testdata", "game2")


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("no host C compiler")
    out = str(tmp_path_factory.mktemp("plan") / "plan_driver")
    subprocess.run(
        [
            cc,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            TEMPLATES,
            DRIVER,
            os.path.join(TEMPLATES, "apply_plan.c"),
            "-o",
            out,
        ],
        check=True,
    )

    def run(*args):
        return subprocess.run(
            [out, *args], check=True, capture_output=True, text=True
        ).stdout.splitlines()

    return run


# ── the model ────────────────────────────────────────────────────────────

GAME = "game_files/"
HEX64 = re.compile(r"[0-9a-f]{64}")


def path_ok(p):
    if not p or p.startswith("/") or any(c in p for c in "\\:") or any(ord(c) < 0x20 for c in p):
        return False
    return all(part not in ("", ".", "..") and part[-1] not in ". " for part in p.split("/"))


def version_ok(v):
    if not v or any(c in v for c in "/\\:") or v.startswith("."):
        return False
    return not (len(v) > 8 and v.endswith(".partial")) and not (len(v) > 4 and v.endswith(".old"))


class Bad(Exception):
    def __init__(self, line, msg):
        super().__init__(msg)
        self.line, self.msg = line, msg


def parse_patch(text):
    keys, entries, header = {}, [], False
    for ln, line in enumerate(text.split("\n"), 1):
        line = line[:-1] if line.endswith("\r") else line
        if not line or line.startswith("#"):
            continue
        k, _, rest = line.partition(" ")
        rest = rest if " " in line else None
        if not header:
            if k != "xbr-patch" or rest != "1":
                raise Bad(ln, "not a patch list this applier reads (xbr-patch 1)")
            header = True
            continue
        if len(k) == 1 and k in "CNDK":
            parts = (rest or "").split(" ", 2) if rest is not None else []
            if len(parts) < 3:
                raise Bad(ln, "an entry needs a size, a sha256 and a path")
            size, h, path = parts
            if not size.isdigit() or len(size) > 18:
                raise Bad(ln, "a bad size")
            if not HEX64.fullmatch(h):
                raise Bad(ln, "a bad sha256")
            if not path_ok(path):
                raise Bad(ln, "an unsafe path")
            entries.append((k, int(size), h, path))
            continue
        if not rest:
            raise Bad(ln, "a key without a value")
        if k in ("app", "version", "base", "base_sha", "new_sha"):
            keys[k] = rest
        elif k == "stub_abi":
            m = re.match(r"\s*[+-]?\d+", rest)
            keys[k] = int(m.group()) if m else 0
    need = ("app", "version", "base", "base_sha", "new_sha", "stub_abi")
    if not all(k in keys for k in need) or keys["stub_abi"] < 0:
        raise Bad(0, "the list lacks app, version, base, base_sha, new_sha or stub_abi")
    if not version_ok(keys["version"]) or not version_ok(keys["base"]):
        raise Bad(0, "a bad version name")
    if not HEX64.fullmatch(keys["base_sha"]) or not HEX64.fullmatch(keys["new_sha"]):
        raise Bad(0, "a bad manifest sha256")
    return keys, entries


def steps(entries):
    """D10's order: every check before the first write."""
    out = [
        ("check-game" if p.startswith(GAME) else "check-program", p)
        for k, _, _, p in entries
        if k == "K"
    ]
    for k, _, _, p in entries:
        if p.startswith(GAME):
            continue
        if k == "K":
            out.append(("link", p))
        elif k in "CN":
            out.append(("copy", p))
    out += [("game-write", p) for k, _, _, p in entries if p.startswith(GAME) and k in "CN"]
    out += [("game-delete", p) for k, _, _, p in entries if p.startswith(GAME) and k == "D"]
    return out


def model_steps(text):
    try:
        keys, entries = parse_patch(text)
    except Bad as e:
        return ["error %d %s" % (e.line, e.msg)]
    head = [
        "app " + keys["app"],
        "version " + keys["version"],
        "base " + keys["base"],
        "stub_abi %d" % keys["stub_abi"],
    ]
    return head + ["%s %s" % s for s in steps(entries)]


def parse_history(text):
    out = []
    for line in text.split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        w = [t for t in line.split(" ") if t]
        if len(w) == 5 and w[3] == "->":
            out.append({"what": w[1], "old": w[2], "new": w[4]})
    return out


def previous(hist, cur):
    if cur is None:
        return None
    for h in reversed(hist):
        if h["new"] == cur and h["old"] not in ("-", cur):
            return h["old"]
    return None


def order(names, hist):
    def last(v):
        hits = [i for i, h in enumerate(hist) if h["new"] == v]
        return hits[-1] if hits else -1

    return sorted(names, key=lambda v: (last(v), v))


def model_prune(text, keep, cur, names):
    hist = parse_history(text)
    cur = None if cur == "-" else cur
    prev = previous(hist, cur)
    names = order(names, hist)
    out = ["previous " + (prev or "-"), "order" + "".join(" " + n for n in names)]
    for i, n in enumerate(names):
        newest = keep > 0 and i + keep >= len(names)
        if not (newest or n == cur or n == prev):
            out.append("remove " + n)
    return out


# ── patch.lst: the C plan and the model agree ───────────────────────────


def lst(entries, **over):
    keys = {
        "app": "Fix",
        "version": "20261017-c2-g1",
        "base": "20261016-c1-g1",
        "base_sha": "a" * 64,
        "new_sha": "b" * 64,
        "stub_abi": "1",
    }
    keys.update(over)
    lines = ["xbr-patch 1"] + ["%s %s" % kv for kv in keys.items() if kv[1] is not None]
    lines += ["%s %d %s %s" % e for e in entries]
    return "\n".join(lines) + "\n"


def entry(kind, path, data=b"x"):
    return (kind, len(data), sha(data), path)


BASIC = [
    entry("C", "Fix.exe", b"new"),
    entry("N", "enhance/hud.pack"),
    entry("D", "old.dll"),
    entry("K", "fix_recomp.exe", b"same"),
    entry("K", "game_files/default.xbe", b"xbe"),
    entry("C", "game_files/media/a.bin", b"a2"),
    entry("N", "game_files/media/with space.bin"),
    entry("D", "game_files/media/gone.bin"),
]


def test_steps_order(driver, tmp_path):
    p = tmp_path / "patch.lst"
    p.write_text(lst(BASIC))
    got = driver("steps", str(p))
    assert got == model_steps(p.read_text())
    acts = [line.split(" ", 1)[0] for line in got[4:]]
    # Every check before the first write; game writes before game deletes.
    assert acts == [
        "check-program",
        "check-game",
        "copy",
        "copy",
        "link",
        "game-write",
        "game-write",
        "game-delete",
    ]
    assert "game-write game_files/media/with space.bin" in got
    # A deleted program file is simply left out of the new version.
    assert not any(line.endswith(" old.dll") for line in got)


BAD_LISTS = [
    ("not a list", "hello\n"),
    ("other format", "xbr-patch 2\napp Fix\n"),
    ("header later", "app Fix\nxbr-patch 1\n"),
    ("bad size", lst([("C", -1, "a" * 64, "x")])),
    ("plus size", lst([("C", 0, "a" * 64, "x")]).replace("C 0 ", "C +0 ")),
    ("bad sha", lst([("C", 1, "A" * 64, "x")])),
    ("short entry", lst([]) + "C 1 " + "a" * 64 + "\n"),
    ("dotdot", lst([entry("N", "../evil")])),
    ("absolute", lst([entry("N", "/evil")])),
    ("drive", lst([entry("N", "C:/evil")])),
    ("backslash", lst([entry("N", "a\\b")])),
    ("dot part", lst([entry("N", "a/./b")])),
    ("empty part", lst([entry("N", "a//b")])),
    ("no stub_abi", lst([], stub_abi=None)),
    ("no app", lst([], app=None)),
    ("empty key", lst([]) + "app \n"),
    ("partial version", lst([], version="v.partial")),
    ("old base", lst([], base="v.old")),
    ("slash version", lst([], version="a/b")),
    ("dot version", lst([], version=".v")),
    ("bad base_sha", lst([], base_sha="z" * 64)),
    ("short new_sha", lst([], new_sha="b" * 63)),
]


@pytest.mark.parametrize("name,text", BAD_LISTS, ids=[n for n, _ in BAD_LISTS])
def test_bad_lists_refused(driver, tmp_path, name, text):
    p = tmp_path / "patch.lst"
    p.write_text(text)
    got = driver("steps", str(p))
    assert got == model_steps(text), name
    assert got[0].startswith("error "), (name, got)


def test_comments_crlf_and_unknown_keys(driver, tmp_path):
    text = "# made by xbr\r\n" + lst(BASIC).replace("\n", "\r\n") + "later_key 7\r\n"
    p = tmp_path / "patch.lst"
    p.write_bytes(text.encode())
    got = driver("steps", str(p))
    assert got == model_steps(text)
    assert got[0] == "app Fix"


def test_random_lists_agree(driver, tmp_path):
    """Random entry mixes, and random single-byte damage to them, decide
    the same in C and in the model."""
    rng = random.Random(8)
    names = ["Fix.exe", "a/b.dll", "game_files/default.xbe", "game_files/m/x y.bin", "z"]
    for i in range(60):
        es = [entry(rng.choice("CNDK"), rng.choice(names), bytes([i, j])) for j in range(5)]
        text = lst(es)
        if i % 3 == 0:
            k = rng.randrange(len(text))
            text = text[:k] + rng.choice([" ", "", "/", ".", "\\", ":", "\n", "q"]) + text[k + 1 :]
        p = tmp_path / ("p%d.lst" % i)
        p.write_text(text)
        assert driver("steps", str(p)) == model_steps(text), text


def test_paths_and_versions(driver):
    paths = [
        "a",
        "a/b",
        "a b/c",
        "../a",
        "a/..",
        "./a",
        "a/",
        "/a",
        "a\\b",
        "c:x",
        "a\tb",
        "a./b",
        "a /b",
        "b.",
        "x.y",
        "",
    ]
    got = driver("path", *[p for p in paths if p])
    assert got == ["%d %s" % (path_ok(p), p) for p in paths if p]
    vers = ["20261017-c1", "v.partial", ".partial", "v.old", ".old", ".v", "a/b", "a:b", "x.y"]
    got = driver("version", *vers)
    assert got == ["%d %s" % (version_ok(v), v) for v in vers]


# ── the patch's lists cover its SHA256SUMS ──────────────────────────────


def sums(entries):
    return "".join("%s  %s\n" % (h, p) for _, _, h, p in entries)


def test_cover(driver, tmp_path):
    p, s = tmp_path / "patch.lst", tmp_path / "SHA256SUMS"
    p.write_text(lst(BASIC))
    live = [e for e in BASIC if e[0] != "D"]
    s.write_text(sums(live))
    assert driver("cover", str(p), str(s)) == ["ok"]
    # A file the sums list and the patch does not.
    s.write_text(sums(live + [entry("N", "stray.dll")]))
    assert driver("cover", str(p), str(s)) == ["stray.dll"]
    # A shipped file whose sha256 differs from the sums'.
    s.write_text(sums([e if e[3] != "Fix.exe" else entry("C", "Fix.exe", b"other") for e in live]))
    assert driver("cover", str(p), str(s)) == ["Fix.exe"]
    # A kept file the sums leave out.
    s.write_text(sums([e for e in live if e[3] != "fix_recomp.exe"]))
    assert driver("cover", str(p), str(s)) == ["fix_recomp.exe"]


# ── history, rollback target and prune ──────────────────────────────────

HIST = """\
2026-10-01T00:00:00Z install - -> v1
2026-10-02T00:00:00Z patch v1 -> v2
2026-10-03T00:00:00Z patch v2 -> v3
2026-10-04T00:00:00Z rollback v3 -> v2
2026-10-05T00:00:00Z install v2 -> v4
not a history line
2026-10-06T00:00:00Z patch v4 -> v5 extra
2026-10-07T00:00:00Z migrate - -> v0
"""


@pytest.mark.parametrize(
    "hist,keep,cur,names",
    [
        (HIST, 3, "v4", ["v1", "v2", "v3", "v4", "v0"]),
        (HIST, 1, "v4", ["v1", "v2", "v3", "v4"]),
        (HIST, 1, "v2", ["v1", "v2", "v3", "v4"]),
        (HIST, 0, "v0", ["v0", "v1", "v2"]),
        ("", 2, "-", ["b", "a", "c"]),
        ("", 1, "a", ["c", "b", "a"]),
        ("x y z -> w  \n", 1, "w", ["w", "z"]),
    ],
)
def test_prune_agrees(driver, tmp_path, hist, keep, cur, names):
    h = tmp_path / "history.txt"
    h.write_text(hist)
    got = driver("prune", str(h), str(keep), cur, *names)
    assert got == model_prune(hist, keep, cur, names)


def test_prune_keeps_current_and_previous(driver, tmp_path):
    h = tmp_path / "history.txt"
    h.write_text(HIST)
    got = driver("prune", str(h), "1", "v4", "v1", "v2", "v3", "v4")
    # v4 is current and newest; v2 is what v4 came from (the rollback
    # target); v1 and v3 go.
    assert got[0] == "previous v2"
    assert sorted(line for line in got if line.startswith("remove")) == [
        "remove v1",
        "remove v3",
    ]


def test_previous_after_rollback(driver, tmp_path):
    """A rollback writes "rollback <cur> -> <prev>": a second rollback goes
    forward again, as on steamos."""
    h = tmp_path / "history.txt"
    h.write_text(HIST)
    assert driver("prune", str(h), "9", "v2", "v2")[0] == "previous v3"
    assert driver("prune", str(h), "9", "v1", "v1")[0] == "previous -"


# ── what the CLI writes ─────────────────────────────────────────────────


@pytest.fixture
def game2(tmp_path):
    """game2 with a [package] table (as in test_windows_templates)."""
    root = tmp_path / "game2"
    root.mkdir()
    with open(os.path.join(GAME2, "game.toml")) as f:
        text = f.read()
    (root / "game.toml").write_text(text + '\n[package]\napp = "Second"\ncontent = "packaging"\n')
    saved = manifest.current()
    manifest.use(manifest.load(str(root)))
    plib()
    try:
        yield
    finally:
        manifest.use(saved)
        plib()


def man(files, version, abi=1):
    return {
        "product": "Second",
        "target": "windows-x86_64",
        "version": version,
        "data_layout": 1,
        "root": {"stub_abi": abi},
        "files": [{"path": p, "sha256": sha(b), "size": len(b)} for p, b in files.items()],
    }


V1 = {
    "Second.exe": b"launcher",
    "second_recomp.exe": b"exe1",
    "old.dll": b"old",
    "game_files/default.xbe": b"xbe",
    "game_files/media/a.bin": b"a1",
}
V2 = {
    "Second.exe": b"launcher",
    "second_recomp.exe": b"exe2",
    "new.dll": b"new",
    "game_files/default.xbe": b"xbe",
    "game_files/media/a.bin": b"a2",
}


def payload_of(d, files, m):
    for p, b in files.items():
        dst = os.path.join(d, *p.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b)
    text = json.dumps(m, indent=2) + "\n"
    with open(os.path.join(d, "manifest.json"), "w", newline="\n") as f:
        f.write(text)
    with open(os.path.join(d, "SHA256SUMS"), "w", newline="\n") as f:
        f.write("".join("%s  %s\n" % (e["sha256"], e["path"]) for e in m["files"]))


def make_patch(tmp_path, base_files=V1, new_files=V2):
    stage = tmp_path / "stage"
    payload = stage / "payload"
    payload.mkdir(parents=True)
    tools = stage / windows.ROOT_TOOLS
    tools.mkdir()
    (tools / windows.APPLY).write_bytes(b"MZapply")
    m1 = man(base_files, "20261016-c1-g1")
    m2 = man(new_files, "20261017-c2-g1")
    base = patch.Base((json.dumps(m1, indent=2) + "\n").encode(), "base")
    payload_of(str(payload), new_files, m2)
    # V2 changes a game file: a run that imported the game files (--dump).
    assert patch.problems(base, m2, "windows", imported=True) == []
    choice = patch.Choice("patch", "test", base, patch.diff(m1, m2))
    out = tmp_path / "out"
    out.mkdir()
    folder = windows.wrap_windows_patch(str(stage), str(payload), m2["version"], str(out), choice)
    return folder, base, m2


def test_patch_folder(game2, tmp_path, driver):
    folder, base, m2 = make_patch(tmp_path)
    assert os.path.basename(folder) == "Second-20261017-c2-g1-windows-patch-from-%s" % patch.short(
        "20261016-c1-g1"
    )
    got = sorted(
        os.path.relpath(os.path.join(dp, f), folder) for dp, _, fs in os.walk(folder) for f in fs
    )
    # Only the changed and new files ship, beside the full manifest.
    assert got == sorted(
        [
            "apply.exe",
            "patch.json",
            "patch.lst",
            os.path.join("files", "manifest.json"),
            os.path.join("files", "SHA256SUMS"),
            os.path.join("files", "second_recomp.exe"),
            os.path.join("files", "new.dll"),
            os.path.join("files", "game_files", "media", "a.bin"),
        ]
    )
    doc = json.load(open(os.path.join(folder, "patch.json")))
    assert doc["base_manifest_sha256"] == base.sha256 and doc["game_data"] is True
    lst_path = os.path.join(folder, "patch.lst")
    text = open(lst_path, encoding="utf-8").read()
    assert text.startswith("xbr-patch 1\napp Second\n")
    new_man = open(os.path.join(folder, "files", "manifest.json"), "rb").read()
    assert "new_sha %s\n" % sha(new_man) in text
    assert "base_sha %s\n" % base.sha256 in text
    steps_ = driver("steps", lst_path)
    assert steps_ == model_steps(text)
    assert "game-write game_files/media/a.bin" in steps_
    assert "check-game game_files/default.xbe" in steps_
    assert "link Second.exe" in steps_ and "copy new.dll" in steps_
    # The lists and the patch's own SHA256SUMS describe one version.
    assert driver("cover", lst_path, os.path.join(folder, "files", "SHA256SUMS")) == ["ok"]


def test_patch_folder_deterministic(game2, tmp_path):
    a, _, _ = make_patch(tmp_path / "a")
    b, _, _ = make_patch(tmp_path / "b")
    for n in ("patch.lst", "patch.json"):
        assert open(os.path.join(a, n), "rb").read() == open(os.path.join(b, n), "rb").read()


def test_patch_list_refuses_bad_names(game2):
    doc = {
        "version": "a/b",
        "base_version": "v1",
        "base_manifest_sha256": "a" * 64,
        "changed": [],
        "new": [],
        "deleted": [],
        "kept": [],
    }
    with pytest.raises(Exception, match="folder name"):
        windows.patch_list(doc, "Second", "b" * 64)
    doc["version"] = "v2"
    doc["new"] = [{"path": "a\nb", "sha256": "c" * 64, "size": 1}]
    with pytest.raises(Exception, match="line break"):
        windows.patch_list(doc, "Second", "b" * 64)


def test_base_rules(game2):
    """A Windows base takes a patch only from the versioned layout with an
    equal stub_abi (D5)."""
    m2 = man(V2, "v2")
    ok = patch.Base(json.dumps(man(V1, "v1")).encode(), "x", versioned=True)
    assert patch.problems(ok, m2, "windows", imported=True) == []
    # Without the import, V2's other game file is another tree.
    assert patch.problems(ok, m2, "windows") == [
        "built from other game files (the same count, other contents)"
    ]
    inplace = patch.Base(json.dumps(man(V1, "v1")).encode(), "x", versioned=False)
    assert "in-place" in patch.problems(inplace, m2, "windows")[0]
    old = man(V1, "v1")
    del old["root"]
    assert any(
        "in-place" in w
        for w in patch.problems(patch.Base(json.dumps(old).encode(), "x"), m2, "windows")
    )
    abi2 = patch.Base(json.dumps(man(V1, "v1", abi=2)).encode(), "x")
    assert any("stub_abi" in w for w in patch.problems(abi2, m2, "windows"))


def test_stub_abi_one_number():
    """stub.c, apply.c and the CLI name the same STUB_ABI."""
    for n in ("stub.c", "apply.c"):
        text = open(os.path.join(TEMPLATES, n)).read()
        assert re.search(r"#define STUB_ABI (\d+)", text).group(1) == str(windows.STUB_ABI), n


def test_root_block(tmp_path):
    tools = tmp_path / windows.ROOT_TOOLS
    tools.mkdir()
    (tools / windows.STUB).write_bytes(b"stub")
    (tools / windows.APPLY).write_bytes(b"apply!")
    assert windows.root_tools_ready(str(tmp_path))
    assert windows.root_block(str(tmp_path)) == {
        "stub_abi": windows.STUB_ABI,
        "stub": {"sha256": sha(b"stub"), "size": 4},
        "apply": {"sha256": sha(b"apply!"), "size": 6},
    }


# ── the installer ───────────────────────────────────────────────────────


def test_installer_versioned(game2, tmp_path):
    from xboxrecomp_cli.package import lib as pl
    from xboxrecomp_cli.package import render

    payload = tmp_path / "payload"
    (payload / "enhance").mkdir(parents=True)
    for n in ("Second.exe", "manifest.json", "SHA256SUMS", "enhance/hud.pack"):
        (payload / n).write_text("x")
    names, inst, uninst = pl.nsis_lists(str(payload), "versions/v1.partial")
    lines = inst.splitlines()
    assert lines[0] == '  SetOutPath "$INSTDIR\\versions\\v1.partial"'
    assert '  SetOutPath "$INSTDIR\\versions\\v1.partial\\enhance"' in lines
    assert lines[-1] == '  SetOutPath "$INSTDIR"'
    # The Delete lines stay the in-place layout's (an interrupted migration).
    assert '  Delete "$INSTDIR\\Second.exe"\n' in uninst
    values = windows.installer_values(
        pl, "v1", "C:/o/setup.exe", str(payload), "C:/i.ico", inst, uninst, 0, "C:/stage/root"
    )
    dst = tmp_path / "installer.nsi"
    render(pkg_template("windows", "installer.nsi.in"), values, str(dst))
    text = dst.read_text()
    assert not re.findall(r"@[A-Z_]+@", text)
    main = text[text.index('Section "') : text.index("SectionEnd")]
    # migrate, then the extract into .partial, the game files, then switch.
    order_ = [
        main.index("apply.exe migrate"),
        main.index('RMDir /r "${PART}"'),
        main.index("versions\\v1.partial\\enhance"),
        main.index('CopyFiles /SILENT "$EXEDIR\\game_files'),
        main.index('switch "v1"'),
        main.index("WriteUninstaller"),
    ]
    assert order_ == sorted(order_)
    un = text[text.index('Section "Uninstall"') :]
    # Each version's game_files (a junction) goes by a plain RMDir before
    # the recursive delete of versions\.
    assert un.index('RMDir "$INSTDIR\\versions\\$1\\game_files"') < un.index(
        'RMDir /r "$INSTDIR\\versions"'
    )
    for n in ("current.txt", "apply.exe", "Second.exe.stub"):
        assert 'Delete "$INSTDIR\\%s"' % n in un
    assert 'RMDir /r "$INSTDIR\\state"' in un


def write_ico(path):
    """A 16x16 32-bit icon, the smallest makensis takes."""
    import struct

    w = h = 16
    pixels = b"\x80\x40\x20\xff" * (w * h)
    mask = b"\0" * (4 * h)
    bmp = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    data = bmp + pixels + mask
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, 1))
        f.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), 22))
        f.write(data)


def test_installer_compiles(game2, tmp_path):
    """makensis accepts the rendered installer (when it is installed)."""
    from xboxrecomp_cli import doctor
    from xboxrecomp_cli.package import lib as pl
    from xboxrecomp_cli.package import render

    mk = doctor.makensis_path()
    if not mk:
        pytest.skip("no makensis")
    payload = tmp_path / "payload"
    (payload / "game_files").mkdir(parents=True)
    for n in ("Second.exe", "manifest.json", "SHA256SUMS", "game_files/default.xbe"):
        (payload / n).write_text("x")
    tools = tmp_path / "root"
    tools.mkdir()
    for n in (windows.STUB, windows.APPLY):
        (tools / n).write_bytes(b"MZ")
    ico = str(tmp_path / "app.ico")
    write_ico(ico)
    names, inst, uninst = pl.nsis_lists(str(payload), "versions/v1.partial")
    setup = tmp_path / "setup.exe"
    values = windows.installer_values(
        pl, "v1", str(setup), str(payload), ico, inst, uninst, 1, str(tools)
    )
    nsi = tmp_path / "installer.nsi"
    render(pkg_template("windows", "installer.nsi.in"), values, str(nsi))
    r = subprocess.run([mk, "-V2", str(nsi)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert setup.is_file()


# ── llvm-mingw: the Windows programs build ──────────────────────────────


def mingw_clang():
    root = os.environ.get("LLVM_MINGW_ROOT")
    if root:
        p = os.path.join(root, "bin", "x86_64-w64-mingw32-clang")
        return p if os.path.isfile(p) else None
    return shutil.which("x86_64-w64-mingw32-clang")


def render_c(name, dst, values):
    text = open(os.path.join(TEMPLATES, name)).read()
    for k, v in values.items():
        text = text.replace("@%s@" % k, v)
    assert not re.findall(r"@[A-Z_]+@", text), name
    with open(dst, "w") as f:
        f.write(text)


def test_mingw_builds(tmp_path):
    cc = mingw_clang()
    if not cc:
        pytest.skip("no llvm-mingw (set LLVM_MINGW_ROOT)")
    vals = {"APP": "Fix", "EXE": "fix_recomp.exe", "DATA_DIR": "Fix", "DATA_DIR_ENV": "FIX_DATA"}
    for n in ("apply.c", "stub.c", "launcher.c"):
        render_c(n, str(tmp_path / n), vals)
    shutil.copy(os.path.join(TEMPLATES, "apply_plan.c"), tmp_path)
    shutil.copy(os.path.join(TEMPLATES, "apply_plan.h"), tmp_path)
    common = ["-municode", "-O2", "-Wall", "-Wextra", "-Werror", "-Wl,--no-insert-timestamp"]
    builds = {
        "apply.exe": ["apply.c", "apply_plan.c", "-lbcrypt", "-ladvapi32"],
        "stub.exe": ["-mwindows", '-DAPP_NAME=L"Fix"', "stub.c"],
        "launcher.exe": [
            "-mwindows",
            '-DAPP_NAME=L"Fix"',
            "launcher.c",
            "-lshell32",
            "-lole32",
            "-luuid",
        ],
    }
    first = {}
    for rnd in (1, 2):
        for exe, args in builds.items():
            out = str(tmp_path / ("%d-%s" % (rnd, exe)))
            subprocess.run([cc, *common, *args, "-o", out], cwd=tmp_path, check=True)
            data = open(out, "rb").read()
            assert data[:2] == b"MZ"
            if rnd == 1:
                first[exe] = data
            else:
                # The same bytes twice (D12: a reused stage keeps them).
                assert data == first[exe], exe
    # apply.exe is a console program (output and prompts); the stub is not.
    assert b"BCryptHashData" in first["apply.exe"]
    assert b"Local\\" in first["apply.exe"].replace(b"\0", b"")


# ── D10: one refusal on every target ────────────────────────────────────


def test_d10_messages_on_every_applier():
    """The three appliers refuse a wrong base in D10's words: the found
    version (or "with another manifest"), a kept file, and no install.
    steamos and macOS run them in test_steamos_patch / test_macos_patch;
    apply.c's run on the PC (tasks 7.5), so its strings are checked here."""
    tpl = os.path.dirname(TEMPLATES)
    texts = {
        n: open(os.path.join(tpl, *n.split("/"))).read()
        for n in ("windows/apply.c", "steamos/install_lib.py", "macos/apply.command")
    }
    for n, t in texts.items():
        flat = re.sub(r'"\s*L?"', "", t)  # join C's and Python's split literals
        assert re.search(r"This update is for \S+; this install is \S+", flat), n
        assert re.search(r"This update is for \S+, but \S+ does not match it", flat), n
        assert flat.count("Use the full installer for ") >= 3, n
        assert "with another manifest" in flat, n
    assert "this install is not installed" in re.sub(r'"\s*L?"', "", texts["windows/apply.c"])
    assert '"not installed"' in texts["steamos/install_lib.py"]


# ── the HUD tweaks across a chain (wizard-paths 6.3) ─────────────────────

PACK = "enhance/hud.pack"
DEFAULTS = "config.default.toml"
STOCK = b"[render]\nscale = 1\n"
WITH_PACK = STOCK + b'\n[textures]\npack = "enhance/hud.pack"\n'


def chain_patch(tmp_path, base_files, new_files, v_base, v_new):
    stage = tmp_path / "stage"
    payload = stage / "payload"
    payload.mkdir(parents=True)
    tools = stage / windows.ROOT_TOOLS
    tools.mkdir()
    (tools / windows.APPLY).write_bytes(b"MZapply")
    m1, m2 = man(base_files, v_base), man(new_files, v_new)
    base = patch.Base((json.dumps(m1, indent=2) + "\n").encode(), "base")
    payload_of(str(payload), new_files, m2)
    choice = patch.Choice("patch", "test", base, patch.diff(m1, m2))
    out = tmp_path / "out"
    out.mkdir()
    return windows.wrap_windows_patch(str(stage), str(payload), m2["version"], str(out), choice)


def applied(folder, installed, driver):
    """The tree apply.exe leaves, by the plan the C code decides (its steps
    are the model's): kept files linked from the base, changed and new ones
    from files/, game files written and deleted in place."""
    lst_path = os.path.join(folder, "patch.lst")
    text = open(lst_path, encoding="utf-8").read()
    assert driver("steps", lst_path) == model_steps(text)
    assert driver("cover", lst_path, os.path.join(folder, "files", "SHA256SUMS")) == ["ok"]
    _keys, entries = parse_patch(text)
    out = {p: b for p, b in installed.items() if p.startswith(GAME)}
    for kind, _size, _h, p in entries:
        if kind == "K" and not p.startswith(GAME):
            out[p] = installed[p]
        elif kind in "CN":
            with open(os.path.join(folder, "files", *p.split("/")), "rb") as f:
                out[p] = f.read()
        elif kind == "D" and p.startswith(GAME):
            del out[p]
    return out


def test_clear_then_new_pack_chain_equals_full_installs(game2, tmp_path, driver):
    """Full with pack A; then --clear-hud-pack (deletes the pack, changes
    the defaults); then --hud-pack B (both back with B's bytes). Each
    applied tree is that version's full install."""
    v1 = dict(V1, **{PACK: b"pack A", DEFAULTS: WITH_PACK})
    v2 = dict(V1, **{"second_recomp.exe": b"exe2", DEFAULTS: STOCK})
    v3 = dict(v2, **{PACK: b"pack B", DEFAULTS: WITH_PACK})
    vs = [("20261016-c1-g1", v1), ("20261017-c2-g1", v2), ("20261018-c3-g1", v3)]
    tree = dict(v1)
    for i, ((vb, base), (vn, new)) in enumerate(zip(vs, vs[1:])):
        folder = chain_patch(tmp_path / str(i), base, new, vb, vn)
        text = open(os.path.join(folder, "patch.lst"), encoding="utf-8").read()
        kinds = {p: k for k, _s, _h, p in parse_patch(text)[1]}
        assert kinds[PACK] == ("D" if i == 0 else "N")
        assert kinds[DEFAULTS] == "C"
        tree = applied(folder, tree, driver)
        assert tree == new
    assert tree[PACK] == b"pack B"


# ── the config.toml note (D8): apply_plan.c and install_lib agree ───────

OVERRIDE_CASES = [
    ('[textures]\npack = "none"\n', WITH_PACK, "hides none"),
    ('[render]\nscale = 2\n[textures]\npack = "enhance/hud.pack"\n', WITH_PACK, "none "),
    ('[textures]\npack = "enhance/hud.pack"\n', STOCK, "stale enhance/hud.pack"),
    ('[textures]\npack = "none"\n', STOCK, "none "),
    ("[ textures ] # t\r\npack='a b' # c\r\n", WITH_PACK, "hides a b"),
    ("[textures]\npack = x.pack # c\n", None, "stale x.pack"),
    ('[render]\npack = "x"\n[textures]\n', WITH_PACK, "none "),
    ('[textures]\npacked = "x"\n', WITH_PACK, "none "),
    ('[textures]\n[other]\npack = "x"\n', WITH_PACK, "none "),
    (None, WITH_PACK, "none "),
]


def test_override_note_agrees_with_install_lib(driver, tmp_path, tmp_path_factory):
    from test_steamos_install import load_il

    il = load_il(tmp_path_factory)
    for i, (config, defaults, want) in enumerate(OVERRIDE_CASES):
        paths = []
        for name, text in (("config", config), ("defaults", defaults)):
            if text is None:
                paths.append("-")
                continue
            p = tmp_path / ("%s%d.toml" % (name, i))
            p.write_bytes(text.encode() if isinstance(text, str) else text)
            paths.append(str(p))
        assert driver("override", *paths) == [want], (config, defaults)
        note = il.override_note(*(p if p != "-" else str(tmp_path / "missing") for p in paths))
        kind, _, val = want.partition(" ")
        if kind == "none":
            assert note == "", (config, note)
        else:
            assert ('"%s"' % val) in note and ("overrides" in note) == (kind == "hides"), note
