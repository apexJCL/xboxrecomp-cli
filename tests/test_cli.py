"""The CLI's host logic, with fake downloads and mocked hosts: no network,
no toolchain, no build. A test that needs a game tree gets a scratch copy
of testdata/game (fake_tree), so nothing real is read or written.

  uv run pytest tests/test_cli.py

(These were BLiNX 2's scripts/test_blinx2_cli.py, against blinx2.py.)
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile

from xboxrecomp_cli import build, env, fetch, host, main, manifest, pipeline, setup, toolkit
from xboxrecomp_cli import doctor as doctor_mod
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.host import CliError
from xboxrecomp_cli.package import lib as pl

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")


def raises(fn, *a, match=""):
    try:
        fn(*a)
    except CliError as e:
        assert match in str(e), (match, str(e))
        return str(e)
    raise AssertionError("no CliError from %s%r" % (fn.__name__, a))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@contextlib.contextmanager
def fake_tree(d):
    """The test game copied to d/cat with the files the pipeline reads, a
    fake toolkit at d/tk; the current game while it runs."""
    root = os.path.join(d, "cat")
    tk = os.path.join(d, "tk")
    os.makedirs(root)
    shutil.copy(os.path.join(GAME, "game.toml"), root)
    shutil.copytree(os.path.join(GAME, "config"), os.path.join(root, "config"))
    G = manifest.load(root)
    for p in (G.gen, G.game_files, G.out):
        os.makedirs(p, exist_ok=True)
    for p, text in (
        (G.xbe, "xbe"),
        (G.seeds[0], "[]"),
        (G.names_hooks[0], "#"),
        (G.recomp_manual, "/* */"),
        (G.spin_waits, "{}"),
    ):
        write(p, text)
    open(os.path.join(G.gen, "recomp_funcs.h"), "w").close()
    os.makedirs(os.path.join(tk, "tools", "recomp", "output"), exist_ok=True)
    keys = ("XBOXRECOMP_DIR", "ICALL_DB", "SPLIT", "XBOXRECOMP_PYTHON", "LLVM_MINGW_ROOT")
    env_saved = {k: os.environ.get(k) for k in keys}
    saved = manifest.current()
    os.environ["XBOXRECOMP_DIR"] = tk
    for k in ("ICALL_DB", "SPLIT"):
        os.environ.pop(k, None)
    manifest.use(G)
    try:
        yield root, tk
    finally:
        manifest.use(saved)
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_host_mapping():
    assert [
        host.host_os(s) for s in ("Windows", "Darwin", "Linux", "CYGWIN_NT-10.0", "MSYS_NT")
    ] == ["windows", "macos", "linux", "windows", "windows"]
    assert [host.host_arch(m) for m in ("AMD64", "x86_64", "arm64", "aarch64", "ARM64")] == [
        "x86_64",
        "x86_64",
        "aarch64",
        "aarch64",
        "aarch64",
    ]
    assert host.exe_suffix("windows") == ".exe" and host.exe_suffix("linux") == ""


def test_asset_selection():
    t = "20260922"
    want = {
        ("windows", "x86_64"): "llvm-mingw-20260922-ucrt-x86_64.zip",
        ("windows", "aarch64"): "llvm-mingw-20260922-ucrt-aarch64.zip",
        ("linux", "x86_64"): "llvm-mingw-20260922-ucrt-ubuntu-22.04-x86_64.tar.xz",
        ("linux", "aarch64"): "llvm-mingw-20260922-ucrt-ubuntu-22.04-aarch64.tar.xz",
        ("macos", "x86_64"): "llvm-mingw-20260922-ucrt-macos-universal.tar.xz",
        ("macos", "aarch64"): "llvm-mingw-20260922-ucrt-macos-universal.tar.xz",
    }
    for (o, a), name in want.items():
        assert fetch.mingw_asset_name(t, o, a) == name
    raises(fetch.mingw_asset_name, t, "freebsd", "x86_64", match="no llvm-mingw build")
    assert fetch.mingw_dir_for(want[("windows", "x86_64")]).endswith(
        os.path.join("third_party", "llvm-mingw-20260922-ucrt-x86_64")
    )


def test_every_asset_pinned():
    """The test game's setup-pins.json (BLiNX 2's) holds every asset this
    CLI may ask for, at the tag game.toml names."""
    pins = fetch.load_pins()
    tag = fetch.mingw_tag({})
    assert pins["llvm_mingw"]["tag"] == tag
    names = {v.format(tag=tag) for v in fetch.MINGW_ASSETS.values()}
    assert names == set(pins["llvm_mingw"]["assets"]), names ^ set(pins["llvm_mingw"]["assets"])
    for a in pins["llvm_mingw"]["assets"].values():
        assert (
            len(a["sha256"]) == 64 and a["size"] > 0 and a["url"].startswith("https://github.com/")
        )
    nsis = host.g().m["toolchain"]["nsis"]
    assert pins["nsis"]["version"] == nsis and len(pins["nsis"]["sha256"]) == 64
    assert "toolkit" not in pins  # game.toml [toolkit] holds that pin


def test_lockfile_hashed():
    """Every package this CLI's uv.lock downloads is pinned by hash."""
    import tomllib

    with open(os.path.join(os.path.dirname(HERE), "uv.lock"), "rb") as f:
        lock = tomllib.load(f)
    pkgs = {}
    for p in lock["package"]:
        if "registry" not in p["source"]:
            continue  # the project itself
        files = ([p["sdist"]] if "sdist" in p else []) + p.get("wheels", [])
        assert files, p["name"]
        for f in files:
            assert f["hash"].startswith("sha256:") and len(f["hash"]) == 71, (p["name"], f)
        pkgs.setdefault(p["name"], []).append(p["version"])
    for name in ("pytest", "ruff", "numpy"):
        assert name in pkgs, name
    assert pkgs["ruff"] == ["0.16.10"], pkgs["ruff"]


