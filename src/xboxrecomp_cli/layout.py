"""The standard layout of a game repo (docs/layout.md): what it tracks, what
it ignores, what never leaves it, and doctor's layout block.

The entries come from game.toml: a manifest key that names a path wins,
and the standard name is its default. The packaging files are the ones
`package` itself looks up for each target (package.required_files()), so
the two cannot drift. The private set and the refusal list are shared
with public.py, which drops the first and refuses the second.

report(game) is the layout block: problems and notes, kept apart from
doctor's own verdict. Only `public audit` and `public export` fail on its
problems: a player's clone that lacks a maintainer file still builds."""

import fnmatch
import os
import re
import struct
import subprocess

from . import manifest

# ── the entries ──────────────────────────────────────────────────────────

# Public documents a game may have; none is required.
OPTIONAL = ("docs", "CONTRIBUTING.md", "CONTRIBUTORS.md", "ROADMAP.md", "NOTICE", "TESTING.md")
# Private working notes: never exported, and never tracked in history mode
# (globs as glob_match() reads them). [public] exclude adds to these and
# cannot remove from them.
PRIVATE = (
    "TASKS.md",
    "RESUME*.md",
    "**/RESUME*.md",
    "OPEN_QUESTIONS.md",
    "WORKTREES.md",
    ".git-blame-ignore-revs",
    "_local/",
)
WORKFLOWS = ".github/workflows"
RELEASE_WORKFLOW = WORKFLOWS + "/release-wizard.yml"
CHECKS_WORKFLOW = WORKFLOWS + "/checks.yml"
GITIGNORE_TEST = "scripts/test_gitignore.py"
OPENSPEC = ("openspec/config.yaml", "openspec/specs", "openspec/changes")
# The marker the game's own .gitignore lines follow (the baseline above it).
GAME_LINES = "# --- this game ---"


def manual_source(m):
    return m["pipeline"]["exclude_manual"] or "src/recomp_manual.c"


def main_source(m):
    return m.get("layout", {}).get("main", "src/main.c")


def env_doc(m):
    return m["game"]["env_doc"] or "docs/env.md"


def required(game):
    """(path, why) of every entry the game must have, its conditional ones
    included, resolved through the manifest."""
    m, slug = game.m, game.slug
    out = [
        ("game.toml", "the manifest"),
        (slug, "the bootstrap"),
        (slug + ".cmd", "the bootstrap"),
        (slug + ".py", "the bootstrap"),
        ("pyproject.toml", "the tools environment"),
        ("uv.lock", "the tools environment (%s pins refresh)" % slug),
        (".gitignore", "the baseline ignores"),
        (".gitattributes", "the line endings"),
        ("README.md", "the front page"),
        ("CHANGELOG.md", "the release notes"),
        ("LICENSE", "the licence"),
        ("CMakeLists.txt", "the build"),
        (main_source(m), "the main C source ([layout] main)"),
        (manual_source(m), "the manual C source ([pipeline] exclude_manual)"),
        ("config/seed_functions.json", "the seeds"),
        ("config/setup-pins.json", "the download pins (%s pins refresh)" % slug),
        (GITIGNORE_TEST, "the baseline test"),
    ]
    if m["game"]["env_header"]:
        out.append((m["game"]["env_header"], "[game] env_header"))
    if m["pipeline"]["spin_waits"]:
        out.append((m["pipeline"]["spin_waits"], "[pipeline] spin_waits"))
    if "wizard" in m:
        out.append((RELEASE_WORKFLOW, "the wizard's release workflow ([wizard])"))
    if "public" in m:
        out.append((CHECKS_WORKFLOW, "the public checks ([public])"))
        out += [(p, "the design record ([public])") for p in OPENSPEC]
    if "package" in m:
        from . import package

        out += [(game.rel(p), why) for p, why in package.required_files(game)]
    return out


def known_top(game):
    """The top-level names that need no [layout] extra entry."""
    m = game.m
    names = {r.split("/")[0] for r, _ in required(game)}
    names |= set(OPTIONAL) | {"scripts", "config", "openspec", ".github", "src"}
    names |= {p.split("/")[0] for _, p in manifest._paths(m)}
    names |= set(m.get("layout", {}).get("extra", []))
    names.add(env_doc(m).split("/")[0])
    return names


