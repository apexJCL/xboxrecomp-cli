"""scripts/audit-public.sh on throwaway repositories: a clean one passes,
and one planted hit per rule fails it. The planted strings are built from
pieces so this file does not trip the audit of this repository.

  uv run pytest tests/test_audit_public.py
"""

import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "audit-public.sh")
NOREPLY = "4037632+apexJCL@users.noreply.github.com"
# Stand-ins for the private patterns (a host name, a personal address).
HOSTNAME = "bench" + "box-7"
EMAIL = "some" + "one.private@"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("git"),
    reason="needs bash and git",
)


def git(repo, *args, email=NOREPLY):
    return subprocess.run(
        ["git", "-c", "user.name=carlos", "-c", "user.email=" + email, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def repo(d):
    """A repo with one clean pushed commit (origin/main) and the private
    patterns file in its git dir."""
    r = os.path.join(d, "r")
    os.makedirs(r)
    git(r, "init", "-q", "-b", "main")
    with open(os.path.join(r, "README.md"), "w") as f:
        f.write("# clean\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first")
    git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    with open(os.path.join(r, ".git", "info", "audit-private"), "w") as f:
        f.write("# private\n%s\n%s\n" % (HOSTNAME, EMAIL.replace(".", "\\.")))
    return r


def audit(r, *args, env=None):
    e = dict(os.environ)
    e.pop("XBR_AUDIT_PRIVATE", None)
    e.update(env or {})
    return subprocess.run(["bash", SCRIPT, *args], cwd=r, capture_output=True, text=True, env=e)


def commit(r, name, data, msg="change", email=NOREPLY):
    p = os.path.join(r, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    git(r, "add", "-A")
    git(r, "commit", "-qm", msg, email=email)


def test_clean(d):
    r = repo(d)
    commit(r, "src/a.py", "x = 1\n")
    p = audit(r)
    assert p.returncode == 0, p.stdout + p.stderr
    assert audit(r, "--all").returncode == 0


@pytest.mark.parametrize(
    "name, data, msg, email, rule",
    [
        ("a.py", "p = '/Us" + "ers/someone/x'\n", "c", NOREPLY, "text"),
        ("a.py", "p = '/ho" + "me/someone/x'\n", "c", NOREPLY, "text"),
        ("a.py", "p = '/var/ho" + "me/someone'\n", "c", NOREPLY, "text"),
        ("a.md", "see https://clau" + "de.ai/chat/x\n", "c", NOREPLY, "text"),
        ("a.md", "the %s host\n" % HOSTNAME.upper(), "c", NOREPLY, "text"),
        ("a.md", "mail %sexample.invalid\n" % EMAIL, "c", NOREPLY, "text"),
        ("a.txt", "ok\n", "from /Us" + "ers/me", NOREPLY, "message"),
        ("game_files/default.xbe", "XBEH", "c", NOREPLY, "game data"),
        ("shot.png", "png", "c", NOREPLY, "game data"),
        ("blob.bin", b"\0\1\2\0" * 64, "c", NOREPLY, "binary"),
        ("a.txt", "ok\n", "c", "someone@example.invalid", "identity"),
    ],
)
def test_each_rule_hits(d, name, data, msg, email, rule):
    r = repo(d)
    commit(r, name, data, msg, email)
    p = audit(r)
    assert p.returncode == 1, (rule, p.stdout, p.stderr)
    assert "HIT  " + rule in p.stdout, (rule, p.stdout)
    assert audit(r, "--all").returncode == 1, rule


def test_refuses_without_private_patterns(d):
    r = repo(d)
    os.remove(os.path.join(r, ".git", "info", "audit-private"))
    p = audit(r)
    assert p.returncode == 2 and "no private patterns" in p.stderr, p.stderr
    other = os.path.join(d, "patterns")
    with open(other, "w") as f:
        f.write(HOSTNAME + "\n")
    assert audit(r, env={"XBR_AUDIT_PRIVATE": other}).returncode == 0


def test_no_upstream_needs_all(d):
    r = repo(d)
    git(r, "update-ref", "-d", "refs/remotes/origin/main")
    p = audit(r)
    assert p.returncode == 2 and "use --all" in p.stderr, p.stderr
    assert audit(r, "--all").returncode == 0


def test_parity_record_names_only_fakehost():
    """The bench record moved from the game names no real host."""
    with open(os.path.join(HERE, "testdata", "bench_parity.json")) as f:
        text = f.read()
    import re

    hosts = set(re.findall(r'"(?:[\w.-]+@)?([\w.-]+):[~/]', text))
    assert hosts <= {"fakehost"}, hosts
