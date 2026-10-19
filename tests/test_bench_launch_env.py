"""bench run, pacing and all start the game from the Windows package's
launch.env.default, as the installed launchers do, with BENCH_ENV (and
what a caller adds) winning; golden's env stays what it was, so its
verdicts compare with the recorded ones. The bench cases run the CLI
against test_bench_cli's scratch tree and fake ssh.

  uv run pytest tests/test_bench_launch_env.py
"""

import os
import re
import subprocess

import pytest
from test_bench_cli import side as bench_side
from test_bench_cli import split_script, values, with_tree

from xboxrecomp_cli.bench import launch_defaults, read_launch_env, with_defaults
from xboxrecomp_cli.bench.remote import BenchError
from xboxrecomp_cli.package import launch_env_path

DEFAULTS = (
    "# Launcher defaults.\n"
    "RECOMP_PB_BACKEND=d3d11\n"
    "\n"
    "RECOMP_AC97_READY=1\r\n"
    "LOG_KEEP=10\n"
    "UMU_RUN=/opt/umu-run\n"
    "RECOMP_TRACE=boot\n"
    "PROTONPATH=GE-Proton\n"
    "RECOMP_NOTE=two words\n"
)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        f.write(text)


def bash_env(text):
    """GAME_ENV=(text) as the host's bash reads it."""
    out = subprocess.run(
        ["bash", "-c", 'GAME_ENV=(%s)\nprintf "%%s\\n" "${GAME_ENV[@]}"' % text],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def test_read_launch_env(tmp_path):
    p = tmp_path / "launch.env.default.windows"
    p.write_text(DEFAULTS, newline="")
    assert read_launch_env(str(p)) == [
        ("RECOMP_PB_BACKEND", "d3d11"),
        ("RECOMP_AC97_READY", "1"),
        ("LOG_KEEP", "10"),
        ("UMU_RUN", "/opt/umu-run"),
        ("RECOMP_TRACE", "boot"),
        ("PROTONPATH", "GE-Proton"),
        ("RECOMP_NOTE", "two words"),
    ]
    assert read_launch_env(str(tmp_path / "missing")) is None
    p.write_text("RECOMP_PB_BACKEND=d3d11\nnot a line\n")
    with pytest.raises(BenchError, match=r":2: not a KEY=value line: not a line"):
        read_launch_env(str(p))
    # As strict as launch.sh: an indented line, comment or not, and a line
    # of blanks fail there, so they fail here.
    for bad in ("  # an indented comment", "\tA=1", "   "):
        p.write_text("A=1\n%s\n" % bad, newline="")
        with pytest.raises(BenchError, match=r":2: not a KEY=value line"):
            read_launch_env(str(p))
    # Only \n ends a line (and a \r before it goes): a form feed or U+2028
    # stays in the value, as bash's read keeps it.
    p.write_text("A=x\x0cy\u2028z\r\nB=2", newline="", encoding="utf-8")
    assert read_launch_env(str(p)) == [("A", "x\x0cy\u2028z"), ("B", "2")]


def test_with_defaults_explicit_wins():
    kvs = [("RECOMP_PB_BACKEND", "d3d11"), ("RECOMP_TRACE", "boot"), ("RECOMP_NOTE", "a b")]
    # The explicit keys replace the defaults outright, list keys included.
    got = bash_env(with_defaults(kvs, "RECOMP_PB_BACKEND=cpu RECOMP_TRACE=flip"))
    assert got == ["RECOMP_NOTE=a b", "RECOMP_PB_BACKEND=cpu", "RECOMP_TRACE=flip"]
    assert bash_env(with_defaults(kvs, "")) == [
        "RECOMP_PB_BACKEND=d3d11",
        "RECOMP_TRACE=boot",
        "RECOMP_NOTE=a b",
    ]
    # Values the host's bash must not expand, split or choke on.
    odd = [("HOME_V", "$HOME"), ("CMD_V", "`id`"), ("QUOTE_V", "it's"), ("EQ_V", "x=y=z")]
    assert bash_env(with_defaults(odd, "")) == ["%s=%s" % kv for kv in odd]
    assert with_defaults(None, "A=1") == "A=1"
    assert with_defaults(None, "") == ""


def test_launch_defaults_is_the_packaged_file(tmp_path):
    """The file package windows and steamos stage, less the keys the bench
    sets itself and the launcher-only ones."""

    class G:
        content = str(tmp_path / "packaging")
        root = str(tmp_path)

    path, kvs = launch_defaults(G)
    assert path == launch_env_path("windows", G) and kvs is None
    write(path, DEFAULTS)
    path, kvs = launch_defaults(G)
    assert [k for k, _ in kvs] == [
        "RECOMP_PB_BACKEND",
        "RECOMP_AC97_READY",
        "RECOMP_TRACE",
        "RECOMP_NOTE",
    ]


def run_envs(calls):
    """GAME_ENV of each run_game.sh the bench shipped, as bash sees it."""
    out = []
    for kind, _argv, stdin in calls:
        if kind != "ssh" or "GAME_ENV=(" not in (stdin or ""):
            continue
        prefix, _body = split_script(stdin)
        m = re.search(r"^declare -a GAME_ENV=\((.*)\)$", values(prefix), re.M)
        out.append(re.findall(r'\[\d+\]="((?:[^"\\]|\\.)*)"', m.group(1)) if m else [])
    return out


def defaults_file(d):
    return os.path.join(d["tree"][0], "packaging", "launch.env.default.windows")


@with_tree
def test_run_and_all_start_from_the_defaults(d):
    rc, out, err, calls = bench_side(d, ["run"])
    assert run_envs(calls) == [[]]
    assert "no packaging/launch.env.default.windows" in err, err
    assert "RECOMP_PB_BACKEND is not set" in err, err
    write(defaults_file(d), DEFAULTS)
    env = {"BENCH_ENV": "RECOMP_TRACE=flip WINEDEBUG=+seh"}
    for argv in (["run"], ["all"]):
        rc, out, err, calls = bench_side(d, argv, env)
        assert run_envs(calls) == [
            [
                "RECOMP_PB_BACKEND=d3d11",
                "RECOMP_AC97_READY=1",
                "RECOMP_NOTE=two words",
                "RECOMP_TRACE=flip",
                "WINEDEBUG=+seh",
            ]
        ], argv
        assert "RECOMP_PB_BACKEND is not set" not in err, err
    rc, out, err, calls = bench_side(d, ["run"], {"BENCH_ENV": "RECOMP_PB_BACKEND=cpu"})
    assert run_envs(calls)[0][-1] == "RECOMP_PB_BACKEND=cpu"
    assert "RECOMP_PB_BACKEND=d3d11" not in run_envs(calls)[0]
    write(defaults_file(d), "LOG_KEEP=10\nRECOMP_AC97_READY=1\n")
    rc, out, err, calls = bench_side(d, ["run"])
    assert run_envs(calls) == [["RECOMP_AC97_READY=1"]]
    assert err.count("RECOMP_PB_BACKEND is not set") == 1, err


@with_tree
def test_pacing_starts_from_the_defaults(d):
    write(defaults_file(d), DEFAULTS)
    rc, out, err, calls = bench_side(d, ["pacing", "--runs", "1", "A=1", "B=2"])
    envs = run_envs(calls)
    assert len(envs) == 2, envs
    for e, arm in zip(envs, ("A=1", "B=2")):
        # The scenario pins the backend and a trace: those defaults go.
        assert e[:2] == ["RECOMP_AC97_READY=1", "RECOMP_NOTE=two words"], e
        assert not any(w.startswith(("LOG_KEEP=", "UMU_RUN=")) for w in e), e
        assert "RECOMP_TRACE=boot" not in e and e.count("RECOMP_PB_BACKEND=d3d11") == 1, e
        assert e[-1] == arm and "RECOMP_TRACE=flip,pacing=all" in e, e
    assert "RECOMP_PB_BACKEND is not set" not in err, err


def test_backend_warning_once_per_command(tmp_path, capsys):
    """pacing's runs share one Bench: the warning comes once, not per run."""
    from xboxrecomp_cli.bench import Bench

    class G:
        content = str(tmp_path / "packaging")
        root = str(tmp_path)

    class Cfg:
        game = G

    write(launch_env_path("windows", G), "RECOMP_AC97_READY=1\n")
    b = Bench.__new__(Bench)
    b.cfg = Cfg
    assert bash_env(b.apply_defaults("A=1")) == ["RECOMP_AC97_READY=1", "A=1"]
    assert bash_env(b.apply_defaults("A=2")) == ["RECOMP_AC97_READY=1", "A=2"]
    assert capsys.readouterr().err.count("RECOMP_PB_BACKEND is not set") == 1


@with_tree
def test_golden_env_ignores_the_defaults(d):
    """golden pins its own env per scenario: the packaged defaults must not
    reach it, or its verdicts would stop comparing with the references."""
    argvs = (["golden"], ["integrate", "--golden", "--dirty"])
    before = [run_envs(bench_side(d, a)[3]) for a in argvs]
    assert all(before)
    write(defaults_file(d), DEFAULTS)
    assert [run_envs(bench_side(d, a)[3]) for a in argvs] == before
    assert not any("RECOMP_AC97_READY" in w for b in before for e in b for w in e)
