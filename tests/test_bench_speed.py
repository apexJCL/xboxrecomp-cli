"""bench golden's kept verdicts, --only and the tests skip key
(bench/golden.py, bench/tests_skip.py): the pure parts. The parity cases in
test_bench_cli.py run them through the fake host.

  uv run pytest tests/test_bench_speed.py
"""

import os
import shutil
import subprocess

import pytest

from xboxrecomp_cli.bench import golden as bg
from xboxrecomp_cli.bench import tests_skip as ts
from xboxrecomp_cli.bench.remote import BenchError

PLAN = "attract\t75\t2275\tA=1\nstage1\t105\t3070\tA=1\nstory\t100\t4480\tA=1\n"


def test_parse_args():
    assert bg.parse_args([]) == ("check", False, False, None)
    assert bg.parse_args(["--only", "story", "--tests"]) == ("check", False, True, ["story"])
    assert bg.parse_args(["--only=a,b", "--only", "c"])[3] == ["a", "b", "c"]
    assert bg.parse_args(["--record", "--force"])[:2] == ("record", True)
    for bad in (["--only"], ["--only="], ["--bogus"]):
        with pytest.raises(BenchError):
            bg.parse_args(bad)


def test_select_rows():
    assert len(bg.select_rows(PLAN, None)) == 3
    # Plan order, whatever order --only names them in.
    rows = bg.select_rows(PLAN, ["story", "attract"])
    assert [r.split("\t")[0] for r in rows] == ["attract", "story"]
    with pytest.raises(BenchError, match="no scenario stroy"):
        bg.select_rows(PLAN, ["stroy"])


def test_verdict_word_hardest_first():
    assert bg.verdict_word(0, 0, 0) == "pass"
    assert bg.verdict_word(1, 1, 1) == "FAIL-RUN"
    assert bg.verdict_word(3, 1, 0) == "FAIL-PRESENT"
    assert bg.verdict_word(3, 0, 1) == "REGRESSION"
    assert bg.verdict_word(3, 0, 2) == "INCOMPLETE"
    assert bg.verdict_word(3, 0, 0) == "INCONCLUSIVE"
    # check's exit 2 is named by what its output shows, worst first.
    newview = "NEWVIEW  story/story-hub (dump 72): matches no known view\ngolden: INCOMPLETE (1)\n"
    missing = "MISSING  story/story-hub (dump 72): x not dumped\n"
    incomplete = "INCOMPLETE story/story-menu (dump 58): the anchored flip is not checkable\n"
    assert bg.verdict_word(0, 0, 2, newview) == "NEWVIEW"
    assert bg.verdict_word(0, 0, 2, newview + missing) == "MISSING"
    assert bg.verdict_word(0, 0, 2, newview + missing + incomplete) == "INCOMPLETE"
    assert bg.verdict_word(0, 0, 2) == "INCOMPLETE"


def test_session_line():
    line = bg.session_line("integrate", "skip", {"attract": ("20261009-100000", "pass")}, 0, now=0)
    f = line.rstrip("\n").split("\t")
    assert f[1:] == ["integrate", "tests=skip", "only=all", "attract=20261009-100000:pass", "rc=0"]
    line = bg.session_line("golden", "pass", {}, 2, only=["story", "stage1"], now=0)
    assert line.split("\t")[3] == "only=story,stage1", line


PARTS = {
    "toolkit": "aa",
    "toolchain": "bb",
    "cmake": "cc",
    "game_src": "ee",
    "proton": "1789520217 GE-Proton11-7",
    "prefix": "none",
    "cli": "161f48a",
    "script": "dd",
    "llvm_mingw": "20260922",
}


def test_key_changes_with_every_part():
    k = ts.key_of(PARTS)
    assert k and len(k) == 64
    for p in PARTS:
        assert ts.key_of(dict(PARTS, **{p: PARTS[p] + "x"})) != k, p
        # Missing or unreadable: no key, so never skipped.
        assert ts.key_of(dict(PARTS, **{p: "unknown"})) is None, p
        assert ts.key_of({q: v for q, v in PARTS.items() if q != p}) is None, p


def test_parse_host_parts():
    text = "toolkit: aa\nproton: unknown\nnoise\nprefix: 1 GE\nother: x\n"
    assert ts.parse_host_parts(text) == {"toolkit": "aa", "proton": "unknown", "prefix": "1 GE"}


