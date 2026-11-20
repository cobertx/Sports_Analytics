"""Read the most recent completed games from saved analysis inputs."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3

import pandas as pd

from .data import DataSourceError, SOURCES


@dataclass(frozen=True)
class SavedGame:
    game_id: str
    event_date: pd.Timestamp
    matchup: str
    saved_at: pd.Timestamp


def _latest(frame, saved_at):
    if 'event_date' not in frame:
        return None
    dates = pd.to_datetime(frame['event_date'], utc=True, errors='coerce', format='mixed')
    valid = dates.le(saved_at)
    if not valid.any():
        return None
    # Several games can start together. Use a stable choice among those finals.
    latest = dates[valid].max()
    candidates = frame.loc[dates.eq(latest)]
    order = [column for column in ('game_id', 'is_home') if column in candidates]
    row = (candidates.sort_values(order) if order else candidates).iloc[-1]
    matchup = row.get('matchup')
    if pd.isna(matchup) or not matchup:
        team, opponent = row.get('team'), row.get('opponent')
        if pd.notna(team) and pd.notna(opponent):
            home = row.get('is_home')
            if pd.isna(home):
                matchup = f'{team} vs {opponent}'
            else:
                matchup = f'{opponent} @ {team}' if home else f'{team} @ {opponent}'
        else:
            matchup = ''
    return SavedGame(str(row.get('game_id', '')), latest, matchup, saved_at)


def _revision(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


@lru_cache(maxsize=256)
def _prepared_game(path, revision):
    columns = {'event_date', 'game_id', 'team', 'opponent', 'matchup', 'is_home'}
    try:
        frame = pd.read_csv(path, usecols=columns.__contains__, dtype={'game_id': str})
    except (OSError, ValueError):
        return None
    return _latest(frame, pd.Timestamp(revision[0], unit='ns', tz='UTC'))


class GameDataStatus:
    def __init__(self, data, modeling_root):
        self.data = data
        self.modeling_root = Path(modeling_root)
        self._cached_games = lru_cache(maxsize=1)(self._read_cached_games)

    def _read_cached_games(self, _revision):
        # These tables contain box scores. Schedules and forecast targets are
        # stored elsewhere and cannot advance the displayed game date.
        games = []
        with self.data.connect() as db:
            rows = db.execute("""
                SELECT key, json_extract(query, '$.sport') FROM datasets
                WHERE json_extract(query, '$.entity_type') = 'team'
            """).fetchall()
        for key, sport in rows:
            if sport not in SOURCES:
                continue
            try:
                dataset = self.data.cached(key)
                if dataset is None:
                    continue
                saved_at = pd.to_datetime(dataset.fetched_at, utc=True, errors='coerce')
                game = _latest(dataset.frame, saved_at)
            except (DataSourceError, ValueError):
                continue
            if game is not None:
                games.append((sport, game))
        return games

    def latest(self):
        games = {}

        def keep(sport, game):
            current = games.get(sport)
            if game and (not current or (game.event_date, game.saved_at) > (current.event_date, current.saved_at)):
                games[sport] = game

        db = self.data.path
        if revision := _revision(db):
            try:
                for sport, game in self._cached_games((revision, _revision(db.with_name(db.name + '-wal')))):
                    keep(sport, game)
            except sqlite3.Error:
                # Prepared inputs remain usable if the box-score cache cannot be read.
                pass

        for sport in SOURCES:
            directory = self.modeling_root / sport.lower()
            for pattern in ('team_games_*.csv', '*/team_games_*.csv'):
                for path in sorted(directory.glob(pattern)):
                    if revision := _revision(path):
                        keep(sport, _prepared_game(path, revision))
        return games
