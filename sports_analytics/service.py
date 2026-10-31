"""Atomic SQLite cache shared by the dashboard and command line importer."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import zlib
from threading import Lock

import pandas as pd

from .data import DataSourceError, Query, SOURCES, fetch
from .cache_refresh import RefreshQueue
from .frame_cache import FrameCache

ROOT = Path(__file__).resolve().parent.parent
CACHE_VERSION = 1


@dataclass
class Dataset:
    key: str
    frame: pd.DataFrame
    source: str
    fetched_at: str
    status: str
    notice: str = ""
    revision: str = ""


class DataService:
    def __init__(self, path=None, ttl_hours=24):
        self.path = Path(path or os.environ.get("SPORTS_CACHE_PATH") or ROOT / "data/cache/sports.sqlite3")
        self.ttl_hours = ttl_hours
        self._load_guard=Lock()
        self._load_locks={}
        self._refresh=RefreshQueue(workers=1)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS datasets (
                    key TEXT PRIMARY KEY, query TEXT NOT NULL, source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, payload BLOB NOT NULL)""")
                yield connection
        finally:
            connection.close()

    @staticmethod
    def key(query):
        serialized = json.dumps({"version": CACHE_VERSION, **query.parameters()}, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    @FrameCache
    def _decode(payload):
        # Key by the actual saved bytes so revised data cannot reuse an old frame.
        return pd.read_json(StringIO(zlib.decompress(payload).decode()), orient="table")

    def cached(self, key):
        with self.connect() as db:
            row = db.execute("SELECT source, fetched_at, payload FROM datasets WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            frame = self._decode(row[2]).copy(deep=True)
        except (ValueError, TypeError, zlib.error) as exc:
            raise DataSourceError("The local cache could not be read. Refresh this dataset to replace it.") from exc
        return Dataset(key, frame, row[0], row[1], "cached", revision=hashlib.sha256(row[2]).hexdigest())

    def compatible(self,query,cached):
        return not ((query.entity_type=='player' and 'position' not in cached.frame) or 'matchup' not in cached.frame or
                    (query.sport=='NFL' and query.entity_type=='team' and not {'pf','pa'}<=set(cached.frame)))

    def fresh(self,query,cached):
        ttl=self.ttl_hours*3600
        if query.sport=='NBA' and query.season>=date.today().year-(date.today().month<7):ttl=min(ttl,15*60)
        age=datetime.now(timezone.utc)-datetime.fromisoformat(cached.fetched_at)
        return 0<=age.total_seconds()<ttl and self.compatible(query,cached)

    def load(self, query, refresh=False, local_first=False):
        """With local_first, return saved data or queue a first download.

        A loading-cache result has an empty frame; navigation polls for the
        completed dataset. Normal model loads and explicit refreshes still wait.
        """
        if local_first and not refresh:
            cached=self.cached(self.key(query))
            if cached and self.compatible(query,cached):
                if self.fresh(query,cached):return cached
                def update():
                    result=self.load(query,refresh=True)
                    if result.status=='stale cache':raise DataSourceError(result.notice)
                state=self._refresh.request(self.key(query),update)
                return replace(cached,status='stale cache' if state['error'] else 'updating cache',
                               notice=state['error'] or 'Checking the source in the background; saved data is available now.')
            if cached is None:
                # First-time history navigation should render immediately too.
                # The normal keyed load lock still deduplicates the download.
                state=self._refresh.request(self.key(query),lambda:self.load(query))
                if state['error']:raise DataSourceError(state['error'])
                return Dataset(self.key(query),pd.DataFrame(),SOURCES[query.sport][0],'','loading cache')
        with self._load_guard:lock=self._load_locks.setdefault(self.key(query),Lock())
        # Navigation can update several controls at once. Share its first download
        # instead of issuing duplicate requests for the same season and scope.
        with lock:return self._load(query,refresh)

    def _load(self, query, refresh=False):
        key = self.key(query)
        cached = None
        try:
            cached = self.cached(key)
        except DataSourceError:
            if not refresh:
                raise
        if cached and not refresh:
            if self.fresh(query,cached):return cached
        try:
            frame = fetch(query)
        except DataSourceError as exc:
            if cached:
                return replace(cached, status="stale cache", notice=str(exc))
            raise
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source = frame.attrs.get("source", SOURCES[query.sport][0])
        payload = zlib.compress(frame.to_json(orient="table", date_format="iso", index=False).encode())
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO datasets VALUES (?, ?, ?, ?, ?)",
                       (key, json.dumps(query.parameters(), sort_keys=True), source, stamp, payload))
        self._read.cache_clear()
        return Dataset(key, frame, source, stamp, "downloaded", revision=hashlib.sha256(payload).hexdigest())

    @lru_cache(maxsize=4)
    def _read(self, key, revision=None):
        return self.cached(key)

    def read(self, key):
        # Local imports and SQLite WAL writes must invalidate previously read frames.
        revision=[]
        for path in (self.path, self.path.with_name(self.path.name+'-wal')):
            try:
                stamp=path.stat()
                revision.append((stamp.st_size,stamp.st_mtime_ns,stamp.st_ctime_ns,stamp.st_ino))
            except FileNotFoundError:revision.append(None)
        dataset = self._read(key, tuple(revision))
        if dataset is None:
            raise DataSourceError("This cached dataset is no longer available. Load the data again.")
        return replace(dataset,frame=dataset.frame.copy(deep=True))
