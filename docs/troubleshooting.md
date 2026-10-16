# Troubleshooting

Each entry is the message as the CLI prints it, then the fix.

| Message | Fix |
|---|---|
| `no uv 0.5.31+ on PATH` | Install uv ([bootstrap.md](bootstrap.md#what-the-host-needs)) and open a new shell. `<slug> --help` works without it; nothing else does. |
| `xboxrecomp-cli: the pin moved`, `git checkout failed`, `git fetch failed` | The game pins a CLI commit or tag the remote does not have (never pushed), or there is no network for a new pin. Clone the CLI beside the game or set `XBOXRECOMP_CLI_DIR`, and fix `[cli]`. |
| `tag v0.2.0 is …, not the pinned commit …: the tag moved` | The tag and the `commit` that locks it disagree: the tag was moved or re-pushed (check the release), or `game.toml` names the wrong pair. The same holds for `[toolkit]` in `setup`. |
| `no config/setup-pins.json` | `<slug> pins refresh` writes it (and `uv.lock`); it needs the network. |
| `uv lock --check failed` | `uv.lock` is out of date with `pyproject.toml`: `<slug> pins refresh`, or `uv lock`, then commit it. |
| `llvm-mingw … quarantined` (macOS) | `xattr -dr com.apple.quarantine third_party/llvm-mingw-*`, as `doctor` prints. |
| `makensis: missing` | Only the windows installer needs it; `doctor` prints the install command for your system, including the toolbox route on an immutable Linux. |
| `the template changed` (from `xbr new`) | The toolkit's `templates/new-game/` no longer has the line the scaffold patches; the message names the edit to make by hand in `CMakeLists.txt` or `src/main.c`. |
| `implicit declaration of function 'recomp_dispatch_init'` | An older template: add `extern int recomp_dispatch_init(void);` to `src/main.c` (`xbr new` does). |
| `title ID 0x… is not …` | The dump in `game_files/` is another game, or `xbe.title_id` is still `0` from a scaffold made without the dump. |
| `gen/ is stale: …` (from `build` or `bench integrate`) | The toolkit's lifter, a seed file or `recomp_manual.c` changed since `recomp`: `<slug> analyze && <slug> recomp`. `--stale-gen-ok` builds anyway. |
| `gen/ is being regenerated` | The last `recomp` failed or is still running; run it again. |
| `gc: reference sources missing` | A path in `[bench.gc] refs` (or `TASKS.md`, `openspec/`, `golden.json`, the seeds and spin-wait files) is missing in the main checkout; gc will not guess from a partial set. The refs can name a maintainer's notes outside the repo, so a plain clone sets them to paths that exist there. |
| Windows: a path error deep in the build | The checkout path is long: move it to `C:\g\<slug>` and enable long paths (`doctor` says which). |
