# The game repo layout

What a game repo tracks, what it ignores, what never leaves it, and how a
release round reaches its public repo. `xbr new` writes this layout;
`mygame doctor` reports drift from it in its layout block; `mygame public
audit` and `public export` fail on that drift.

## Entries

`game.toml` defines the layout: where a key names a path, the key wins and
the name below is its default.

| Entry | Status | Notes |
|---|---|---|
| `game.toml` | required | |
| `<slug>`, `<slug>.cmd`, `<slug>.py` | required | the CLI's template, byte for byte (`wrapper --check`) |
| `pyproject.toml`, `uv.lock` | required | a dev group with pytest and ruff, `[tool.ruff]`, and `[tool.pytest.ini_options] testpaths = ["scripts"]` |
| `.gitignore`, `.gitattributes` | required | the baseline below, then the game's own lines |
| `README.md`, `CHANGELOG.md`, `LICENSE` | required | `CHANGELOG.md` is Keep a Changelog: a release tag's section is its notes |
| `CMakeLists.txt` | required | |
| the main C source | required | `[layout] main`, default `src/main.c` |
| the manual C source | required | `[pipeline] exclude_manual`, default `src/recomp_manual.c` |
| `config/seed_functions.json`, `config/setup-pins.json` | required | |
| `scripts/test_gitignore.py` | required | the one test every game has (Gates) |
| `openspec/config.yaml`, `openspec/specs/`, `openspec/changes/` | with `[public]` | without it, a note |
| the env doc | with `[game] env_header` | `[game] env_doc`, default `docs/env.md`; a note when missing |
| `.github/workflows/release-wizard.yml` | with `[wizard]` | the CLI's template (`workflow --check`) |
| `.github/workflows/checks.yml` | with `[public]` | the CLI's template (`workflow --check`) |
| `config/spin_waits.json`, `packaging/` files | when `[pipeline]` or `[package]` names them | a file with a template fallback is not required |
| `docs/`, `CONTRIBUTING.md`, `CONTRIBUTORS.md`, `ROADMAP.md`, `NOTICE`, `TESTING.md` | optional | `docs/images/*.png` are the only tracked images |
| `analysis/` | ignored | the pipeline's `out` |
| anything else at the top level | `[layout] extra` | otherwise a note |

`[layout] extra` is for a fork that keeps its upstream's files where the
upstream has them (a `regen.sh`, a `tools/` folder), so upstream's own
build and every upstream diff stay as they are.

**Names.** The slug is the command, the bootstrap's name, `pyproject`'s
`[project] name` and the prefix of the wizard's assets. `[public] url` is
the ssh form, `git@github.com:<owner>/<repo>.git`; `[wizard] repo` the https
form. The CLI compares two repo URLs by `<owner>/<repo>`, ignoring case, the
scheme and a trailing `.git`.

## The private set

Working notes that are never exported, and never tracked in history mode:

| Pattern | What |
|---|---|
| `TASKS.md` | the running task list |
| `RESUME*.md`, `**/RESUME*.md` | resume notes, inside openspec changes too |
| `OPEN_QUESTIONS.md` | a decision log |
| `WORKTREES.md` | worktree bookkeeping |
| `.git-blame-ignore-revs` | private-history shas: the export is a fresh commit |
| `_local/` | local notes and scratch (ignored as well) |

`[public] exclude` adds globs to the set and cannot remove from it. These
names never count as unknown top-level entries.

## The `.gitignore` baseline

The scaffold's `.gitignore` is the baseline, filled with the game's own
`[data] game_files`, `[pipeline] gen` and `out`, and `[build]` folders:

- the dump and its `.partial` and `.old` siblings; gen and its `gen.*`
  key files; the pipeline's `out`; the build trees, `build-*/` and
  `build-logs/`;
- `.venv/`, `third_party/`, `external/`, `dist/`, `bench-logs/`;
- `/wt/`, `_local/`, `runs/`, `saves/`, `*.bak`, `*.bak-*`;
- `hud-pack.toml`, `CACHE/` and the data extensions: `xbe iso xiso bmp png
  wav sav pack xpr xwb adx sfd ipk xsv xmv rws awd ppm raw dmg exe pdb nsi
  tar ico icns`;
- `.claude/settings.local.json`, `CLAUDE.local.md`.

The game's own lines go after the marker `# --- this game ---`. `/wt/` is a
safety net, not a place: worktrees live outside the repo, and doctor reports
any it finds.

## Gates

Every game runs the same gates, locally and in `checks.yml`:

```sh
uv run pytest scripts
uv run ruff check . && uv run ruff format --check .
```