def private_top(name):
    """Whether a top-level name is in the private set (known, never noted)."""
    return any(glob_match(p.rstrip("/"), name) for p in PRIVATE if "/" not in p.rstrip("/"))


# ── globs ────────────────────────────────────────────────────────────────


def _glob_re(pat):
    """A tracked-path glob as a regex: `*` and `?` stay inside one path
    segment, `**/` is any number of leading directories, `**` anything; a
    pattern ending in `/` is everything under that directory. Anchored at
    the top: `TASKS.md` is the top-level file only."""
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    if pat.endswith("/"):
        out += ".*"
    return re.compile(out + r"\Z")


def glob_match(pat, path):
    return bool(_glob_re(pat).match(path))


def any_glob(pats, path):
    return any(glob_match(p, path) for p in pats)


def private(game, path):
    """Whether the export drops path: the private set or [public] exclude."""
    extra = game.m.get("public", {}).get("exclude", [])
    return any_glob(PRIVATE, path) or any_glob(extra, path)


# ── the ignore baseline ──────────────────────────────────────────────────

# Data and bundle extensions: refused at any depth, without case (the
# union of the audits the games had). Each is also a baseline ignore line.
DATA_EXT = (
    "xbe iso xiso bmp png wav sav pack xpr xwb adx sfd ipk xsv xmv rws awd ppm raw "
    "dmg exe pdb nsi tar ico icns"
).split()
# Directories refused wherever they are, as the baseline ignores them
# anywhere; the ones below are refused at the top only (build*/ there is
# a build tree; openspec/changes/build-resources/ is not).
ANY_DEPTH_DIRS = ("_local", "saves", "runs", "bench-logs")
TOP_DIRS = ("dist", "bin")
# What `setup` makes at the top, refused as a folder or a file: a
# worktree's copy is a symlink to the main checkout's, one `git add -A`
# from being tracked.
SETUP_TOP = ("third_party", "external")


def baseline_samples(game):
    """A path each baseline line must ignore, from the manifest's values
    (git check-ignore --no-index; scripts/test_gitignore.py checks the same
    in the game's own gates)."""
    m = game.m
    gf, gen = m["data"]["game_files"], m["pipeline"]["gen"]
    gen_parent = gen.rsplit("/", 1)[0] if "/" in gen else "."
    b = m["build"]
    out = [
        gf + "/default.xbe",
        # Bare: in a worktree the dump, the clones and the venv are symlinks
        # to the main checkout's, and a `dir/` line matches a directory only.
        gf,
        gf + ".partial/x",
        gf + ".old/x",
        gen + "/recomp_0000.c",
        gen_parent + "/gen.key.json",
        m["pipeline"]["out"] + "/x",
        b["windows_dir"] + "/x",
        b["macos_dir"] + "/x",
        "build-x/x",
        "build-logs/x",
        ".venv/x",
        ".venv",
        ".venv-ghidra",
        "third_party/x",
        "third_party",
        "external/x",
        "external",
        "dist/x",
        "bench-logs/x",
        "bench-logs",
        "wt/x/game.toml",
        "_local/x",
        "runs/x",
        "saves/x",
        "game.toml.bak",
        "game.toml.bak-20261016",
        "a/hud-pack.toml",
        "a/CACHE/x",
        ".claude/settings.local.json",
        "CLAUDE.local.md",
    ]
    out += ["a/x." + e for e in DATA_EXT]
    return [p[2:] if p.startswith("./") else p for p in out]


# Never ignored: the manifest and the tests that guard the rest, and two
# paths an unanchored top-level line would catch (a change named build-*,
# a source folder named external).
NOT_IGNORED = (
    "game.toml",
    GITIGNORE_TEST,
    "scripts/x.py",
    "docs/images/x.png",
    "openspec/changes/build-x/x",
    "src/external/x",
)


# ── the refusal list ─────────────────────────────────────────────────────


def _under(path, d):
    return path == d or path.startswith(d.rstrip("/") + "/")


