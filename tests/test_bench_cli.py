#!/usr/bin/env python3
"""Parity of `<game> bench` with what BLiNX 2's scripts/bench.sh did: the
CLI's bench runs against a scratch project (testdata/game's game.toml) and toolkit with a fake ssh and rsync first
on PATH, which record every call (argv, and the script on ssh's stdin) in
order. Each case is compared with bench.sh's record of the same case in
testdata/bench_parity.json: per call the argv, the host script shipped (by
its file under benchlib/host/), and its variable prologue once bash has
parsed it to values (printf %q and shell_quote may spell a value
differently); then stdout and the exit code. Paths under the scratch
directory, the host name, stamps, dates and commit shas are normalised;
so are a Python traceback's frames (the pacing case's report fails on the
fake host's missing log; the record names the game's old
scripts/pacing_stats.py lines) and the `cli:` provenance line's state.
Plain asserts; runs alone or under pytest. Needs bash and git; skipped on
Windows.

  uv run pytest tests/test_bench_cli.py

The record was made in BLiNX 2 (blinx2-recomp) from bench.sh's last full
version (git show 61ea933:scripts/bench.sh there), when the game held this
code; the move here added only the `cli:` provenance line to the cases
that print it. To record again from such a copy (in a game tree whose
scripts/ has golden.py, pacing_stats.py and package_lib.py, as bench.sh
called them), which also checks the CLI's bench against it case by case:

  BENCH_PARITY_REF=/path/to/old/bench.sh uv run python tests/test_bench_cli.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

from xboxrecomp_cli.bench import remote
from xboxrecomp_cli.bench.remote import fill, host_text
from xboxrecomp_cli.package import lib as package_lib

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
HOST = os.path.join(os.path.dirname(os.path.abspath(remote.__file__)), "host")
FIXTURE = os.path.join(HERE, "testdata", "bench_parity.json")
# Recording: the bench.sh to record (a full one, not the shim).
REF = os.environ.get("BENCH_PARITY_REF")

SKIP = None
if sys.platform == "win32":
    SKIP = "bash-based parity: not on Windows"
elif not shutil.which("bash") or not shutil.which("git"):
    SKIP = "needs bash and git"

# The fake reads its stdin to EOF before it exits, as a remote bash -s does:
# a controller that left ssh's stdin open would hang a case into its timeout.
FAKE_SSH = r"""#!/bin/bash
n=$(cat "$FAKE_REC/n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_REC/n"
f="$FAKE_REC/$(printf %04d "$n")-ssh"
printf '%s\0' "$@" > "$f.argv"
cat > "$f.stdin"
if grep -q 'sha256sum -- \*' "$f.stdin"; then echo "$FAKE_GEN_DIGEST"; fi
# A call whose argv or script matches FAKE_EXIT_RE exits FAKE_EXIT_RC: the
# host failing, or its lock timing out (75), at that step.
if [ -n "${FAKE_EXIT_RE:-}" ] && { tr '\0' ' ' < "$f.argv"; cat "$f.stdin"; } | grep -qE -- "$FAKE_EXIT_RE"; then
    exit "$FAKE_EXIT_RC"
fi
exit 0
"""
FAKE_RSYNC = r"""#!/bin/bash
n=$(cat "$FAKE_REC/n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_REC/n"
printf '%s\0' "$@" > "$FAKE_REC/$(printf %04d "$n")-rsync.argv"
exit 0
"""


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def git(d, *args):
    sh(
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "init.defaultBranch=main",
        *args,
        cwd=d,
    )


def scratch(d):
    """d/cat (a clean main with gen/ and the golden references), d/tk (a
    clean toolkit on posix-host/portability), d/bin (the fakes). The
    project is named cat, as the record's paths are."""
    cat, tk, bindir = (os.path.join(d, n) for n in ("cat", "tk", "bin"))
    for rel in (".gitignore", "game.toml", "analysis/golden/golden.json"):
        os.makedirs(os.path.dirname(os.path.join(cat, rel)), exist_ok=True)
        shutil.copy(os.path.join(GAME, rel), os.path.join(cat, rel))
    # No dump in the scratch: the known-dump check is off (an empty list),
    # as bench.sh had none; test_golden_refuses_unknown_dump covers it.
    toml = os.path.join(cat, "game.toml")
    with open(toml) as f:
        text = re.sub(r"(?ms)^sha256 = \[.*?\]", "sha256 = []", f.read(), count=1)
    with open(toml, "w") as f:
        f.write(text)
    git(cat, "init", "-q")
    git(cat, "add", "-A")
    git(cat, "commit", "-qm", "scratch")
    HEADS.append(head(cat))
    # Ignored, as in a real checkout.
    os.makedirs(os.path.join(cat, "scripts"))
    with open(os.path.join(cat, "scripts", "bench.env"), "w") as f:
        f.write(
            "# scratch\nBENCH_HOST=fakehost\nBENCH_DIR=~/bench test\n".replace(
                "~/bench test", "'~/bench-test'"
            )
        )
    gen = os.path.join(cat, "src", "recomp", "gen")
    os.makedirs(gen)
    for n in ("recomp_0001.c", "recomp_funcs.h"):
        with open(os.path.join(gen, n), "w") as f:
            f.write("/* %s */\n" % n)
    g = json.load(open(os.path.join(cat, "analysis", "golden", "golden.json")))
    frames = os.path.join(cat, "analysis", "golden", "frames")
    os.makedirs(frames)
    for sc in g["scenarios"].values():
        for fr in sc["frames"]:
            names = (
                ["%s.%s.png" % (fr["name"], r["label"]) for r in fr["references"]]
                if "references" in fr
                else [fr["name"] + ".png"]
            )
            for n in names:
                open(os.path.join(frames, n), "wb").close()
    os.makedirs(tk)
    with open(os.path.join(tk, "README"), "w") as f:
        f.write("toolkit\n")
    git(tk, "init", "-q", "-b", "posix-host/portability")
    git(tk, "add", "-A")
    git(tk, "commit", "-qm", "tk")
    HEADS.append(head(tk))
    os.makedirs(bindir)
    for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
    return cat, tk, bindir, package_lib.gen_digest(gen)[0]


def run_side(d, side, argv, extra_env=None):
    cat, tk, bindir, digest = d["tree"]
    rec = os.path.join(d["tmp"], "rec-%s-%d" % (side, d["n"]))
    os.makedirs(rec)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BENCH_", "XBOXRECOMP", "LLVM_MINGW", "PROTONPATH"))
    }
    env.update(
        PATH=bindir + os.pathsep + env["PATH"],
        BENCH_GAME_DIR=cat,
        XBOXRECOMP_DIR=tk,
        FAKE_REC=rec,
        FAKE_GEN_DIGEST=digest,
        LC_ALL="C",
    )
    env.update(extra_env or {})
    cmd = (
        ["bash", REF]
        if side == "sh"
        else [
            sys.executable,
            "-m",
            "xboxrecomp_cli.main",
            "--game",
            cat,
            "--prog",
            "blinx2",
            "bench",
        ]
    ) + argv
    p = subprocess.run(
        cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300
    )
    calls = []
    for n in sorted(x for x in os.listdir(rec) if x.endswith(".argv")):
        kind = n.split("-")[1].split(".")[0]
        argv_ = open(os.path.join(rec, n), "rb").read().decode().split("\0")[:-1]
        stdin = None
        if kind == "ssh":
            stdin = open(os.path.join(rec, n[: -len(".argv")] + ".stdin"), "rb").read().decode()
        calls.append((kind, argv_, stdin))
    return p.returncode, p.stdout, p.stderr, calls


STAMP = re.compile(r"\d{8}-\d{6}")
DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:\d\d)")
# Commit shas, full or short (the scratch trees are committed afresh each
# run); a word of digits only is a count, not a sha.
SHA = re.compile(r"\b(?=[0-9]*[a-f])[0-9a-f]{7,40}\b")
# The scratch trees' commits, which may abbreviate to digits only.
HEADS = []
HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def head(d):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True, check=True
    ).stdout.strip()


