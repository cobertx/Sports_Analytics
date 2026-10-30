"""Atomic local writes and a durable recovery path for derived model results.

Windows sync/scan processes can briefly deny replacement of an otherwise valid
file. Source-data writes remain strict; derived forecasts can survive in SQLite
and be restored without refitting while the JSON destination is locked.
"""
from contextlib import closing
import errno
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock
from time import sleep

_LOCKS = [RLock() for _ in range(64)]
_DELAYS = (.025, .05, .1, .2)
_LOG = logging.getLogger(__name__)


def _nonfinite(_):
    raise ValueError('Nonfinite cached result')


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _lock(path):
    return _LOCKS[int(sha256(_key(path).encode()).hexdigest()[:8],16)%len(_LOCKS)]


def _retry(action):
    for attempt in range(len(_DELAYS)+1):
        try:return action()
        except OSError as exc:
            transient = (isinstance(exc,PermissionError) or getattr(exc,'winerror',None) in {5,32,33} or
                         exc.errno in {errno.EACCES,errno.EBUSY})
            if not transient or attempt==len(_DELAYS):raise
            sleep(_DELAYS[attempt])


def atomic_text(path, text):
    """Publish complete UTF-8 content; identical concurrent saves are a no-op."""
    path = Path(path)
    encoded = text.encode('utf-8')
    with _lock(path):
        _retry(lambda:path.parent.mkdir(parents=True,exist_ok=True))
        def same():
            try:return path.read_bytes()==encoded
            except OSError:return False
        if same():return
        temporary = None
        try:
            def create():
                return tempfile.NamedTemporaryFile(mode='wb',dir=path.parent,suffix='.tmp',delete=False)
            with _retry(create) as file:
                temporary = Path(file.name)
                file.write(encoded);file.flush();os.fsync(file.fileno())
            def publish():
                # Another process may have finished this exact result already.
                if not same():temporary.replace(path)
            _retry(publish)
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass  # Cleanup must not mask a successful save/recovery.


def atomic_json(path, data):
    atomic_text(path,json.dumps(data,allow_nan=False,indent=2))


def _database(root):
    return Path(root)/'model_cache'/'result_recovery.sqlite3'


def _read_recovery(path, root):
    database = _database(root)
    try:
        if not database.is_file():return None
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            row = db.execute('SELECT payload, checksum FROM results WHERE path = ?',(_key(path),)).fetchone()
        if row and sha256(row[0].encode()).hexdigest()==row[1]:return json.loads(row[0],parse_constant=_nonfinite)
    except (OSError,sqlite3.Error,ValueError):pass
    return None


def _forget(path, root):
    database = _database(root)
    try:
        if database.is_file():
            with _lock(database), closing(sqlite3.connect(database,timeout=2)) as db, db:
                db.execute('DELETE FROM results WHERE path = ?',(_key(path),))
    except (OSError,sqlite3.Error):pass


def save_json(path, data, root):
    """Save derived output, returning False only if both disk stores fail.

    Serialization failures still raise: invalid/nonfinite predictions must not
    be disguised as filesystem problems. A disk failure never discards a valid
    in-memory forecast. Original source-data callers use strict atomic_json.
    """
    encoded = json.dumps(data,allow_nan=False,indent=2)
    try:
        atomic_text(path,encoded)
    except OSError:
        database = _database(root)
        try:
            database.parent.mkdir(parents=True,exist_ok=True)
            with _lock(database), closing(sqlite3.connect(database,timeout=2)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                with db:
                    db.execute('CREATE TABLE IF NOT EXISTS results (path TEXT PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL)')
                    db.execute('INSERT INTO results VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET payload=excluded.payload, checksum=excluded.checksum',
                               (_key(path),encoded,sha256(encoded.encode()).hexdigest()))
            return True
        except (OSError,sqlite3.Error):
            _LOG.warning('Model result is available in memory, but local result storage is unavailable: %s',path)
            return False
    _forget(path,root)
    return True


def load_json(path, root, identity=None, validate=None):
    """Read only matching, finite JSON; repair a recoverable JSON file if possible."""
    def matches(value):
        if not isinstance(value,dict) or (identity is not None and value.get('identity')!=identity):return False
        try:return validate(value) if validate else True
        except (KeyError,TypeError,ValueError,OverflowError):return False
    try:
        value = json.loads(_retry(lambda:Path(path).read_text(encoding='utf-8')),parse_constant=_nonfinite)
        if matches(value):return value
    except (OSError,ValueError):pass
    value = _read_recovery(path,root)
    if matches(value):
        # Publishing is opportunistic. The durable recovery record remains
        # authoritative until a complete matching JSON file can be written.
        save_json(path,value,root)
        return value
    return None
