"""Shared pieces of the benchdb tests: a workspace in a temp dir with its
benchdb.toml, and the db opened on it. Every path is synthetic."""

import os

from xboxrecomp_cli.benchdb import config, store

BASE = """
db = "bench-logs/bench.db"

[roots.store]
path = "bench-logs"
kind = "store"
pattern = ['[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?']

[roots.runs]
path = "runs"
kind = "runs"
pattern = ['[A-Za-z0-9_.-]+']

[hosts.hostA]
os = "linux-proton"
ssh = "hosta"
names = ["Box1"]
"""


def workspace(d, extra="", text=None):
    """d/benchdb.toml plus its store and runs dirs; returns the Config."""
    os.makedirs(os.path.join(d, "bench-logs"), exist_ok=True)
    os.makedirs(os.path.join(d, "runs"), exist_ok=True)
    p = os.path.join(d, config.NAME)
    with open(p, "w") as f:
        f.write((BASE if text is None else text) + extra)
    return config.load(p)


def open_db(d, extra="", monkeypatch=None):
    cfg = workspace(d, extra)
    if monkeypatch is not None:
        monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))
    return store.open(cfg=cfg)


SHA_A = "a" * 64
SHA_B = "b" * 64
GAME_SHA = "1" * 40
TK_SHA = "2" * 40
CLI_SHA = "3" * 40

# run-info.txt in the three formats, as the bench writes them (made-up
# hosts and paths, shortened to what the parsers read).
PROTON_INFO = f"""host:   Box1  2026-10-25T07:21:35-07:00
proton: /srv/u/proton/GE-Proton11-7  prefix: /srv/u/prefix
env:    RECOMP_PB_BACKEND=d3d11 RECOMP_TRACE=flip RECOMP_PAD_SCRIPT=3000:start
args:   (none)
limit:  40 s (SIGINT)
lock:   /srv/u/.run.lock, waited 3s
{SHA_A}  build-win/demo_recomp.exe
built:  shipped (not built by bench): 2026-10-25T14:21:34Z on hostm from /srv/u/demo (BENCH_BUILD=local)
built:  demo: {GAME_SHA} main clean
built:  toolkit: {TK_SHA} HEAD clean
built:  cli: {CLI_SHA} HEAD clean
built:  exe: {SHA_A} demo_recomp.exe
procs:  17 tagged RECOMP_BENCH_RUN=20261025-072135 at +10 s
flips:  2063
load:   0.08 0.31 0.66 at start, 0.44 0.37 0.66 at end (16 cpus)
cpu-stall: 0.7% (PSI cpu some over the run; busy at 5%)
busy:   no
"""

WINDOWS_INFO = f"""host:   BOX1  2026-10-25T01:30:48
os:     windows (native, no Proton)
env:    RECOMP_PB_BACKEND=d3d11 RECOMP_TRACE=flip
limit:  40 s (the game quits itself; kill at +30 s)
lock:   D:/xbr/.bench-lock, waited 0s
{SHA_B}  build-win/demo_recomp.exe
built:  demo: {GAME_SHA} feat/x dirty (4 paths)
built:  toolkit: {TK_SHA} HEAD clean
flips:  2218
load:   1% at start, 2% at end (16 cpus)
busy:   no
elapsed: 41s, game exit 0
"""

MAC_INFO = """host:   macbox  2026-10-25T07:45:07-0700
backend: metal
exe:    {exe}  /srv/u/demo/build/demo_recomp
tree:   /srv/u/demo {game} clean
env:    RECOMP_PB_BACKEND=metal RECOMP_HDD_DIR={ws}/bench-logs/20261025-074507-metal-menu/hdd
limit:  96 s (SIGINT); stop at the last frame's flip
lock:   /srv/u/.mac-run.lock, waited 0s
load:   2.6 on 14 CPUs at start
stop:   done at flip 2312 after 39.4 s (max load 2.6)
"""

GOLDEN_TXT = """end: stopped after the last frame's flip
EXACT    menu/menu-fade (dump 5, present 301) = d3d11-proton
CLOSE    menu/menu-thx (dump 10, present 601) vs metal: mae 0.384/0.367/0.357 max 67/84/83 bad 0.8766% worst tile 23.7% at 256,256 (limits mae 1.0, bad 2.00%, tile 60% at tol 8)
         window +-2: best flip offset -2: mae 0.000/0.000/0.000
golden: pass
verdict: menu pass
"""


def flip_log(n=200, ms=16, first=0):
    """A game log with a [GPU] flip trace: flip 1 at `first` ms, then one
    flip every `ms` ms, with one 80 ms hitch at flip 100."""
    out, t = [], first
    for f in range(1, n + 1):
        if f > 1:
            t += 80 if f == 100 else ms
        out.append("[GPU] flip %d %d ms fence 0x1 batches %d tex 0" % (f, t, f % 7))
    return "\n".join(out) + "\n"


def run_dir(base, name, info=None, golden=None, log=None, extra=None):
    """base/name with the files given; returns its path."""
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    files = {"run-info.txt": info, "golden.txt": golden, "game-stdio.log": log}
    files.update(extra or {})
    for n, text in files.items():
        if text is None:
            continue
        p = os.path.join(d, n)
        if n.endswith("/"):
            os.makedirs(p, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    return d


TESTDATA_GAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "game")


def game_dir(d, slug="demo", exe="demo_recomp"):
    """d/<slug>: testdata's game.toml renamed to slug and exe."""
    root = os.path.join(d, slug)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(TESTDATA_GAME, "game.toml")) as f:
        toml = f.read()
    toml = toml.replace('slug = "blinx2"', 'slug = "%s"' % slug)
    toml = toml.replace('exe = "cat_recomp"', 'exe = "%s"' % exe)
    with open(os.path.join(root, "game.toml"), "w") as f:
        f.write(toml)
    return root


GAMES = """
[games]
demo = "demo"
"""

WIN_HOST = """
[hosts.hostB]
os = "windows"
ssh = "hostb"
markers = ['^os: +windows']
"""