TRACEBACK = re.compile(r"^Traceback \(most recent call last\):\n(?:[ \t].*\n)+", re.M)
CLI_LINE = re.compile(r"^cli: .*$", re.M)


def norm_stdout(s, tmp):
    """stdout as norm() has it, with a traceback's frames (which file and
    line raised; the exception line stays) and the CLI checkout's state on
    its provenance line folded."""
    s = TRACEBACK.sub("Traceback (most recent call last):\n  ...\n", norm(s, tmp))
    return CLI_LINE.sub("cli: TREE", s)


def norm(s, tmp):
    """The parts of a run that change from run to run or machine to machine."""
    if s is None:
        return s
    for t in (os.path.realpath(tmp), tmp):
        s = s.replace(t, "TMP")
    host = socket.gethostname()
    for h in (host, host.split(".")[0]):
        if h:
            s = s.replace(h, "HOSTNAME")
    s = DATE.sub("DATE", STAMP.sub("STAMP", s))
    s = HEX.sub(lambda m: "SHA" if any(h.startswith(m.group()) for h in HEADS) else m.group(), s)
    return SHA.sub("SHA", s)


# setup_box.sh.in as the scratch config fills it (BENCH_IMAGE's default).
BODY_NAMES = {host_text(n): n for n in os.listdir(HOST) if n.endswith(".sh")}
BODY_NAMES[fill("setup_box.sh.in", BENCH_IMAGE="fedora:42")] = "setup_box.sh.in"
BODIES = sorted(BODY_NAMES, key=len, reverse=True)
# What a prologue may hold. The prefix runs in a local bash below, so any
# other line (a host script that drifted out of BODIES) must stop the test
# before it runs mkdir or distrobox here.
PROLOGUE_LINE = re.compile(r"^(set -euo pipefail|[A-Za-z_][A-Za-z0-9_]*=)")


