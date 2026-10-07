# Where each file came from

This CLI began as the toolchain of the BLiNX 2 recompilation,
[blinx2-recomp](https://github.com/apexJCL/blinx2-recomp): its single-file
`blinx2.py` and its `scripts/`. The repository starts with one fresh
commit instead of that history, which mixes game and tooling work. To
find out why an older line is there, run `git log --follow` on the path
below in blinx2-recomp, starting from its commit 70bf5c8.

The commits there that built this code:
- 333358e: packaging, where `blinx2.py` was born.
- 1623e8b, 61ea933, 25ec32c and 70bf5c8: the toolchain-cli change,
  phases 0, 1, 2 and 4.

| Here | blinx2-recomp (70bf5c8) |
|---|---|
| `src/xboxrecomp_cli/main.py` | `blinx2.py`: the command table, `main()` |
| `src/xboxrecomp_cli/helptext.py` | `blinx2.py`: the module docstring (the help) |
| `src/xboxrecomp_cli/host.py` | `blinx2.py`: host detection, `run`, `say`/`step`, `build_env` |
| `src/xboxrecomp_cli/env.py` | `blinx2.py`: uv, the venv |
| `src/xboxrecomp_cli/fetch.py` | `blinx2.py`: llvm-mingw and NSIS downloads |
| `src/xboxrecomp_cli/toolkit.py` | `blinx2.py`: the toolkit clone and its pin |
| `src/xboxrecomp_cli/setup.py` | `blinx2.py`: `setup` |
| `src/xboxrecomp_cli/doctor.py` | `blinx2.py`: `doctor` |
| `src/xboxrecomp_cli/pins.py` | `blinx2.py`: `pins refresh` |
| `src/xboxrecomp_cli/pipeline.py` | `blinx2.py`: the stages and the generation key |
| `src/xboxrecomp_cli/build.py` | `blinx2.py`: `build` |
| `src/xboxrecomp_cli/package/__init__.py` | `blinx2.py`: `package` (plan, payload, manifest) |
| `src/xboxrecomp_cli/package/windows.py` | `blinx2.py`: `wrap_windows`, the launcher, NSIS |
| `src/xboxrecomp_cli/package/steamos.py` | `blinx2.py`: `wrap_steamos` |
| `src/xboxrecomp_cli/package/templates/steamos/*` | `packaging/steamos/*`, made game-agnostic |
| `src/xboxrecomp_cli/package/templates/windows/*` | `packaging/windows/{launcher.c,installer.nsi.in,BLiNX2.rc.in}`, made game-agnostic |
| `src/xboxrecomp_cli/package/templates/README.*` | `packaging/README.txt.in`, its first paragraphs as `README.intro` |
| `src/xboxrecomp_cli/package/macos.py` | `blinx2.py`: `wrap_macos` |
| `src/xboxrecomp_cli/package/lib.py` | `scripts/package_lib.py` |
| `src/xboxrecomp_cli/package/icon.py` | `scripts/game_icon.py` |
| `src/xboxrecomp_cli/package/progress.py` | `scripts/progress.py` |
| `src/xboxrecomp_cli/golden.py` | `scripts/golden.py` |
| `src/xboxrecomp_cli/pacing_stats.py` | `scripts/pacing_stats.py` |
| `src/xboxrecomp_cli/benchlog_retention.py` | `scripts/benchlog-retention.py` |
| `src/xboxrecomp_cli/audio_check.py` | `scripts/audio_check.py` |
| `src/xboxrecomp_cli/bench/*.py` | `scripts/benchlib/*.py` (`__init__`, `checks`, `config`, `golden`, `pacing`, `remote`, `sync`) |
| `src/xboxrecomp_cli/bench/host/*` | `scripts/benchlib/host/*`, byte for byte |
| `tests/test_cli.py` | `scripts/test_blinx2_cli.py` |
| `tests/test_bench_cli.py`, `tests/testdata/bench_parity.json` | `scripts/test_bench_cli.py`, `scripts/testdata/bench_parity.json` |
| `tests/test_<name>.py` | `scripts/test_<name>.py` for `bench_checks`, `bench_hold`, `package_lib`, `golden`, `game_icon`, `progress`, `pacing_stats`, `benchlog_retention` and `audio_check` |
| `tests/testdata/game/` | `game.toml`, `.gitignore`, `config/setup-pins.json` and `analysis/golden/{golden,audio}.json` (frame hashes zeroed, host name generic) |

These are new here and have no earlier history: `manifest.py`,
`cli_dir.py`, `wrapper/`, `scripts/audit-public.sh`, `tests/conftest.py`,
`tests/test_manifest.py`, `tests/test_wrapper.py`,
`tests/test_audit_public.py` and `docs/`.
