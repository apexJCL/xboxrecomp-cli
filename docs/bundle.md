# Bundles and patches

What `<game> package <target>` writes, what the installers leave on the
player's machine, and how a patch finds and checks its base. The
`game.toml` side is in [manifest.md](manifest.md).

A **bundle** is the full install for one target. A **patch** is the
update from one installed version to the next. It ships only the files
that changed and refuses any install that is not that exact version.
Both carry the same `manifest.json` for the new version, so a patched
install and a fresh one end in the same state.

## manifest.json

Every bundle and every patch carries one. It also sits in every
installed version: `versions/<v>/` on steamos and Windows, and
`Contents/Resources/` in the macOS app. `SHA256SUMS` beside it lists the
same files as `<sha256>  <path>` lines.

| Field | Meaning |
|---|---|
| `schema` | `2`. Readers take 1 and 2; 2 only adds `source` and `volatile`. A schema 1 manifest is still a usable patch base. |
| `product`, `name` | The game's product id and display name. A base of another product is refused. |
| `version` | `<yyyymmdd.hhmm>-c<game7>-t<toolkit7>-g<gen8>`, then `-o<6>` for a non-stock build and `-dirty<hash>` for uncommitted trees. A tree with no commit to name has `nogit<6>` (not a git repository) or `nocommit<6>` (a repo before its first commit) in place of its sha, 6 hex of its files' digest, and is always `-dirty`: `20261018.2310-cnogit3fa2c1-tfe52446-g209e11c4-dirty1a2b3c4d`. |
| `target` | `windows-x86_64`, `steamos-x86_64-proton` or `macos-arm64`. |
| `built` | UTC build time. |
| `volatile` | Fields two builds of the same inputs may differ in (`built`, `sources.*.branch`). Everything else is equal between them. |
| `sources` | `commit`, `branch`, `dirty`, `dirty_paths` for the game, the toolkit and the CLI. A tree with no commit has `commit` and `branch` `none`, `dirty_paths` its file count, and `content`: the sha256 of its non-ignored files' names and sha256s, a symlink's target in place of a sha256 (`git ls-files --cached --others --exclude-standard` before the first commit; outside git a walk that skips the dump, generated code, builds, the top-level toolchain clones and `.xbr/`, then applies the folder's `.gitignore`). A game folder inside another repo counts as outside git. |
| `source` | Where the game tree came from (below). |
| `gen` | `sha256` and `files` of the generated code's listing. |
| `build` | `type`, `enhance`, `gen_opt`, `compiler`, `overrides`. |
| `data_layout` | `1`. A different value means a full installer: patches never cross it. |
| `files` | `{path, sha256, size}` for every file in the bundle, game files included (under `game_files/`), except `manifest.json` and `SHA256SUMS`. |
| `game_tree` | The identity of the game files the bundle was built from: `xbe_sha256` (the game's XBE), `files` (how many `game_files/` entries) and `listing` (the sha256 of their sorted `path sha256 size` lines). Older manifests lack it; package then computes it from `files`. |
| `root` | Windows only, outside `files`: `stub_abi`, and `stub` and `apply` as `{sha256, size}` (the root tools, below). |
| `enhance` | `hud_pack`: the HUD pack the build ships, as `{sha256, scale, entries, made_with}`, or absent for none. `made_with` is what makes the same pack again: `scale`, `languages`, `only`, `font_sha256`, `config` (the `[enhance.hud]` digest) and `cli`, from the `hud-pack.toml` beside the pack. No paths. |
| `notice` | The personal-use notice. |

`source` is one of:

- `{"kind": "tag", "repo", "tag", "commit"}`: the wizard's release clone,
  at the commit it marked.
- `{"kind": "local", "via": "checkout" | "bundle", "commit", "ref"}`: the
  wizard's clone of a developer's checkout or git bundle.
- `{"kind": "dev", "commit"}`: anything else, such as a developer's own
  tree or a marked clone that was moved or edited.

It never holds a path or a `file://` origin.

## patch.json

A patch's description of itself, at the top of every patch:

```json
{
  "schema": 1,
  "base_version": "20261016.1200-c1a2b3c4-t...-g...",
  "base_manifest_sha256": "<sha256 of the base's manifest.json bytes>",
  "version": "20261017.0900-c5d6e7f8-t...-g...",
  "changed": [{"path": "...", "sha256": "...", "size": 1}],
  "new": [],
  "deleted": [],
  "kept": [],
  "game_data": false
}
```

- `changed`, `new`, `deleted` and `kept` are sorted by path and hold
  every file of either version exactly once. `deleted` carries the base's
  entry.
- Only `changed` and `new` ship. `kept` files are checked on the machine,
  never copied.
- `game_data` is true when any `game_files/` path is changed, new or
  deleted.
- Patches ship whole files, never binary deltas.

## How package chooses

`package <target>` makes a patch when it finds a usable base and the
patch is under half the full bundle's size. Otherwise it makes the full
bundle. `--full` always makes the bundle. `--plan --json` lists the
bases it can see for the wizard to offer. Each row has its patch size,
`id` (the update ID: the first 8 hex of the manifest's sha256, which
`install.sh status` prints too), `usable`, `why` (the reasons it
cannot take this update) with `why_codes` (one code per reason, below),
`game_files` (`{changed, new, deleted}`: how many game files the patch
would change, add and remove), `tweaks` (`{"hud_pack": {sha256, scale,
entries} | null}`) and `seed_config`. Its `output` predicts what the
run makes (`kind` full, patch or none, `why` and `why_codes`, the `base`, and the
`installed` version on this host with its folder as `path`), judged
against the newest bundle record; `full_size` is that record's size.
`import` says whether the run must import the game files again
(`needed`, `why`), and `config_overrides` lists the `textures.pack`
line of this host's `config.toml` that would override the build's
choice. `disk` lists each volume the run writes to (`path`, `free`,
`need` in bytes with 1 GiB of slack, and `stages`, each stage's share);
when one is short, `output` is `kind` none with `why_codes` `["disk"]`. The run decides again on the real manifest.

`--patch-from` is strict: the named base gets a patch or a refusal,
never a full bundle the caller did not ask for (`--patch-from X (V):
REASON; make the full bundle with --full`). Its plan says `kind` none
with the reason. A refusal ends with a line `<name>: refused: CODE`, so
a front end can tell it from a failure without reading the message.

The codes are `product`, `target`, `data_layout`, `entries` (a file
without its sha256 or size), `in_place` and `stub_abi` (Windows),
`game_files` (built from other game files), `game_deletes` (the patch
would remove game files), `same_version`, `share` (the patch would be
half the full bundle or more), `unreadable`, `hud_guard`, `hud_keep`
`no_base` and `disk` (a volume has less free space than the run's
stages need; `--ignore-disk-space` overrides it). A front end shows the message for a code it does not
know.

The base is, in order:

1. `--patch-from`: a `manifest.json`, an installed version folder, an
   `.app`, a bundle tar, or the sha256 prefix (12 hex or more) of a bundle
   record;
2. the version installed on this host;
3. a record. Every bundle the folder made is recorded under
   `.git/xbr/bundles/<target>/<sha256>.json`, but a record is used as a
   base only when `--patch-from` names it.

A base is usable when the product, the target and `data_layout` are
equal, and every file it lists has a sha256 and a size. On Windows it
must also come from the versioned layout with an equal `stub_abi`.
These need only the base's manifest and the game's settings, so
`--patch-from` checks them before anything is built, next to the HUD
pack check below. The rest needs the new manifest, so it is judged
after the build:

- The game tree must be the base's (`game_tree`), unless this run
  imported the game files with `--dump`: a base built from other game
  files is refused ("built from other game files").
- A patch that would remove `game_files/` entries is refused unless
  `--allow-game-file-deletes` is given. A base whose manifest records no
  `game_tree` is refused for it even with the flag, since nothing says
  the removal is the game's.
- A base that is already this version is not used.

## The HUD pack of a patch

A base that ships a HUD pack would see a patch made without one delete
it. So when the run's base carries a pack, `package` refuses, before
anything is built, unless one of three flags says what to do:

- `--hud-pack [DIR]`: a new pack, made now by `enhance hud`;
- `--keep-hud-pack`: the base's pack, the same bytes;
- `--clear-hud-pack`: no pack.

A kept pack is found by its sha256: in the pack store
(`.git/xbr/packs/<sha256>.pack`, which keeps the packs of each target's
3 newest bundle records and is listed by `clean` with the build state),
then in `dist/enhance/hud/`, then made again from the base's
`made_with` (the same `[enhance.hud]` settings and the same font), and
used only when its sha256 is the base's. When it can be found nowhere
and not made again, `--keep-hud-pack` refuses and names the other two.

## The appliers' base check

Every applier runs the same check before it writes anything:

1. The install's **current** version must be `base_version`. A version
   that is installed but not current does not count.
2. The sha256 of that version's `manifest.json` must be
   `base_manifest_sha256`.
3. Every `kept` file must match, by size and then by sha256. Program
   files are checked in the current version, game files in the install's
   game files.

On any mismatch the applier exits non-zero and changes nothing:

> This update is for A; this install is B. Use the full installer for C.

B is the found version, `B with another manifest`, or `not installed`.
A kept file that does not match is named instead:

> This update is for A, but PATH does not match it. Use the full installer for C.

Game files are synced by the patch: the changed and new ones are written
to `<name>.partial`, flushed and renamed over the old file. Only the
deleted ones are removed, and any other file is left alone. On steamos
and Windows the game files are unversioned, so a rollback does not
restore them. A patch that changes them warns first:

> this update also changes game data; rolling back will not undo that

It goes on only after a yes, or with `--yes` when nothing can ask.

After an install or a patch, an applier reads the player's
`config/config.toml`. A `textures.pack` line there wins over the
build's `config.default.toml`, so when it names another pack than the
build's, or a pack the build no longer ships, the applier prints a
note and leaves the file alone:

> note: your config/config.toml sets textures.pack = "V", which overrides this build's HUD pack; remove that line to use it

## steamos

The bundle is `<App>-<version>-steamos.tar`. Unpack it and run
`./install.sh`. The patch is
`<App>-<version>-steamos-patch-from-<short>.tar`. It holds `install.sh`,
`install_lib.py`, `patch.json`, the new manifest and `SHA256SUMS`, and
the shipped files. `./install.sh` on a patch folder runs `install.sh
patch`.

Under the root:

```
<App>                  the stable launcher (the Steam shortcut's target)
current -> versions/<v>
versions/<v>/          program files, hard-linked against the previous version
game_files/            unversioned
state/                 history, layout, umu-run, runtime-update, launch.lock
prefix/                the Wine prefix
hdd/ config/ logs/     the player's: never written by install, patch or rollback
```

- `install.sh patch` checks the base, then builds `versions/<v>.partial`.
  Kept files are hard links from the current version and shipped files
  come from the patch. It syncs the game files, then moves `current` in
  one rename. It runs the same pre-steps and Proton step as `install`.
- `rollback [VERSION]`, `status` and `list` work on patched installs as
  on full ones. `status` prints the current version's `update id:` (the
  first 8 hex of its manifest's sha256, the ID the wizard shows) and its
  `tweaks:` (`HUD pack 4x, N entries (sha8)` or `none`). `--keep N` (default 3) prunes old versions but never
  removes the current one or the one it came from.
- A patch is refused while the game runs (`state/launch.lock`).

## Windows

The bundle is a folder holding `setup.exe` and `game_files\` (NSIS caps
an installer at 2 GB). The patch is the folder
`<App>-<version>-windows-patch-from-<short>\`:

```
apply.exe
patch.json
patch.lst              what apply.exe reads (below)
files\                 the shipped files, manifest.json, SHA256SUMS
```

Under `%LOCALAPPDATA%\Programs\<App>\`:

```
<App>.exe              the root stub: runs versions\<current.txt>\<App>.exe
current.txt            the current version's folder name
versions\<v>\          program files, the version's launcher, manifest.json, SHA256SUMS
game_files\            unversioned
apply.exe              the applier, replaced by each patch after its switch
state\history.txt      "<utc> <what> <old> -> <new>", one line per switch
uninstall.exe
```

- The root stub never changes between versions. A new `stub_abi` means a
  full installer. The version's launcher sets `RECOMP_GAME_FILES` to the
  root `game_files\` and holds the mutex `Local\<App>-running` while the
  game runs. apply.exe refuses to run while that mutex exists.
- `setup.exe` extracts `apply.exe` first. It runs `apply.exe migrate`,
  which moves an older in-place install into `versions\<old>\` and links
  its game files. It then extracts the program into
  `versions\<v>.partial\`, copies the game files and runs `apply.exe
  switch <v>`. The uninstaller removes `versions\`, `current.txt`, the
  stub, `apply.exe` and `state\`. It never follows the `game_files`
  junctions. The data folder is never touched.
- `apply.exe [patch] [--root DIR] [--keep N] [--yes]` applies the patch
  folder it sits in. Double-clicking it works. The root is `--root`, else
  the uninstall key's `InstallLocation`, else its own folder when that
  holds `current.txt`. `apply.exe rollback [VERSION]` and `apply.exe
  switch VERSION` change the current version. Every file is flushed
  before the `current.txt` move that makes a version current, so a power
  loss always leaves a version that runs.

`patch.lst` is UTF-8 lines; `#` lines and empty lines are skipped:

```
xbr-patch 1
app <App>
version <v>
base <base version>
base_sha <sha256 of the base manifest.json>
new_sha <sha256 of files\manifest.json>
stub_abi 1
C|N|D|K <size> <sha256> <path>
```

- Paths are `/`-separated and relative. Game files carry the
  `game_files/` prefix.
- apply.exe refuses another first line, an unsafe path (`..`, a drive, a
  backslash), or lists that disagree with `files\SHA256SUMS`.
- It ignores keys it does not know, so a later CLI can add some.

## macOS

The bundle is `<App>-<version>.dmg`, which holds the app. The patch is
`<App>-<version>-patch-from-<short>.dmg`:

```
apply.command          double-click, or run in Terminal
xbr-swap               swaps two folders in one call (renamex_np RENAME_SWAP)
patch/                 the changed and new files of Contents/Resources/, the
                       whole Info.plist, MacOS/, Frameworks/ and _CodeSignature/,
                       patch.env, kept.list, deleted.list, whole.list, patch.json
```

- The signed parts of the app ship whole, since the signature covers
  them. They are not counted in the half-size rule.
- `apply.command` finds the app in `/Applications`, then in
  `~/Applications` (`--app` names another). It checks the base, then
  clones the app beside itself as `.<App>.app.partial` (an APFS clone
  copies nothing) and lays the patch over the clone. It checks the clone
  against the new `SHA256SUMS` and with `codesign`, then swaps it with
  the app.
- The old app stays as `.<App>.previous.app`, with the tools in
  `.<App>.previous.tools/`. `rollback` works after the patch is deleted.
  The game files live inside the app, so rollback restores them and no
  game-data warning is shown.
- `--into DIR` installs the new version elsewhere, for when the app's
  folder cannot be written. Saves and settings
  (`~/Library/Application Support/<App>/`) are never written.
