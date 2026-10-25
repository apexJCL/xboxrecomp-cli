"""record.json files: what each writer leaves beside its output before it
calls the db (D7), so `benchdb import` can rebuild every row from disk.

- run records: <run dir>/record.json
- build records: <build dir>/build-record.json
- package records: beside the bundle
- session records: <store>/benchdb-sessions/<uuid>.json

Every record carries a uuid, made once by its writer before the work
starts. It is the row's key, so one record imported from its dir, an
inbox and a rebuild is one row. Writes are atomic (a temp file in the
same dir, flushed, then renamed), so a reader never sees half of one."""

import hashlib
import json
import os
import uuid as uuid_mod

SCHEMA = 1
RUN = "record.json"
BUILD = "build-record.json"
SESSIONS = "benchdb-sessions"
INBOX_ENV = "BENCHDB_INBOX"
HOST_ENV = "BENCHDB_HOST"


def new_uuid():
    return str(uuid_mod.uuid4())


def derived_uuid(*parts):
    """A stable uuid for a row no record describes (an old run's exe, a
    bundle index entry): the same inputs give the same key on re-import."""
    h = hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()
    return str(uuid_mod.UUID(h[:32]))


def write(path, doc):
    """Writes doc to path atomically; returns path."""
    doc = dict(doc)
    doc.setdefault("schema", SCHEMA)
    doc.setdefault("uuid", new_uuid())
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read(path):
    """The record at path, or None when it is missing or not a JSON object."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def inbox():
    """(host key, inbox dir) from BENCHDB_HOST and BENCHDB_INBOX, or None:
    a host side with neither set writes nothing outside its own dirs."""
    d, h = os.environ.get(INBOX_ENV), os.environ.get(HOST_ENV)
    if not d or not h:
        return None
    return h, d


def to_inbox(doc, env=None):
    """A copy of a record in this host's inbox as <uuid>.json; None when
    there is no inbox. The temp name starts with '.', so a pull skips it."""
    ib = env if env is not None else inbox()
    if not ib:
        return None
    host, d = ib
    doc = dict(doc)
    doc.setdefault("uuid", new_uuid())
    doc.setdefault("host", host)
    return write(os.path.join(d, "%s.json" % doc["uuid"]), doc)
