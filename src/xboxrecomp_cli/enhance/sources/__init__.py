"""Where textures are read: one Source interface (base.py) over the readers.

  archive  IPK1 pairs under game_files (archive.py)
  hook     a stdlib script in the game's repo indexes a container the CLI
           has no reader for (hook.py)
  loose    .dds, .xpr and .xbx files under globs of game_files (loose.py)
  dump     what the runtime wrote under RECOMP_DEBUG=tex_dump (dump.py)

game.toml's [enhance.hud] source picks `archive` (its value "ipk") or `hook`
for the HUD pack; [enhance.textures] sources lists any of them, in order.
"""

import os

from .archive import IpkSource
from .base import Entry, Source, SourceError
from .dump import DumpSource
from .hook import (
    PROTOCOL,
    HookArchive,
    _kill_tree,
    check_index,
    hook_path,
    resolve_nocase,
    run_hook,
)
from .loose import LooseSource

__all__ = [
    "PROTOCOL",
    "DumpSource",
    "Entry",
    "HookArchive",
    "IpkSource",
    "LooseSource",
    "Source",
    "SourceError",
    "_kill_tree",
    "check_index",
    "hook_path",
    "label",
    "open_source",
    "resolve_nocase",
    "run_hook",
]


def label(game_files, name, cfg):
    """The path errors name for an archive before it is opened."""
    if cfg.get("source", "ipk") == "hook":
        return os.path.join(game_files, name)
    return os.path.join(game_files, name + ".iph")


def open_source(game_files, name, cfg, repo):
    """The archive `name` read by game.toml's [enhance.hud] source; OSError
    or SourceError when it cannot be opened."""
    if cfg.get("source", "ipk") == "hook":
        return HookArchive(game_files, name, cfg["hook"], repo)
    return IpkSource(game_files, name)
