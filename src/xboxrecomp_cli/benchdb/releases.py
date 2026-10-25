"""Releases: the published tags of each game, their private shas and pins,
and which builds belong to them (design D10).

The release commit is rarely the benched one (it is made after the
round's benches), and builds carry the private toolkit sha while
game.toml pins the public fork's. So a build B matches release R on its
inputs, not its shas:
1. game: B's sha is R's private sha or an ancestor, B's tree was clean,
   and nothing under [release] inputs differs (nor game.toml's [toolkit],
   [cli], [pipeline] and [build] tables);
2. toolkit: B's tree was clean, and its sha is R's private toolkit sha or
   differs from it only in docs;
3. CLI: B's sha is R's [cli] commit or differs only outside src/.
A target with no matching build gets a stand-in: the newest integration
build before R's date, marked so it is never read as measured."""

import datetime
import fnmatch
import os
import subprocess
import sys
import tomllib

from . import ingest, retain

DEFAULT_INPUTS = ("src", "config", "CMakeLists.txt", "cmake")
PIN_TABLES = ("toolkit", "cli", "pipeline", "build")


class Git:
    """git -C repo, with each answer cached for one refresh."""

    def __init__(self, repo):
        self.repo = repo
        self.cache = {}

    def run(self, *args, ok=(0,)):
        key = args
        if key not in self.cache:
            try:
                r = subprocess.run(
                    ["git", "-C", self.repo, *args], capture_output=True, text=True, timeout=120
                )
                self.cache[key] = (r.returncode, r.stdout)
            except (OSError, subprocess.TimeoutExpired):
                self.cache[key] = (-1, "")
        return self.cache[key]

    def out(self, *args):
        rc, out = self.run(*args)
        return out if rc == 0 else None

    def quiet(self, *args):
        """True when `git diff --quiet ...` says equal; False when not or on error."""
        rc, _ = self.run("diff", "--quiet", *args)
        return rc == 0

    def ancestor(self, a, b):
        rc, _ = self.run("merge-base", "--is-ancestor", a, b)
        return rc == 0

    def has(self, sha):
        return bool(sha) and self.run("cat-file", "-e", sha + "^{commit}")[0] == 0


