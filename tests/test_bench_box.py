"""The bench's distrobox check (bench/host/box_check.sh.in, doctor.sh): a box
whose /etc/resolv.conf bind mount points at a deleted file (link count 0)
is restarted before the tests enter it, and doctor reports it. Runs the
generated commands in a local bash against a fake distrobox and flock.

  uv run pytest tests/test_bench_box.py
"""

import os
import shutil
import subprocess
import sys

import pytest

from xboxrecomp_cli.bench.remote import Remote, host_text

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash"), reason="bash host scripts"
)

# Records each call, marked [fd9] when the run lock's fd 9 reached it. stat
# reads its stdin to EOF (a stat that got ssh's stdin would swallow the
# script meant for bash -s), then prints the next word of FAKE_LINKS (one
# per stat; "-" prints nothing, "_" a line that is not a count) after
# FAKE_NOISE; stop exits FAKE_STOP_RC; `enter -- bash -s` runs the script.
FAKE_DISTROBOX = r"""#!/bin/bash
fd=; [ -e /dev/fd/9 ] && fd=" [fd9]"
echo "distrobox $*$fd" >> "$FAKE_LOG"
case "$*" in
    list*) printf 'ID | NAME | STATUS | IMAGE\nabc | xbr-build | %s | fedora:42\n' "$FAKE_STATUS" ;;
    *" stat "*)
        cat >/dev/null
        n=$(cat "$FAKE_LOG.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_LOG.n"
        set -- $FAKE_LINKS; shift "$n"; w=${1:-${@: -1}}
        [ -n "$FAKE_NOISE" ] && printf '%s\n' "$FAKE_NOISE"
        case "$w" in -) ;; _) echo "Starting container 2" ;; *) echo "$w" ;; esac ;;
    stop*) exit "${FAKE_STOP_RC:-0}" ;;
    *" bash -s"*) exec bash -s ;;
esac
"""
FAKE_FLOCK = """#!/bin/bash
fd=; [ -e /dev/fd/9 ] && fd=" [fd9]"
echo "flock $*$fd" >> "$FAKE_LOG"
"""