def refused_path(game, path):
    """Why path may never be published, whatever its content, or ""
    (the extension rule is data_ext(); its exemptions are the caller's)."""
    m = game.m
    segs = path.split("/")
    # Without case: a case-insensitive file system (macOS, Windows) checks
    # out Runs/ where runs/ was meant, and the baseline would not ignore it.
    low = [x.lower() for x in segs]
    if low[-1] == "hud-pack.toml":
        return "a HUD texture pack's manifest (made from game data)"
    gf, gen = m["data"]["game_files"], m["pipeline"]["gen"]
    if _under(path, gf):
        return "the dump ([data] game_files)"
    gen_parent, gen_name = (gen.rsplit("/", 1) + [""])[:2] if "/" in gen else ("", gen)
    if _under(path, gen):
        return "generated code ([pipeline] gen)"
    sib = path[len(gen_parent) + 1 :] if gen_parent and path.startswith(gen_parent + "/") else path
    if gen_parent or "/" not in path:
        if sib.split("/")[0].startswith(gen_name + "."):
            return "beside generated code (%s.*)" % gen
    if low[0] in SETUP_TOP or low[0].startswith(".venv"):
        return "%s (setup's clones and venv)" % segs[0]
    if low[0] in TOP_DIRS and len(segs) > 1:
        return "%s/ (bundles and binaries)" % segs[0]
    if len(segs) > 1 and fnmatch.fnmatchcase(low[0], "build*"):
        return "a build tree (%s/)" % segs[0]
    for d in ANY_DEPTH_DIRS:
        # The last name too: a tracked symlink to a shared bench-logs/
        # is that folder's entry.
        if d in low:
            return "%s/ (never tracked)" % d
    return ""


def data_ext(path):
    """The data or bundle extension of path, without case, or ""."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext if ext in DATA_EXT else ""


README_IMAGE = re.compile(r"^docs/images/[^/]+\.png\Z")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Chunks that carry text or camera data: a screenshot must hold pixels only.
PNG_TEXT = (b"tEXt", b"iTXt", b"zTXt", b"eXIf")


def png_problem(data):
    """Why a README image is not a plain PNG, or ""."""
    if not data.startswith(PNG_SIG):
        return "not a PNG"
    i = len(PNG_SIG)
    while i + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[i : i + 8])
        if kind in PNG_TEXT:
            return "carries a %s chunk (text or Exif): strip it" % kind.decode()
        if kind == b"IEND":
            return ""
        i += 12 + n
    return "truncated PNG"


def exempt(game, path, blob, upstream_blob=None, chunks=True):
    """Whether a path that would be refused for its extension or as a
    binary may be published: a README image (with chunks, a plain one:
    the audit's rule; doctor's leaves that to the audit), a [public] allow
    glob, or an [public] upstream_files path that equals the upstream's
    blob (upstream_blob(path) -> sha or None; blob -> (sha, bytes))."""
    p = game.m.get("public", {})
    if README_IMAGE.match(path):
        return not chunks or not png_problem(blob[1])
    if any_glob(p.get("allow", []), path):
        return True
    if any_glob(p.get("upstream_files", []), path):
        return upstream_blob is not None and upstream_blob(path) == blob[0]
    return False


def binary(data):
    """git's own test: a NUL byte in the first 8000 bytes."""
    return b"\0" in data[:8000]


# ── doctor's layout block ────────────────────────────────────────────────


def git(root, *args, input=None):
    r = subprocess.run(
        ["git", "-C", root, *args],
        input=input,
        capture_output=True,
    )
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def git_out(root, *args):
    rc, out, _ = git(root, *args)
    return out if rc == 0 else ""


def toplevel(root):
    rc, out, _ = git(root, "rev-parse", "--show-toplevel")
    if rc:
        return ""
    return os.path.realpath(out.strip())


def config_get(root, key, all_values=False):
    rc, out, _ = git(root, "config", "--get-all" if all_values else "--get", key)
    if rc:
        return [] if all_values else ""
    return out.splitlines() if all_values else out.strip()


def hooks_dir(root):
    """The <git common dir>/hooks the guard is installed in."""
    common = git_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.join(common, "hooks") if common else ""


HOOK_MARK = "# xboxrecomp-cli public guard"


