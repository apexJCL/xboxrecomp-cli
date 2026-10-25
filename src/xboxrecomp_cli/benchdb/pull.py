"""benchdb pull (D7.6): the records a host wrote to its inbox while the db
was away (a Windows build on the PC), brought to the db and imported.

One ssh command lists the inbox with each file's sha256 and its bytes in
base64 (records are small), the copies land in <store>/inbox/<host>/,
one transaction imports them, and only after the commit a second command
deletes on the host the files whose name and sha256 were imported. It
does not go through `clean` or the run lock: the inbox is the db's own
queue, and a pull must not wait behind another agent's run. Two pulls at
once are safe: the uuid makes the second import a no-op, and a file
already gone is skipped."""

import base64
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess

from . import ingest, store
from .store import DbError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TIMEOUT = 120


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def ps_command(script):
    """cmd.exe (the PC's ssh shell) runs this; -EncodedCommand keeps the
    script's quotes and $ away from it."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return "powershell -NoProfile -NonInteractive -EncodedCommand %s" % enc


def list_script(h):
    """The host command that prints one 'name sha256 base64' line per
    record in the inbox. Temp files start with '.', so they never match."""
    if h.os == "windows":
        return ps_command(
            "$ErrorActionPreference='Stop'; $d=%s; "
            "if (Test-Path -LiteralPath $d) { "
            "Get-ChildItem -LiteralPath $d -Filter *.json -File | "
            "Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object { "
            "$b=[IO.File]::ReadAllBytes($_.FullName); "
            "$s=(Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLower(); "
            "[Console]::Out.Write($_.Name + ' ' + $s + ' ' + [Convert]::ToBase64String($b) + "
            '"`n") } }' % ps_quote(h.inbox)
        )
    return (
        'd=%s; [ -d "$d" ] || exit 0; cd "$d" || exit 1; '
        'for f in *.json; do [ -f "$f" ] || continue; '
        'printf "%%s %%s " "$f" "$(sha256sum < "$f" | cut -c1-64)"; '
        "base64 < \"$f\" | tr -d '\\n'; echo; done" % sh_quote(h.inbox)
    )


def delete_script(h, done):
    """The host command that removes each (name, sha256) whose file still
    has that sha256: a record rewritten since the list stays."""
    if h.os == "windows":
        # Hashtables, not nested @(): PowerShell unrolls @(@('a','b')) into
        # its strings, and $i[0] would be one character of a name.
        items = "; ".join(
            "@{n=%s; s=%s}" % (ps_quote(n), ps_quote(s)) for n, s in sorted(done.items())
        )
        return ps_command(
            "$d=%s; foreach ($i in @(%s)) { $p=Join-Path $d $i.n; "
            "if ((Test-Path -LiteralPath $p) -and "
            "(Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLower() -eq $i.s) "
            "{ Remove-Item -LiteralPath $p } }" % (ps_quote(h.inbox), items)
        )
    lines = ["cd %s || exit 0" % sh_quote(h.inbox)]
    for n, s in sorted(done.items()):
        lines.append(
            '[ -f %s ] && [ "$(sha256sum < %s | cut -c1-64)" = %s ] && rm -f -- %s'
            % (sh_quote(n), sh_quote(n), s, sh_quote(n))
        )
    return "; ".join(lines) + "; true"


def ssh(h, command):
    """(rc, stdout) of command on the host; a module function so the
    tests can put a fake in its place."""
    s = shutil.which("ssh")
    if not s:
        raise DbError("no ssh on PATH")
    r = subprocess.run(
        [s, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", h.ssh, command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT,
    )
    return r.returncode, r.stdout.decode(errors="replace")


def parse_listing(text):
    """[(name, sha256, bytes)] of the list command's output; a line that
    does not parse or whose bytes do not hash to its sha256 is left out."""
    out, bad = [], []
    for line in text.splitlines():
        parts = line.strip().split(" ")
        if len(parts) != 3 or not NAME_RE.match(parts[0]) or not SHA_RE.match(parts[1]):
            if line.strip():
                bad.append(line.strip()[:60])
            continue
        try:
            data = base64.b64decode(parts[2], validate=True)
        except ValueError:
            bad.append(parts[0])
            continue
        if hashlib.sha256(data).hexdigest() != parts[1]:
            bad.append(parts[0])
            continue
        out.append((parts[0], parts[1], data))
    return out, bad


def store_dir(cfg):
    for r in cfg.roots.values():
        if r.local and r.kind == "store":
            return r.path
    raise DbError("benchdb.toml has no local store root for the inbox copies")


def hosts_to_pull(cfg, key=None):
    if key:
        h = cfg.hosts.get(key)
        if h is None:
            raise DbError("no host %s in benchdb.toml" % key)
        if not h.inbox:
            raise DbError("host %s has no inbox in benchdb.toml" % key)
        return [h]
    return [h for _k, h in sorted(cfg.hosts.items()) if h.inbox]


def pull(cfg, key=None, say=print, db=None):
    """Pulls every host's inbox (or key's). A host that does not answer is
    skipped with a line: it is down, or up on the other OS. Returns
    {host: (imported, failed)}."""
    db = db or store.open(cfg=cfg)
    base = store_dir(cfg)
    result = {}
    for h in hosts_to_pull(cfg, key):
        try:
            rc, text = ssh(h, list_script(h))
        except (OSError, subprocess.SubprocessError, DbError) as e:
            say("pull: %s: not reached (%s)" % (h.key, e))
            continue
        if rc != 0:
            say("pull: %s: not reached (ssh exit %d)" % (h.key, rc))
            continue
        files, bad = parse_listing(text)
        for b in bad:
            say("pull: %s: skipped %s (unreadable or its sha256 does not match)" % (h.key, b))
        if not files:
            result[h.key] = (0, 0)
            continue
        d = os.path.join(base, ingest.INBOX, h.key)
        os.makedirs(d, exist_ok=True)
        local = []
        for name, sha, data in files:
            p = os.path.join(d, name)
            tmp = os.path.join(d, ".%s.tmp" % name)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            local.append((name, sha, p))
        done, failed = {}, 0
        with db.write() as w:
            for name, sha, p in local:
                # One savepoint per record: a record that fails after some
                # inserts leaves none of its rows, and the rest still land.
                w.conn.execute("SAVEPOINT rec")
                try:
                    ingest.ingest_record(w, cfg, p)
                except (DbError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as e:
                    w.conn.execute("ROLLBACK TO rec")
                    w.conn.execute("RELEASE rec")
                    failed += 1
                    say("pull: %s: %s not imported (%s)" % (h.key, name, e))
                    continue
                w.conn.execute("RELEASE rec")
                done[name] = sha
        # After the commit: what is in the db goes from the host.
        if done:
            try:
                rc, _ = ssh(h, delete_script(h, done))
                if rc != 0:
                    say("pull: %s: the delete exited %d; the next pull retries" % (h.key, rc))
            except (OSError, subprocess.SubprocessError) as e:
                say("pull: %s: the delete failed (%s); the next pull retries" % (h.key, e))
        say("pull: %s: %d imported, %d failed" % (h.key, len(done), failed))
        result[h.key] = (len(done), failed)
    return result


def pull_quietly(cwd, alias=None, say=print):
    """integrate's and golden's pull at the start: for the host they run
    on, when this machine has the db and that host an inbox. Warn-only."""
    from . import config, live

    try:
        cfg = config.load(cwd=cwd)
        if not cfg.present or not cfg.db:
            return None
        key = live.host_of(cfg, alias) if alias else None
        if alias and (key is None or not cfg.hosts[key].inbox):
            return None
        return pull(cfg, key, say=say)
    except Exception as e:  # noqa: BLE001 (a pull never fails the bench)
        live.warn("the inbox pull", e)
        return None
