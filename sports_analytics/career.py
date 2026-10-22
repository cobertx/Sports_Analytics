"""Player career season summaries, with a shared local cache for source tables.

Match by provider player ID across teams and seasons, never by display name.
Counting stats use season totals; rates retain the provider's original units.
"""

from collections import OrderedDict
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
import re
import sqlite3
import threading
import time
import zlib

import pandas as pd

from .data import DataSourceError, HttpClient
from .historical_links import market_metrics

ARCHIVE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/"
FLOORS = {"NBA": 1996, "NFL": 1999}
NBA_STATS = ["gp", "min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg_pct",
             "fg3m", "fg3a", "fg3_pct", "ftm", "fta", "ft_pct", "oreb", "dreb", "pf", "plus_minus"]


@dataclass
class Career:
    frame: pd.DataFrame
    searched: list[int]
    failed: list[int]
    phase: str
    source: str


def nba_season(year, phase, client):
    raw = pd.read_csv(StringIO(client.get(
        f"{ARCHIVE}nba_stats_player_season_stats/player_season_stats_{year + 1}.csv").text),
        dtype={"player_id": str}, low_memory=False)
    phase_name = "regular-season" if phase == "regular" else "playoffs"
    raw = raw[(raw["season_type"].str.lower() == phase_name) &
              (raw["measure_type"].str.lower() == "base") & (raw["per_mode"].str.lower() == "totals")].copy()
    # Prefer a combined-team row, if one is supplied, instead of double counting it.
    raw["_combined"] = pd.to_numeric(raw["team_id"], errors="coerce").eq(0)
    raw = raw.sort_values("_combined", ascending=False).drop_duplicates("player_id")
    frame = raw[["player_id", "team_abbreviation"] + [c for c in NBA_STATS if c in raw]].rename(
        columns={"player_id": "entity_id", "team_abbreviation": "team", "gp": "games"})
    return frame.reset_index(drop=True)


