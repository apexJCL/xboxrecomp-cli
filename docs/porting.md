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
| `pipeline.disasm.text_only` | `false` (the default) sweeps every code section, the XDK libraries included; a title whose XBE flags data sections executable sets it true and lists its code sections in `extra_sections` |
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

## The D3D fence

XDK D3D waits for the GPU through a fence. It keeps a pointer to its device
in a global; the device holds the fence it last submitted at `+0x2C` and,
at `+0x30`, a pointer to the word the GPU writes as it completes work. The
fence wait loads the device, reads both, and loops until the completed word
reaches the fence it wants; the wait-for-idle polls PGRAPH `0x400B10`,
which the toolkit drives from the same word. With nothing writing that
word, both waits spin forever. The line that ties the word to the device is
in `src/main.c`, right after `xbox_MemoryLayoutInit`:

```c
xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30);
```

It registers a fence mirror: the runtime copies submitted into completed
until the pushbuffer executor's semaphore releases land on that word, then
steps aside. The mirror stays registered as the catch-up path when a walk
stops short of PUT, and as the only fence with `RECOMP_PB_EXEC=0`.

`new` fills the line in. When it reads the dump it looks in the XBE's `D3D`
section (or every executable section, when there is no `D3D` section) for
the fence wait's instructions, with the device pointer as the one unknown,
and when it finds exactly one match it replaces the template's commented
example with the live line. Its summary says what it found:

```
D3D fence: device pointer 0x00235528 (+0x2C submitted, +0x30 completed), from the fence wait at 0x00228FA0 in D3D8 1.0.5849; main.c carries the mirror line
XDK libraries: XAPILIB 1.0.5849, XMV 1.0.5849, D3D8 1.0.5849, ...
```

or `D3D fence: not found (...)` with the reason (`no XBE read`, `no D3D
section`, `no match in D3D`, `2 matches in D3D`, `D3D section truncated`), in which case the example
stays commented out and the line is yours to write. The signature is the
XDK 5849 one (D3D8 and D3D8LTCG); a title on another XDK may not match.

By hand: after `analyze`, open `analysis/disasm/asm/D3D.asm` (the XDK
library section the disassembler names) and look for these three
instructions in a row (the listing pads the mnemonic, so search for the
bytes `8b4630`, or with a regular expression such as
`mov +eax, dword ptr \[esi \+ 0x30\]`):

```
8b4630    mov eax, dword ptr [esi + 0x30]
8b08      mov ecx, dword ptr [eax]
8b462c    mov eax, dword ptr [esi + 0x2c]
```

(the LTCG build puts `push edi; mov edi, eax` before them). The
`mov esi, dword ptr [0x......]` just above is the device pointer, and the
two displacements are the offsets the line takes. Three titles, all
XDK 5849, as worked examples:

| Title | D3D library | Fence wait | Device pointer |
|---|---|---|---|
| BLiNX 2 | D3D8 1.0.5849 | `0x002E8110` | `0x002F1FB8` |
| Spartan: Total Warrior | D3D8 1.0.5849 | `0x00228FA0` | `0x00235528` |
| Burnout 3 | D3D8LTCG 1.0.5849 | `0x00351770` | `0x0035FB48` |

The device pointer is loaded in a hundred places or more, so it cannot be
found by how often it is referenced; the fence wait carries it.

At runtime the log confirms it: `NV2A fence mirror: device at 0x...` when
the line registers, then `NV2A fence mirror: device 0x... fence word 0x...
now written by the executor; mirror off` once the executor's first release
lands. `RECOMP_TRACE=sema` prints the releases. A boot that hangs in the
fence wait shows, under `sample` or the watchdog, a loop in that function
reading `[edx]` with no calls; one with the line missing stops in the
wait-for-idle that follows.

## macOS as a target

