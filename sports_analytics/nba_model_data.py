"""Prepare paired NBA team box scores from the existing historical adapter."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .data import DataSourceError, Query
from .nfl_model_data import atomic_text
from .service import DataService


def aggregate_team_games(frame):
    required = {"game_id", "team", "opponent", "matchup", "event_date", "pts", "fga", "fta", "oreb", "tov"}
    if not required <= set(frame):
        raise DataSourceError("NBA PPP requires points, shot attempts, free throws, offensive rebounds and turnovers.")
    frame = frame.copy().reset_index(drop=True)
    frame["event_date"] = pd.to_datetime(frame.event_date, utc=True, errors="coerce")
    frame["team"] = frame.team.replace({"BRK": "BKN", "CHO": "CHA", "PHO": "PHX"})
    frame["opponent"] = frame.opponent.replace({"BRK": "BKN", "CHO": "CHA", "PHO": "PHX"})
    if frame.duplicated(["game_id", "team"]).any():
        raise DataSourceError("NBA box scores contain duplicate team-game identities.")
    for name in ("pts", "fga", "fta", "oreb", "tov"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    frame["estimated_possessions"] = frame.fga + .44 * frame.fta - frame.oreb + frame.tov
    rows = []
    for game_id, pair in frame.groupby("game_id"):
        # Skip incomplete/unplayed fixtures; never manufacture the other team.
        if len(pair) != 2:
            continue
        a, b = pair.iloc[0], pair.iloc[1]
        if (a.team != b.opponent or b.team != a.opponent or pair.event_date.isna().any() or
                a.event_date != b.event_date or pair.matchup.isna().any() or a.matchup != b.matchup or
                not np.isfinite(pair[["pts", "fga", "fta", "oreb", "tov", "estimated_possessions"]].to_numpy()).all() or
                (pair[["pts", "fga", "fta", "oreb", "tov"]] < 0).any().any()):
            continue
        # Matchup is stored by both adapters as AWAY @ HOME.
        parts = a.matchup.split(" @ ")
        if len(parts) != 2:
            continue
        away, home = [part.replace("BRK", "BKN").replace("CHO", "CHA").replace("PHO", "PHX") for part in parts]
        possessions = float(pair.estimated_possessions.mean())
        if ({away, home} != {a.team, b.team} or away == home or (pair.estimated_possessions <= 0).any()
                or (pair.pts <= 0).any() or a.pts == b.pts):
            continue
        for own, opponent in ((a, b), (b, a)):
            rows.append({"game_id": str(game_id), "team": own.team, "opponent": opponent.team,
                         "event_date": own.event_date.isoformat(),
                         "available_at": (own.event_date.normalize() + pd.Timedelta(days=2)).isoformat(),
                         "points": float(own.pts), "points_allowed": float(opponent.pts),
                         "possessions": possessions, "is_home": int(own.team == home)})
    if not rows:
        raise DataSourceError("No complete paired NBA box scores are available for PPP modeling.")
    return pd.DataFrame(rows)


class NbaModelData:
    def __init__(self, directory=None, service=None):
        self.directory = Path(directory or Path(__file__).resolve().parent.parent / "data/modeling/nba")
        self.service = service or DataService()

    def prepare(self, seasons, refresh=False):
        frames, notices = [], []
        for season in seasons:
            for phase in ("regular", "postseason"):
                query = Query("NBA", season, "team", phase)
                try:
                    dataset = self.service.load(query, refresh=refresh)
                    prepared = aggregate_team_games(dataset.frame)
                except (DataSourceError, ValueError) as exc:
                    notices.append(f"NBA {season} {phase} unavailable: {exc}")
                    continue
                destination = self.directory / f"team_games_{season}_{phase}.csv"
                atomic_text(destination, prepared.to_csv(index=False))
                manifest = {"version": 1, "query": asdict(query), "source": dataset.source,
                            "source_fetched_at": dataset.fetched_at, "cache_key": dataset.key,
                            "games": prepared.game_id.nunique(), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                            "possessions": "mean of both teams' FGA + 0.44*FTA - OREB + TOV; includes overtime",
                            "availability": "game UTC date + 2 days; conservative completion lag"}
                atomic_text(destination.with_suffix(".manifest.json"), json.dumps(manifest, indent=2))
                if dataset.notice:
                    notices.append(dataset.notice)
                frames.append(prepared)
        if not frames:
            raise DataSourceError("No NBA modeling seasons available. " + " ".join(notices))
        frame = pd.concat(frames, ignore_index=True)
        if frame.duplicated(["game_id", "team"]).any():
            raise DataSourceError("NBA sources overlap on team-game identities; cannot fit a duplicated sample.")
        return frame, notices
