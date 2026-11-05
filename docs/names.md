# Port names: one source for every release

A port is written against one dump, the reference (`[xbe] sha256`). Its
hand-written side uses that XBE's addresses: the functions it wraps in the
`exclude_manual` file, the globals it reads, the seeds, the D3D fence.
Another release of the same game (another region or version) has the same
code at other addresses. The CLI resolves those addresses through a name
map, so the same hand-written code builds for each release.

```toml
[pipeline]
names = "config/names.toml"       # the names, at the reference's addresses
releases = "config/releases"      # one map per other release (the default)
```

Without `names`, nothing changes: no header, no `--names`, the same
`gen.key.json`. With it, the reference's generated code is byte for byte
what it was. The only new file in `gen/` is `recomp_names.h`. Unlike the
symbol map ([symbols.md](symbols.md)), names never rename or comment gen.

## names.toml

One `[[name]]` table for each address or value the hand-written side uses:

```toml
[[name]]
name = "gfx_sphere_visible"   # a C identifier, never sub_XXXXXXXX
kind = "function"             # function, global, site or value
addr = 0x0009AD80             # the reference's VA (every kind but value)
note = "the bounding-sphere test"

[[name]]
name = "d3d_fence_put"
kind = "value"                # a struct offset, a constant: not an address
value = 0x2C
```

`function` is a function start, `global` a data address, `site` a code
address that is not a function start (a patch point), and `value` a number.

## In the code

`analyze` and `recomp` write `gen/recomp_names.h` for the release in
`game_files`. Include it as `#include "gen/recomp_names.h"`.

| Form | Gives | Kinds |
|---|---|---|
| `XBR_FN(name)` | `sub_XXXXXXXX`, the function's identifier | function |
| `XBR_GEN(name)` | `sub_XXXXXXXX_gen`, the generated body a wrap calls | function |
| `XBR_VA(name)` | `0xXXXXXXXXu`, the address | function, global, site |
| `XBR_VAL(name)` | the value | value |
| `XBR_HAVE(name)` | `1` when this release has the name, else `0` | all |
| `XBR_RELEASE` | the release id, as a string | |

```c
#include "gen/recomp_names.h"

#if XBR_HAVE(gfx_sphere_visible)
extern void XBR_GEN(gfx_sphere_visible)(void);

void XBR_FN(gfx_sphere_visible)(void)
{
    RECOMP_ABI_CALL(XBR_VA(gfx_sphere_visible), XBR_GEN(gfx_sphere_visible));
}
#endif
```

The address form (`sub_0009AD80`, `0x0009AD80u`) still works. Use it for
an address that is the same in every release, or before any release map
exists.

### A name a release does not have

A release map that cannot place a name leaves it out. Then:

- code under `#if XBR_HAVE(name)` is left out of that release, and its
  `#else` side is built in its place;
- any other use stops `analyze`, `recomp` and `build` (and so `package`)
  before they run, naming the file, the line and the name. It never fails
  at link;
- a seed by that name is dropped with a warning.

