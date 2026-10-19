"""Atomic SQLite cache shared by the dashboard and command line importer."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import zlib

import pandas as pd

from .data import DataSourceError, Query, SOURCES, fetch

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


class DataService:
    def __init__(self, path=None, ttl_hours=24):
        self.path = Path(path or os.environ.get("SPORTS_CACHE_PATH") or ROOT / "data/cache/sports.sqlite3")
        self.ttl_hours = ttl_hours

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

    def cached(self, key):
        with self.connect() as db:
            row = db.execute("SELECT source, fetched_at, payload FROM datasets WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            frame = pd.read_json(StringIO(zlib.decompress(row[2]).decode()), orient="table")
        except (ValueError, TypeError, zlib.error) as exc:
            raise DataSourceError("The local cache could not be read. Refresh this dataset to replace it.") from exc
        return Dataset(key, frame, row[0], row[1], "cached")

    def load(self, query, refresh=False):
        key = self.key(query)
        cached = None
        try:
            cached = self.cached(key)
        except DataSourceError:
            if not refresh:
                raise
        if cached and not refresh:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached.fetched_at)
            needs_positions = query.entity_type == "player" and "position" not in cached.frame
            needs_matchups = "matchup" not in cached.frame
            if age.total_seconds() < self.ttl_hours * 3600 and not needs_positions and not needs_matchups:
                return cached
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
        return Dataset(key, frame, source, stamp, "downloaded")

    @lru_cache(maxsize=4)
    def _read(self, key):
        return self.cached(key)

    def read(self, key):
        dataset = self._read(key)
        if dataset is None:
            raise DataSourceError("This cached dataset is no longer available. Load the data again.")
        return dataset