def fake_urlopen(payload):
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    return lambda req: R(payload)


def test_digest_mismatch_deletes(d, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(b"evil bytes"))
    dest = os.path.join(d, "x.zip")
    raises(
        fetch.download, "https://example.invalid/x.zip", dest, "0" * 64, match="checksum mismatch"
    )
    assert os.listdir(d) == [], os.listdir(d)
    good = hashlib.sha256(b"evil bytes").hexdigest()
    raises(
        fetch.download, "https://example.invalid/x.zip", dest, good, 999, match="checksum mismatch"
    )
    assert os.listdir(d) == []
    fetch.download("https://example.invalid/x.zip", dest, good, len(b"evil bytes"))
    assert os.listdir(d) == ["x.zip"]


def test_fetch_mingw_mismatch_unpacks_nothing(d, monkeypatch):
    with fake_tree(d):
        monkeypatch.setattr(fetch, "mingw_tag", lambda env=None: "t1")
        asset = fetch.mingw_asset_name("t1")
        pins = {
            "llvm_mingw": {
                "tag": "t1",
                "assets": {
                    asset: {"url": "https://example.invalid/a", "sha256": "1" * 64, "size": 3}
                },
            }
        }
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(b"abc"))
        raises(fetch.fetch_mingw, pins, match="checksum mismatch")
        assert os.listdir(host.g().third_party) == []


