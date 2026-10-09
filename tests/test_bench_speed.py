"""bench golden's kept verdicts, --only and the tests skip key
(bench/golden.py, bench/tests_skip.py): the pure parts. The parity cases in
test_bench_cli.py run them through the fake host.

  uv run pytest tests/test_bench_speed.py
"""

import os

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


def test_session_line():
    line = bg.session_line("integrate", "skip", {"attract": ("20261009-100000", "pass")}, 0, now=0)
    f = line.rstrip("\n").split("\t")
    assert f[1:] == ["integrate", "tests=skip", "attract=20261009-100000:pass", "rc=0"], f


PARTS = {
    "toolkit": "aa",
    "toolchain": "bb",
    "cmake": "cc",
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
