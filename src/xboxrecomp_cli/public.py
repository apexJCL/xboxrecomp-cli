"""`public`: a game's public repository (docs/layout.md, "Publishing").

  public init               make the [public] remote safe: one remote for
                            the public repo, no tags fetched, and in export
                            mode the push refspec and the pre-push guard
  public export [REF]       REF's tree, without the private set, rewritten
                            and checked, as one commit on the public tip in
                            refs/public/<branch> (and --tag TAG); prints
                            the push, never runs it
  public export --fresh-root OLD
                            an approved rewrite: no public parent, and a
                            one-time guard allowance to replace OLD
  public pr NAME [REF]      an upstream PR branch (a fork's pr/NAME):
                            upstream..REF audited per commit, pushed as is
  public audit [RANGE]      what a push would publish, checked for leaks,
                            data, identities, the layout and the pins
  public audit --all        export mode: a dry-run export of HEAD; history
                            mode: every tracked file and the whole history

Exit 1 on a hit, 2 when the check cannot run (no private patterns, no
network for the pins).

The private inputs never sit in the repo: the patterns (audit-private, one
extended regex per line, matched without case) come from
$XBR_PUBLIC_DIR/audit-private, then $XBR_AUDIT_PRIVATE, then
<git dir>/info/audit-private; the rewrite rules from $XBR_PUBLIC_DIR/rewrite,
else <git dir>/info/xbr-public/rewrite (required in export mode)."""

import datetime
import json
import os
import re
import subprocess
import sys
import time

from . import host, layout, manifest
from .host import CliError

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GUARD_MARK = layout.HOOK_MARK
# Checked on every added line, message, path and tag, whatever the private
# file says: home directories (macOS, Linux, an immutable host's /var/home,
# Windows) and links into Claude's web app, all without case (a lowercase
# path, an MSYS one under /c/, a Wine prefix under a real user's name, with
# either separator). Nobody's home: a Wine prefix's own steamuser and public
# users (drive_c/users/steamuser/, or C: and backslashes as Wine spells
# them, but only with the separator after the name) and GitHub's API path
# github.com/users/. scripts/audit-public.sh masks the same exemptions.
# Bracketed so this file does not match itself.
NOBODY = r"(?![\\/]*(?:steamuser|public)[\\/])"
BUILTIN = (
    (r"(?<!drive_c)(?<![A-Za-z]:)(?<!github\.com)/[U]sers/", re.I),
    (r"drive_c[\\/]+[U]sers[\\/]+" + NOBODY, re.I),
    (r"/[h]ome/", re.I),
    (r"/var/[h]ome/", re.I),
    (r"[A-Za-z]:[\\/]+[U]sers[\\/]+" + NOBODY, re.I),
    (r"[c]laude\.ai", re.I),
)


class Cannot(CliError):
    """The check cannot run: exit 2, as the audit scripts did."""


# ── git ──────────────────────────────────────────────────────────────────