def test_archive_member_checks(d):
    bad = [
        [("top/../../etc/x", None, False)],
        [("/abs", None, False)],
        [("C:\\x", None, False)],
        [("top/link", "../../outside", True)],
        [("top/hl", "/etc/passwd", False)],
    ]
    for m in bad:
        raises(fetch.check_archive_members, m, match="unsafe")
    fetch.check_archive_members(
        [
            ("top/bin/clang", None, False),
            ("top/bin/cc", "clang", True),
            ("top/lib/x", "../bin/clang", True),
        ]
    )
    # A good tar: strip=1, the in-tree symlink kept.
    tp = os.path.join(d, "a.tar.xz")
    with tarfile.open(tp, "w:xz") as t:
        data = b"#!/bin/sh\n"
        ti = tarfile.TarInfo("llvm-mingw-x/bin/clang")
        ti.size, ti.mode = len(data), 0o755
        t.addfile(ti, io.BytesIO(data))
        li = tarfile.TarInfo("llvm-mingw-x/bin/cc")
        li.type, li.linkname = tarfile.SYMTYPE, "clang"
        t.addfile(li)
    fetch.extract(tp, os.path.join(d, "out"))
    assert os.access(os.path.join(d, "out", "bin", "clang"), os.X_OK)
    assert os.readlink(os.path.join(d, "out", "bin", "cc")) == "clang"
    # A zip with a '..' member is refused before anything is written.
    zp = os.path.join(d, "a.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("top/ok", "1")
        z.writestr("top/../../evil", "2")
    raises(fetch.extract, zp, os.path.join(d, "zout"), match="unsafe")
    assert not os.path.exists(os.path.join(d, "zout", "ok"))


def test_venv_layout():
    assert host.venv_bin("/v", "windows") == os.path.join("/v", "Scripts")
    assert host.venv_bin("/v", "linux") == os.path.join("/v", "bin")
    assert host.venv_python("/v", "windows") == os.path.join("/v", "Scripts", "python.exe")
    assert host.venv_python("/v", "macos") == os.path.join("/v", "bin", "python")


def test_macos_target_refused():
    for h in ("linux", "windows"):
        msg = raises(build.check_build_target, "macos", h, match="needs a macOS host")
        assert "windows" in msg
    build.check_build_target("macos", "macos")
    build.check_build_target("windows", "linux")
    assert build.default_build_target("macos") == "macos"
    assert build.default_build_target("windows") == build.default_build_target("linux") == "windows"


def test_build_target_not_in_manifest(d):
    """A game whose build.targets leaves macos out is refused it on a Mac."""
    with fake_tree(d):
        host.g().m["build"]["targets"] = ["windows"]
        raises(build.check_build_target, "macos", "macos", match="build.targets")
        build.check_build_target("windows", "macos")


def test_ninja_required_on_windows(d):
    with fake_tree(d):
        venv = host.g().venv
        os.makedirs(os.path.join(venv, "Scripts"))
        open(os.path.join(venv, "Scripts", "cmake.exe"), "w").close()
        raises(build.build_tools, False, "windows", match="Ninja is required")
        os.makedirs(os.path.join(venv, "bin"))
        open(os.path.join(venv, "bin", "cmake"), "w").close()
        assert build.build_tools(False, "linux") == (os.path.join(venv, "bin", "cmake"), None)


def test_windows_path_warnings():
    assert doctor_mod.windows_path_warnings("C:\\b2", True) == []
    w = doctor_mod.windows_path_warnings(
        "C:\\Users\\someone\\Documents\\projects\\games\\recomp\\blinx2-recomp-checkout", False
    )
    assert len(w) == 2 and "short" in w[0] and "LongPathsEnabled" in w[1], w
    assert doctor_mod.windows_path_warnings("C:\\b2", None) == []


def test_reserved_names():
    for bad in ("CON", "nul.txt", "COM1", "LPT9.bin", "x.", "x ", "a?b"):
        assert pl.name_problem(bad), bad
    for ok in ("console", "default.xbe", "COM10", "media"):
        assert pl.name_problem(ok) is None, ok


def test_developer_dir(d, monkeypatch):
    monkeypatch.setattr(host, "CLT", d)
    e = host.build_env({"PATH": "/usr/bin"}, "macos")
    assert e["DEVELOPER_DIR"] == d
    e = host.build_env({"PATH": "/usr/bin", "DEVELOPER_DIR": "/Applications/Xcode.app"}, "macos")
    assert e["DEVELOPER_DIR"] == "/Applications/Xcode.app"
    assert "DEVELOPER_DIR" not in host.build_env({"PATH": "/usr/bin"}, "linux")
    monkeypatch.setattr(host, "CLT", os.path.join(d, "absent"))
    assert "DEVELOPER_DIR" not in host.build_env({"PATH": "/usr/bin"}, "macos")


def fake_uv_on_path(d, version, monkeypatch):
    """PATH holding only an executable named uv, whose version uv_version
    reports as given (None: no uv on PATH at all)."""
    bindir = os.path.join(d, "uvbin")
    os.makedirs(bindir, exist_ok=True)
    if version is not None:
        exe = os.path.join(bindir, "uv" + host.exe_suffix())
        write(exe, "")
        os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", bindir)
    monkeypatch.setattr(env, "uv_version", lambda uv: version)


def test_uv_hint():
    for os_name, cmd in (
        ("macos", "brew install uv"),
        ("linux", "https://astral.sh/uv/install.sh"),
        ("windows", "winget install --id=astral-sh.uv"),
    ):
        h = env.uv_hint(os_name)
        assert cmd in h and env.UV_DOCS in h, h


def test_find_uv(d, monkeypatch):
    fake_uv_on_path(d, None, monkeypatch)
    raises(env.find_uv, match="no uv on PATH; install it with: " + env.UV_HINT[host.host_os()])
    fake_uv_on_path(os.path.join(d, "old"), (0, 4, 30), monkeypatch)
    raises(env.find_uv, match="is uv 0.4.30, older than 0.5.31")
    fake_uv_on_path(os.path.join(d, "good"), (0, 12, 21), monkeypatch)
    uv, ver = env.find_uv()
    assert os.path.basename(uv).startswith("uv") and ver == (0, 12, 21), (uv, ver)


def test_sync_venv_without_uv(d, monkeypatch):
    """setup proper needs uv; the package plan's setup leaves a working
    .venv alone without one, and an empty .venv still needs uv."""
    with fake_tree(d):
        fake_uv_on_path(d, None, monkeypatch)
        raises(env.sync_venv, False, True, match="no uv on PATH")
        raises(env.sync_venv, False, False, match="no uv on PATH")
        for t in ("cmake", "ninja", "python"):
            write(os.path.join(host.venv_bin(), t + host.exe_suffix()), "")
        assert env.venv_ready()
        env.sync_venv(False, False)
        raises(env.sync_venv, False, True, match="no uv on PATH")


def test_cli_rejects_stray_args():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            main.main(["--game", GAME, "doctor", "--bogus"])
            raise AssertionError("accepted --bogus")
        except SystemExit as e:
            assert e.code == 2
    assert "unrecognized arguments" in err.getvalue()


def test_manifest_error_named():
    """A broken game.toml stops every command with the key and the reason."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main.main(["--game", HERE, "--prog", "blinx2", "doctor"]) == 1
    assert err.getvalue().startswith("blinx2: "), err.getvalue()


# ── the one-liner: native target, generation key, plan, packaging build ──


def test_native_target(monkeypatch):
    assert [main.native_target(o) for o in ("macos", "linux", "windows")] == [
        "macos",
        "steamos",
        "windows",
    ]
    seen = []
    monkeypatch.setattr(main, "cmd_package", lambda a: seen.append(a.target))
    assert main.main(["--game", GAME]) == 0
    assert seen == [main.native_target()]


def test_command_table():
    main.register()
    funcs = {name: f.__name__ for name, _, _, f in main.COMMANDS}
    assert funcs["package"] == "cmd_package" and funcs["setup"] == "cmd_setup"
    assert funcs["doctor"] == "cmd_doctor" and funcs["build"] == "cmd_build"
    names = [name for name, _, _, _ in main.COMMANDS]
    assert names == [
        "build",
        "parse",
        "disasm",
        "funcid",
        "abi",
        "ghidra",
        "names",
        "recomp",
        "analyze",
        "all",
        "setup",
        "doctor",
        "pins",
        "package",
    ], names


def test_help_sections():
    ap, _ = main.make_parser()
    text = ap.format_help()
    cmds, dev = text.split("\nCommands:")[1].split("Developer commands:")
    for w in ("analyze", "recomp", "build [", "pins"):
        assert w not in cmds and w in dev, w
    assert "blinx2 package" in cmds and "blinx2 doctor" in cmds


def test_help_bench_is_a_developer_command():
    ap, _ = main.make_parser()
    text = ap.format_help()
    cmds, dev = text.split("\nCommands:")[1].split("Developer commands:")
    assert "bench" not in cmds and "blinx2 bench <command>" in dev, text
    # The player block is the one from before bench existed.
    assert cmds.strip().splitlines()[-1].strip().startswith("blinx2 setup [--dev]"), cmds


def test_help_is_the_bootstraps():
    """--help through argparse and helptext.py run alone (the bootstrap's
    no-uv path) print the same bytes."""
    import subprocess

    from xboxrecomp_cli import helptext

    ap, _ = main.make_parser("blinx2")
    alone = subprocess.run(
        [sys.executable, helptext.__file__, GAME, "blinx2"], capture_output=True, text=True
    ).stdout
    assert ap.format_help() == alone


def test_bench_help_and_dispatch():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main.main(["--game", GAME, "bench", "--help"]) == 0
    text = out.getvalue()
    for w in (
        "integrate",
        "golden",
        "pacing",
        "doctor",
        "BENCH_HOST",
        "outer flock",
        "$BENCH_DIR/xboxrecomp",
        "blinx2 golden record",
        "cat_recomp.exe",
    ):
        assert w in text, w
    import re

    assert not re.search(r"@[A-Z_]+@", text), text  # every placeholder filled
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main.main(["--game", GAME, "bench", "nonsense"]) == 1


def test_stage_argv_snapshot(d):
    with fake_tree(d):
        argv = json.loads(pipeline.stage_argv())
        sections = ",".join(host.g().m["pipeline"]["disasm"]["extra_sections"])
    tm = "$TK/tools/ghidra_naming/merge_names.py"
    assert tm not in json.dumps(argv)  # no Ghidra export: no names stage
    assert argv == [
        [
            "tool",
            "xbe_parser",
            ["$ROOT/game_files/default.xbe", "--json", "$ROOT/game_files/default_analysis.json"],
        ],
        [
            "tool",
            "disasm",
            [
                "$ROOT/game_files/default.xbe",
                "--analysis-json",
                "$ROOT/game_files/default_analysis.json",
                "-o",
                "$ROOT/analysis/disasm",
                "--text-only",
                "--extra-sections",
                sections,
                "--seed-functions",
                "$ROOT/config/seed_functions.json",
                "-v",
            ],
        ],
        [
            "tool",
            "func_id",
            [
                "$ROOT/game_files/default.xbe",
                "--functions",
                "$ROOT/analysis/disasm/functions.json",
                "--strings",
                "$ROOT/analysis/disasm/strings.json",
                "--xrefs",
                "$ROOT/analysis/disasm/xrefs.json",
                "-o",
                "$ROOT/analysis/func_id",
                "-v",
            ],
        ],
        [
            "tool",
            "abi_analysis",
            [
                "$ROOT/game_files/default.xbe",
                "--disasm-dir",
                "$ROOT/analysis/disasm",
                "--func-id-dir",
                "$ROOT/analysis/func_id",
                "--output-dir",
                "$ROOT/analysis/abi",
                "-v",
            ],
        ],
        [
            "tool",
            "recomp",
            [
                "$ROOT/game_files/default.xbe",
                "--all",
                "--split",
                "250",
                "--gen-dir",
                "$ROOT/src/recomp/gen",
                "--exclude-manual",
                "$ROOT/src/recomp_manual.c",
                "--game-name",
                "cat",
                "--disasm-dir",
                "$ROOT/analysis/disasm",
                "--func-id-dir",
                "$ROOT/analysis/func_id",
                "--abi-dir",
                "$ROOT/analysis/abi",
                "--spin-waits",
                "$ROOT/config/spin_waits.json",
                "-o",
                "$ROOT/analysis/recomp",
            ],
        ],
    ], argv


def test_stage_argv_optional_inputs(d):
    """A game without exclude_manual, spin_waits or extra sections passes
    none of their flags (upstream's README order otherwise)."""
    with fake_tree(d):
        p = host.g().m["pipeline"]
        p["exclude_manual"] = p["spin_waits"] = ""
        p["disasm"]["extra_sections"] = []
        manifest.use(manifest.Game(host.g().root, host.g().m))
        argv = json.dumps(json.loads(pipeline.stage_argv()))
    for flag in ("--exclude-manual", "--spin-waits", "--extra-sections"):
        assert flag not in argv, flag


def test_gen_key_inputs(d):
    with fake_tree(d) as (root, tk):
        inputs = pipeline.gen_inputs()
        assert inputs["$ROOT/config/seed_functions.json"] != "absent"
        assert inputs["$ROOT/config/spin_waits.json"] != "absent"
        assert inputs["$TK/tools/recomp/output/icall_targets.json"] == "absent"
        assert inputs["$ROOT/analysis/ghidra/export/functions.json"] == "absent"
        for p in inputs:
            real = p.replace("$ROOT", root).replace("$TK", tk)
            assert os.path.isfile(real) == (inputs[p] != "absent"), p


def test_gen_key_staleness(d, monkeypatch):
    with fake_tree(d):
        G = host.g()
        assert pipeline.gen_stale_reasons() == ["no key"]
        pipeline.write_gen_key()
        assert pipeline.gen_stale_reasons() == []
        cases = [
            (lambda: write(G.seeds[0], "[1]"), "config/seed_functions.json changed"),
            (lambda: write(G.xbe, "xbe2"), "the XBE changed"),
            (lambda: write(G.recomp_manual, "/* 2 */"), "src/recomp_manual.c changed"),
            (lambda: write(G.spin_waits, '{"auto": true}'), "config/spin_waits.json changed"),
            # The gitignored feedback database appearing changes inputs and argv.
            (
                lambda: write(pipeline.icall_db(), "{}"),
                "toolkit:tools/recomp/output/icall_targets.json changed",
            ),
            (lambda: os.environ.__setitem__("SPLIT", "100"), "stage commands changed"),
            (lambda: write(G.ghidra_export, "{}"), "Ghidra export appeared or went"),
        ]
        for change, why in cases:
            change()
            got = pipeline.gen_stale_reasons()
            assert why in got, (why, got)
            pipeline.write_gen_key()
            assert pipeline.gen_stale_reasons() == [], why
        # Any stage run invalidates the key; extra arguments keep it stale.
        pipeline.begin_stage("disasm", ["--foo"])
        assert pipeline.gen_stale_reasons() == ["no key"]
        pipeline.write_gen_key()
        assert pipeline.gen_stale_reasons() == ["a stage ran with extra arguments"]
        pipeline.begin_stage("disasm", [])
        pipeline.write_gen_key()
        assert pipeline.gen_stale_reasons() == []
        # Extras from hand-run stages the package regenerate does not rerun
        # (names with arguments; ghidra records none, its export is an input)
        # are cleared by that regenerate, so the next package run is a no-op.
        pipeline.begin_stage("ghidra", ["-max-cpu", "2"])
        pipeline.begin_stage("names", ["--min-len", "3"])
        pipeline.write_gen_key()
        assert pipeline.gen_stale_reasons() == ["a stage ran with extra arguments"]
        for n in ("parse", "disasm", "funcid", "abi"):
            monkeypatch.setattr(pipeline, "stage_" + n, lambda n=n: pipeline.begin_stage(n))
        monkeypatch.setattr(pipeline, "maybe_names", lambda: pipeline.begin_stage("names"))
        monkeypatch.setattr(
            pipeline,
            "stage_recomp",
            lambda: (pipeline.begin_stage("recomp"), pipeline.write_gen_key()),
        )
        pkg.run_plan_prefix(A(), [("generate", "stale")])
        monkeypatch.undo()
        assert pipeline.gen_stale_reasons() == []
        open(G.regen_marker, "w").close()
        assert pipeline.gen_stale_reasons() == ["the last recomp did not finish"]


def test_recomp_verbose_not_hashed(d, monkeypatch):
    """recomp runs with -v (progress lines for the view), but the key's argv
    leaves it out, so the flag never stales gen/; the game name is fixed."""
    with fake_tree(d):
        G = host.g()
        write(os.path.join(G.out, "abi", "abi_functions.json"), "{}")
        ran = []
        monkeypatch.setattr(pipeline, "run_cmds", lambda cmds: ran.extend(cmds))
        pipeline.stage_recomp()
        monkeypatch.undo()
        assert ran and ran[0][2][-1] == "-v", ran
        assert json.loads(pipeline.stage_argv())[-1][2][-1] != "-v"
        assert pipeline.gen_stale_reasons() == []
        assert G.m["pipeline"]["game_name"] == "cat"


def test_exclude_pin_mark(d):
    """The pin mark stays out of `git status` in a plain clone and in a
    worktree, whose exclude file is under the main repo's .git."""
    import subprocess as sp

    def git(*args, cwd):
        return sp.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    main_ = os.path.join(d, "tk")
    os.makedirs(main_)
    git("init", "-q", cwd=main_)
    write(os.path.join(main_, "f"), "x")
    git("add", "f", cwd=main_)
    git("commit", "-qm", "f", cwd=main_)
    wt = os.path.join(d, "tk-wt")
    git("worktree", "add", "-q", wt, cwd=main_)
    for tk in (wt, main_):  # the worktree first: the exclude file is shared
        write(os.path.join(tk, toolkit.PIN_MARK), "pin")
        if tk == wt:
            assert git("status", "--porcelain", cwd=tk).strip(), tk
        toolkit.exclude_pin_mark(tk)
        toolkit.exclude_pin_mark(tk)  # once only
        assert git("status", "--porcelain", cwd=tk) == "", tk
    with open(os.path.join(main_, ".git", "info", "exclude")) as f:
        assert f.read().count(toolkit.PIN_MARK) == 1


def test_legacy_pin_mark_renamed(d):
    """A toolkit clone the game's old CLI made (.blinx2-pin) keeps counting
    as the pinned clone: the mark is renamed, not lost."""
    tk = os.path.join(d, "tk")
    write(os.path.join(tk, ".blinx2-pin"), "abc\n")
    toolkit.rename_legacy_mark(tk)
    assert not os.path.exists(os.path.join(tk, ".blinx2-pin"))
    with open(os.path.join(tk, toolkit.PIN_MARK)) as f:
        assert f.read() == "abc\n"


class A:
    def __init__(self, **kw):
        self.__dict__.update(
            dict(
                target="macos",
                no_setup=False,
                no_build=False,
                system_tools=False,
                reconfigure=False,
            )
        )
        self.__dict__.update(kw)


def test_package_plan(d, monkeypatch):
    with fake_tree(d):
        monkeypatch.setattr(setup, "setup_needs", lambda t, s=False: [])
        pipeline.write_gen_key()
        assert [n for n, _ in pkg.package_plan(A())] == ["build", "package"]
        assert (
            pkg.plan_line(pkg.package_plan(A(target="windows")))
            == "plan: build (build-pkg-win), package (windows)"
        )
        write(host.g().seeds[0], "[2]")
        plan = pkg.package_plan(A())
        assert plan[0] == ("generate", "config/seed_functions.json changed"), plan
        assert [n for n, _ in pkg.package_plan(A(no_build=True))] == ["package"]
        monkeypatch.setattr(setup, "setup_needs", lambda t, s=False: ["no llvm-mingw"])
        plan = pkg.package_plan(A(target="steamos"))
        assert plan[0] == ("setup", "no llvm-mingw") and plan[1][0] == "generate"
        assert pkg.package_plan(A(no_setup=True))[0][0] == "generate"


def test_setup_needs(d, monkeypatch):
    with fake_tree(d) as (root, tk):
        os.environ["XBOXRECOMP_PYTHON"] = sys.executable
        os.environ["LLVM_MINGW_ROOT"] = os.path.join(d, "mingw")
        path = os.environ.get("PATH", "")
        fake_uv_on_path(d, (0, 12, 21), monkeypatch)
        assert setup.setup_needs("macos") == ["no cmake", "no ninja"]
        # No uv: named only when setup would need it for the venv.
        fake_uv_on_path(os.path.join(d, "nouv"), None, monkeypatch)
        assert setup.setup_needs("macos") == ["no cmake", "no ninja", "no uv"]
        assert setup.setup_needs("macos", True) == []
        shutil.rmtree(os.path.join(tk, "tools"))
        assert setup.setup_needs("macos", True) == ["no toolkit"]
        # The windows and steamos targets need llvm-mingw as well.
        os.environ.pop("LLVM_MINGW_ROOT")
        monkeypatch.setenv("PATH", os.path.join(d, "empty"))
        assert setup.setup_needs("steamos", True) == ["no toolkit", "no llvm-mingw"]
        monkeypatch.setenv("PATH", path)


def fake_cmake(d):
    """A cmake that logs its argv and, on --build, creates the exe."""
    log = os.path.join(d, "cmake.log")
    path = os.path.join(d, "fakecmake.py")
    write(
        path,
        "import json, os, sys\n"
        "with open(%r, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "a = sys.argv[1:]\n"
        "if a[0] == '--build':\n"
        "    open(os.path.join(a[1], 'cat_recomp'), 'w').close()\n"
        "else:\n"
        "    b = a[a.index('-B') + 1]\n"
        "    os.makedirs(b, exist_ok=True)\n"
        "    open(os.path.join(b, 'CMakeCache.txt'), 'a').close()\n" % log,
    )
    return path, log


def test_packaging_build_dir(d, monkeypatch):
    with fake_tree(d) as (root, tk):
        write(os.path.join(tk, "CMakeLists.txt"), "")
        # A developer's build/ with enhancements off stays as it is.
        dev_cache = os.path.join(root, "build", "CMakeCache.txt")
        write(dev_cache, "XBOXRECOMP_ENHANCE:BOOL=OFF\n")
        script, log = fake_cmake(d)
        real_run = host.run
        calls = []
        monkeypatch.setattr(build, "build_tools", lambda s, o=None: ("CMAKE", None))

        def run(cmd, cwd=None, env=None, check=True, parser=None):
            calls.append(cmd)
            return real_run([sys.executable, script] + list(cmd[1:]), cwd=cwd, env=env)

        monkeypatch.setattr(host, "run", run)
        bdir = build.pkg_build_dir("macos")
        for _ in range(2):
            exe = build.build("macos", (), False, bdir=bdir, stock=True)
        assert exe == os.path.join(root, "build-pkg-macos", "cat_recomp")
        monkeypatch.undo()
    configures = [c for c in calls if c[1] == "-S"]
    assert len(configures) == 2, calls  # configured on every run
    for c in configures:
        assert c[c.index("-B") + 1] == bdir
        for opt in ("-DCMAKE_BUILD_TYPE=Release", "-DXBOXRECOMP_ENHANCE=ON", "-UCAT_GEN_OPT"):
            assert opt in c, (opt, c)
    dev = os.path.join(root, "build")
    assert not any(str(x) == dev or str(x).startswith(dev + os.sep) for c in calls for x in c), (
        calls
    )
    with open(dev_cache) as f:
        assert f.read() == "XBOXRECOMP_ENHANCE:BOOL=OFF\n"


def test_stock_refusal_text():
    msg = pkg.stock_refusal(
        "macos",
        os.path.join(host.g().root, "build-pkg-macos"),
        ["XBOXRECOMP_ENHANCE=OFF (want ON)"],
    )
    assert msg.startswith("build-pkg-macos is not a stock build: XBOXRECOMP_ENHANCE=OFF (want ON)")
    assert "package macos --reconfigure" in msg and "delete build-pkg-macos/" in msg
