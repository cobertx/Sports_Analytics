"""Public NBA EPA/EPV input preparation and readiness checks, not a forecast.

SportsDataverse archives use ENDING years; project seasons use STARTING years.
Raw inputs stay separate from box-score PPP and saved betting quotes.
"""

from datetime import datetime, timezone
import csv
import gzip
import hashlib
import json
from pathlib import Path
import tempfile

import pandas as pd

from sports_analytics.data import DataSourceError, HttpClient, Query
from sports_analytics.nfl_model_data import atomic_text
from sports_analytics.service import DataService

RELEASES = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
SOURCES = {
    "possessions": ("nba_stats_possessions", "nba_possessions",
                    {"game_id", "period", "possession_number", "offense_team_id", "defense_team_id", "points",
                     "count_as_possession", "start_order_index", "end_order_index", "possession_start_type"}),
    "play_by_play": ("nba_stats_pbp", "nba_play_by_play",
                     {"game_id", "period", "order_index", "possession_number", "seconds_remaining",
                      "action_type", "score_home", "score_away"}),
}
ROOT = Path(__file__).resolve().parents[3]


def archive_source(kind, season):
    tag, stem, _ = SOURCES[kind]
    filename = f"{stem}_{season + 1}.csv.gz"
    return filename, f"{RELEASES}/{tag}/{filename}"


def archive_columns(path, kind):
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        columns = next(csv.reader(stream), [])
    missing = SOURCES[kind][2] - set(columns)
    if missing:
        raise DataSourceError(f"{kind} archive is missing required columns: {', '.join(sorted(missing))}.")
    return columns