The toolkit builds for macOS (Apple silicon, Metal), and the toolkit's
template `main.c`, the one `new` copies, has both hosts in one file: a shared
boot sequence (`host_main`) with the host parts under `#if defined(_WIN32)`
/ `#else`. On Windows that is `WinMain`, the VEH crash handler and dbghelp;
elsewhere a SIGBUS/SIGSEGV handler that prints the same `[CRASH]` report
(the host PC and the generated function it is in, the guest registers and
return addresses, the native stack), and the SDL window, with the guest on
its own thread. So `new` writes `build.targets = ["windows", "macos"]`.
The macos build needs the Command Line Tools and
`brew install sdl2 openssl libepoxy pkg-config` (a new game's `game.toml`
has no `[package] brew` list, so `setup` does not check them). Then, on a
Mac:

```sh
./mygame build macos                      # build/<exe>, Apple clang
SDL_AUDIODRIVER=dummy RECOMP_HEADLESS=1 ./build/<exe>
```

Run it from the game's root, where `game_files/` is. `RECOMP_HEADLESS=1`
runs with no window; `SDL_AUDIODRIVER=dummy` keeps it silent. The backend
is Metal unless `RECOMP_PB_BACKEND` says otherwise (`cpu` is the CPU
rasteriser, a few frames a second on a Mac). `RECOMP_STDIO_LOG=<file>`
sends stdout and stderr to a file on both hosts, which is how the bench and
the launchers read the Windows build's log under Proton.

The toolkit's template `main.c` turns on two keys every XDK D3D title
needs, each only when the environment leaves it unset: `RECOMP_VBLANK`
(D3D paces `Swap`, `BlockUntilVerticalBlank` and the title's vblank
callback on the vblank interrupt, so without it the boot stops in D3D's
first vblank wait) and `RECOMP_TITLE_KEVENTS` (D3D8 builds the KEVENT its
vblank DPC sets without `KeInitializeEvent`; without a host event for it
`KeSetEvent` fails quietly on Windows and faults on POSIX, in the vblank
DPC before the first frame). `=0` in the environment turns either off: the
A/B when a title that does not use them behaves differently with them on,
or to see which of the two a stop depends on. A game scaffolded from an
older template adds the two lines to `main.c` after
`xbox_MemoryLayoutInit` itself: `recomp_env_set(RENV_VBLANK, "1")` when
`recomp_env(RENV_VBLANK)` is unset, and the same for
`RENV_TITLE_KEVENTS`.

The template also carries what Burnout 3 and BLiNX 2 each added to their
own `main.c`. When the toolkit is built with `XBOXRECOMP_ENHANCE`, both
entry points start the enhancements layer (`xbox_enhance_init`, which
reads `config.toml` over `config.default.toml` and logs one `[ENHANCE]`
line) and `CMakeLists.txt` links `xbox_enhance`; without the option
neither happens. `CMakeLists.txt` also copies the game's
`packaging/config.default.toml` beside the executable when there is one.
Both entry points name the window with `xbox_HostWindowSetTitle("<name>")`,
the game's name from `new`. And the template reads two toolkit config
keys before it maps the drives: `RECOMP_GAME_FILES` (the extracted game
folder; unset, the `YOUR_GAME_DIR` that `new` filled in) and
`RECOMP_HDD_DIR` (the emulated hard disk; unset, the toolkit's default
place for saves), so a launcher can point an installed bundle at the
owner's files without a rebuild. A game whose own env header still has
`GAME_FILES` or `HDD_DIR` rows drops them when it moves to a toolkit that
has them (the table refuses a key defined twice).

A game pinned to a toolkit at or before `blinx2-v0.3.0` has the Win32-only
template: `new` says so, and `build macos` will not compile. Move the
toolkit pin forward (`[toolkit] commit` in `game.toml`, then `setup`) and
copy the new `templates/new-game/src/main.c` and `CMakeLists.txt` over your
own, re-applying your constants, or drop `macos` from `build.targets` and
keep the Mac a build host for the windows target.