def test_record_round_trip(tmp_path):
    a = ts.record_path(str(tmp_path), "benchhost", "~/xbox-recomp/cat")
    b = ts.record_path(str(tmp_path), "benchhost", "~/xbox-recomp-x/cat")
    assert a != b and a.startswith(os.path.join(str(tmp_path), "bench-logs", "tests-pass"))
    assert ts.read_record(a) == (None, None)
    assert ts.skip_reason(ts.key_of(PARTS), ts.read_record(a)) is None
    ts.write_record(a, ts.key_of(PARTS), PARTS, "benchhost", "~/xbox-recomp/cat", now=0)
    rec = ts.read_record(a)
    assert rec[0] == ts.key_of(PARTS)
    assert "unchanged since the pass at" in ts.skip_reason(ts.key_of(PARTS), rec)
    assert ts.skip_reason(ts.key_of(dict(PARTS, toolkit="ab")), rec) is None
    assert ts.skip_reason(None, rec) is None
    assert "proton: 1789520217 GE-Proton11-7\n" in open(a).read()


def git(d, *a):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", d] + list(a),
        check=True,
        capture_output=True,
    )


def test_cli_part_sees_uncommitted_and_untracked(tmp_path):
    d = str(tmp_path)
    git(d, "init", "-q")
    (tmp_path / "a.py").write_text("x\n")
    git(d, "add", "a.py")
    git(d, "commit", "-qm", "a")
    clean = ts.cli_part(d)
    assert len(clean) == 40, clean
    (tmp_path / "new.sh").write_text("echo 1\n")
    untracked = ts.cli_part(d)
    assert untracked.startswith(clean + "+"), untracked
    (tmp_path / "new.sh").write_text("echo 2\n")
    assert ts.cli_part(d) not in (clean, untracked)
    os.remove(tmp_path / "new.sh")
    (tmp_path / "a.py").write_text("y\n")
    assert ts.cli_part(d).startswith(clean + "+")
    assert ts.cli_part(str(tmp_path / "nogit")) == "unknown"


HOST = os.path.join(os.path.dirname(os.path.abspath(bg.__file__)), "host")


@pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("sha256sum"), reason="bash, sha256sum"
)
def test_tests_key_script_reads_the_game_sources(tmp_path):
    """tests_key.sh against a scratch host layout: the env header's dir and
    cmake/ go into game_src; a named dir that is gone is unknown."""
    from xboxrecomp_cli.bench.remote import host_text

    b = tmp_path / "b"
    (b / "xboxrecomp").mkdir(parents=True)
    (b / "xboxrecomp" / "a.c").write_text("x\n")
    (b / "cat" / "src" / "env").mkdir(parents=True)
    (b / "cat" / "src" / "env" / "g.h").write_text("A\n")
    (b / "cat" / "CMakeLists.txt").write_text("c\n")

    def parts(env_dir):
        pro = "set -euo pipefail\nBENCH_DIR=%s\nREMOTE_GAME=%s\nTOOLCHAIN=/nope\n" % (b, b / "cat")
        pro += "BENCH_PREFIX=/nope\nPROTONPATH=GE-Proton\nENV_DIR=%s\n" % env_dir
        r = subprocess.run(
            ["bash", "-s"], input=pro + host_text("tests_key.sh"), capture_output=True, text=True
        )
        return ts.parse_host_parts(r.stdout)

    first = parts("src/env")
    assert len(first["game_src"]) == 64 and len(first["toolkit"]) == 64, first
    assert first["toolchain"] == "unknown" and first["prefix"] == "none", first
    (b / "cat" / "src" / "env" / "g.h").write_text("B\n")
    second = parts("src/env")
    assert second["game_src"] != first["game_src"] and second["toolkit"] == first["toolkit"]
    (b / "cat" / "cmake").mkdir()
    (b / "cat" / "cmake" / "x.cmake").write_text("set(A 1)\n")
    assert parts("src/env")["game_src"] != second["game_src"]
    assert parts("src/gone")["game_src"] == "unknown"
    os.remove(b / "cat" / "cmake" / "x.cmake")
    os.rmdir(b / "cat" / "cmake")
    assert parts("")["game_src"] == "none"