def prepare_archive(directory, kind, season, *, download=False, refresh=False, client=None):
    filename, url = archive_source(kind, season)
    destination = directory / "raw" / filename
    if destination.exists() and not refresh:
        archive_columns(destination, kind)
        return destination
    if not download:
        raise DataSourceError(f"Missing local {kind} archive for {season}-{str(season + 1)[-2:]}. Run with --download.")
    response = (client or HttpClient()).get(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(response.content)
        archive_columns(temporary, kind)
        # Read through EOF, checking gzip completeness before replacing a cache.
        with gzip.open(temporary, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(destination)
        atomic_text(destination.with_suffix(".manifest.json"), json.dumps({
            "source": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "project_season": season, "archive_ending_year": season + 1, "sha256": digest,
        }, indent=2))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def audit_archives(possessions, play_by_play, box_scores=None):
    """Audit raw coverage and scoring; outcome columns are never EPV features."""
    pcols = archive_columns(possessions, "possessions")
    ecols = archive_columns(play_by_play, "play_by_play")
    totals, possession_games, event_games, seen = {}, set(), set(), set()
    rows = excluded = invalid = duplicates = administrative_points = counted = 0
    lineup_rows = lineup_complete = 0
    lineups = [f"{side}_player_{i}" for side in ("off", "def") for i in range(1, 6)]
    usecols = list(SOURCES["possessions"][2] | (set(lineups) & set(pcols)))
    for chunk in pd.read_csv(possessions, compression="gzip", dtype="string", usecols=usecols, chunksize=50000):
        chunk["game_id"] = chunk.game_id.str.zfill(10)
        keep = chunk.game_id.str.startswith(("002", "004"), na=False)
        excluded += int((~keep).sum())
        chunk = chunk[keep].copy()
        rows += len(chunk)
        possession_games.update(chunk.game_id)
        points = pd.to_numeric(chunk.points, errors="coerce")
        count_as = chunk.count_as_possession.str.casefold()
        valid = points.ge(0) & points.mod(1).eq(0) & count_as.isin(["true", "false"]) & chunk.offense_team_id.notna() & chunk.defense_team_id.notna()
        valid &= chunk.offense_team_id.ne(chunk.defense_team_id) & chunk.offense_team_id.ne("0") & chunk.defense_team_id.ne("0")
        invalid += int((~valid).sum())
        counted += int(count_as.eq("true").sum())
        administrative_points += float(points[count_as.eq("false")].sum())
        for game_id, period, number in chunk[["game_id", "period", "possession_number"]].itertuples(index=False, name=None):
            identity = (game_id, period, number)
            duplicates += identity in seen
            seen.add(identity)
        score_rows = chunk[valid].assign(points=points[valid])
        for identity, value in score_rows.groupby(["game_id", "offense_team_id"]).points.sum().items():
            totals[identity] = totals.get(identity, 0.) + float(value)
        if set(lineups) <= set(chunk):
            players = chunk[lineups].apply(pd.to_numeric, errors="coerce")
            complete = players.gt(0).all(axis=1) & players.nunique(axis=1).eq(10)
            lineup_complete += int(complete.sum())
            lineup_rows += len(chunk)
    event_rows = excluded_events = 0
    final_events = {}
    usecols = ["game_id", "period", "order_index", "possession_number", "seconds_remaining", "score_home", "score_away"]
    for chunk in pd.read_csv(play_by_play, compression="gzip", dtype="string", usecols=usecols, chunksize=100000):
        ids = chunk.game_id.str.zfill(10)
        keep = ids.str.startswith(("002", "004"), na=False)
        excluded_events += int((~keep).sum())
        event_rows += int(keep.sum())
        event_games.update(ids[keep])
        chunk = chunk[keep].copy()
        chunk["game_id"] = ids[keep]
        for name in ("period", "order_index", "score_home", "score_away"):
            chunk[name] = pd.to_numeric(chunk[name], errors="coerce")
        last = chunk.dropna(subset=["period", "order_index", "score_home", "score_away"])
        last = last.sort_values(["period", "order_index"]).groupby("game_id", sort=False).tail(1)
        for row in last.itertuples(index=False):
            item = (int(row.period), int(row.order_index), float(row.score_home), float(row.score_away))
            if row.game_id not in final_events or item[:2] > final_events[row.game_id][:2]:
                final_events[row.game_id] = item
    compared, mismatches, missing = 0, [], []
    pbp_compared, pbp_mismatches = 0, []
    if box_scores is not None and not box_scores.empty:
        for row in box_scores[["game_id", "entity_id", "pts"]].itertuples(index=False):
            game_id, team_id = str(row.game_id).zfill(10), str(row.entity_id)
            observed = totals.get((game_id, team_id))
            if observed is None:
                missing.append({"game_id": game_id, "team_id": team_id})
                continue
            compared += 1
            if abs(observed - float(row.pts)) > 1e-9:
                mismatches.append({"game_id": game_id, "team_id": team_id,
                                   "possession_points": observed, "box_score_points": float(row.pts)})
        if {"team", "matchup"} <= set(box_scores):
            for row in box_scores[["game_id", "team", "matchup", "pts"]].itertuples(index=False):
                final = final_events.get(str(row.game_id).zfill(10))
                parts = str(row.matchup).split(" @ ")
                if final is None or len(parts) != 2 or row.team not in parts:
                    continue
                pbp_compared += 1
                points = final[2] if row.team == parts[1] else final[3]
                if points != float(row.pts):
                    pbp_mismatches.append({"game_id": str(row.game_id), "team": str(row.team),
                                           "pbp_points": points, "box_score_points": float(row.pts)})
    return {
        "possession_rows": rows, "counted_possessions": counted, "possession_games": len(possession_games),
        "event_rows": event_rows, "event_games": len(event_games),
        "games_missing_events": sorted(possession_games - event_games),
        "games_missing_possessions": sorted(event_games - possession_games),
        "excluded_non_regular_postseason_possessions": excluded,
        "excluded_non_regular_postseason_events": excluded_events,
        "invalid_possession_rows": invalid, "duplicate_possession_keys": duplicates,
        "points_in_non_counted_records": administrative_points,
        "complete_ten_player_lineup_rows": lineup_complete, "lineup_rows_checked": lineup_rows,
        "lineup_caution": "Nonzero unique IDs do not establish correct lineup reconstruction; audit against box scores/substitutions.",
        "box_score_team_rows_compared": compared, "score_mismatches": mismatches,
        "pbp_box_score_team_rows_compared": pbp_compared, "pbp_score_mismatches": pbp_mismatches,
        "box_score_team_rows_missing_possessions": missing,
        "shot_clock_column_available": any("shot_clock" in name.lower() or "shotclock" in name.lower() for name in ecols),
        "tracking_available": False,
        "tracking_note": "Shot coordinates are event locations, not continuous player/ball tracking.",
        "nba_epa_ready": False,
        "remaining_work": ["Validate possession boundaries, administrative scoring and lineup attribution.",
                           "Build pre-action states and same-offense remaining-possession-point labels.",
                           "Train and chronologically validate an event-based EPV model.",
                           "Fit pregame efficiency/pace and a joint score distribution with overtime/settlement.",
                           "Evaluate held-out line probabilities, calibration, Brier/log loss and tail coverage.",
                           "Archive approved model artifacts before enabling NBA EPA in the dashboard."],
    }


def prepare_inputs(seasons, directory=None, *, download=False, refresh=False, service=None):
    directory = Path(directory or ROOT / "data/modeling/nba/epa")
    service = service or DataService()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "nba_epa_ready": False, "seasons": {}}
    for season in sorted(set(seasons)):
        Query("NBA", season)  # Validate project season convention and range.
        paths = {kind: prepare_archive(directory, kind, season, download=download, refresh=refresh) for kind in SOURCES}
        cached = [service.cached(service.key(Query("NBA", season, "team", phase))) for phase in ("regular", "postseason")]
        frames = [dataset.frame for dataset in cached if dataset is not None]
        box_scores = pd.concat(frames, ignore_index=True) if frames else None
        audit = audit_archives(paths["possessions"], paths["play_by_play"], box_scores)
        audit["raw_sources"] = {kind: str(path) for kind, path in paths.items()}
        audit["box_score_cache_available"] = bool(frames)
        report["seasons"][str(season)] = audit
    atomic_text(directory / "readiness.json", json.dumps(report, indent=2))
    return report