def nfl_season(year, phase, client):
    code = "reg" if phase == "regular" else "post"
    raw = pd.read_csv(StringIO(client.get(
        f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_{code}_{year}.csv").text),
        dtype={"player_id": str}, low_memory=False)
    raw = raw[raw["player_id"].notna()].copy()
    frame = pd.DataFrame({"entity_id": raw["player_id"], "team": raw["recent_team"] if "recent_team" in raw else raw["team"]})
    excluded = {"player_id", "player_name", "player_display_name", "position", "position_group",
                "headshot_url", "season", "season_type", "recent_team", "team"}
    stats = {}
    for column in raw:
        if column not in excluded:
            values = pd.to_numeric(raw[column], errors="coerce")
            if values.notna().any():
                stats[column] = values
    return pd.concat([frame, pd.DataFrame(stats, index=raw.index)], axis=1).reset_index(drop=True)


def season_value(rows, metric):
    """Aggregate transfers without summing percentages, means, or longest plays."""
    values = pd.to_numeric(rows[metric], errors="coerce").dropna()
    if values.empty:
        return None
    if len(rows) == 1:
        return float(values.iloc[0])
    if metric.endswith("_long"):
        return float(values.max())
    if metric.endswith(("_pct", "_avg")) or metric == "passing_cpoe":
        weights = {"fg_pct": "fg_att", "pat_pct": "pat_att", "fg3_pct": "fg3a", "ft_pct": "fta",
                   "passing_cpoe": "attempts"}
        weight = weights.get(metric)
        if weight not in rows:
            return None
        mask = rows[metric].notna() & rows[weight].notna()
        denominator = rows.loc[mask, weight].sum()
        return float((rows.loc[mask, metric] * rows.loc[mask, weight]).sum() / denominator) if denominator else None
    return float(values.sum())


class CareerService:
    def __init__(self, path, ttl_hours=24, workers=4):
        self.path = Path(path)
        self.ttl_hours = ttl_hours
        self.workers = workers
        self._guard = threading.Lock()
        self._locks = {}
        self._memory = OrderedDict()

    def _cached_table(self, key, download):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = time.monotonic()
            with self._guard:
                saved = self._memory.get(key)
                if saved and now - saved[0] < self.ttl_hours * 3600:
                    self._memory.move_to_end(key)
                    return saved[1]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS career_tables (key TEXT PRIMARY KEY, fetched_at TEXT, payload BLOB)")
                row = db.execute("SELECT fetched_at, payload FROM career_tables WHERE key = ?", (key,)).fetchone()
            fresh = row and (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < self.ttl_hours * 3600
            stale = None
            if row:
                try:
                    stale = pd.read_json(StringIO(zlib.decompress(row[1]).decode()), orient="table")
                except (ValueError, TypeError, zlib.error):
                    fresh = False
            if fresh and stale is not None:
                frame = stale
            else:
                frame = download()
                payload = zlib.compress(frame.to_json(orient="table", index=False).encode())
                with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                    db.execute("INSERT OR REPLACE INTO career_tables VALUES (?, ?, ?)",
                               (key, datetime.now(timezone.utc).isoformat(), payload))
            with self._guard:
                self._memory[key] = (now, frame)
                self._memory.move_to_end(key)
                while len(self._memory) > 64:
                    self._memory.popitem(last=False)
            return frame

    def available_seasons(self, sport, entity, name):
        self._validate_sport(sport)
        tag = "nba_stats_player_season_stats" if sport == "NBA" else "stats_player"
        repo = "sportsdataverse/sportsdataverse-data" if sport == "NBA" else "nflverse/nflverse-data"
        def discover():
            release = HttpClient().json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            pattern = r"player_season_stats_(\d{4})\.csv" if sport == "NBA" else r"stats_player_reg_(\d{4})\.csv"
            years = set()
            for asset in release["assets"]:
                match = re.fullmatch(pattern, asset["name"])
                if match:
                    year = int(match[1]) - (1 if sport == "NBA" else 0)
                    if FLOORS[sport] <= year <= date.today().year:
                        years.add(year)
            if not years:
                raise DataSourceError("The source did not list any published career seasons.")
            return pd.DataFrame({"season": sorted(years)})
        try:
            return self._cached_table(f"years:{sport}", discover)["season"].astype(int).tolist()
        except DataSourceError:
            # Public GitHub metadata may be rate-limited while downloads still work.
            end = date.today().year - (1 if sport == "NBA" else 0)
            return list(range(FLOORS[sport], end + 1))

    def season(self, sport, year, phase):
        self._validate_sport(sport)
        def download():
            try:
                frame = {"NBA": nba_season, "NFL": nfl_season}[sport](year, phase, HttpClient())
                frame["entity_id"] = frame["entity_id"].astype("string")
                return frame
            except (KeyError, ValueError, TypeError) as exc:
                raise DataSourceError("The career source returned an unexpected season-summary schema.") from exc
        return self._cached_table(f"summary:v1:{sport}:{year}:{phase}", download)

    def load(self, sport, entity, name, metric, phase):
        self._validate_sport(sport)
        years = self.available_seasons(sport, entity, name)
        points, failed = [], []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            tasks = {pool.submit(self.season, sport, year, phase): year for year in years}
            for task in as_completed(tasks):
                year = tasks[task]
                try:
                    frame = market_metrics(task.result(), sport, 'player')
                except DataSourceError:
                    failed.append(year)
                    continue
                selected = frame[frame["entity_id"] == entity]
                if not selected.empty and metric in selected:
                    value = season_value(selected, metric)
                    if value is not None:
                        points.append({"season": year, "value": value,
                                       "team": ", ".join(selected["team"].dropna().astype(str).unique())})
        source = {"NBA": "NBA Stats via SportsDataverse", "NFL": "nflverse"}[sport]
        frame = pd.DataFrame(points, columns=["season", "value", "team"]).sort_values("season")
        return Career(frame, years, sorted(failed), phase, source)

    @staticmethod
    def _validate_sport(sport):
        if sport not in FLOORS:
            raise DataSourceError("Player career history is available only for NBA and NFL.")


def main():
    """Optional command-line check of one player's available career history."""
    import argparse
    from dotenv import load_dotenv
    from .service import DataService
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description="Load player career season summaries")
    parser.add_argument("sport", choices=list(FLOORS))
    parser.add_argument("player_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--phase", choices=["regular", "postseason"], default="regular")
    args = parser.parse_args()
    service = CareerService(DataService().path.with_name("career.sqlite3"))
    try:
        result = service.load(args.sport, args.player_id, args.name, args.metric, args.phase)
    except DataSourceError as exc:
        print(str(exc))
        return 1
    print(result.frame.to_string(index=False))
    print(f"{len(result.frame)} player seasons; {len(result.searched)} source seasons checked; failures: {result.failed}")
    return 1 if result.frame.empty or result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