def public_remotes(root, url):
    """{remote: (url, pushurls)} of every remote whose fetch or push URL is
    the same repo as url."""
    want = manifest.repo_id(url)
    out = {}
    for name in git_out(root, "remote").split():
        u = config_get(root, "remote.%s.url" % name)
        pushes = config_get(root, "remote.%s.pushurl" % name, all_values=True)
        if want and any(manifest.repo_id(x) == want for x in [u] + pushes):
            out[name] = (u, pushes)
    return out


def guard_problems(game, root):
    """The push guard of an export-mode game, where a remote with
    [public] url exists (public init sets each part)."""
    p = game.m["public"]
    problems = []
    remotes = public_remotes(root, p["url"])
    if not remotes:
        return []
    fix = " (%s public init)" % game.slug
    if set(remotes) != {p["remote"]}:
        problems.append(
            "remotes %s name the public repo; only [public] remote %r should%s"
            % (", ".join(sorted(remotes)), p["remote"], fix)
        )
    for name, (_, pushes) in sorted(remotes.items()):
        for pu in pushes:
            if pu != p["url"]:
                problems.append("remote %s: pushurl %r is not [public] url%s" % (name, pu, fix))
        if p["mode"] != "export":
            continue
        want = "refs/public/%s:refs/heads/%s" % (p["branch"], p["branch"])
        if config_get(root, "remote.%s.push" % name, all_values=True) != [want]:
            problems.append("remote %s: push refspec is not %s%s" % (name, want, fix))
        if config_get(root, "remote.%s.tagOpt" % name) != "--no-tags":
            problems.append("remote %s: tagOpt is not --no-tags%s" % (name, fix))
    if p["mode"] == "export":
        hook = os.path.join(hooks_dir(root), "pre-push")
        try:
            with open(hook, encoding="utf-8") as f:
                ours = HOOK_MARK in f.read()
        except OSError:
            ours = False
        if not ours:
            problems.append("no pre-push guard in %s%s" % (hook, fix))
    return problems


def maintainer_repo(game, root):
    """Whether `public init` or `public export` ever ran here: the [public]
    remote, or a refs/public* ref. A plain clone of the public repo has
    neither (its origin names the public repo), and the guard is not its
    business; the private repo keeps every guard problem."""
    if game.m["public"]["remote"] in git_out(root, "remote").split():
        return True
    return bool(
        git_out(
            root,
            "for-each-ref",
            "--count=1",
            "refs/public/",
            "refs/public-tags/",
            "refs/public-pr/",
        ).strip()
    )


def public_clone(game, root, remotes):
    """A plain clone of the public repo, whose guard is not its business:
    not a maintainer's repo, and HEAD is all public, reachable from a
    public remote's branches. In history mode a repo that tracks nothing
    of the private set passes too; in export mode that proves nothing (a
    new game tracks none of it yet), so a private repo that merely has the
    public URL under another remote name keeps every guard problem."""
    if maintainer_repo(game, root):
        return False
    if game.m["public"]["mode"] != "export" and not any(
        private(game, p) for _, _, p in tracked(root)
    ):
        return True
    for name in remotes:
        rc, out, _ = git(
            root,
            "for-each-ref",
            "--contains",
            "HEAD",
            "--count=1",
            "refs/remotes/%s/" % name,
        )
        if rc == 0 and out.strip():
            return True
    return False


def tracked(root):
    """[(mode, sha, path)] of the index."""
    out = []
    for rec in git_out(root, "ls-files", "-s", "-z").split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, sha, _ = meta.split()
        out.append((mode, sha, path))
    return out


