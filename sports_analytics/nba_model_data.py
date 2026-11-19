"""Prepare paired NBA team box scores from the existing historical adapter."""

from dataclasses import asdict, replace
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
from models.replay import fingerprint

from .data import DataSourceError, Query
from .nfl_model_data import atomic_text
from .service import DataService

NBA_PACE_INPUT_METHODS = frozenset({"pace", "aode", "ff"})
BOX_COUNTS = ("fgm", "fga", "fg3m", "ftm", "fta", "oreb", "dreb", "tov")
FACTOR_INPUTS = frozenset((*BOX_COUNTS, "opponent_dreb"))


def elapsed_minutes(values):
    """Accept elapsed or five-player team minutes from complete paired boxes."""
    if values is None:
        return None
    minutes = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    minutes = np.where(minutes >= 240 - .15, minutes / 5, minutes)
    overtime = np.rint((minutes - 48) / 5)
    if (not np.isfinite(minutes).all() or (overtime < 0).any()
            or not np.allclose(minutes, 48 + 5 * overtime, rtol=0, atol=.15)
            or overtime[0] != overtime[1]):
        return None
    return int(48 + 5 * overtime[0])


def aggregate_team_games(frame, *, include_box_counts=False):
    required = {"game_id", "team", "opponent", "matchup", "event_date", "pts", "fga", "fta", "oreb", "tov"}
    if not required <= set(frame):
        raise DataSourceError("NBA PPP requires points, shot attempts, free throws, offensive rebounds and turnovers.")
    frame = frame.copy().reset_index(drop=True)
    frame["event_date"] = pd.to_datetime(frame.event_date, utc=True, errors="coerce", format='mixed')
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
        duration = elapsed_minutes(pair.get("min"))
        available=(pd.to_datetime(pair['available_at'],utc=True,format='mixed').max() if 'available_at' in pair else
                   a.event_date.normalize()+pd.Timedelta(days=2))
        for own, opponent in ((a, b), (b, a)):
            row = {"game_id": str(game_id), "team": own.team, "opponent": opponent.team,
                         "event_date": own.event_date.isoformat(),
                         "available_at": available.isoformat(),
                         "points": float(own.pts), "points_allowed": float(opponent.pts),
                         "possessions": possessions, "elapsed_minutes": duration, "is_home": int(own.team == home)}
            if include_box_counts:
                row.update({field: own.get(field) for field in BOX_COUNTS}, opponent_dreb=opponent.get("dreb"))
            rows.append(row)
    if not rows:
        raise DataSourceError("No complete paired NBA box scores are available for PPP modeling.")
    return pd.DataFrame(rows)


class NbaModelData:
    include_box_counts = False

    def __init__(self, directory=None, service=None):
        self.directory = Path(directory or Path(__file__).resolve().parent.parent / "data/modeling/nba")
        self.service = service or DataService()
        self._prepared = OrderedDict()
        self._prepared_lock = Lock()

    @staticmethod
    def _revisions(paths):
        try:return tuple((s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino) for p in paths for s in [p.stat()])
        except OSError:return None

    def _prepare_dataset(self,dataset,query):
        destination=self.directory/f'team_games_{query.season}_{query.season_type}.csv'
        metadata=destination.with_suffix('.manifest.json')
        key=(query.season,query.season_type,dataset.key,dataset.source,dataset.fetched_at,fingerprint(dataset.frame))
        with self._prepared_lock:
            cached=self._prepared.get(key)
            if cached is not None and cached[1]==self._revisions([destination,metadata]):
                self._prepared.move_to_end(key)
                return cached[0].copy(deep=True)
            prepared=aggregate_team_games(dataset.frame,include_box_counts=True) if self.include_box_counts else aggregate_team_games(dataset.frame)
            prepared['season_type']=query.season_type
            atomic_text(destination,prepared.to_csv(index=False))
            manifest={"version":1,"query":asdict(query),"source":dataset.source,
                "source_fetched_at":dataset.fetched_at,"cache_key":dataset.key,
                "games":prepared.game_id.nunique(),"sha256":hashlib.sha256(destination.read_bytes()).hexdigest(),
                "possessions":"mean of both teams' FGA + 0.44*FTA - OREB + TOV; includes overtime",
                "availability":"Observed source availability; legacy rows use UTC game date + 2 days"}
            atomic_text(metadata,json.dumps(manifest,indent=2))
            self._prepared[key]=(prepared.copy(deep=True),self._revisions([destination,metadata]))
            self._prepared.move_to_end(key)
            while len(self._prepared)>8:self._prepared.popitem(last=False)
            return prepared

    def prepare(self, seasons, refresh=False):
        from .history_freshness import active_season, active_phases
        frames, notices = [], []
        for season in seasons:
            for phase in ("regular", "postseason"):
                query = Query("NBA", season, "team", phase)
                try:
                    dataset = self.service.load(query, refresh=refresh)
                    if getattr(dataset,'status',None)=='stale cache':
                        if season>=active_season('NBA') and phase in active_phases('NBA'):
                            raise DataSourceError('Current NBA team results could not be refreshed. '+dataset.notice)
                    prepared = self._prepare_dataset(dataset,query)
                except (DataSourceError, ValueError) as exc:
                    if season>=active_season('NBA') and phase in active_phases('NBA'):raise
                    notices.append(f"NBA {season} {phase} unavailable: {exc}")
                    continue
                if dataset.notice:
                    notices.append(dataset.notice)
                frames.append(prepared)
        if not frames:
            raise DataSourceError("No NBA modeling seasons available. " + " ".join(notices))
        frame = pd.concat(frames, ignore_index=True)
        if frame.duplicated(["game_id", "team"]).any():
            raise DataSourceError("NBA sources overlap on team-game identities; cannot fit a duplicated sample.")
        return frame, notices


