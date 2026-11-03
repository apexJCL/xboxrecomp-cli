"""Is the game, or an emulator whose disk a source sits on, running (D4)?

A running game holds its saves open and writes them back on exit, over
whatever was imported; a running emulator may be writing the disk being
read. Linux reads /proc and macOS uses ps (running_game.py); Windows asks
tasklist, which lists image names only. When the list cannot be had the
answer is "unknown" and the import goes ahead with a warning: the backup
still covers it.
"""

import csv
import io
import os
import subprocess
import sys

from .. import running_game

XEMU = ("xemu", "app.xemu.xemu")  # the program, the Flatpak id
CXBXR = ("cxbx", "cxbxr-ldr")  # the GUI, the loader that hosts the emulator


def parse_tasklist(text):
    """`tasklist /FO CSV /NH` -> [(pid, 0, [image name])]."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[1].strip().isdigit():
            out.append((int(row[1]), 0, [row[0]]))
    return out


def list_processes(exe):
    """[(pid, ppid, argv)], or None when this host cannot list them."""
    try:
        if sys.platform.startswith("linux"):
            return running_game.processes_linux()
        if sys.platform == "darwin":
            return running_game.processes_macos(exe)
        if os.name == "nt":
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                # No console window flashing up from the wizard's GUI.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0:
                return None
            return parse_tasklist(r.stdout.decode("utf-8", "replace")) or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def game_running(procs, exe, me=None):
    """[(pid, command line)] for the game's processes outside our own tree."""
    if procs is None:
        return None
    return running_game.find_games(procs, os.getpid() if me is None else me, exe)


def _program(argv):
    if not argv:
        return ""
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def emulator_running(procs, names, me=None):
    """[(pid, command line)] for processes whose program (or, for a Flatpak
    or AppImage launch, one of whose arguments) is one of names."""
    if procs is None:
        return None
    skip = running_game.own_tree(procs, os.getpid() if me is None else me)
    out = []
    for pid, _, argv in procs:
        if pid in skip or not argv:
            continue
        progs = {_program(argv)} | {_program([a]) for a in argv[1:3]}
        if progs & set(names):
            out.append((pid, " ".join(argv)))
    return out
