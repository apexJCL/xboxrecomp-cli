# Porting a game

What happens after `xbr new` and the first build. The toolkit's own docs
carry the long form: its README's Quick Start, `docs/GETTING_STARTED.md`
(Step 8, Debug Iteratively), `docs/pipeline/` and
`docs/technical/indirect-calls.md`; its `tools/doctor.py` checks the
pipeline's environment.

## The loop

The first run of a recompiled game crashes. The port starts there:

1. **Run it**, on Windows or with `./mygame bench run` on a Proton host, and
   read the crash report: the faulting guest function, the guest return
   addresses, the last indirect-call targets.
2. **Seed the functions the disassembler missed.** An ICALL failure names a
   target the lifter did not know was a function. Add it to
   `config/seed_functions.json` (`{"start": "0x...", "source": "...",
   "observed": true}`), then `./mygame analyze` and `./mygame recomp`;
   `recomp` alone misses new seeds. Never edit `src/recomp/gen/` by hand:
   it is regenerated, and `src/recomp/gen.key.json` records what from, so
   `package` knows when it is stale and `build` refuses it. Seeds marked
   `"observed"` also reach `recomp` (`--seeds`), so its flag-fallback
   report lists the sites inside them.
3. **Override what the lifter got wrong** in `src/recomp_manual.c`:
   `recomp_lookup_manual()` runs before the generated dispatch, so a
   function can be wrapped, stubbed or replaced by a native one.
4. **Add what the runtime lacks.** A kernel call, a D3D path or an input
   detail the toolkit does not have is toolkit work: the fork's branch is
   `blinx2/portability`, and `XBOXRECOMP_DIR` points `setup`, `build` and
   the bench at your own checkout (a checkout the bootstrap did not clone
   is never moved to the pin).
5. **Build again:** `./mygame build` is incremental.

## The manifest keys a port meets first

Every setting a game has is a key in `game.toml` ([manifest.md](manifest.md)
documents each one with its default):

| Key | Why it comes up |
|---|---|
| `pipeline.disasm.extra_sections` | code outside `.text`, such as the XDK library sections |
| `pipeline.split` | how many functions per generated file |
| `pipeline.spin_waits` | the guest's busy-wait sites, so the host can sleep in them |
| `pipeline.seeds` | the seed files (`config/seed_functions.json`) |
| `xbe.sha256` | the dumps the goldens were recorded on |
| `[package]` | turns packaging on |

## Environment and enhancements

The runtime reads its environment through one table (`recomp_env`;
`RECOMP_*` keys in three tiers: config, trace, debug). A game adds its own
keys in a header named by `game.env_header` and documents them in
`game.env_doc`. The toolkit's opt-in enhancements layer
(`XBOXRECOMP_ENHANCE`: render scale, present filter, pacing, read from
the player's `config.toml` over the `config.default.toml` the bundle
ships beside the executable, `package.content`'s copy) is off by default and never changes stock behaviour; a game
that builds it lists the option in `build.stock_cmake`.

`enhance hud` makes a texture pack of the game's HUD from the owner's own
files (`[enhance.hud]` names the set; a TrueType font the owner installs,
named there, re-renders the Latin atlas) and `package --hud-pack` puts it in
their private bundle. Packs are game data and are never published.

## macOS as a target

The toolkit builds for macOS (Apple silicon, Metal) and BLiNX 2 runs there,
but the toolkit's template `main.c` is Win32 (WinMain, the VEH crash
handler, dbghelp). A game that lists `macos` in `build.targets` needs a
`main.c` with the POSIX host code: BLiNX 2's `src/main.c` has it
(`host_main`, the signal handlers, the SDL window), and a portable template
is a toolkit follow-up. Until then a Mac is a build host for the windows
target and a run host for nothing, unless you port the host code.