def iso(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tags(git, glob):
    """refs/public-tags/<tag> -> (public commit, tag date)."""
    out = git.out(
        "for-each-ref",
        "--format=%(refname:strip=2) %(*objectname) %(objectname) %(creatordate:iso-strict)",
        "refs/public-tags/",
    )
    tags = {}
    for line in (out or "").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        name = p[0]
        if not fnmatch.fnmatch(name, glob):
            continue
        # An annotated tag peels to its commit (*objectname); a light tag
        # leaves that field empty and is the commit itself.
        commit, date = (p[1], p[3]) if len(p) == 4 else (p[1], p[2])
        tags[name] = (commit, iso(date))
    return tags


def remote_tags(git, remote, glob):
    """git ls-remote --tags <remote>: tag -> public commit (peeled)."""
    rc, out = git.run("ls-remote", "--tags", remote)
    if rc != 0:
        return None
    tags = {}
    for line in out.splitlines():
        try:
            sha, ref = line.split("\t")
        except ValueError:
            continue
        name = ref[len("refs/tags/") :]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if not fnmatch.fnmatch(name, glob):
            continue
        if peeled or name not in tags:
            tags[name] = sha
    return tags


def read_map(path, cols=(0, 1, 2)):
    """Lines of whitespace-separated shas -> {first: rest}."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and not line.startswith("#"):
                    out[p[cols[0]]] = p[1:]
    except OSError:
        pass
    return out


def export_map(cfg, slug):
    """<public> <private> <date> -> {public: (private, date)}."""
    if not cfg.export_maps:
        return {}
    m = read_map(os.path.join(cfg.export_maps, slug + ".map"))
    return {k: (v[0], v[1] if len(v) > 1 else None) for k, v in m.items()}


def toolkit_map(cfg):
    """map.txt's <private> <public> -> {public: private}."""
    if not cfg.toolkit_map:
        return {}
    return {v[0]: k for k, v in read_map(cfg.toolkit_map).items()}


def by_prefix(mapping, sha):
    """A map lookup that takes a short sha (pins may be abbreviated)."""
    if not sha:
        return None
    if sha in mapping:
        return mapping[sha]
    hits = [v for k, v in mapping.items() if k.startswith(sha) or sha.startswith(k)]
    return hits[0] if len(hits) == 1 else None


def game_toml(git, sha):
    text = git.out("show", "%s:game.toml" % sha)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def pins(doc):
    doc = doc or {}
    return doc.get("toolkit", {}).get("commit"), doc.get("cli", {}).get("commit")


def refresh(db, cfg, game=None, remote=True, say=print):
    """Reads every game's tags and export map again, writes the releases
    rows, then matches builds (match()). Returns {game: [tags]}."""
    tkmap = toolkit_map(cfg)
    seen = {}
    for slug, repo in sorted(cfg.games.items()):
        if game and slug != game:
            continue
        gm = ingest.settings(cfg, slug)
        glob = gm["release"]["tag_glob"]
        git = Git(repo)
        local = local_tags(git, glob)
        far = None
        if remote:
            rname = gm.get("public", {}).get("remote", "public") if "public" in gm else "public"
            far = remote_tags(git, rname, glob)
            if far is None:
                say("benchdb: %s: ls-remote %s failed; local tags only" % (slug, rname))
        emap = export_map(cfg, slug)
        tags = dict((t, (c, d)) for t, (c, d) in local.items())
        for t, c in (far or {}).items():
            if t not in tags:
                tags[t] = (c, None)
        with db.write() as w:
            old = {r["tag"]: r for r in w.sql("SELECT * FROM releases WHERE game = ?", (slug,))}
            for tag, (public, date) in sorted(tags.items()):
                hit = emap.get(public)
                if not hit:
                    w.release(
                        slug,
                        tag,
                        old.get(tag, {}).get("game_sha") or "",
                        "unmapped",
                        public_sha=public,
                        published=date,
                    )
                    continue
                private, mdate = hit
                tk_pin, cli_pin = pins(game_toml(git, private))
                w.release(
                    slug,
                    tag,
                    private,
                    "published",
                    public_sha=public,
                    published=date or iso(mdate),
                    toolkit_pin=tk_pin,
                    toolkit_sha=by_prefix(tkmap, tk_pin) or tk_pin,
                    cli_sha=cli_pin,
                )
            # A tag gone from both sides keeps its row; when ls-remote
            # failed, only the local side was read, so nothing is withdrawn.
            if far is not None or not remote:
                for tag in old:
                    if tag not in tags and old[tag]["state"] != "withdrawn":
                        w.sql(
                            "UPDATE releases SET state = 'withdrawn' WHERE game = ? AND tag = ?",
                            (slug, tag),
                        )
        seen[slug] = sorted(tags)
    match(db, cfg, game)
    return seen


def inputs_of(gm):
    return list(gm["release"]["inputs"]) or list(DEFAULT_INPUTS)


def game_match(git, b, r, gm):
    if not b["game_sha"] or b["game_dirty"] or not r["game_sha"]:
        return False
    if not git.has(b["game_sha"]) or not git.ancestor(b["game_sha"], r["game_sha"]):
        return False
    if b["game_sha"] == r["game_sha"]:
        return True
    if not git.quiet(b["game_sha"], r["game_sha"], "--", *inputs_of(gm)):
        return False
    tb, tr = game_toml(git, b["game_sha"]) or {}, game_toml(git, r["game_sha"]) or {}
    return all(tb.get(t) == tr.get(t) for t in PIN_TABLES)


def toolkit_match(tgit, b, r):
    if not b["toolkit_sha"] or b["toolkit_dirty"] or not r["toolkit_sha"]:
        return False
    if b["toolkit_sha"].startswith(r["toolkit_sha"]) or r["toolkit_sha"].startswith(
        b["toolkit_sha"]
    ):
        return True
    return tgit is not None and tgit.quiet(
        b["toolkit_sha"], r["toolkit_sha"], "--", ".", ":!docs", ":!*.md"
    )


def cli_match(cgit, b, r):
    if not b["cli_sha"] or not r["cli_sha"]:
        return False
    if b["cli_sha"].startswith(r["cli_sha"]) or r["cli_sha"].startswith(b["cli_sha"]):
        return True
    return cgit is not None and cgit.quiet(b["cli_sha"], r["cli_sha"], "--", "src")


def when(b):
    """A build's time as UTC seconds, or None: times are compared parsed,
    since a Z, a +00:00 and a naive time sort wrongly as text."""
    return retain.parse_time(b["started"] or b["ended"] or "")


def pub(r):
    return retain.parse_time(r["published"] or "")


def match(db, cfg, game=None):
    """Recomputes builds.release, release_match and after_release for each
    game (D10). Returns {(game, tag): {target: (build id, match)}}."""
    tgit = Git(cfg.toolkit) if cfg.toolkit and os.path.isdir(cfg.toolkit) else None
    cgit = Git(cfg.cli) if cfg.cli and os.path.isdir(cfg.cli) else None
    result = {}
    slugs = sorted(set(cfg.games) | {r["game"] for r in db.sql("SELECT DISTINCT game FROM builds")})
    for slug in slugs:
        if game and slug != game:
            continue
        gm = ingest.settings(cfg, slug)
        repo = cfg.games.get(slug)
        git = Git(repo) if repo and os.path.isdir(repo) else None
        rels = db.sql(
            "SELECT * FROM releases WHERE game = ? AND state = 'published' "
            "ORDER BY coalesce(published, '') DESC, tag DESC",
            (slug,),
        )
        builds = db.sql("SELECT * FROM builds WHERE game = ? AND ok IS NOT 0", (slug,))
        # With no repo to match against (a removed worktree, an unmounted
        # drive, --config on another machine), keep the assignment the db
        # has: a recompute would clear every real match and leave only
        # stand-ins, and retention would then let the release builds' runs go.
        shas = {b["game_sha"] for b in builds if b["game_sha"]}
        if rels and shas and (git is None or not any(git.has(x) for x in sorted(shas))):
            print(
                "benchdb: WARNING: %s: cannot read the game repo (%s); its release "
                "matches are kept as they were" % (slug, repo or "not in [games]"),
                file=sys.stderr,
            )
            continue
        assign = {}
        for r in rels:
            for b in builds:
                if b["id"] in assign or git is None:
                    continue
                if (
                    game_match(git, b, r, gm)
                    and toolkit_match(tgit, b, r)
                    and cli_match(cgit, b, r)
                ):
                    assign[b["id"]] = (r["tag"], "match")
        for i, r in enumerate(reversed(rels)):
            # The stand-in comes from the builds since the previous release,
            # so two releases with no build between them share none.
            prev = rels[len(rels) - i] if i > 0 else None
            have = {b["target"] for b in builds if assign.get(b["id"], (None,))[0] == r["tag"]}
            for target in sorted({b["target"] for b in builds} - have):
                cands = [
                    b
                    for b in builds
                    if b["target"] == target
                    and b["lineage"] == "integration"
                    and b["id"] not in assign
                    and pub(r) is not None
                    and when(b) is not None
                    and when(b) < pub(r)
                    and not (prev and pub(prev) is not None and when(b) < pub(prev))
                    and b["kind"] in ("full", "incremental", "unknown")
                ]
                if cands:
                    b = max(cands, key=lambda x: (when(x), x["id"]))
                    assign[b["id"]] = (r["tag"], "stand-in")
        with db.write() as w:
            w.sql(
                "UPDATE builds SET release = NULL, release_match = NULL, after_release = NULL "
                "WHERE game = ?",
                (slug,),
            )
            for bid, (tag, how) in assign.items():
                w.sql(
                    "UPDATE builds SET release = ?, release_match = ? WHERE id = ?", (tag, how, bid)
                )
                result.setdefault((slug, tag), {})
            for b in builds:
                if b["id"] in assign:
                    continue
                before = [
                    r
                    for r in rels
                    if pub(r) is not None and when(b) is not None and pub(r) < when(b)
                ]
                if before:
                    w.sql(
                        "UPDATE builds SET after_release = ? WHERE id = ?",
                        (before[0]["tag"], b["id"]),
                    )
        for bid, (tag, how) in assign.items():
            b = next(x for x in builds if x["id"] == bid)
            result[(slug, tag)][b["target"]] = (bid, how)
    return result


def listing(db, game=None):
    q = "SELECT * FROM releases"
    args = []
    if game:
        q += " WHERE game = ?"
        args.append(game)
    out = []
    for r in db.sql(q + " ORDER BY game, coalesce(published, ''), tag", args):
        r["builds"] = db.sql(
            "SELECT id, target, host, release_match, exe_sha256 FROM builds "
            "WHERE game = ? AND release = ? ORDER BY target, id",
            (r["game"], r["tag"]),
        )
        out.append(r)
    return out


def after_tag(game_root, say=print):
    """`public export --tag` hook: refresh the releases of the game at
    game_root from its local tags (the new one is not pushed yet). Only
    with a benchdb.toml that lists the game, and warn-only: the export
    already succeeded, and a db problem must not read as its failure."""
    from . import config, store

    try:
        cfg = config.load(cwd=game_root)
        if not cfg.path:
            return None
        real = os.path.realpath(game_root)
        slug = next((s for s, r in sorted(cfg.games.items()) if os.path.realpath(r) == real), None)
        if not slug:
            return None
        db = store.open(cfg=cfg)
        return refresh(db, cfg, game=slug, remote=False, say=say)
    except Exception as e:  # noqa: BLE001 (warn-only hook)
        say("benchdb: releases refresh after the tag failed: %s" % e)
        return None