`scripts/test_gitignore.py` (standard library only, so it runs before
`setup`) checks that a sample path for each baseline line is ignored,
filled from `game.toml`, and that `game.toml`, `scripts/` and
`docs/images/*.png` are not. It judges the rules in a scratch repo, so a
symlinked `bench-logs` or a global excludes file does not change the
answer.

`checks.yml` runs on pushes and pull requests to `main` with read-only
permissions, a pinned Python and actions pinned by commit, and needs no game
data or toolkit clone, so it runs on the exported tree.

## The refusal list

Never published, whatever the content. The export refuses (not drops) them,
so the fix lands in the private commit:

- the data and bundle extensions above;
- `hud-pack.toml` at any depth;
- `[data] game_files`, `[pipeline] gen` and its `gen.*` siblings;
- `dist/`, `bin/` and `build*/` at the top; `_local/`, `saves/`, `runs/` and
  `bench-logs/` at any depth, as a folder or a file (a symlink included);
- gitlinks (embedded repos);
- any other binary (a NUL in the first 8000 bytes);
- for `export`, text that is not UTF-8 (convert it in the private commit).

Names are matched without case, so `Runs/` and `DIST/` count.

Exempt from the extension and binary rules: `docs/images/*.png` with no
text or Exif chunks (`tEXt`, `iTXt`, `zTXt`, `eXIf`; strip them first),
`[public] allow`, and `[public] upstream_files` while each equals the
upstream's blob.

## Doctor's layout block

`doctor` prints a `layout:` line (`standard`, or the number of problems)
with each problem and note, and `--json` has them under `layout`. The block
never changes doctor's `next:`, its problems or its exit status: a player's
clone that lacks a maintainer file still builds.

Problems: a missing required entry; a baseline line that does not ignore its
sample; a tracked file on the refusal list; a worktree or repo inside the
tree; the bootstrap or a workflow that is not its template; with `[public]`,
a missing `checks.yml` or `openspec/`, a `[wizard] repo` naming another repo,
a set `core.hooksPath`, and in export mode a public remote without the push
guard (hook, push refspec, `--no-tags`) or with another `pushurl`.

Notes: no `openspec/` without `[public]`; an unknown top-level name; an env
header without its doc.

## Publishing

`[public]` names the public repo ([manifest.md](manifest.md#public)). The
table is published with the tree, so nothing private goes in it.

**The modes.**
- `export` (the default): each round is one fresh commit, built from the
  private tree, on the public tip. The private history keeps its own
  identity and may track the private set; none of it is published.
- `history`: the private branch itself is pushed. Every commit must carry
  `[public] author`, the private set must not be tracked, and a leak in any
  commit blocks the push until that history is rewritten. `export` refuses
  to run.

**A round, in export mode:**

```sh
mygame public init                  # once per clone
mygame public export --tag v2026.10.0
git push public refs/public/main:refs/heads/main refs/public-tags/v2026.10.0:refs/tags/v2026.10.0
```

`public init` finds every remote whose URL is `[public] url`'s repo, keeps
one under `[public] remote` and removes the rest (adds it when there is
none), sets its URL and push URL to `[public] url` and `tagOpt --no-tags`,
and in export mode the push refspec `refs/public/<branch>:refs/heads/<branch>`
and the pre-push guard. The guard sits in the repo's hooks folder (an
existing `pre-push` is kept as `pre-push.chained` and runs after it). For a
push to the public repo, by the remote's name or any URL form, it allows
only `refs/public/<branch>` as a fast-forward of the branch, and
`refs/public-tags/TAG` annotated tags on that history, pushed as
`refs/tags/TAG` with the same name; it refuses deletes,
any other commit (by ref or raw sha) and other refs. A set `core.hooksPath`
makes git skip it: init says so and exits 1.

`public export [REF]`:
1. fetches the remote, and refuses unless its branch is what `git ls-remote`
   says the public repo has;
2. lists REF's tree. REF must be HEAD (the default), since the layout
   check reads the working tree; a dirty HEAD needs `--allow-dirty`;
3. drops the private set, refuses the refusal list;
4. applies the rewrite rules to every text blob, then checks every exported
   text blob and path name against the private patterns and the `!fail`
   checks;
5. commits the tree with `[public] author` as author and committer, in an
   environment with no global or system git config, never signed. The parent is the
   public tip (none on an empty repo); with `[public] upstream`, the newest
   upstream commit REF contains becomes a second parent when the public tip
   lacks it. The message is `--message`, `[public] message` or
   `<name> static recompilation: <date> round`. A tree equal to the tip's
   is "nothing to publish";
6. with `--tag TAG`, makes the annotated tag `refs/public-tags/TAG`, with
   the tag's `CHANGELOG.md` section as its message. A public tag never
   shares a private tag's name;
7. audits the result, and only then moves `refs/public/<branch>` (and the
   tag), appends `<public sha> <REF sha> <date>` to the map file
   `<slug>.map`, and prints what it excluded and rewrote, the rules it
   read (`rewrite: <path> (N rules, M checks)`, or `rewrite: none`) and the
   push. The CLI never pushes.

`public audit [BASE..TIP]` checks what a push would publish. The default
range is `<remote>/<branch>..refs/public/<branch>` (history mode:
`..<branch>`):
- the content diff against the base (the empty tree when there is none),
  and in history mode each commit, merges included, against its first
  parent: added lines for home-directory paths, links into Claude's web
  app and the private patterns; the added and changed paths for the
  refusal list and the private patterns. The diff reads binaries as text,
  ignores every attributes file (a `-diff` or `binary` attribute hides
  nothing) and decodes quoted path names; an added line under a header it
  could not read is a hit;
- each commit's message, and its author and committer, which must be
  `[public] author`; commits reachable from `[public] upstream` are
  upstream's and skipped;
- each public tag not yet on the remote: its tagger and message;
- the tip's tree for the refusal list and the private set (in export
  mode a tip not made by `public export` may still hold it), and the
  layout block's problems. The layout block reads the working tree, so it
  counts only when the tip is HEAD or the map file records it as HEAD's
  export; otherwise, and when the block itself fails, the audit exits 2
  after reporting any hits;
