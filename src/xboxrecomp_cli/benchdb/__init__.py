"""The bench results database (OpenSpec change bench-results-db).

One SQLite db per workspace records every bench run, build, session and
release, and plans the cleanup of what they left on disk. The db and its
benchdb.toml are private: they are never committed or published.

  db = benchdb.open()
  with db.write() as w:
      b = w.build(uuid, game, target, kind, steps=[...])
  db.sql("SELECT ...")"""

import sys

from . import config as config_mod
from . import store
from .config import ConfigError
from .store import Db, DbError

__all__ = ["ConfigError", "Db", "DbError", "load_config", "open", "try_write"]


def load_config(path=None, cwd=None):
    """D7's config(): the name is the config submodule's, so it is this."""
    return config_mod.load(path, cwd)


def open(path=None, cfg=None, readonly=False, cwd=None):  # noqa: A001 (benchdb.open)
    return store.open(path, cfg, readonly, cwd)


_warned = set()


def try_write(what, fn, *args, **kw):
    """Runs a writer's db step. A failure (no config, a lock past its
    timeout, a full disk) warns once per kind and returns None: the record
    file is already on disk and `benchdb import` catches up, so the bench
    or build command's rc never changes (D7)."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 (warn-only by design)
        key = (what, type(e).__name__)
        if key not in _warned:
            _warned.add(key)
            print("benchdb: WARNING: %s not recorded: %s" % (what, e), file=sys.stderr)
        return None
