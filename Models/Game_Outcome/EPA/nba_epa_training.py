"""Reconcile public NBA scoring and construct pre-action EPV training statistics."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from Models.Game_Outcome.EPA.NBA_EPA import FEATURES, state_features
from Models.Game_Outcome.EPA.nba_epa_data import ROOT, archive_source, prepare_archive
from sports_analytics.data import DataSourceError, Query
from sports_analytics.nfl_model_data import atomic_text
from sports_analytics.service import DataService

VERSION = 1


@dataclass
class NBAEPADataset:
    games: pd.DataFrame
    game_ids: list
    state_xx: np.ndarray
    state_xy: np.ndarray
    quality: dict


def reconcile_game(possessions, events, box):
    """Return exact-scoring team rows and state statistics, or reject the game.

    Source possession points are not labels: rewards come from validated
    scoreboard increments. Known technical FT rewards are kept separately.
    Ambiguous corrections, boundaries and scoring ownership quarantine a game.
    """
    if len(box) != 2 or box.team.nunique() != 2 or box.entity_id.nunique() != 2:
        raise ValueError("Incomplete team box scores")
    matchup = str(box.iloc[0].matchup).split(" @ ")
    if len(matchup) != 2:
        raise ValueError("Missing home/away matchup")
    home = box[box.team.eq(matchup[1])]
    away = box[box.team.eq(matchup[0])]
    if len(home) != 1 or len(away) != 1:
        raise ValueError("Inconsistent home/away teams")
    h, a = home.iloc[0], away.iloc[0]
    ids = {int(h.entity_id): h, int(a.entity_id): a}
    e = events.sort_values("order_index").copy()
    p = possessions.sort_values("start_order_index").copy()
    if e.empty or p.empty or e.order_index.duplicated().any():
        raise ValueError("Missing or duplicate event order")
    numeric = ["period", "order_index", "seconds_remaining"]
    if not np.isfinite(e[numeric].to_numpy(float)).all():
        raise ValueError("Missing event timing")
    scores = e[["score_home", "score_away"]].apply(pd.to_numeric, errors="coerce").ffill()
    if scores.iloc[0].isna().any() or not scores.iloc[0].eq(0).all():
        raise ValueError("Incomplete game-start scoreboard")
    if tuple(scores.iloc[-1]) != (float(h.pts), float(a.pts)):
        raise ValueError("Event final scores disagree with box scores")
    rewards = np.diff(scores.to_numpy(float), axis=0, prepend=np.zeros((1, 2)))
    if (rewards < 0).any() or (np.mod(rewards, 1) != 0).any() or (rewards > 4).any() or ((rewards > 0).sum(axis=1) > 1).any():
        raise ValueError("Ambiguous score correction or scoring increments")
    starts = p.start_order_index.to_numpy(int)
    ends = p.end_order_index.to_numpy(int)
    if (ends < starts).any() or (starts[1:] <= ends[:-1]).any():
        raise ValueError("Overlapping possession boundaries")
    orders = e.order_index.to_numpy(int)
    mapping = np.searchsorted(starts, orders, side="right")-1
    bounds = mapping >= 0
    safe = np.maximum(mapping, 0)
    bounds &= (orders <= ends[safe]) & (e.period.to_numpy(int) == p.period.to_numpy(int)[safe])
    mapping[~bounds] = -1
    text = (e.action_type.fillna("").astype(str) + " " + e.sub_type.fillna("").astype(str) + " " + e.description.fillna("").astype(str)).str.lower()
    technical = text.str.contains("technical|defensive three second|defensive 3 second", regex=True).to_numpy()
    admin = rewards[technical].sum(axis=0)
    ordinary = rewards.copy()
    ordinary[technical] = 0
    if ordinary[mapping < 0].sum() != 0:
        raise ValueError("Scoring event outside possession boundaries")
    actions = e.action_type.fillna("").astype(str).to_numpy()
    clocks = e.seconds_remaining.to_numpy(float)
    periods = e.period.to_numpy(int)
    event_teams = pd.to_numeric(e.team_id, errors="coerce").fillna(0).to_numpy(int)
    totals = {team: {"points": 0., "count": 0, "starts": np.zeros(len(FEATURES))} for team in ids}
    xs, ys, sample = [], [], []
    corrected = 0
    for i, row in enumerate(p.itertuples(index=False)):
        offense, defense = int(row.offense_team_id), int(row.defense_team_id)
        if offense not in ids or defense not in ids or offense == defense:
            raise ValueError("Invalid possession ownership")
        idx = np.flatnonzero(mapping == i)
        if not len(idx):
            raise ValueError("Possession has no events")
        own_col = 0 if offense == int(h.entity_id) else 1
        if ordinary[idx, 1-own_col].sum() > 0:
            raise ValueError("Nontechnical score attributed to opposing possession")
        own_rewards = ordinary[idx, own_col]
        points = float(own_rewards.sum())
        if not np.isfinite(row.points) or points != float(row.points):
            corrected += 1
        counted = str(row.count_as_possession).lower() == "true"
        if not counted:
            if points:
                raise ValueError("Uncounted possession has nontechnical scoring")
            continue
        start_clock = float(clocks[idx[0]])
        accumulated = 0.
        rebounds = 0
        first_x = None
        for j in idx:
            if not 0 <= clocks[j] <= (720 if periods[j] <= 4 else 300) or clocks[j] > start_clock:
                raise ValueError("Inconsistent possession clock")
            x = state_features(periods[j], clocks[j], start_clock-clocks[j], rebounds,
                               accumulated, row.possession_start_type, actions[j-1] if j else "")
            remaining = points-accumulated
            xs.append(x)
            ys.append(remaining)
            if first_x is None:
                first_x = x
            if len(sample) < 10:
                sample.append({"game_id": str(h.game_id), "period": int(periods[j]), "order_index": int(orders[j]),
                               "remaining_points": remaining, **dict(zip(FEATURES, map(float, x)))})
            accumulated += ordinary[j, own_col]
            if "rebound" in actions[j].lower() and event_teams[j] == offense:
                rebounds += 1
        totals[offense]["count"] += 1
        totals[offense]["points"] += points
        totals[offense]["starts"] += first_x
    if any(item["count"] <= 0 for item in totals.values()):
        raise ValueError("Missing offensive possessions")
    for team, item in totals.items():
        extra = float(admin[0 if team == int(h.entity_id) else 1])
        if item["points"]+extra != float(ids[team].pts):
            raise ValueError("Reconstructed possessions fail final-score reconciliation")
    if not xs:
        raise ValueError("No training states")
    date = pd.Timestamp(h.event_date)
    date = date.tz_localize("UTC") if date.tzinfo is None else date.tz_convert("UTC")
    rows = []
    shared_pace = float(np.mean([item["count"] for item in totals.values()]))
    for team, other in [(int(h.entity_id), int(a.entity_id)), (int(a.entity_id), int(h.entity_id))]:
        own, opposing = totals[team], totals[other]
        rows.append({"game_id": str(h.game_id), "team": str(ids[team].team), "opponent": str(ids[other].team),
                     "event_date": date.isoformat(), "available_at": (date.normalize()+pd.Timedelta(days=2)).isoformat(),
                     "is_home": int(team == int(h.entity_id)), "points": float(ids[team].pts),
                     "points_allowed": float(ids[other].pts), "possessions": shared_pace,
                     "offensive_possessions": own["count"], "defensive_possessions": opposing["count"],
                     "offensive_points": own["points"], "additional_points": float(ids[team].pts)-own["points"],
                     **{"start_"+name: float(value) for name, value in zip(FEATURES, own["starts"]/own["count"])},
                     **{"def_start_"+name: float(value) for name, value in zip(FEATURES, opposing["starts"]/opposing["count"])}})
    x, y = np.asarray(xs), np.asarray(ys)
    return rows, x.T@x, x.T@y, len(y), corrected, sample


class NBAEPAData:
    def __init__(self, directory=None, service=None):
        self.directory = Path(directory or ROOT/"data/modeling/nba/epa")
        self.service = service or DataService()
        self._memory = {}

    def _season(self, season):
        paths = {kind: self.directory/"raw"/archive_source(kind, season)[0] for kind in ("possessions", "play_by_play")}
        for kind, path in paths.items():
            if not path.exists():
                prepare_archive(self.directory, kind, season, download=True)
        boxes, source_ids = [], []
        for phase in ("regular", "postseason"):
            query = Query("NBA", season, "team", phase)
            cached = self.service.cached(self.service.key(query))
            if cached is None:
                try:
                    cached = self.service.load(query)
                except DataSourceError:
                    continue
            boxes.append(cached.frame)
            source_ids.append([cached.key, cached.fetched_at])
        if not boxes:
            raise DataSourceError("NBA EPA requires independent team box scores for reconciliation.")
        signature = {"version": VERSION, "features": list(FEATURES), "sources": source_ids,
                     "raw": {k: [p.stat().st_size, p.stat().st_mtime_ns] for k, p in paths.items()}}
        key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        if key in self._memory:
            return self._memory[key]
        manifest = self.directory/f"team_games_{season}.manifest.json"
        games_path = self.directory/f"team_games_{season}.csv"
        stats_path = self.directory/f"state_statistics_{season}.npz"
        if manifest.exists() and games_path.exists() and stats_path.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("identity") == key:
                with np.load(stats_path, allow_pickle=False) as stats:
                    result = NBAEPADataset(pd.read_csv(games_path, dtype={"game_id": str}),
                                           stats["game_ids"].tolist(), stats["xx"], stats["xy"], saved["quality"])
                self._memory[key] = result
                return result
        box = pd.concat(boxes, ignore_index=True)
        p = pd.read_csv(paths["possessions"], dtype={"game_id": str})
        cols = ["game_id", "order_index", "period", "seconds_remaining", "team_id", "action_type", "sub_type",
                "description", "score_home", "score_away"]
        events = pd.read_csv(paths["play_by_play"], usecols=cols, dtype={"game_id": str}, low_memory=False)
        p_groups = p.groupby("game_id").indices
        e_groups = events.groupby("game_id").indices
        rows, xx, xy, game_ids, samples, quarantine = [], [], [], [], [], []
        states_count = corrected_count = 0
        for game_id, paired in box.groupby("game_id", sort=True):
            try:
                if game_id not in p_groups or game_id not in e_groups:
                    raise ValueError("Missing possession/event coverage")
                values = reconcile_game(p.iloc[p_groups[game_id]], events.iloc[e_groups[game_id]], paired)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                quarantine.append({"game_id": game_id, "reason": str(exc)})
                continue
            game_rows, state_xx, state_xy, count, corrected, sample = values
            rows.extend(game_rows)
            xx.append(state_xx)
            xy.append(state_xy)
            game_ids.append(game_id)
            states_count += count
            corrected_count += corrected
            if len(samples) < 1000:
                samples.extend(sample)
        if not rows:
            raise DataSourceError("No NBA games passed event-score and ownership reconciliation.")
        quality = {"season": season, "source_games": box.game_id.nunique(), "reconciled_games": len(game_ids),
                   "quarantined_games": len(quarantine), "states": states_count,
                   "possession_point_labels_reconstructed": corrected_count,
                   "scoring_policy": "Scoreboard increments; technical rewards separate; ambiguous games quarantined.",
                   "exclusion_caution": "Filtered games may not be a random sample; exclusion reasons are retained."}
        result = NBAEPADataset(pd.DataFrame(rows), game_ids, np.asarray(xx), np.asarray(xy), quality)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_text(games_path, result.games.to_csv(index=False))
        atomic_text(self.directory/f"quarantine_{season}.json", json.dumps(quarantine, indent=2))
        atomic_text(self.directory/f"state_sample_{season}.csv", pd.DataFrame(samples).to_csv(index=False))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                np.savez_compressed(file, game_ids=np.asarray(game_ids), xx=result.state_xx, xy=result.state_xy)
            temporary.replace(stats_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        atomic_text(manifest, json.dumps({"identity": key, "inputs": signature, "quality": quality}, indent=2))
        self._memory[key] = result
        return result

    def prepare(self, seasons):
        datasets, notices = [], []
        cached_seasons = {season for season in seasons if all((self.directory/"raw"/archive_source(kind, season)[0]).exists()
                                                            for kind in ("possessions", "play_by_play"))}
        for season in sorted(set(seasons)):
            if cached_seasons and season not in cached_seasons:
                notices.append(f"NBA {season}-{str(season+1)[-2:]} event archives not cached; using reconciled available seasons.")
                continue
            try:
                dataset = self._season(season)
            except (DataSourceError, OSError, ValueError) as exc:
                notices.append(f"NBA EPA {season} unavailable: {exc}")
                continue
            datasets.append(dataset)
            notices.append(f"NBA {season}: {dataset.quality['reconciled_games']} reconciled games; {dataset.quality['quarantined_games']} quarantined.")
        if not datasets:
            raise DataSourceError("NBA EPA input preparation failed. " + " ".join(notices))
        games = pd.concat([d.games for d in datasets], ignore_index=True)
        if games.duplicated(["game_id", "team"]).any():
            raise DataSourceError("NBA EPA datasets overlap in game/team identities.")
        return NBAEPADataset(games, sum([d.game_ids for d in datasets], []),
                             np.concatenate([d.state_xx for d in datasets]), np.concatenate([d.state_xy for d in datasets]),
                             {str(d.quality['season']): d.quality for d in datasets}), notices