@pytest.fixture
def host(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("distrobox", FAKE_DISTROBOX), ("flock", FAKE_FLOCK)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    log = tmp_path / "log"

    def run(script, stdin, links="1", status="Up 2 hours", noise="", stop_rc="0"):
        log.write_text("")
        if os.path.exists(str(log) + ".n"):
            os.remove(str(log) + ".n")
        env = dict(
            os.environ,
            PATH="%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]),
            HOME=str(tmp_path),
            FAKE_LOG=str(log),
            FAKE_LINKS=links,
            FAKE_NOISE=noise,
            FAKE_STATUS=status,
            FAKE_STOP_RC=stop_rc,
        )
        r = subprocess.run(
            ["bash", "-c", script], input=stdin, env=env, capture_output=True, text=True
        )
        calls = log.read_text().splitlines()
        # The lock fd reaches flock (so the marker works) and no box call.
        assert all(c.endswith("[fd9]") for c in calls if c.startswith("flock")), calls
        assert not any(c.endswith("[fd9]") for c in calls if c.startswith("distrobox")), calls
        return r, [c for c in calls if c.startswith("distrobox")]

    return run


def lock_cmd(exclusive, what=None):
    return Remote({"BENCH_BOX": "xbr-build"}).in_box_lock_cmd(exclusive, what)


def test_tests_restart_a_box_with_a_deleted_resolv_conf(host):
    r, calls = host(lock_cmd(True), "echo in-box\n", links="0 1")
    assert r.returncode == 0, r.stderr
    assert "still" not in r.stderr, r.stderr
    assert r.stdout == "in-box\n", "the script for bash -s did not reach the box"
    assert "deleted file (the host replaced it); restarting the box" in r.stderr
    assert calls == [
        "distrobox enter xbr-build -- stat -c %h /etc/resolv.conf",
        "distrobox stop -Y xbr-build",
        "distrobox enter xbr-build -- stat -c %h /etc/resolv.conf",
        "distrobox enter xbr-build -- bash -s",
    ], calls


def test_tests_warn_when_the_restart_did_not_help(host):
    r, calls = host(lock_cmd(True), "echo in-box\n", links="0 0")
    assert r.returncode == 0 and r.stdout == "in-box\n", r.stderr
    assert "still a deleted file after the restart; umu's bwrap will fail" in r.stderr
    assert [c.split(" --")[0] for c in calls] == [
        "distrobox enter xbr-build",
        "distrobox stop -Y xbr-build",
        "distrobox enter xbr-build",
        "distrobox enter xbr-build",
    ], calls


def test_tests_stop_failing_never_enters(host):
    r, calls = host(lock_cmd(True), "echo in-box\n", links="0", stop_rc="3")
    assert r.returncode == 1 and r.stdout == "", (r.returncode, r.stdout)
    assert calls[-1] == "distrobox stop -Y xbr-build", calls


def test_tests_read_only_a_line_of_digits(host):
    """Digits on another line (distrobox starting the box) are no count."""
    for links, noise in (("_", ""), ("-", "Starting container 0"), ("1", "box 0")):
        r, calls = host(lock_cmd(True), "echo in-box\n", links=links, noise=noise)
        assert r.returncode == 0 and r.stdout == "in-box\n", (links, r.stderr)
        assert not any("stop" in c for c in calls), (links, noise, calls)
    # A count after distrobox's own line is still read.
    r, calls = host(lock_cmd(True), "echo in-box\n", links="0 1", noise="Starting container")
    assert "distrobox stop -Y xbr-build" in calls and "still" not in r.stderr, calls


def test_setup_takes_the_shared_lock():
    cmd = lock_cmd(False, "setup")
    assert "flock -s -n 9" in cmd and "bench: setup waiting" in cmd and "stat" not in cmd


def test_tests_leave_a_current_box_alone(host):
    for links in ("1", "-", "_"):
        r, calls = host(lock_cmd(True), "echo in-box\n", links=links)
        assert r.returncode == 0 and r.stdout == "in-box\n", (links, r.stderr)
        assert "restarting" not in r.stderr
        assert not any("stop" in c for c in calls), calls


def test_builds_never_check_or_stop_the_box(host):
    """A shared holder may not stop the box: another build may be in it."""
    r, calls = host(lock_cmd(False), "echo in-box\n", links="0")
    assert r.returncode == 0 and r.stdout == "in-box\n", r.stderr
    assert calls == ["distrobox enter xbr-build -- bash -s"], calls


def doctor(host, **kw):
    pro = (
        "set -euo pipefail\nBENCH_BOX='xbr-build'\nSLUG=blinx2\nLLVM_MINGW_ROOT=/nope\n"
        "REMOTE_GAME=/nope\nEXE_REL=x.exe\n"
    )
    script = pro + host_text("doctor.sh")
    r, calls = host("bash -s", script, **kw)
    resolv = [x for x in r.stdout.splitlines() if x.startswith("resolv:")]
    return r, calls, resolv


def test_doctor_reports_a_stale_resolv_conf(host):
    r, calls, resolv = doctor(host, links="0")
    assert resolv == [
        "resolv:   STALE in xbr-build: the host replaced /etc/resolv.conf "
        "(blinx2 bench tests restarts the box)"
    ], r.stdout
    # The rest of the script still ran (stat had no stdin to swallow), and
    # doctor changed nothing.
    assert "built:" in r.stdout, r.stdout
    assert not any("stop" in c for c in calls), calls
    _, _, resolv = doctor(host, links="1")
    assert resolv == ["resolv:   ok"]
    # distrobox is asked for its list once.
    _, calls, _ = doctor(host, links="1")
    assert sum(c.startswith("distrobox list") for c in calls) == 1, calls


def test_doctor_does_not_start_a_stopped_box(host):
    r, calls, resolv = doctor(host, links="0", status="Exited (0) 1 hour ago")
    assert resolv == [] and "built:" in r.stdout, r.stdout
    assert not any("enter" in c for c in calls), calls


def test_doctor_says_when_it_cannot_tell(host):
    _, _, resolv = doctor(host, links="_")
    assert resolv == ["resolv:   unknown (stat /etc/resolv.conf in xbr-build failed)"]