- the tip's `[cli]` and `[toolkit]` tags, which must be on their public
  URLs and name the pinned commits, or a public clone could not run
  `setup`.

`public audit --all` is a dry-run export of HEAD in export mode (the tree,
the rewrites, the refusals, the layout and the pins; no ref moves, and the
private history is not read), and every tracked file plus the whole history
in history mode.

Exit 1 on a hit, 2 when a check cannot run (no private patterns, no network
for a pin).

## Private inputs

The patterns and the rewrite rules are private, so they live outside every
repo, in `$XBR_PUBLIC_DIR`; the CLI refuses a directory inside the game's
work tree. One directory can serve several games and the CLI's own repo.

- **`audit-private`**: one extended regex per line, matched without case;
  `#` starts a comment. The first of `$XBR_PUBLIC_DIR/audit-private`,
  `$XBR_AUDIT_PRIVATE`, and `<git dir>/info/audit-private` that holds a
  pattern is read (a file of comments counts as missing, here and in the
  CLI's `scripts/audit-public.sh`). Without one, `audit` and `export`
  refuse to run (exit 2), as they do for a file that is not UTF-8. The CLI
  reads the patterns with Python `re` and the script with `grep -E`, so
  keep to what both accept: no `\d`, `\w` or `\s` (use `[0-9]` and
  bracket classes) and no lookarounds.
- **`rewrite`**: `$XBR_PUBLIC_DIR/rewrite`, else
  `<git dir>/info/xbr-public/rewrite`. Optional without `XBR_PUBLIC_DIR`;
  with it set, a missing `rewrite` there refuses the export (exit 2), so a
  wrong folder does not publish an unrewritten tree.
- **`<slug>.map`**: written by `export`, in the same folder as `rewrite`.

### The rewrite format

Ordered rules, one per line; `#` lines and blank lines are skipped. Each
regex is Python `re` syntax, matched as ASCII.

| Line | Effect |
|---|---|
| `REGEX<TAB>REPLACEMENT` | applied to each line of each text blob, the line ending kept |
| `!blob REGEX<TAB>REPLACEMENT` | applied to the whole blob, so a match may span lines (`(?m)` for per-line `^` and `$`, `(?s)` for a `.` that takes newlines) |
| `!fail REGEX` | a check, without case: a match on any line of an exported text blob, or in a path name, fails the export |

The replacement takes `\1` to `\9` and `\g<N>` for groups (not perl's
`$1`), `\u` to upper-case the next character, and `\n`, `\t`, `\\`. A
group the regex does not have fails when the rules load, as `rewrite:LINE`.

```text
# the bench host's name, in prose
\bbenchbox\b	the Linux host
!blob (?s)\n\nNotes from the bench host:\n.*?\n\n	\n\n
!fail \bbenchbox\b
```

### Known limits

- `public init` and the guard know the public repo by `[public] url`. An
  old URL that GitHub redirects after a rename is not recognised as it, so
  remove such remotes by hand.
- A rewrite rule changes the text it matches and nothing around it: a
  replacement for a session or host name keeps its case as written, so a
  sentence that starts with the name may need its own rule.