def blob_bytes(root, sha):
    r = subprocess.run(["git", "-C", root, "cat-file", "blob", sha], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def upstream_blob_fn(game, root, ref):
    """upstream_blob(path) for exempt(): the blob sha at path in [public]
    upstream, as of ref's merge with it."""
    up = game.m.get("public", {}).get("upstream", "")
    if not up:
        return None

    def blob(path):
        rc, out, _ = git(root, "rev-parse", "-q", "--verify", "%s:%s" % (up, path))
        return out.strip() if rc == 0 else None

    return blob


def tracked_problems(game, root):
    out = []
    ub = upstream_blob_fn(game, root, "HEAD")
    for mode, sha, path in tracked(root):
        if mode == "160000":
            out.append("%s: a gitlink (embedded repo) is tracked" % path)
            continue
        why = refused_path(game, path)
        if not why and data_ext(path):
            if not exempt(game, path, (sha, b""), ub, chunks=False):
                why = "game data or a bundle (.%s)" % data_ext(path)
        if why:
            out.append("%s is tracked: %s" % (path, why))
    return out


# Setup-made clones and heavy trees the nested-repo walk skips: the
# toolkit and CLI clones live in external/ by design.
WALK_SKIP = {".git", "external", "third_party", "node_modules", "dist", "__pycache__"}


# The walk that digests a game folder outside git skips these names at
# any depth, and these at the top only: main tracks src/external/ and the
# like, and the baseline anchors its lines (/external, /dist/).
CONTENT_SKIP_ANY = {".git", "__pycache__"}
CONTENT_SKIP_TOP = {".xbr", "node_modules"}


def content_skip(game):
    """skip(rel, is_dir) for the walk that digests a game folder outside
    git (package.lib.walk_files): .git and __pycache__ anywhere, .xbr/
    (buildstate's records) and node_modules at the top, the pipeline's out
    and the build folders, the refusal list (setup's clones and venvs, the
    dump, generated code, the build trees, dist/, runs, saves) and the
    data extensions, as the baseline ignores them. The folder's .gitignore
    then drops the rest."""
    m = game.m
    tops = {m["pipeline"]["out"], m["build"]["windows_dir"], m["build"]["macos_dir"]}
    tops = {t[2:] if t.startswith("./") else t for t in tops}

    def skip(rel, is_dir):
        name = rel.rsplit("/", 1)[-1]
        if name in CONTENT_SKIP_ANY or ("/" not in rel and name in CONTENT_SKIP_TOP):
            return True
        if is_dir:
            return rel in tops or bool(refused_path(game, rel + "/x"))
        if refused_path(game, rel):
            return True
        return bool(data_ext(rel)) and not README_IMAGE.match(rel)

    return skip


def nested_repos(game, root):
    """Every worktree or repository under the root but the root's own."""
    found = set()
    real_root = os.path.realpath(root)
    for line in git_out(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            w = os.path.realpath(line[len("worktree ") :])
            if w != real_root and w.startswith(real_root + os.sep):
                found.add(w)
    skip = {
        os.path.realpath(game.path(p))
        for p in (game.m["data"]["game_files"], game.m["pipeline"]["gen"])
    } | {os.path.realpath(game.build_dir(t)) for t in ("windows", "macos")}
    for d, dirs, files in os.walk(root):
        if d != root and (".git" in dirs or ".git" in files):
            found.add(os.path.realpath(d))
            dirs[:] = []
            continue
        dirs[:] = [
            x
            for x in dirs
            if x not in WALK_SKIP
            and not x.startswith(".venv")
            and os.path.realpath(os.path.join(d, x)) not in skip
        ]
    return sorted(os.path.relpath(f, real_root).replace(os.sep, "/") for f in found)


def ignored(gitignore, paths):
    """The paths a .gitignore's text ignores, judged in an empty scratch
    repo: the game's tree may hold a symlink (a shared bench-logs/) that
    stops check-ignore, and this user's global excludes must not count."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
        with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(gitignore)
        r = subprocess.run(
            [
                "git",
                "-C",
                d,
                "-c",
                "core.excludesFile=",
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ],
            input="\0".join(paths).encode() + b"\0",
            capture_output=True,
        )
        if r.returncode not in (0, 1):
            raise RuntimeError("git check-ignore: %s" % r.stderr.decode("utf-8", "replace").strip())
        return {p for p in r.stdout.decode("utf-8").split("\0") if p}


def ignore_problems(game):
    try:
        with open(game.path(".gitignore"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []  # required()'s problem
    samples = baseline_samples(game)
    hit = ignored(text, samples + list(NOT_IGNORED))
    problems = [
        "%s is not ignored: .gitignore lacks a baseline line (docs/layout.md)" % s
        for s in samples
        if s not in hit
    ]
    problems += ["%s is ignored: it must be tracked" % s for s in NOT_IGNORED if s in hit]
    return problems


def template_problems(game):
    """The bootstrap and the workflows against this CLI's templates (a
    missing one is required()'s problem, not reported twice)."""
    from . import workflow, wrapper

    out = []
    ok, _ = wrapper.check(game)
    if not ok and os.path.exists(game.path(game.slug + ".py")):
        out.append(
            "%s.py differs from the CLI's template (%s wrapper --check)" % (game.slug, game.slug)
        )
    for rel, ok in workflow.check_bootstrap(game):
        if not ok:
            out.append("%s differs from the CLI's template (xbr new writes it)" % rel)
    for rel, ok, _ in workflow.check(game):
        if not ok and os.path.exists(game.path(rel)):
            out.append(
                "%s differs from the CLI's template (%s workflow --check)" % (rel, game.slug)
            )
    return out


def report(game, strict=False):
    """{"problems": [...], "notes": [...]}: the layout block of the working
    tree. Never raises unless strict: doctor runs it on any tree, a
    player's clone included, while the audit must not pass a block that
    did not run. Strict, the push guard is always checked; otherwise a
    plain public clone gets a note instead (maintainer_repo())."""
    if strict:
        return _report(game, strict)
    try:
        return _report(game, strict)
    except Exception as e:  # a broken tree must not break doctor
        return {"problems": [], "notes": ["layout not checked: %s" % e]}


def _report(game, strict=False):
    m, root = game.m, game.root
    problems, notes = [], []
    for rel, why in required(game):
        if not os.path.exists(game.path(rel)):
            if rel.startswith("openspec/") and "public" not in m:
                continue
            problems.append("missing %s (%s)" % (rel, why))
    if "public" not in m and not os.path.isdir(game.path("openspec")):
        notes.append("no openspec/ (required once the game has [public])")
    if m["game"]["env_header"] and not os.path.isfile(game.path(env_doc(m))):
        notes.append(
            "%s has no doc: every env key gets a row in %s" % (m["game"]["env_header"], env_doc(m))
        )
    pub = m.get("public")
    w = m.get("wizard")
    if pub and w and w["repo"] and manifest.repo_id(w["repo"]) != manifest.repo_id(pub["url"]):
        problems.append(
            "[wizard] repo %s is not the repo of [public] url %s" % (w["repo"], pub["url"])
        )
    problems += template_problems(game)
    problems += ignore_problems(game)
    top = toplevel(root)
    if top != os.path.realpath(root):
        notes.append(
            "not a git repository%s: the tracked-file and push-guard checks need one;"
            " package names the folder by its files (version cnogit<hex>)"
            % (" (inside %s)" % top if top else "")
        )
        return {"problems": problems, "notes": notes}
    if git(root, "rev-parse", "-q", "--verify", "HEAD^{commit}")[0]:
        notes.append(
            "no commits yet: package names the tree by its files (version cnocommit<hex>)"
            " until the first commit"
        )
    problems += tracked_problems(game, root)
    for n in nested_repos(game, root):
        problems.append(
            "%s: a worktree or repository inside the game repo (use the workspace's wt/)" % n
        )
    remotes = public_remotes(root, pub["url"]) if pub else {}
    if pub and not strict and remotes and public_clone(game, root, remotes):
        notes.append(
            "a clone of the public repo: the push guard is a maintainer's"
            " (%s public init, in the private repo)" % game.slug
        )
    elif pub:
        hp = config_get(root, "core.hooksPath")
        if hp:
            problems.append("core.hooksPath is %r: git would skip the push guard" % hp)
        problems += guard_problems(game, root)
    known = known_top(game)
    names = sorted({p.split("/")[0] for _, _, p in tracked(root)})
    for n in names:
        if n not in known and not private_top(n):
            notes.append("%s is not a standard entry: list it in [layout] extra" % n)
    return {"problems": problems, "notes": notes}


def text_lines(rep):
    """The block as doctor prints it."""
    lines = []
    for p in rep["problems"]:
        lines.append("  problem: " + p)
    for n in rep["notes"]:
        lines.append("  note:    " + n)
    head = "layout:     %s" % (
        "%d problem%s" % (len(rep["problems"]), "" if len(rep["problems"]) == 1 else "s")
        if rep["problems"]
        else "standard"
    )
    return [head] + lines