Only the plain form is understood, by this check and by recomp's scan of
the `exclude_manual` file: a line that is `#if XBR_HAVE(name)` and nothing
else (a trailing comment is fine), one name per `#if`, closed by `#else`
or `#endif`. Nest the blocks for two names. Every other use of `XBR_HAVE`
stops the build: `#if !XBR_HAVE(...)`, `&&` or `||`, `#elif`, a line
continued with `\`, or a macro that wraps it. So does a name in
`XBR_HAVE(...)` that `names.toml` does not have, since a typo there would
leave the block out of every release.

So guard what the game can go without (an enhancement, a mod), and leave
unguarded what it cannot boot without, such as the fence.

## Seeds by name

A seed row (`pipeline.seeds`) can name a function instead of its start:

```json
[{"name": "sys_thread_start_1", "observed": true, "source": "..."}]
```

`analyze` writes the file with each name's `start` for this release to
`analysis/seeds/<path>` (the file's path under the game's root) and passes
that copy to disasm and recomp. A file with no row by name is passed
unchanged. A row gives `name` or `start`, never both: a `start` is the
reference's address alone, so a row with both is refused.

## Release maps: `mygame match PATH`

```
mygame match ../other_dump/game_files [--release pal-v3]
```

Maintainers run it with the reference dump in `game_files`, analysed
(`mygame analyze`). PATH is the other release's XBE or the folder that holds
it. `match`:

1. parses and disassembles the other XBE into `analysis/releases/<id>/`,
   with the port's `[pipeline.disasm]` options;
2. pairs the two releases' functions and globals;
3. places each name of `names.toml`;
4. writes `config/releases/<id>.toml`.

The id comes from the certificate, such as `pal-v3`, or from `--release`.

```toml
[release]
id = "pal-v3"
sha256 = ["3e4b51ed..."]   # the dumps this map is for
region = 0x4
version = 3
reference = "771b8b63..."  # the reference dump it was matched against

[[name]]
name = "gfx_sphere_visible"
ref = 0x0009AD80             # the reference address it was matched from
addr = 0x00034F90
verdict = "unsure"
how = "function (calls)"

[[name]]
name = "d3d_fence_put"
ref = 0x2C
# unplaced: a value, which match does not place (the reference has 0x2C);
# set value = ... and verdict = "reviewed" by hand
```

Each placed row has a `verdict`:

| Verdict | Meaning |
|---|---|
| `sure` | the matcher found no doubt: a function with the same bytes, a global no conflicting reference moves, a section of the same size |
| `unsure` | one path, or a function name that lands inside a function (`check` says so); `analyze` and `recomp` warn until it is reviewed |
| `reviewed` | a person checked it against the code; `match` keeps it as it is when it runs again |

`analyze` and `recomp` warn about every placed row that is not `reviewed`,
`sure` ones too: the matcher being sure is no review.

`match` does not place a value: fill each one in by hand. Running `match`
again adds the dump's sha256 to the list and keeps every `reviewed` row
whose `ref` still agrees with `names.toml`.

`ref` is the reference's address (or value) each row was matched from.
When `names.toml` moves a name, the rows made for the old address are
stale: `analyze` and `recomp` stop and name them, and `match` places them
afresh as `unsure`, with a `check` that says so. A file without `ref`
rows (from before them) is used with a warning. A map whose `reference`
is not in `[xbe] sha256` is used with a warning too: it was matched
against another dump than the port's.

## Which map is used

The release is chosen by the fingerprint of the dump in `game_files`
([commands.md](commands.md#mygame-fingerprint-path)):

1. a dump `[xbe] sha256` lists (`known`): `names.toml` itself;
2. a dump a map's `sha256` lists: that map, whatever the fingerprint
   takes it for;
3. the reference's code under a patched header or other data (`header`,
   `data`), even when the patched certificate names another region or
   title: `names.toml`;
4. a dump whose certificate names another release, with other code
   (`code` or `unknown`): `analyze` and `recomp` stop and name `mygame
   match`;
5. anything else (a code mod of the reference, or `unknown` with no claim
   to another release): `names.toml`, as before names existed.

A map that lists the reference's own sha256, or a sha256 another map lists
too, is refused.

`gen.key.json` records `names.toml`, the chosen map and the release id, so
a dump of another release makes `gen/` stale, and `build` refuses it until
`analyze` and `recomp` run again.

`recomp` gets `analysis/names.json`: `{"addresses": {name: VA}, "have":
[name, ...]}`. `addresses` is what it resolves; `have` lists every name
the release has, values included, so the `exclude_manual` scan keeps the
same `#if XBR_HAVE` blocks the compiler does.

The toolkit's `recomp` must take `--names`. With an older
toolkit, `recomp` stops and says so.