def with_player_minutes(teams, players):
    """Recover duration from complete ESPN player boxes rounded to minutes."""
    keys = ["game_id", "team"]
    required = {*keys, "min", "pts", "available_at", "fgm", "ftm"}
    if not required <= set(players) or not set(keys + ["pts", "fgm", "ftm"]) <= set(teams):
        return teams
    stats = ["min", "pts", "fgm", "ftm"]
    observed = pd.to_datetime(players.available_at, utc=True, errors="coerce", format="mixed")
    complete = players[stats].notna().all(axis=1) & observed.notna()
    groups = players.loc[complete].assign(available_at=observed[complete]).groupby(keys)
    totals = groups[stats].sum().join(groups.available_at.max().rename("available"))
    totals["participants"] = groups.size()
    joined = teams.join(totals.add_prefix("players_"), on=keys)
    overtime = ((joined.players_min - 240) / 25).round()
    minutes = 240 + 25 * overtime
    valid = (overtime.ge(0) & joined.players_min.sub(minutes).abs().le(8)
             & joined.players_participants.ge(5)
             & joined.pts.eq(joined.players_pts) & joined.fgm.eq(joined.players_fgm)
             & joined.ftm.eq(joined.players_ftm)).fillna(False)
    # Both sides must independently confirm the same elapsed time. A
    # partial release cannot silently become a regulation observation.
    valid &= valid.groupby(joined.game_id).transform('all')
    valid &= minutes.groupby(joined.game_id).transform('nunique').eq(1)
    result = teams.copy()
    if "min" not in result:
        result["min"] = np.nan
    fill = valid & result["min"].isna()
    result.loc[fill, "min"] = minutes.loc[fill]
    team_available = pd.to_datetime(result.available_at, utc=True, format="mixed") if "available_at" in result else (
        pd.to_datetime(result.event_date, utc=True, format="mixed").dt.normalize() + pd.Timedelta(days=2))
    player_available = joined.players_available
    result["available_at"] = team_available.where(~fill | team_available.ge(player_available), player_available)
    return result


class NbaPaceData(NbaModelData):
    """Maintain a duration-aware archive without changing other model inputs."""
    include_box_counts = True

    def __init__(self, directory=None, service=None):
        super().__init__(directory or Path(__file__).resolve().parents[1] / "data/modeling/nba/pace", service)

    def _prepare_dataset(self, dataset, query):
        frame = dataset.frame
        if dataset.source.startswith("ESPN") and ("min" not in frame or frame["min"].isna().any()):
            player_query = Query("NBA", query.season, "player", query.season_type)
            try:
                players = self.service.load(player_query)
            except DataSourceError:
                players = None
            if players is not None:
                dataset = replace(dataset, frame=with_player_minutes(frame, players.frame))
        return super()._prepare_dataset(dataset, query)
