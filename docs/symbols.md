# The symbol map

A game can name what it has learnt about its guest code in one TOML file
and point `[pipeline] symbols` at it:

```toml
[pipeline]
symbols = "config/symbols.toml"
```

`analyze` then carries the names into the generated code:

- each `function` name becomes the function's C identifier in gen (its
  definition, declaration, calls and dispatch entry);
- its `note` goes in the doc block above the definition, as
  ` * <name> (sub_XXXXXXXX): <note>`;
- a code line with a hex literal equal to a `global`'s or a fixed
  `field`'s address gets a trailing `/* <name> */`;
- a `loc_XXXXXXXX:` label at a `label` address gets one too.

Only comments and identifiers change: the lifted code is the same as
without the map. Hooks, seeds (`seed_functions.json`), `[mods]
hook_functions`, spin waits and every runtime table stay keyed by address,
and the toolkit's runtime still prints addresses.

## The format

One `[[symbol]]` table per address:

```toml
[[symbol]]
addr = 0x000133E0
name = "menu_title_main"
kind = "function"
note = "Title screen state machine"

[[symbol]]
addr = 0x00ADE76C
name = "sys_frame_counter"
kind = "global"

[[symbol]]
name = "ent_hp"
kind = "field"
struct = "ent_enemy"
offset = 0x10
```

| Key | Meaning |
|---|---|
| `addr` | The guest VA, an integer. Every kind but a heap `field` has one. |
| `name` | A C identifier, unique in the map. Not reserved (below). |
| `kind` | `function` (a function start), `global`, `field` or `label` (an address inside a function). |
| `note` | Optional. One line; it is wrapped in the doc block. |
| `in_gen` | Optional, functions only. `false` leaves gen's name alone (an HLE override, a function gen does not hold). The note still lands. |
| `struct`, `offset` | A field of a struct. A field with `addr` is at that absolute address (a static struct) and is commented like a global; one without lives on the heap and gets no comment. |

A name is reserved, and the CLI refuses it, when it is:

- a C keyword;
- an x86 register name (`eax`, `esp`, `al`, `xmm0`, ...), which the lifted
  code uses as locals and globals;
- under a prefix the lifter and runtime use: `_` (the lifter's locals,
  `_t`, `_i`, `_flags`, ...), `g_` and `recomp_` (the runtime's helpers in
  `recomp_types.h`);
- shaped like a name the pipeline makes (`sub_XXXXXXXX`, `loc_XXXXXXXX`).

A clash with the game's own sources (its hand-written C, its headers) is
not visible to the CLI: the game's test checks that.

Other keys are the game's own (a source, a conflict note) and are kept as
they are. The CLI checks only the above and fails `names` on a bad entry,
listing each. Naming conventions (prefixes, sorting, sources) belong in
the game's own test.

## Which functions get the name

- Only a function still called `sub_<its address>` is renamed. A name
  disasm or Ghidra gave it (`xbe_entry_point`, a library name) stays, and
  the map's name appears only in the doc block.
- An address that is no function start is reported and skipped.
- A map name equal to another function's name fails the stage.
- The game's `exclude_manual` file pins every `sub_XXXXXXXX` it mentions
  back to `sub_` (and its wrapped bodies to `sub_X_gen`), as do the hook
  slots. Hand-written code that calls a gen function by symbol therefore
  goes in that file, or calls by address; a game test that fails on a
  `sub_` reference to a mapped function anywhere else catches the slip
  before the link does.

With a map, `names` runs without a Ghidra export: Ghidra's names are then
skipped, and so are the game's `names_hooks`, which feed on that export.

`names` marks the entries it renamed in functions.json, so running it again
(after an edit to the map, without `disasm`) puts `sub_` back on a name
that left the map.

## Rebuilds

The map file and the comment format are part of the generation key, only
for a game that sets `symbols`. Any edit to the map, a note or a global as
much as a function name, therefore stales the key and means a full
`recomp`. `recomp` applies the map to functions.json itself before it
runs, so an edit followed by `recomp` alone keeps names and comments in
step. Annotating is idempotent: annotated gen run through it again comes
out the same. The comments are written after `recomp`;
a gen file whose bytes come out as they were keeps its mtime, so a regen
that changes nothing rebuilds nothing.
