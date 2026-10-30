"""Validated, cached NFL team/game observations for forecasting, not betting quotes.

Public nflverse schedules, team box scores, nflfastR PBP and PFR player snaps
are joined by game/team IDs. Player snaps are participation checks, never summed
to invent team plays. The CSV and its source manifest can be reused by models.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import tempfile

import pandas as pd

from .data import DataSourceError, HttpClient
from .result_cache import atomic_text

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "data" / "modeling" / "nfl"
RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
ALIASES = {"LA": "LAR", "STL": "LAR", "WAS": "WSH", "OAK": "LV", "SD": "LAC"}
SCRIMMAGE = {"pass", "run", "qb_kneel", "qb_spike"}
PBP_COLUMNS = {"game_id", "play_id", "posteam", "defteam", "play_type", "drive", "touchdown", "td_team",
               "extra_point_result", "two_point_conv_result", "two_point_attempt", "field_goal_result",
               "total_home_score", "total_away_score"}
VERSION = 3


def canonical_team(value):
    return ALIASES.get(str(value), str(value))


def offensive_points(pbp, teams):
    """Offensive TDs + associated conversions + FGs; other scoring stays separate.

    Following a defensive/ST TD, its conversion also belongs to non-offensive
    scoring. Penalty/no-play records must not create scoring events twice.
    """
    points = dict.fromkeys(teams, 0)
    last_td = None
    for row in pbp.sort_values("play_id").itertuples(index=False):
        if row.play_type == "no_play":
            continue
        offense = canonical_team(row.posteam)
        scorer = canonical_team(row.td_team)
        if row.touchdown == 1 and scorer in points:
            is_offensive = scorer == offense and row.play_type in SCRIMMAGE
            last_td = (scorer, is_offensive)
            if is_offensive:
                points[scorer] += 6
        if row.field_goal_result == "made" and offense in points:
            points[offense] += 3
        conversion = 1 if row.extra_point_result == "good" else 2 if row.two_point_conv_result == "success" else 0
        if conversion:
            if not last_td or last_td[0] != offense:
                raise DataSourceError("PBP conversion could not be linked to its touchdown; scoring attribution needs review.")
            if last_td[1]:
                points[offense] += conversion
            last_td = None
    return points


def epa_features(attack, defense):
    """Preserve signed EPA and separate passing/rushing samples for rating fits."""
    result = {}
    for prefix, plays in (("off", attack), ("def", defense)):
        values = pd.to_numeric(plays.get("epa", pd.Series(index=plays.index, dtype=float)), errors="coerce")
        result[f"{prefix}_epa_plays"] = int(values.count())
        result[f"{prefix}_epa_sum"] = values.sum(min_count=1)
        result[f"{prefix}_epa_per_play"] = values.mean()
        for kind, types in (("pass", ["pass", "qb_spike"]), ("rush", ["run", "qb_kneel"])):
            subset = values[plays["play_type"].isin(types)]
            result[f"{prefix}_{kind}_epa_plays"] = int(subset.count())
            result[f"{prefix}_{kind}_epa_sum"] = subset.sum(min_count=1)
    return result


def aggregate_team_games(pbp, schedule, box_scores, snaps):
    required_games = {"game_id", "season", "week", "game_type", "gameday", "home_team", "away_team", "home_score", "away_score"}
    if not PBP_COLUMNS <= set(pbp) or not required_games <= set(schedule):
        raise DataSourceError("NFL modeling source schema is incomplete. PBP scoring, possession and schedule fields are required.")
    if pbp.duplicated(["game_id", "play_id"]).any() or schedule.duplicated("game_id").any():
        raise DataSourceError("Duplicate NFL game/play identifiers found; model data was not overwritten.")
    if not {"game_id", "team", "offense_snaps", "defense_snaps"} <= set(snaps):
        raise DataSourceError("Player snap-count fields are missing from the modeling source.")
    if not {"game_id", "team"} <= set(box_scores):
        raise DataSourceError("Team box-score game/team fields are missing.")
    games = schedule[schedule["game_type"].isin(["REG", "POST"]) &
                     schedule[["home_score", "away_score"]].notna().all(axis=1)].copy()
    games = games.set_index("game_id")
    records = []
    for game_id, plays in pbp.groupby("game_id", sort=False):
        if game_id not in games.index:
            continue
        game = games.loc[game_id]
        ordered = plays.sort_values("play_id")
        for side in ("home", "away"):
            final = pd.to_numeric(ordered["total_" + side + "_score"], errors="coerce").dropna()
            if final.empty or final.iloc[-1] != game[side + "_score"]:
                raise DataSourceError(f"PBP final score does not match the completed schedule in NFL game {game_id}.")
        teams = [canonical_team(game["home_team"]), canonical_team(game["away_team"])]
        selected = plays[plays["play_type"].isin(SCRIMMAGE) & plays["two_point_attempt"].fillna(0).eq(0)].copy()
        selected["posteam"] = selected["posteam"].map(canonical_team)
        selected["defteam"] = selected["defteam"].map(canonical_team)
        if (selected[["posteam", "defteam"]].isin(teams).all(axis=1).eq(False).any() or
                selected["posteam"].eq(selected["defteam"]).any() or selected["drive"].isna().any()):
            raise DataSourceError(f"Invalid possession/drive fields in NFL game {game_id}.")
        offense = offensive_points(plays, teams)
        for side, team, opponent in [("home", teams[0], teams[1]), ("away", teams[1], teams[0])]:
            attack = selected[selected["posteam"].eq(team)]
            defense = selected[selected["defteam"].eq(team)]
            if attack.empty or defense.empty:
                raise DataSourceError(f"Incomplete offensive/defensive play coverage in NFL game {game_id}.")
            score = float(game[side + "_score"])
            opponent_score = float(game[("away" if side == "home" else "home") + "_score"])
            if offense[team] > score or offense[opponent] > opponent_score:
                raise DataSourceError(f"PBP offensive scoring exceeds final score in NFL game {game_id}.")
            gameday = pd.Timestamp(game["gameday"])
            # Conservative availability: only use completed games on earlier dates.
            available = (gameday + pd.Timedelta(days=1, hours=12)).tz_localize("UTC")
            records.append({"game_id": game_id, "season": int(game["season"]), "week": int(game["week"]),
                            "season_type": game["game_type"], "event_date": gameday.date().isoformat(),
                            "available_at": available.isoformat(), "team": team, "opponent": opponent,
                            "is_home": int(side == "home"), "neutral": int(game.get("location") == "Neutral"),
                            "offensive_plays": len(attack), "defensive_plays": len(defense),
                            "offensive_drives": attack["drive"].nunique(), "defensive_drives": defense["drive"].nunique(),
                            "offensive_points": offense[team], "offensive_points_allowed": offense[opponent],
                            "non_offensive_points": score - offense[team], "points": score, "points_allowed": opponent_score,
                            "passing_plays": int(attack["play_type"].eq("pass").sum()),
                            "rushing_plays": int(attack["play_type"].eq("run").sum()),
                            "kneels": int(attack["play_type"].eq("qb_kneel").sum()),
                            "spikes": int(attack["play_type"].eq("qb_spike").sum()),
                            **epa_features(attack, defense)})
    frame = pd.DataFrame(records)
    if frame.empty:
        raise DataSourceError("No completed games with full play-by-play are available for modeling.")
    box = box_scores.copy()
    box["team"] = box["team"].map(canonical_team)
    if box.duplicated(["game_id", "team"]).any():
        raise DataSourceError("Duplicate team box scores found in modeling sources.")
    stats = [column for column in box if column not in {"game_id", "team", "season", "week"} and
             pd.api.types.is_numeric_dtype(box[column])]
    box = box[["game_id", "team", *stats]].rename(columns={name: "box_" + name for name in stats})
    frame = frame.merge(box, on=["game_id", "team"], how="left", validate="one_to_one", indicator="box_coverage")
    snap = snaps.copy()
    snap["team"] = snap["team"].map(canonical_team)
    # A player's maximum is a lower bound, not a team total; substitutions overlap.
    checks = snap.groupby(["game_id", "team"])[["offense_snaps", "defense_snaps"]].max().rename(
        columns={"offense_snaps": "max_player_offense_snaps", "defense_snaps": "max_player_defense_snaps"}).reset_index()
    frame = frame.merge(checks, on=["game_id", "team"], how="left", validate="one_to_one")
    frame["snap_coverage"] = frame["max_player_offense_snaps"].notna()
    frame["offensive_ppp"] = frame["offensive_points"] / frame["offensive_plays"]
    frame["defensive_ppp"] = frame["offensive_points_allowed"] / frame["defensive_plays"]
    return frame.sort_values(["event_date", "game_id", "is_home"]).reset_index(drop=True)


class NflModelData:
    def __init__(self, directory=None, client=None):
        self.directory = Path(directory or DIRECTORY)
        self.client = client or HttpClient()
        self._source_notices = []

    def _source(self, name, url, refresh=False):
        path = self.directory / "raw" / name
        metadata = path.with_suffix(".source.json")
        if path.exists() and metadata.exists() and not refresh:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(json.loads(metadata.read_text())["fetched_at"])
            if age < timedelta(hours=24):
                return path
        try:
            response = self.client.get(url)
        except DataSourceError:
            if path.exists() and metadata.exists():
                self._source_notices.append(f"Saved {name} used after a source refresh failed.")
                return path
            raise
        content = response.content
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, suffix=".download", delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
            # Do not replace a valid cache with an HTML error page.
            header = content.split(b"\n", 1)[0].decode("utf-8-sig").strip()
            if "game_id" not in header:
                raise DataSourceError("NFL source did not return a game dataset; cached modeling files were preserved.")
            temporary.replace(path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        atomic_text(metadata, json.dumps({"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                                          "sha256": hashlib.sha256(content).hexdigest()}, indent=2))
        return path

    def prepare(self, seasons, refresh=False):
        seasons = sorted(set(seasons))
        if not seasons or any(not isinstance(year, int) or year < 2012 or year > datetime.now().year for year in seasons):
            raise ValueError("Choose NFL model seasons from 2012 through the current year.")
        self._source_notices = notices = []
        schedule_path = self._source("games.csv", SCHEDULE, refresh)
        schedule = pd.read_csv(schedule_path, dtype={"game_id": str})
        frames = []
        for year in seasons:
            prepared = self.directory / f"team_games_{year}.csv"
            manifest_path = prepared.with_suffix(".manifest.json")
            if prepared.exists() and manifest_path.exists() and not refresh:
                manifest = json.loads(manifest_path.read_text())
                age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest["prepared_at"])
                if manifest.get("version") == VERSION and age < timedelta(hours=24):
                    frames.append(pd.read_csv(prepared, dtype={"game_id": str}))
                    continue
            try:
                pbp_path = self._source(f"play_by_play_{year}.csv", f"{RELEASES}/pbp/play_by_play_{year}.csv", refresh)
                box_path = self._source(f"stats_team_week_{year}.csv", f"{RELEASES}/stats_team/stats_team_week_{year}.csv", refresh)
                snap_path = self._source(f"snap_counts_{year}.csv", f"{RELEASES}/snap_counts/snap_counts_{year}.csv", refresh)
                pbp = pd.read_csv(pbp_path, usecols=lambda name: name in PBP_COLUMNS or name == "epa",
                                  dtype={"game_id": str}, low_memory=False)
                box = pd.read_csv(box_path, dtype={"game_id": str}, low_memory=False)
                snaps = pd.read_csv(snap_path, dtype={"game_id": str}, low_memory=False)
                frame = aggregate_team_games(pbp, schedule[schedule["season"].eq(year)], box, snaps)
            except DataSourceError as exc:
                if prepared.exists():
                    frame = pd.read_csv(prepared, dtype={"game_id": str})
                    notices.append(f"{year}: saved modeling data used after a source error. {exc}")
                else:
                    notices.append(f"{year}: source unavailable. {exc}")
                    continue
            else:
                atomic_text(prepared, frame.to_csv(index=False))
                sources = [json.loads(path.with_suffix(".source.json").read_text())
                           for path in (schedule_path, pbp_path, box_path, snap_path)]
                atomic_text(manifest_path, json.dumps({"version": VERSION, "prepared_at": datetime.now(timezone.utc).isoformat(),
                    "season": year, "games": frame["game_id"].nunique(), "sources": sources,
                    "missing_box_rows": int(frame["box_coverage"].ne("both").sum()),
                    "missing_snap_rows": int((~frame["snap_coverage"]).sum()),
                    "missing_epa_rows": int(frame["off_epa_plays"].ne(frame["offensive_plays"]).sum()),
                    "play_definition": "pass (including sacks), run, kneel, spike; no conversions, penalties/no-play or kicks",
                    "scoring_definition": "offensive TDs and their conversions plus FGs; all other scoring separate"}, indent=2))
            frames.append(frame)
        if not frames:
            raise DataSourceError("No NFL modeling data is available. " + " ".join(notices))
        result = pd.concat(frames, ignore_index=True).drop_duplicates(["game_id", "team"])
        return result, notices