class Git:
    def __init__(self, root):
        self.root = root

    def run(self, *args, input=None, env=None, check=True):
        r = subprocess.run(
            [
                "git",
                "-C",
                self.root,
                "-c",
                "core.quotePath=false",
                # A global attributes file is this user's, not the repo's:
                # its -diff or textconv must not change what the audit reads.
                "-c",
                "core.attributesFile=" + os.devnull,
                *args,
            ],
            input=input,
            capture_output=True,
            env=env,
        )
        if check and r.returncode != 0:
            raise CliError(
                "git %s: %s"
                % (args[0], r.stderr.decode("utf-8", "replace").strip() or r.returncode)
            )
        return r

    def out(self, *args, **kw):
        return self.run(*args, **kw).stdout.decode("utf-8", "replace")

    def sha(self, rev):
        r = self.run("rev-parse", "-q", "--verify", rev, check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    def ancestor(self, a, b):
        return self.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def git_dir(self):
        return self.out("rev-parse", "--path-format=absolute", "--git-dir").strip()


class Blobs:
    """git cat-file --batch: every blob of a tree without a process each."""

    def __init__(self, root):
        self.p = subprocess.Popen(
            ["git", "-C", root, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def get(self, sha):
        self.p.stdin.write(sha.encode() + b"\n")
        self.p.stdin.flush()
        head = self.p.stdout.readline().split()
        if len(head) < 3:
            raise CliError("git cat-file: no object %s" % sha)
        data = self.p.stdout.read(int(head[2]))
        self.p.stdout.read(1)
        return data

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def ls_tree(git, rev):
    """[(mode, sha, path)] of every entry, gitlinks included."""
    out = []
    for rec in git.out("ls-tree", "-r", "-z", "--full-tree", rev).split("\0"):
        if rec:
            meta, path = rec.split("\t", 1)
            mode, _kind, sha = meta.split()
            out.append((mode, sha, path))
    return out


def clean_env(author):
    """No system or global config takes part, and the identity is the
    [public] author's: a private identity cannot reach a public object."""
    name, email = manifest.IDENTITY_RE.match(author).groups()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
    )
    return env


# ── the private inputs ───────────────────────────────────────────────────


def public_dir(root):
    """$XBR_PUBLIC_DIR, refused inside the game's work tree (a tracked
    copy would publish the very names it hides)."""
    d = os.environ.get("XBR_PUBLIC_DIR", "")
    if not d:
        return ""
    real, top = os.path.realpath(d), os.path.realpath(root)
    if real == top or real.startswith(top + os.sep):
        raise Cannot("XBR_PUBLIC_DIR (%s) is inside the game's work tree: keep it outside" % d)
    return real


def pattern_lines(text):
    return [s for s in (x.strip() for x in text.splitlines()) if s and not s.startswith("#")]


def private_patterns(git):
    """[(source regex, compiled)] from the first audit-private file found."""
    pd = public_dir(git.root)
    looked = []
    for path in (
        os.path.join(pd, "audit-private") if pd else "",
        os.environ.get("XBR_AUDIT_PRIVATE", ""),
        os.path.join(git.git_dir(), "info", "audit-private"),
    ):
        if not path:
            continue
        looked.append(path)
        try:
            with open(path, encoding="utf-8") as f:
                lines = pattern_lines(f.read())
        except OSError:
            continue
        except UnicodeDecodeError as e:
            raise Cannot("%s is not UTF-8: %s" % (path, e)) from e
        if not lines:
            continue
        out = []
        for p in lines:
            try:
                out.append((p, re.compile(p, re.I)))
            except re.error as e:
                raise Cannot("%s: %r: %s" % (path, p, e)) from e
        return out
    raise Cannot(
        "no private patterns (one regex per line: the bench host's name, the personal "
        "email), so nothing can be checked for them; looked in %s. docs/layout.md, "
        '"Private inputs", says where they go' % ", ".join(looked)
    )


def rewrite_dir(git):
    return public_dir(git.root) or os.path.join(git.git_dir(), "info", "xbr-public")


class Rules:
    """The rewrite file (docs/layout.md, "The rewrite format"):
      regex<TAB>replacement         per line, the line ending kept
      !blob regex<TAB>replacement   over the whole blob (a rule that joins
                                    a wrapped line; (?m) for ^ and $ per line)
      !fail regex                   a check: matched without case
    In file order; Python re with ASCII classes (perl's byte semantics, as
    the shell scripts the rules came from ran them). In a replacement \\1 or
    \\g<name> is a group and \\u upper-cases the next character."""

    def __init__(self, text="", source="", path=""):
        self.path = path
        source = source or "rewrite"
        self.rules, self.fails = [], []
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            where = "%s:%d" % (source, n)
            try:
                if line.startswith("!fail "):
                    self.fails.append((line[6:].strip(), re.compile(line[6:].strip(), re.I)))
                    continue
                whole = line.startswith("!blob ")
                body = line[6:] if whole else line
                if "\t" not in body:
                    raise CliError("%s: a rule is regex<TAB>replacement" % where)
                pat, rep = body.split("\t", 1)
                rx = re.compile(pat, re.ASCII)
                check_replacement(rx, rep, where)
                self.rules.append((whole, rx, rep, where))
            except re.error as e:
                raise CliError("%s: %s" % (where, e)) from e

    def apply(self, text):
        for whole, rx, rep, _ in self.rules:
            if whole:
                text = rx.sub(lambda m, rep=rep: expand(m, rep), text)
            else:
                text = "".join(
                    rx.sub(lambda m, rep=rep: expand(m, rep), body) + end
                    for body, end in split_lines(text)
                )
        return text


def check_replacement(rx, rep, where):
    """Each group a replacement names exists in rx: a bad one is the rule
    file's error, found when it loads, not a traceback mid-export."""
    for m in re.finditer(r"\\(?:(\d)|g(<[^>]*>?|))|\\.", rep):
        if m.group(1):
            if int(m.group(1)) > rx.groups:
                raise CliError(
                    "%s: \\%s, but the regex has %d group%s"
                    % (where, m.group(1), rx.groups, "" if rx.groups == 1 else "s")
                )
        elif m.group(0).startswith("\\g"):
            g = m.group(2)
            if not g.startswith("<") or not g.endswith(">") or len(g) < 3:
                raise CliError("%s: \\g needs <number> or <name>" % where)
            name = g[1:-1]
            ok = int(name) <= rx.groups if name.isdigit() else name in rx.groupindex
            if not ok:
                raise CliError("%s: \\g%s names no group of the regex" % (where, g))


def split_lines(text):
    for line in text.splitlines(True):
        body = line.rstrip("\r\n")
        yield body, line[len(body) :]


def expand(m, rep):
    out, upper, i = [], False, 0
    while i < len(rep):
        c = rep[i]
        piece = c
        if c == "\\" and i + 1 < len(rep):
            n = rep[i + 1]
            i += 1
            if n.isdigit():
                piece = m.group(int(n)) or ""
            elif n == "g" and rep.startswith("<", i + 1):
                j = rep.index(">", i)
                g = rep[i + 2 : j]
                piece = m.group(int(g) if g.isdigit() else g) or ""
                i = j
            elif n == "u":
                upper = True
                i += 1
                continue
            else:
                piece = {"n": "\n", "t": "\t", "\\": "\\"}.get(n, "\\" + n)
        if upper and piece:
            piece, upper = piece[0].upper() + piece[1:], False
        out.append(piece)
        i += 1
    return "".join(out)


def load_rules(git):
    """The rewrite file, which must exist: a checkout without it (a main
    checkout run without XBR_PUBLIC_DIR, or a directory that lost it)
    would export the names it hides unrewritten and skip its !fail checks."""
    pd = public_dir(git.root)
    path = os.path.join(pd or rewrite_dir(git), "rewrite")
    try:
        with open(path, encoding="utf-8") as f:
            return Rules(f.read(), path, path)
    except FileNotFoundError:
        if pd:
            raise Cannot(
                "XBR_PUBLIC_DIR is set but %s does not exist (an empty file means no rules)" % path
            ) from None
        raise Cannot(
            "no rewrite file: set XBR_PUBLIC_DIR to the folder that holds rewrite and"
            " audit-private, or create %s (an empty file means no rules)" % path
        ) from None
    except UnicodeDecodeError as e:
        raise Cannot("%s is not UTF-8: %s" % (path, e)) from e


def text_of(data):
    """The blob as text, or None for a binary (git's test) or non-UTF-8."""
    if layout.binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def line_hits(pats, label, text):
    """['label:N: /pat/: line'] of each line any pattern matches."""
    out = []
    # On \n only, as git counts lines: splitlines() also breaks on \x0b,
    # \x1c-\x1e and \u2028, so the numbers would drift from the file's.
    for n, line in enumerate(text.split("\n"), 1):
        for src, rx in pats:
            if rx.search(line):
                out.append("%s:%d: /%s/: %s" % (label, n, src, line.strip()[:160]))
    return out


def golden_path(ctx):
    """golden.json's path in the repo, or None when the game has none."""
    gj = getattr(ctx.game, "golden_json", "")
    return os.path.relpath(gj, ctx.game.root).replace(os.sep, "/") if gj else None


def drop_known(ctx, path, text):
    """golden.json without its known_divergence entries. Each names a
    private tracker item (golden check --host-os); the rewrite rules would
    only cut the ref out of a JSON string, so the entry goes whole. Text
    without the key is returned as is, so the export is unchanged."""
    if path != golden_path(ctx) or '"known_divergence"' not in text:
        return text

    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k != "known_divergence"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    # golden.py writes it with json.dump(indent=2) and a newline.
    return json.dumps(strip(json.loads(text)), indent=2) + "\n"


# ── the context ──────────────────────────────────────────────────────────


class Ctx:
    def __init__(self, game, need_patterns=True):
        if "public" not in game.m:
            raise CliError('game.toml has no [public] table (docs/layout.md, "Publishing")')
        self.game = game
        self.p = game.m["public"]
        self.git = Git(game.root)
        if layout.toplevel(game.root) != os.path.realpath(game.root):
            raise CliError("%s is not the top of a git repository" % game.root)
        self.patterns = private_patterns(self.git) if need_patterns else []
        self.builtin = [(p, re.compile(p, f)) for p, f in BUILTIN]
        self.remote_ref = "refs/remotes/%s/%s" % (self.p["remote"], self.p["branch"])
        self.public_ref = "refs/public/%s" % self.p["branch"]

    def upstream_blob(self):
        up = self.p["upstream"]
        if not up:
            return None
        return lambda path: self.git.sha("%s:%s" % (up, path)) or None


# ── refusals, shared by export and audit ─────────────────────────────────


def refusal(ctx, mode, sha, path, data, upstream_blob):
    """Why this entry may never be published, or ""."""
    if mode == "160000":
        return "a gitlink (embedded repo)"
    why = layout.refused_path(ctx.game, path)
    if why:
        return why
    ext = layout.data_ext(path)
    bad = ("game data or a bundle (.%s)" % ext) if ext else ""
    if not bad and data is not None and layout.binary(data):
        bad = "a binary"
    if bad and layout.exempt(ctx.game, path, (sha, data or b""), upstream_blob):
        return ""
    if bad and layout.README_IMAGE.match(path):
        return "a README image: " + layout.png_problem(data or b"")
    return bad


# ── export ───────────────────────────────────────────────────────────────


def fetch_check(ctx):
    """Fetch the public remote and refuse unless its branch is what the
    public repo really has: the export must fast-forward the real tip."""
    g, p = ctx.git, ctx.p
    if g.run("remote", "get-url", p["remote"], check=False).returncode:
        raise CliError("no remote %r: run `%s public init` first" % (p["remote"], ctx.game.slug))
    r = g.run("fetch", "--no-tags", "--quiet", p["remote"], check=False)
    if r.returncode:
        raise Cannot("git fetch %s: %s" % (p["remote"], r.stderr.decode().strip()))
    r = g.run("ls-remote", p["url"], "refs/heads/" + p["branch"], check=False)
    if r.returncode:
        raise Cannot("git ls-remote %s: %s" % (p["url"], r.stderr.decode().strip()))
    real = r.stdout.decode().split()[0] if r.stdout.strip() else ""
    have = g.sha(ctx.remote_ref)
    if real != have:
        raise CliError(
            "%s is %s but the public repo's %s is %s: fetch again (or check the remote's URL)"
            % (ctx.remote_ref, have or "missing", p["branch"], real or "missing")
        )
    return have


def build_tree(ctx, ref, rules=None):
    """REF's tree as published: (tree sha, summary, problems). The private
    set and [public] exclude dropped; the refusal list refused; the rules
    applied to every text blob, then the private patterns and the !fail
    checks run over every exported text blob and path name."""
    g = ctx.git
    rules = rules or load_rules(g)
    upstream_blob = ctx.upstream_blob()
    keep, dropped, rewritten, problems = [], [], [], []
    blobs = Blobs(g.root)
    try:
        for mode, sha, path in ls_tree(g, ref):
            if layout.private(ctx.game, path):
                dropped.append(path)
                continue
            data = blobs.get(sha) if mode != "160000" else None
            why = refusal(ctx, mode, sha, path, data, upstream_blob)
            if why:
                problems.append("refused %s: %s (fix it in the private commit)" % (path, why))
                continue
            text = text_of(data) if data is not None else None
            if (
                text is None
                and data is not None
                and not layout.binary(data)
                and not layout.exempt(ctx.game, path, (sha, data), upstream_blob)
            ):
                # The rules and checks read text: a Latin-1 or UTF-16
                # without NULs would pass them unread.
                problems.append(
                    "refused %s: text that is not UTF-8 (convert it in the private commit)" % path
                )
                continue
            if text is not None:
                new = rules.apply(drop_known(ctx, path, text))
                if new != text:
                    sha = g.out("hash-object", "-w", "--stdin", input=new.encode()).strip()
                    rewritten.append(path)
                    text = new
                problems += line_hits(ctx.builtin + ctx.patterns, path, text)
                problems += [h + " (a !fail check)" for h in line_hits(rules.fails, path, text)]
            keep.append((mode, sha, path))
    finally:
        blobs.close()
    names = sorted(
        {
            "/".join(path.split("/")[: i + 1])
            for _, _, path in keep
            for i in range(path.count("/") + 1)
        }
    )
    for name in names:
        for src, rx in ctx.builtin + ctx.patterns + rules.fails:
            if rx.search(name):
                problems.append("path name %s: /%s/" % (name, src))
    if not keep:
        problems.append("nothing to export from %s" % ref)
        return EMPTY_TREE, (keep, dropped, rewritten, rules), problems
    idx = os.path.join(g.git_dir(), "xbr-public.index")
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    try:
        g.run("read-tree", "--empty", env=env)
        g.run(
            "update-index",
            "-z",
            "--index-info",
            input=b"".join(
                b"%s %s\t%s\0" % (m.encode(), s.encode(), p.encode()) for m, s, p in keep
            ),
            env=env,
        )
        tree = g.out("write-tree", env=env).strip()
    finally:
        if os.path.exists(idx):
            os.remove(idx)
    return tree, (keep, dropped, rewritten, rules), problems


def changelog_section(text, tag):
    """The `## [X]` section of CHANGELOG.md for tag vX (Keep a Changelog)."""
    want = tag[1:] if tag.startswith("v") else tag
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## ["):
            if on:
                break
            on = line[4:].split("]", 1)[0] == want
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


def map_path(ctx):
    pd = public_dir(ctx.git.root)
    d = pd or os.path.join(ctx.git.git_dir(), "info", "xbr-public")
    return os.path.join(d, ctx.game.slug + ".map")


def tz_offset():
    off = time.localtime().tm_gmtoff // 60
    return "%s%02d%02d" % ("-" if off < 0 else "+", abs(off) // 60, abs(off) % 60)


def make_tag(ctx, commit, tag, tree):
    """An annotated tag object on commit, its message the tag's CHANGELOG
    section as exported (made with mktag in the clean environment)."""
    g = ctx.git
    sha = g.sha("%s:CHANGELOG.md" % tree)
    if not sha:
        raise CliError("--tag %s: the exported tree has no CHANGELOG.md" % tag)
    section = changelog_section(g.out("cat-file", "blob", sha), tag)
    if not section:
        raise CliError(
            "--tag %s: CHANGELOG.md has no `## [%s]` section, or it is empty"
            % (tag, tag[1:] if tag.startswith("v") else tag)
        )
    body = "object %s\ntype commit\ntag %s\ntagger %s %d %s\n\n%s %s\n\n%s\n" % (
        commit,
        tag,
        ctx.p["author"],
        int(time.time()),
        tz_offset(),
        ctx.game.name,
        tag,
        section,
    )
    return g.out("mktag", input=body.encode(), env=clean_env(ctx.p["author"])).strip()


def allowance_path(git):
    """The one-time allowance `export --fresh-root` writes for the guard,
    in the common git dir (the guard reads it from any worktree)."""
    common = git.out("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.join(common, ALLOWANCE)


ALLOWANCE = "xbr-public-rewrite"


def drop_allowance(git):
    """A plain export ends any rewrite allowance: it named a root the
    public ref has moved past."""
    path = allowance_path(git)
    if os.path.exists(path):
        os.remove(path)
        host.say("public export: dropped the rewrite allowance %s" % path)


def approved_tip(fresh, parent):
    """--fresh-root OLD must name the public tip it replaces: the approval
    is typed for that history, and a tip that moved since is not it."""
    if not parent:
        raise CliError("--fresh-root: the public branch is empty, so a plain export is a root")
    want = fresh.lower()
    if len(want) < 7 or not re.fullmatch(r"[0-9a-f]+", want) or not parent.startswith(want):
        raise CliError(
            "--fresh-root %s: the public tip is %s; name it (7 or more hex digits) to approve"
            " replacing its history" % (fresh, parent[:12])
        )


def export(ctx, a):
    g, p = ctx.git, ctx.p
    if p["mode"] != "export":
        raise CliError("[public] mode is %r: export runs in export mode only" % p["mode"])
    fresh = a.fresh_root or ""
    if a.drop_tags and not fresh:
        raise CliError("--drop-tags goes with --fresh-root")
    tag = a.tag or ""
    if tag:
        if g.run("check-ref-format", "refs/tags/" + tag, check=False).returncode:
            raise CliError("--tag %r is not a tag name" % tag)
        for r in ("refs/public-tags/" + tag, "refs/tags/" + tag):
            if g.sha(r):
                raise CliError(
                    "--tag %s: %s exists (a public tag never shares a private name)" % (tag, r)
                )
    ref = a.ref or "HEAD"
    commit = g.sha(ref + "^{commit}")
    if not commit:
        raise CliError("no such commit: %s" % ref)
    if commit != g.sha("HEAD"):
        raise CliError(
            "%s is not HEAD: the layout check reads the working tree, so export only"
            " what is checked out (check %s out in a worktree and export there)" % (ref, ref)
        )
    if not a.allow_dirty:
        if g.out("status", "--porcelain", "--untracked-files=no").strip():
            raise CliError("the working tree has changes: commit them, or --allow-dirty")
    # Before the network: a checkout without the rules refuses first.
    rules = load_rules(g)
    parent = fetch_check(ctx)
    old_tags = {}
    if fresh:
        approved_tip(fresh, parent)
        if a.drop_tags:
            old_tags = remote_tags(ctx)
            if tag in old_tags:
                raise CliError("--tag %s: the public repo has it, and --drop-tags deletes it" % tag)
    tree, (keep, dropped, rewritten, rules), problems = build_tree(ctx, commit, rules)
    if problems:
        return report_hits("export", problems, ref_moved=False)
    # A fresh root has no public parent: the old history it replaces is
    # not in the new one, so none of its messages or names are pushed.
    replaced, parent = (parent, "") if fresh else ("", parent)
    parents = [parent] if parent else []
    up = p["upstream"]
    if up:
        if not g.sha(up + "^{commit}"):
            raise CliError("[public] upstream %r: no such ref (fetch it)" % up)
        u = g.out("merge-base", commit, up, check=False).strip()
        if u and not (parent and g.ancestor(u, parent)):
            parents.append(u)
    if not fresh and parent and len(parents) == 1 and g.sha(parent + "^{tree}") == tree:
        host.say("nothing to publish: %s's tree is the public tip's" % ref)
        drop_allowance(g)
        return 0
    msg = (
        a.message
        or p["message"]
        or "%s static recompilation: %s round"
        % (
            ctx.game.name,
            datetime.date.today().isoformat(),
        )
    )
    # Never signed: a signature carries the private key's identity, and a
    # repo's commit.gpgSign must not make it (mktag never signs).
    args = ["commit-tree", "--no-gpg-sign", tree]
    for x in parents:
        args += ["-p", x]
    new = g.out(*args, "-m", msg, env=clean_env(p["author"])).strip()
    tag_sha = make_tag(ctx, new, tag, tree) if tag else ""
    hits = audit_commit(
        ctx, parent, new, extra_tags=[(tag, tag_sha)] if tag else [], fails=rules.fails
    )
    if hits:
        return report_hits("export", hits, ref_moved=False)
    old = g.sha(ctx.public_ref)
    g.run("update-ref", "-m", "public export", ctx.public_ref, new, old or "0" * 40)
    if tag:
        g.run("update-ref", "refs/public-tags/" + tag, tag_sha, "0" * 40)
    if fresh:
        write_allowance(g, p["branch"], replaced, new, old_tags)
        # The dropped tags' local copies name the old history: left, the
        # next audit would read them as new tags and the guard refuse them.
        for t in old_tags:
            if g.sha("refs/public-tags/" + t):
                g.run("update-ref", "-d", "refs/public-tags/" + t)
    else:
        drop_allowance(g)
    mp = map_path(ctx)
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    with open(mp, "a", encoding="utf-8") as f:
        f.write("%s %s %s\n" % (new, commit, datetime.date.today().isoformat()))
    host.say("public export: %s -> %s %s" % (ref, ctx.public_ref, new[:12]))
    host.say("  %d files, from %s" % (len(keep), commit[:12]))
    host.say("  parents: %s" % (", ".join(x[:12] for x in parents) or "none (first round)"))
    host.say("  excluded %d:" % len(dropped))
    for f in dropped:
        host.say("    " + f)
    host.say(
        "  rewrite: %s (%d rules, %d checks)" % (rules.path, len(rules.rules), len(rules.fails))
    )
    host.say("  rewritten %d:" % len(rewritten))
    for f in rewritten:
        host.say("    " + f)
    if tag:
        host.say("  tag refs/public-tags/%s %s" % (tag, tag_sha[:12]))
        # The new tag and its map line are both written: the db's
        # releases can name the release's private sha and pins now.
        from .benchdb import releases

        releases.after_tag(ctx.game.root, say=lambda m: host.say("  " + m))
    host.say("  map: %s" % mp)
    push = "git push %s %s:refs/heads/%s" % (p["remote"], ctx.public_ref, p["branch"])
    if fresh:
        host.say("  fresh root: replaces the public history at %s" % replaced[:12])
        host.say("  allowance: %s (this push only)" % allowance_path(g))
        if not old_tags:
            kept = remote_tags(ctx)
            if kept:
                host.say(
                    "  %d public tag%s the old history (--drop-tags deletes them): %s"
                    % (
                        len(kept),
                        " still reaches" if len(kept) == 1 else "s still reach",
                        ", ".join(sorted(kept)),
                    )
                )
        # Atomic: a branch rewritten while a tag delete fails (or the
        # reverse) would leave tags on a history that is gone.
        push = "git push --atomic --force-with-lease=refs/heads/%s:%s %s %s:refs/heads/%s" % (
            p["branch"],
            replaced,
            p["remote"],
            ctx.public_ref,
            p["branch"],
        )
        push += "".join(" :refs/tags/%s" % t for t in sorted(old_tags))
    if tag:
        push += " refs/public-tags/%s:refs/tags/%s" % (tag, tag)
    host.say("push (audited; the CLI never pushes):")
    host.say("  " + push)
    return 0


def allowance_names(git, branch, old, new):
    """Whether the rewrite allowance approves branch moving old -> new."""
    try:
        with open(allowance_path(git), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    return "branch %s %s %s" % (branch, old, new) in lines


def write_allowance(git, branch, old, new, tags):
    """The guard's one-time allowance: the branch may move from exactly old
    to exactly new, and each listed tag, at exactly its sha, may be
    deleted. Once the push lands the remote is no longer at old, so the
    line allows nothing more; the next plain export removes the file."""
    lines = ["branch %s %s %s" % (branch, old, new)]
    lines += ["tag %s %s" % (t, sha) for t, sha in sorted(tags.items())]
    with open(allowance_path(git), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def report_hits(what, hits, ref_moved=True):
    for h in hits:
        print("HIT  " + h)
    print(
        "public %s: %d hit%s%s"
        % (what, len(hits), "" if len(hits) == 1 else "s", "" if ref_moved else "; no ref moved")
    )
    return 1


# ── audit ────────────────────────────────────────────────────────────────


def added_lines(git, base, tip):
    """{path: [(line number, text)]} added from base to tip, text only."""
    out, path, n = {}, None, 0
    r = git.run(
        "diff",
        "--text",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "-U0",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base,
        tip,
    )
    head = False  # in a file's header: an added "++ x" line is not "+++ b/"
    for raw in r.stdout.decode("utf-8", "replace").split("\n"):
        if raw.startswith("diff --git "):
            head, path = True, None
        elif head and raw.startswith("+++ "):
            name = unquote_c(raw[4:])
            path = name[2:] if name.startswith("b/") else None
        elif raw.startswith("@@"):
            head = False
            m = re.match(r"@@ -\S+ \+(\d+)", raw)
            n = int(m.group(1)) if m else 0
        elif not head and raw.startswith("+"):
            # A line whose file was not understood is still checked, under
            # a name that says so: an unread header must not hide it.
            out.setdefault(path or UNKNOWN_PATH, []).append((n, raw[1:]))
            n += 1
    return out


UNKNOWN_PATH = "(a path the diff header did not give)"
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_c(s):
    """A diff header's name as git quotes it ("b/a\\"b" for a path with a
    quote, a backslash or a control character; octal for other bytes)."""
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    out, i, body = bytearray(), 0, s[1:-1]
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n in C_ESCAPES:
                out.append(C_ESCAPES[n])
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1 : i + 4]):
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def changed(git, base, tip):
    """[(status, mode, sha, path)] of the entries tip adds or changes."""
    out = []
    recs = git.out("diff-tree", "-r", "-z", "--no-renames", base, tip).split("\0")
    i = 0
    while i < len(recs) - 1:
        meta = recs[i]
        if not meta.startswith(":"):
            i += 1
            continue
        _om, nm, _os, ns, st = meta[1:].split()
        path = recs[i + 1]
        i += 2
        if st[0] != "D":
            out.append((st[0], nm, ns, path))
    return out


def path_hits(pats, fails, label, path):
    """A path name against the patterns and FAILS: a name is pushed as
    its content is."""
    out = ["%s: /%s/" % (label, src) for src, rx in pats if rx.search(path)]
    out += ["%s: /%s/ (a !fail check)" % (label, src) for src, rx in fails if rx.search(path)]
    return out


def content_hits(ctx, base, tip, fails=()):
    """The diff base..tip: added lines and the added or changed paths'
    names, against the patterns and FAILS (the rewrite file's checks)."""
    g = ctx.git
    pats = ctx.builtin + ctx.patterns
    hits = []
    for path, lines in added_lines(g, base, tip).items():
        if path == UNKNOWN_PATH:
            hits.append(
                "%d added line%s under a diff header that was not read"
                % (len(lines), "" if len(lines) == 1 else "s")
            )
        for n, line in lines:
            for src, rx in pats:
                if rx.search(line):
                    hits.append("%s:%d: /%s/: %s" % (path, n, src, line.strip()[:160]))
            for src, rx in fails:
                if rx.search(line):
                    hits.append(
                        "%s:%d: /%s/: %s (a !fail check)" % (path, n, src, line.strip()[:160])
                    )
    upstream_blob = ctx.upstream_blob()
    blobs = Blobs(g.root)
    try:
        for _st, mode, sha, path in changed(g, base, tip):
            hits += path_hits(pats, fails, "path %s" % path, path)
            data = blobs.get(sha) if mode != "160000" else None
            why = refusal(ctx, mode, sha, path, data, upstream_blob)
            if why:
                hits.append("%s: %s" % (path, why))
    finally:
        blobs.close()
    return hits


def tree_hits(ctx, tip):
    """The tip's whole tree: the refusal list (paths only: the content was
    the diff's) and the private set (export drops it, history mode must not
    track it)."""
    hits = []
    for mode, _sha, path in ls_tree(ctx.git, tip):
        if mode == "160000":
            hits.append("%s: a gitlink in the tree" % path)
        elif layout.refused_path(ctx.game, path):
            hits.append("%s in the tree: %s" % (path, layout.refused_path(ctx.game, path)))
        if layout.private(ctx.game, path):
            hits.append(
                "%s is in the private set (%s)"
                % (
                    path,
                    "tracked in history mode"
                    if ctx.p["mode"] == "history"
                    else "export drops it: this tip was not made by `public export`",
                )
            )
    return hits


def log_hits(ctx, revs, fails=()):
    """Messages and identities of the commits revs lists; FAILS, the
    rewrite file's checks, hold for the messages too."""
    g = ctx.git
    want = ctx.p["author"]
    pats = ctx.builtin + ctx.patterns + list(fails)
    hits = []
    raw = g.out("log", "--format=%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x01", *revs, "--")
    for rec in raw.split("\x01"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, author, committer, body = rec.split("\0", 3)
        for who, ident in (("author", author), ("committer", committer)):
            if ident != want:
                hits.append("commit %s: %s %s (want %s)" % (sha[:12], who, ident, want))
        hits += line_hits(pats, "message %s" % sha[:12], body)
    return hits


def tag_hits(ctx, tags):
    """[(name, tag object sha)]: tagger and message."""
    g = ctx.git
    pats = ctx.builtin + ctx.patterns
    hits = []
    for name, sha in tags:
        if g.out("cat-file", "-t", sha).strip() != "tag":
            hits.append("tag %s: not an annotated tag" % name)
            continue
        head, _, msg = g.out("cat-file", "tag", sha).partition("\n\n")
        tagger = next((x[7:] for x in head.splitlines() if x.startswith("tagger ")), "")
        ident = re.sub(r" \d+ [-+]\d{4}$", "", tagger)
        if ident != ctx.p["author"]:
            hits.append("tag %s: tagger %s (want %s)" % (name, ident, ctx.p["author"]))
        hits += line_hits(pats, "tag %s" % name, msg)
    return hits


def remote_tags(ctx):
    """{name: sha} of the public repo's tags (ls-remote)."""
    r = ctx.git.run("ls-remote", "--tags", ctx.p["url"], check=False)
    if r.returncode:
        raise Cannot("git ls-remote %s: %s" % (ctx.p["url"], r.stderr.decode().strip()))
    out = {}
    for line in r.stdout.decode().splitlines():
        sha, ref = line.split("\t", 1)
        if not ref.endswith("^{}"):
            out[ref[len("refs/tags/") :]] = sha
    return out


def new_public_tags(ctx):
    """The refs/public-tags/* a push would add: those not on the remote."""
    have = remote_tags(ctx)
    out = []
    for line in ctx.git.out(
        "for-each-ref", "--format=%(refname:strip=2) %(objectname)", "refs/public-tags/"
    ).splitlines():
        name, sha = line.rsplit(" ", 1)
        if have.get(name) != sha:
            out.append((name, sha))
    return out


def pin_hits(ctx, tip):
    """The tip's [cli] and [toolkit] pins: each a tag on its public URL
    that names the pinned commit, or a public clone could not run setup."""
    from . import pins

    g = ctx.git
    sha = g.sha("%s:%s" % (tip, manifest.FILE))
    if not sha:
        return ["the tip has no %s" % manifest.FILE]
    try:
        m = manifest.parse_text(g.out("cat-file", "blob", sha), manifest.FILE)
    except manifest.ManifestError as e:
        return ["the tip's %s: %s" % (manifest.FILE, e)]
    hits = []
    for table, url in (("cli", m["cli"]["url"] or pins.CLI_URL), ("toolkit", m["toolkit"]["url"])):
        t = m[table]
        if not t["tag"]:
            hits.append(
                "[%s] pins no tag: a public clone could not run setup (pin a release tag)" % table
            )
            continue
        r = g.run(
            "ls-remote", url, "refs/tags/" + t["tag"], "refs/tags/%s^{}" % t["tag"], check=False
        )
        if r.returncode:
            raise Cannot("git ls-remote %s: %s" % (url, r.stderr.decode().strip()))
        refs = dict(reversed(x.split("\t", 1)) for x in r.stdout.decode().splitlines())
        got = refs.get("refs/tags/%s^{}" % t["tag"]) or refs.get("refs/tags/" + t["tag"])
        if not got:
            hits.append(
                "[%s] tag %s is not on %s: a public clone could not run setup"
                % (table, t["tag"], url)
            )
        elif t["commit"] and got != t["commit"]:
            hits.append(
                "[%s] tag %s on %s is %s, not the pinned commit %s"
                % (table, t["tag"], url, got[:12], t["commit"][:12])
            )
    return hits


def layout_hits(ctx):
    """The layout block's problems. A block that could not run is exit 2:
    an unchecked layout is not a clean one."""
    try:
        rep = layout.report(ctx.game, strict=True)
    except Exception as e:
        raise Cannot("layout not checked: %s" % e) from e
    return ["layout: " + x for x in rep["problems"]]


def layout_source_problem(ctx, tip):
    """Why the layout block cannot speak for tip, or "": it reads the
    working tree, so only for HEAD or the export of HEAD (the map file's
    record)."""
    g = ctx.git
    head = g.sha("HEAD")
    if tip == head:
        return ""
    try:
        with open(map_path(ctx), encoding="utf-8") as f:
            made = {x.split()[0]: x.split()[1] for x in f if len(x.split()) >= 2}
    except OSError:
        made = {}
    src = made.get(tip)
    if src == head:
        return ""
    return "layout not checked: it reads the working tree, and %s is %s: check out %s" % (
        tip[:12],
        "the export of %s" % src[:12] if src else "neither HEAD nor an export of it",
        src[:12] if src else "the commit it was made from",
    )


def audit_commit(ctx, base, tip, extra_tags=(), fails=()):
    """Everything a push of tip over base would publish (export's last
    step, before any ref moves)."""
    up = ctx.p["upstream"]
    revs = [tip] + (["^" + base] if base else []) + (["^" + up] if up else [])
    hits = content_hits(ctx, base or EMPTY_TREE, tip)
    hits += log_hits(ctx, revs, fails)
    tags = list(extra_tags)
    if not extra_tags:
        tags = new_public_tags(ctx)
    hits += tag_hits(ctx, tags)
    hits += tree_hits(ctx, tip)
    hits += layout_hits(ctx)
    hits += pin_hits(ctx, tip)
    return hits


def audit(ctx, a):
    g, p = ctx.git, ctx.p
    unchecked = ""
    if a.all:
        if p["mode"] == "export":
            tree, _, problems = build_tree(ctx, "HEAD")
            hits = problems + content_hits(ctx, EMPTY_TREE, tree)
            hits += tree_hits(ctx, tree) + layout_hits(ctx) + pin_hits(ctx, "HEAD")
            what = "a dry-run export of HEAD"
        else:
            up = p["upstream"]
            hits = content_hits(ctx, EMPTY_TREE, "HEAD")
            hits += log_hits(ctx, ["HEAD"] + (["^" + up] if up else []))
            hits += history_hits(ctx, ["HEAD"] + (["^" + up] if up else []))
            hits += tree_hits(ctx, "HEAD") + layout_hits(ctx) + pin_hits(ctx, "HEAD")
            what = "HEAD, whole history"
    else:
        if a.range:
            if ".." not in a.range:
                raise CliError("RANGE is BASE..TIP")
            base, tip = a.range.split("..", 1)
        else:
            base = ctx.remote_ref
            tip = ctx.public_ref if p["mode"] == "export" else p["branch"]
        base_sha = g.sha(base + "^{commit}") if base else ""
        tip_sha = g.sha(tip + "^{commit}")
        if not tip_sha:
            raise Cannot("no %s: nothing to audit (run `%s public export`)" % (tip, ctx.game.slug))
        whole = ""
        if base_sha and not g.ancestor(base_sha, tip_sha):
            # Only the approved rewrite replaces the base's history, so only
            # then is all of the tip new; otherwise the tip is stale or
            # rewound, and the guard would refuse its push anyway.
            if not allowance_names(g, p["branch"], base_sha, tip_sha):
                raise CliError(
                    "%s is not a fast-forward of %s and no `export --fresh-root` approved"
                    " replacing it: fetch the remote and export again" % (tip, base)
                )
            base_sha, whole = "", "; the approved fresh root, so the whole tip"
        unchecked = layout_source_problem(ctx, tip_sha)
        up = p["upstream"]
        revs = [tip_sha] + (["^" + base_sha] if base_sha else []) + (["^" + up] if up else [])
        # Export mode pushes rewritten trees: its checks hold for what is
        # pushed, and a checkout without the rewrite file refuses here too.
        fails = load_rules(g).fails if p["mode"] == "export" else ()
        hits = content_hits(ctx, base_sha or EMPTY_TREE, tip_sha, fails)
        hits += log_hits(ctx, revs, fails)
        if p["mode"] == "history":
            hits += history_hits(ctx, revs)
        hits += tag_hits(ctx, new_public_tags(ctx))
        hits += tree_hits(ctx, tip_sha) + pin_hits(ctx, tip_sha)
        hits += [] if unchecked else layout_hits(ctx)
        what = "%s..%s%s" % (base if base_sha else "(empty)", tip, whole)
    if hits:
        return report_hits("audit", hits)
    if unchecked:
        raise Cannot(unchecked)
    print("public audit: clean (%s)" % what)
    return 0


def history_hits(ctx, revs, fails=()):
    """History mode: each commit against its first parent, since every one
    of them is pushed (a merge too: what it brings in is in that diff)."""
    hits = []
    for sha in ctx.git.out("rev-list", *revs).split():
        parent = ctx.git.sha(sha + "^") or EMPTY_TREE
        hits += ["commit %s: %s" % (sha[:12], h) for h in content_hits(ctx, parent, sha, fails)]
    return hits


# ── upstream pull request branches ───────────────────────────────────────


REWRITTEN = "a rewrite rule would change this; a PR branch is pushed as is"


def rewrite_hits(git, rules, revs, name):
    """What the rewrite file would change in the commits revs lists (added
    lines, paths, messages) and in the branch name: export would rewrite
    it, so a PR branch would publish it as written."""
    hits = []

    def check(label, text, show=""):
        if rules.apply(text) != text:
            hits.append("%s: %s%s" % (label, REWRITTEN, show and ": " + show.strip()[:160]))

    check("branch name pr/%s" % name, "pr/" + name)
    for sha in git.out("rev-list", *revs).split():
        parent = git.sha(sha + "^") or EMPTY_TREE
        c = "commit %s" % sha[:12]
        check("%s: message" % c, git.out("log", "-1", "--format=%B", sha))
        for path, lines in added_lines(git, parent, sha).items():
            for n, line in lines:
                check("%s: %s:%d" % (c, path, n), line, line)
        for _st, _mode, _sha, path in changed(git, parent, sha):
            check("%s: path %s" % (c, path), path)
    return hits


def pr(ctx, a):
    """An upstream PR branch on a fork's public repo: upstream's history
    plus our commits, pushed as they are (no rewrite). Each commit is
    audited as history mode audits one, then the commit goes in
    refs/public-pr/NAME, the one thing the guard lets reach
    refs/heads/pr/NAME."""
    g, p = ctx.git, ctx.p
    up = p["upstream"]
    if not up:
        raise CliError("[public] upstream is not set: a PR branch is upstream plus commits")
    name = a.name
    if g.run("check-ref-format", "refs/heads/pr/" + name, check=False).returncode:
        raise CliError("%r is not a branch name" % name)
    src = a.ref or "HEAD"
    commit = g.sha(src + "^{commit}")
    if not commit:
        raise CliError("no such commit: %s" % src)
    if not g.sha(up + "^{commit}"):
        raise CliError("[public] upstream %r: no such ref (fetch it)" % up)
    revs = [commit, "^" + up]
    n = g.out("rev-list", "--count", *revs).strip()
    if n in ("", "0"):
        raise CliError("%s has no commits on top of %s: nothing to open a PR with" % (src, up))
    # The rewrite file's checks hold for what is published, as in export:
    # the content, the paths, the messages and the branch's own name. Its
    # rules hide names that export rewrites; nothing rewrites a PR branch.
    rules = load_rules(g) if p["mode"] == "export" else None
    fails = rules.fails if rules else ()
    hits = line_hits(ctx.builtin + ctx.patterns + list(fails), "branch name", "pr/" + name)
    hits += history_hits(ctx, revs, fails)
    hits += log_hits(ctx, revs, fails)
    if rules:
        hits += rewrite_hits(g, rules, revs, name)
    # Per commit: a file added and removed inside the range is still
    # pushed, in the commit that added it.
    for sha in g.out("rev-list", *revs).split():
        parent = g.sha(sha + "^") or EMPTY_TREE
        for _st, _mode, _sha, path in changed(g, parent, sha):
            if layout.private(ctx.game, path):
                hits.append(
                    "commit %s: %s is in the private set: a PR branch is pushed as it is"
                    % (sha[:12], path)
                )
    if hits:
        return report_hits("pr", hits, ref_moved=False)
    ref = "refs/public-pr/" + name
    g.run("update-ref", "-m", "public pr", ref, commit)
    host.say(
        "public pr: %s -> %s %s (%s commit%s on %s)"
        % (src, ref, commit[:12], n, "" if n == "1" else "s", up)
    )
    host.say("push (audited; the CLI never pushes):")
    host.say("  git push --force-with-lease %s %s:refs/heads/pr/%s" % (p["remote"], ref, name))
    host.say(
        "close it: git push %s :refs/heads/pr/%s && git update-ref -d %s" % (p["remote"], name, ref)
    )
    return 0


# ── init ─────────────────────────────────────────────────────────────────

HOOK = """\
#!/bin/sh
@MARK@
# Written by `@SLUG@ public init` (xboxrecomp-cli); init rewrites it. For a
# push to the public repo (the remote @REMOTE@, or any URL form of
# @ID@) it allows only refs/public/@BRANCH@ as a fast-forward of
# refs/heads/@BRANCH@, refs/public-tags/* tags on that history, and
# refs/public-pr/NAME to refs/heads/pr/NAME (`public pr`). A history
# rewrite passes only by the allowance `public export --fresh-root` writes
# in the git dir, for that one push. Any other hook that was here runs
# after it, as pre-push.chained.
remote_name=$1
url=$2
id_of() {
    printf '%s\\n' "$1" | tr ':' '/' | sed -e 's#/*$##' -e 's#\\.git$##' |
        awk -F/ 'NF >= 2 { print tolower($(NF - 1) "/" $NF) }'
}
input=$(cat)
guard=0
[ "$remote_name" = "@REMOTE@" ] && guard=1
[ "$(id_of "$url")" = "@ID@" ] && guard=1
for u in "$(git remote get-url @REMOTE@ 2>/dev/null)" "$(git remote get-url --push @REMOTE@ 2>/dev/null)"; do
    [ -n "$u" ] && [ "$url" = "$u" ] && guard=1
done
if [ $guard = 1 ]; then
    pub=$(git rev-parse -q --verify refs/public/@BRANCH@)
    # Before git 2.31 rev-parse echoes --path-format back: no folder, so no push.
    common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
    if [ ! -d "$common" ]; then
        echo "public guard: git 2.31 or newer is needed (rev-parse --path-format)" >&2
        exit 1
    fi
    allowance="$common/@ALLOWANCE@"
    allowed() {
        [ -f "$allowance" ] && grep -qxF "$*" "$allowance"
    }
    bad=0
    refuse() {
        echo "public guard: $*" >&2
        bad=1
    }
    while read -r lref lsha rref rsha; do
        [ -z "$lref" ] && continue
        case $lsha in
            *[!0]*) ;;
            *)
                case $rref in
                    # A closed PR's branch goes, if `public pr` made it.
                    refs/heads/pr/*)
                        git rev-parse -q --verify "refs/public-pr/${rref#refs/heads/pr/}" >/dev/null ||
                            refuse "$rref: a delete of a branch no \\`@SLUG@ public pr\\` made"
                        ;;
                    refs/tags/*)
                        allowed "tag ${rref#refs/tags/} $rsha" ||
                            refuse "$rref: a delete; the public repo only moves forward"
                        ;;
                    *) refuse "$rref: a delete; the public repo only moves forward" ;;
                esac
                continue
                ;;
        esac
        case $rref in
            refs/heads/@BRANCH@)
                if [ "$lsha" != "$pub" ]; then
                    refuse "$rref: only refs/public/@BRANCH@ goes there (\\`@SLUG@ public export\\`, then the push it prints)"
                    continue
                fi
                case $rsha in
                    *[!0]*)
                        git merge-base --is-ancestor "$rsha" "$lsha" 2>/dev/null ||
                            allowed "branch @BRANCH@ $rsha $lsha" ||
                            refuse "$rref: not a fast-forward of the public tip (fetch it)"
                        ;;
                esac
                ;;
            refs/heads/pr/*)
                # Rebased at will, so any old tip; only the audited commit.
                name=${rref#refs/heads/pr/}
                if [ "$lsha" != "$(git rev-parse -q --verify "refs/public-pr/$name")" ]; then
                    refuse "$rref: only refs/public-pr/$name goes there (\\`@SLUG@ public pr $name\\`, then the push it prints)"
                fi
                ;;
            refs/tags/*)
                # The same name both sides: a public tag pushed under
                # another name would be a tag no export made.
                if [ "$(git rev-parse -q --verify "refs/public-tags/${rref#refs/tags/}")" != "$lsha" ]; then
                    refuse "$rref: not refs/public-tags/${rref#refs/tags/}"
                elif [ "$(git cat-file -t "$lsha")" != tag ]; then
                    refuse "$rref: not an annotated tag"
                elif [ -z "$pub" ] || ! git merge-base --is-ancestor "$lsha^{commit}" "$pub" 2>/dev/null; then
                    refuse "$rref: its commit is not on refs/public/@BRANCH@"
                fi
                ;;
            *) refuse "$rref: only refs/heads/@BRANCH@, refs/heads/pr/* and public tags go to the public repo" ;;
        esac
    done <<EOF
$input
EOF
    [ $bad = 0 ] || exit 1
fi
chained="$(dirname "$0")/pre-push.chained"
if [ -x "$chained" ]; then
    if [ -n "$input" ]; then
        printf '%s\\n' "$input" | "$chained" "$@"
    else
        "$chained" "$@" </dev/null
    fi
    exit $?
fi
exit 0
"""


def hook_text(ctx):
    p = ctx.p
    for k in ("remote", "branch"):
        if not re.match(r"^[A-Za-z0-9._/-]+$", p[k]):
            raise CliError("[public] %s %r: letters, digits, . _ / - only" % (k, p[k]))
    v = {
        "MARK": GUARD_MARK,
        "SLUG": ctx.game.slug,
        "REMOTE": p["remote"],
        "BRANCH": p["branch"],
        "ID": manifest.repo_id(p["url"]),
        "ALLOWANCE": ALLOWANCE,
    }
    text = HOOK
    for k, x in v.items():
        text = text.replace("@%s@" % k, x)
    return text


def init(ctx):
    g, p = ctx.git, ctx.p
    root = ctx.game.root
    name = p["remote"]
    found = list(layout.public_remotes(root, p["url"]))
    said = []
    existing = g.out("remote").split()
    if name in existing and name not in found:
        raise CliError(
            "remote %r exists and is not the public repo: rename it, or set [public] remote" % name
        )
    if not found:
        g.run("remote", "add", name, p["url"])
        said.append("added remote %s" % name)
    else:
        first = name if name in found else found[0]
        if first != name:
            g.run("remote", "rename", first, name)
            said.append("renamed remote %s to %s" % (first, name))
        for other in found:
            if other not in (first, name):
                g.run("remote", "remove", other)
                said.append("removed remote %s (the same repo)" % other)
    g.run("remote", "set-url", name, p["url"])
    g.run("config", "--unset-all", "remote.%s.pushurl" % name, check=False)
    g.run("config", "remote.%s.pushurl" % name, p["url"])
    g.run("config", "remote.%s.tagOpt" % name, "--no-tags")
    g.run("config", "--unset-all", "remote.%s.push" % name, check=False)
    if p["mode"] == "export":
        g.run("config", "remote.%s.push" % name, "%s:refs/heads/%s" % (ctx.public_ref, p["branch"]))
        hooks = layout.hooks_dir(root)
        os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "pre-push")
        chained = hook + ".chained"
        if os.path.exists(hook):
            with open(hook, encoding="utf-8", errors="replace") as f:
                ours = GUARD_MARK in f.read()
            if not ours:
                if os.path.exists(chained):
                    raise CliError(
                        "%s and %s both exist: merge them into one hook first" % (hook, chained)
                    )
                os.rename(hook, chained)
                said.append("kept the existing pre-push as pre-push.chained (it runs after)")
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write(hook_text(ctx))
        os.chmod(hook, 0o755)
        said.append("pre-push guard in %s" % hook)
    said.append(
        "remote %s: url and pushurl %s, tagOpt --no-tags%s"
        % (
            name,
            p["url"],
            ", push %s:refs/heads/%s" % (ctx.public_ref, p["branch"])
            if p["mode"] == "export"
            else "",
        )
    )
    hp = layout.config_get(root, "core.hooksPath")
    for s in said:
        host.say("public init: " + s)
    if hp:
        host.say(
            "public init: core.hooksPath is %r, so git skips the guard: unset it "
            "(git config --unset core.hooksPath)" % hp
        )
        return 1
    return 0


# ── the command ──────────────────────────────────────────────────────────


def configure(p):
    sub = p.add_subparsers(dest="action", metavar="action")
    sub.required = True
    sub.add_parser("init", description="make the [public] remote safe and install the push guard")
    e = sub.add_parser(
        "export",
        description="REF's tree as one commit on the public tip (refs/public/<branch>); "
        "prints the push",
    )
    e.add_argument("ref", nargs="?", default="", help="the commit to export (default HEAD)")
    e.add_argument("--tag", default="", help="also the annotated tag refs/public-tags/TAG")
    e.add_argument("--message", default="", help="the commit message (default [public] message)")
    e.add_argument(
        "--allow-dirty",
        action="store_true",
        help="export HEAD although the working tree has changes",
    )
    e.add_argument(
        "--fresh-root",
        metavar="OLD",
        default="",
        help="an approved history rewrite: a commit with no public parent, replacing the"
        " public tip OLD (its sha, 7+ digits); the guard allows that one push",
    )
    e.add_argument(
        "--drop-tags",
        action="store_true",
        help="with --fresh-root: the push also deletes the public repo's tags",
    )
    r = sub.add_parser(
        "pr",
        description="an upstream PR branch: upstream..REF audited commit by commit, into"
        " refs/public-pr/NAME for refs/heads/pr/NAME; prints the push",
    )
    r.add_argument("name", help="the branch is pr/NAME")
    r.add_argument("ref", nargs="?", default="", help="its tip (default HEAD)")
    u = sub.add_parser("audit", description="what a push would publish, checked")
    u.add_argument(
        "range", nargs="?", default="", help="BASE..TIP (default <remote>/<branch>..<tip>)"
    )
    u.add_argument(
        "--all",
        action="store_true",
        help="export mode: a dry-run export of HEAD; history mode: the whole history",
    )


def run(a):
    G = host.g()
    try:
        if a.action == "init":
            return init(Ctx(G, need_patterns=False))
        ctx = Ctx(G)
        if a.action == "export":
            return export(ctx, a)
        if a.action == "pr":
            return pr(ctx, a)
        if a.all and a.range:
            raise CliError("RANGE or --all, not both")
        return audit(ctx, a)
    except Cannot as e:
        print("%s public %s: %s" % (G.slug, a.action, e), file=sys.stderr)
        return 2