def split_script(text):
    """(prologue and assignments, the host script body) of a shipped script."""
    for body in BODIES:
        i = text.rfind(body)
        if i >= 0 and i + len(body) == len(text):
            return text[:i], body
    return text, ""


def values(prefix):
    """bash's view of a prologue: every variable it sets, declare -p'd."""
    bad = [x for x in prefix.splitlines() if x and not PROLOGUE_LINE.match(x)]
    assert not bad, ("not a prologue line; not run", bad)
    names = sorted(set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", prefix, re.M)))
    if not names:
        return prefix
    script = prefix + "\ndeclare -p %s\n" % " ".join(names)
    script = script.replace("set -euo pipefail\n", "", 1)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout


def compact(res, tmp):
    """A run as the record keeps it: exit code, stdout, and per call its argv
    and, for ssh, the host script's name and the prologue before it."""
    rc, out, _err, calls = res
    cs = []
    for kind, argv, stdin in calls:
        c = {"kind": kind, "argv": [norm(x, tmp) for x in argv]}
        if kind == "ssh":
            prefix, body = split_script(stdin)
            if body:
                c["body"] = BODY_NAMES[body]
                c["prologue"] = norm(prefix, tmp)
            else:
                c["script"] = CLI_LINE.sub("cli: TREE", norm(stdin, tmp))
        cs.append(c)
    return {"rc": rc, "stdout": norm_stdout(out, tmp), "calls": cs}


def compare(what, want, got):
    assert got["rc"] == want["rc"], (what, "exit", want["rc"], got["rc"])
    kw, kg = [c["kind"] for c in want["calls"]], [c["kind"] for c in got["calls"]]
    assert kw == kg, (what, kw, kg)
    for i, (x, y) in enumerate(zip(want["calls"], got["calls"])):
        assert x["argv"] == y["argv"], (what, i, x["argv"], y["argv"])
        if x["kind"] != "ssh":
            continue
        assert x.get("body") == y.get("body"), (what, i, x.get("body"), y.get("body"))
        assert x.get("script") == y.get("script"), (what, i, "script")
        if x.get("prologue") != y.get("prologue"):
            assert values(x["prologue"]) == values(y["prologue"]), (what, i, x, y)
    assert got["stdout"] == want["stdout"], (
        what,
        "stdout",
        want["stdout"][-1500:],
        got["stdout"][-1500:],
    )


_record = None


def record():
    global _record
    if _record is None:
        _record = {}
        if os.path.isfile(FIXTURE):
            with open(FIXTURE, encoding="utf-8") as f:
                _record = json.load(f)
    return _record


def assert_parity(d, argv, extra_env=None):
    """blinx2 bench against bench.sh's record of this case (or, with
    BENCH_PARITY_REF, against that bench.sh, recording it). Returns the
    blinx2 run (exit code, stdout, stderr, calls), twice."""
    d["n"] += 1
    key = "%s#%d %s" % (d["test"], d["n"], " ".join(argv))
    b = run_side(d, "py", argv, extra_env)
    got = compact(b, d["tmp"])
    rec = record()
    if REF:
        rec[key] = compact(run_side(d, "sh", argv, extra_env), d["tmp"])
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
            f.write("\n")
    assert key in rec, "no record of %r in %s (record it: see the docstring)" % (key, FIXTURE)
    want = dict(rec[key], stdout=norm_stdout(rec[key]["stdout"], d["tmp"]))
    compare(key, want, got)
    return b, b


def with_tree(fn):
    def run():
        if SKIP:
            print("skip %s: %s" % (fn.__name__, SKIP))
            return
        with tempfile.TemporaryDirectory() as tmp:
            d = {"tmp": tmp, "n": 0, "tree": scratch(tmp), "test": fn.__name__}
            fn(d)

    run.__name__ = fn.__name__
    return run


@with_tree
def test_parity_sync_build_tests(d):
    assert_parity(d, ["sync"])
    assert_parity(d, ["sync", "--game-files"])
    assert_parity(d, ["build"])
    assert_parity(d, ["build", "-DX=1", "-DY=a b"])
    assert_parity(d, ["tests"])
    assert_parity(d, ["setup"])


@with_tree
def test_parity_run_logs_symbolize(d):
    assert_parity(d, ["run", "--arg", "two words"])
    assert_parity(
        d, ["run"], {"BENCH_ENV": "RECOMP_TRACE=flip WINEDEBUG=+seh", "BENCH_TIMEOUT": "30"}
    )
    assert_parity(d, ["run", "--kill-game"])
    assert_parity(d, ["symbolize"])
    assert_parity(d, ["symbolize", "20260101-000000"])
    assert_parity(d, ["logs"])


@with_tree
def test_parity_integrate_golden(d):
    a, _ = assert_parity(d, ["integrate", "--golden"])
    assert "gen: 2 files" in a[1], a[1]
    assert_parity(d, ["golden"])


@with_tree
def test_parity_pacing(d):
    assert_parity(d, ["pacing", "--runs", "1", "A=1", "B=2"])


@with_tree
def test_parity_host_failures(d):
    # The build's and the tests' lock commands are told apart by flock's
    # mode; the pacing hold by its unit name.
    for rc in ("75", "2"):
        a, _ = assert_parity(d, ["build"], {"FAKE_EXIT_RE": "flock -s", "FAKE_EXIT_RC": rc})
        assert a[0] != 0, (rc, a[0])
    for rc in ("75", "1"):
        a, _ = assert_parity(d, ["tests"], {"FAKE_EXIT_RE": "flock -x", "FAKE_EXIT_RC": rc})
        assert a[0] != 0, (rc, a[0])
    a, _ = assert_parity(
        d,
        ["pacing", "--runs", "1", "A=1", "B=2"],
        {"FAKE_EXIT_RE": "unit=recomp-pacing-hold", "FAKE_EXIT_RC": "75"},
    )
    assert a[0] != 0, a[0]


@with_tree
def test_parity_golden_record(d):
    # The fake host writes no logs, so every run "ends early": record
    # refuses without --force and records with a warning with it.
    a, _ = assert_parity(d, ["golden", "--record"])
    assert a[0] == 1 and "not recording" in a[1], a[1][-500:]
    a, _ = assert_parity(d, ["golden", "--record", "--force"])
    assert "recorded with --force" in a[1], a[1][-500:]


@with_tree
def test_parity_refusals(d):
    cat = d["tree"][0]
    # Not on main; then dirty; each refuses before anything is sent.
    git(cat, "checkout", "-qb", "side")
    a, _ = assert_parity(d, ["integrate"])
    assert a[0] == 1 and not a[3], a
    git(cat, "checkout", "-q", "main")
    with open(os.path.join(cat, "untracked.txt"), "w") as f:
        f.write("x\n")
    a, _ = assert_parity(d, ["integrate"])
    assert a[0] == 1 and "is dirty" in a[2] and not a[3], a
    os.remove(os.path.join(cat, "untracked.txt"))
    marker = os.path.join(cat, "src", "recomp", ".gen-regenerating")
    open(marker, "w").close()
    a, _ = assert_parity(d, ["sync"])
    assert a[0] == 1 and "mid-regeneration" in a[2], a
    os.remove(marker)
    a, _ = assert_parity(d, ["pacing", "--runs", "x"])
    assert a[0] == 1, a
    a, _ = assert_parity(d, ["golden", "--bogus"])
    assert a[0] == 1, a
    a, _ = assert_parity(d, ["build"], {"BENCH_HOST": ""})


def test_golden_refuses_unknown_dump(capsys):
    import hashlib

    from xboxrecomp_cli import manifest
    from xboxrecomp_cli.bench import BenchError
    from xboxrecomp_cli.bench.golden import check_dump

    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(GAME, "game.toml"), d)
        game = manifest.load(d)
        # No dump here: a warning, not a refusal (the host has its own).
        for xbe, ok in ((None, True), (b"other dump", False), (b"known", True)):
            if xbe is not None:
                os.makedirs(os.path.join(d, "game_files"), exist_ok=True)
                with open(game.xbe, "wb") as f:
                    f.write(xbe)
            game.m["xbe"]["sha256"].append(hashlib.sha256(b"known").hexdigest())
            try:
                check_dump(game)
                assert ok, xbe
            except BenchError as e:
                assert not ok, (xbe, e)
        assert "no game_files/default.xbe here, so the host's dump" in capsys.readouterr().err
        game.m["xbe"]["sha256"][:] = []
        with open(game.xbe, "wb") as f:
            f.write(b"other dump")
        check_dump(game)  # an empty list: no check


def test_golden_presets_checked():
    from xboxrecomp_cli import manifest
    from xboxrecomp_cli.bench import BenchError
    from xboxrecomp_cli.bench.golden import check_presets

    game = manifest.load(GAME)
    check_presets(game)
    game.m["input"]["presets"] = ["@attract"]
    try:
        check_presets(game)
        raise AssertionError("accepted @stage1")
    except BenchError as e:
        assert "stage1 (@stage1)" in str(e), e
    game.m["input"]["presets"] = []
    check_presets(game)


def test_bench_env_file():
    from xboxrecomp_cli.bench.config import ConfigError, read_env_file

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bench.env")
        with open(p, "w") as f:
            f.write("# c\n\nBENCH_HOST=me@box\nBENCH_DIR=~/x\nA='~/y z'\nB=\"p q\"\nC=a:~/b\n")
        got = read_env_file(p, home="/h")
        assert got == {
            "BENCH_HOST": "me@box",
            "BENCH_DIR": "/h/x",
            "A": "~/y z",
            "B": "p q",
            "C": "a:/h/b",
        }, got
        for bad in ("export X=1\n", "X=$HOME\n", "X=a b\n", 'X="$Y"\n', "just words\n"):
            with open(p, "w") as f:
                f.write("# ok\n" + bad)
            try:
                read_env_file(p, home="/h")
            except ConfigError as e:
                assert "bench.env:2:" in str(e), e
            else:
                raise AssertionError("accepted %r" % bad)


def test_host_path_warning():
    from xboxrecomp_cli.bench.config import Config

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "scripts"))
        shutil.copy(os.path.join(GAME, "game.toml"), d)
        os.makedirs(os.path.join(d, "tk"))
        home = os.path.expanduser("~")
        with open(os.path.join(d, "scripts", "bench.env"), "w") as f:
            f.write("BENCH_HOST=h\nBENCH_DIR=~/b\nLLVM_MINGW_ROOT='~/m'\n")
        env = {"XBOXRECOMP_DIR": os.path.join(d, "tk"), "HOME": home}
        w = Config(d, env).warnings()
        # BENCH_DIR, and BENCH_PREFIX which defaults under it; not the quoted one.
        assert [x.split("=")[0] for x in w] == ["BENCH_DIR", "BENCH_PREFIX"], w
        assert w[0].startswith("BENCH_DIR=%s/b is a path on this machine" % home), w


def test_sync_excludes_follow_gitignore():
    """Every directory .gitignore names is kept out of the project sync, or
    is synced on purpose: an untracked dist/ once sent a 5 GB bundle (with
    the game in it) to the host."""
    from xboxrecomp_cli import manifest
    from xboxrecomp_cli.bench.sync import game_excludes, synced_ignored

    game = manifest.load(GAME)
    GAME_EXCLUDES, SYNCED_IGNORED = game_excludes(game), synced_ignored(game)
    dirs = []
    with open(os.path.join(GAME, ".gitignore")) as f:
        for line in f:
            e = line.strip()
            if not e or e[0] in "#!" or "*" in e:
                continue
            base = e.rstrip("/").rsplit("/", 1)[-1].lstrip(".")
            if e.endswith("/") or "." not in base:
                dirs.append(e.strip("/"))
    assert "dist" in dirs and "game_files" in dirs, dirs
    ex = [x.strip("/") for x in GAME_EXCLUDES]
    anchored = [x.strip("/") for x in GAME_EXCLUDES if x.startswith("/")]

    def covered(e):
        if any(e == x or e.startswith(x + "/") for x in ex if x):
            return True
        # An unanchored, slash-free exclude matches that name at any depth.
        b = e.rsplit("/", 1)[-1]
        return any(x == b and x not in anchored and "/" not in x for x in ex)

    missing = [e for e in dirs if not covered(e) and e not in SYNCED_IGNORED]
    assert not missing, "gitignored but synced to the host: %s" % missing
    for pat in ("*.7z", "*.iso", "*.xiso", "*.zip", "/wt/"):
        assert pat in GAME_EXCLUDES, pat


def test_shell_quote_matches_printf_q():
    if SKIP and "bash" in SKIP:
        print("skip: no bash")
        return
    from xboxrecomp_cli.bench.remote import quote_words, shell_quote

    words = [
        "plain",
        "",
        "two words",
        "x=1",
        "~home",
        "a=~b",
        "a,b",
        "#c",
        "d#e",
        "$HOME",
        "it's",
        'say "hi"',
        "back\\slash",
        "semi;colon",
        "é",
        "*?[]",
        "a\tb",
    ]
    got = subprocess.run(
        ["bash", "-c", 'printf "%q\\n" "$@"', "_"] + words, capture_output=True, text=True
    ).stdout.splitlines()
    for w, want in zip(words, got):
        if "\t" in w:
            continue  # bash writes $'a\tb'; ours single-quotes it: same value
        assert shell_quote(w) == want, (w, shell_quote(w), want)
    assert quote_words([]) == ""
    assert quote_words(["a", "b c"]) == "a b\\ c "


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   %s" % name)
