"""Provider adapters. One row represents one team/player appearance in a game.

Identifiers remain strings; numeric stat columns retain the provider's units.
No synthetic data or player-to-team aggregation is used as a fallback.
"""

from dataclasses import asdict, dataclass
from datetime import date
from io import BytesIO, StringIO
import math
import os
import re

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class DataSourceError(RuntimeError):
    """An actionable, credential-safe error suitable for display in the UI."""


SOURCES = {
    "NBA": ("NBA Stats", "https://www.nba.com/stats"),
    "NFL": ("nflverse", "https://github.com/nflverse/nflverse-data"),
    "NCAAF": ("CollegeFootballData", "https://collegefootballdata.com"),
}
PLAYER_SPORTS = {"NBA", "NFL"}
META = ["sport", "season", "season_type", "game_id", "event_date", "week",
        "team", "opponent", "entity_type", "entity_id", "entity_name", "position", "matchup"]


@dataclass(frozen=True)
class Query:
    sport: str
    season: int
    entity_type: str = "team"
    season_type: str = "regular"
    team: str = ""  # NCAAF API scope; both sides of each game may be returned.
    week: int | None = None

    def __post_init__(self):
        if self.sport not in SOURCES:
            raise ValueError("Choose NBA, NFL, or NCAAF.")
        if self.entity_type not in {"team", "player"}:
            raise ValueError("Choose team or player statistics.")
        if self.entity_type == "player" and self.sport not in PLAYER_SPORTS:
            raise ValueError("Player statistics are available only for NBA and NFL. NCAAF supports team statistics only.")
        if self.season_type not in {"regular", "postseason"}:
            raise ValueError("Choose regular season or postseason.")
        minimum = {"NBA": 1946, "NFL": 1999, "NCAAF": 2001}[self.sport]
        if not isinstance(self.season, int) or not minimum <= self.season <= date.today().year:
            raise ValueError(f"Season must be between {minimum} and {date.today().year}.")
        if self.week is not None and (not isinstance(self.week, int) or not 0 <= self.week <= 25):
            raise ValueError("Week must be between 0 and 25.")
        if self.sport == "NCAAF" and not self.team.strip() and self.week is None:
            raise ValueError("For NCAAF, enter a school name or a week to scope the API request.")

    def parameters(self):
        return asdict(self)


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        retry = Retry(total=2, connect=1, read=0, backoff_factor=0.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"], respect_retry_after_header=False, raise_on_status=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers["User-Agent"] = "SportsAnalytics/0.1 (historical research)"

    def get(self, url, *, params=None, headers=None):
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=(5, 25))
        except requests.RequestException as exc:
            # Do not expose request headers, tokens, or upstream bodies in user errors.
            raise DataSourceError("The data source could not be reached. Check connectivity or retry later.") from exc
        if response.status_code in (401, 403):
            raise DataSourceError("Access was denied. For NCAAF check CFBD_API_KEY and endpoint access; NBA Stats may block some networks.")
        if response.status_code == 404:
            raise DataSourceError("No dataset exists for this season or endpoint. Try another historical season.")
        if response.status_code == 429:
            raise DataSourceError("The source's request quota was reached. Use cached data or retry later.")
        if not response.ok:
            raise DataSourceError(f"The data source returned HTTP {response.status_code}. Retry later.")
        return response

    def json(self, url, **kwargs):
        try:
            return self.get(url, **kwargs).json()
        except ValueError as exc:
            raise DataSourceError("The source returned an invalid JSON response.") from exc


def slug(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def numeric_stats(raw):
    """Keep numeric stats; split compound CFBD box-score counts explicitly."""
    result = {}
    for name, value in raw.items():
        key = slug(name)
        text = str(value).replace(",", "").strip()
        try:
            number = float(text)
            if math.isfinite(number):
                result[key] = number
        except (ValueError, TypeError):
            parts = re.fullmatch(r"(\d+)[/-](\d+)", text)
            if parts:
                labels = {"completion_attempts": ("completions", "attempts"),
                          "third_down_eff": ("third_down_conversions", "third_down_attempts"),
                          "fourth_down_eff": ("fourth_down_conversions", "fourth_down_attempts"),
                          "total_penalties_yards": ("penalties", "penalty_yards")}.get(slug(name))
                if labels:
                    result.update({label: float(v) for label, v in zip(labels, parts.groups())})
            # Time-of-possession and unrecognized compound values are not numeric features.
    return result


def normalize(frame, query):
    if frame.empty:
        raise DataSourceError("No completed game statistics match these filters.")
    frame = frame.copy()
    frame["sport"] = query.sport
    frame["season"] = query.season
    frame["season_type"] = query.season_type
    frame["entity_type"] = query.entity_type
    for column in META:
        if column not in frame:
            frame[column] = None
    for column in ("game_id", "entity_id", "entity_name", "team", "opponent", "position", "matchup"):
        frame[column] = frame[column].astype("string")
    if frame[["game_id", "entity_id", "entity_name"]].isna().any().any():
        raise DataSourceError("The source response is missing required game or entity identifiers.")
    frame["event_date"] = pd.to_datetime(frame["event_date"], errors="coerce", utc=True)
    frame["week"] = pd.to_numeric(frame["week"], errors="coerce")
    stats = [c for c in frame if c not in META and pd.api.types.is_numeric_dtype(frame[c])]
    stats = [c for c in stats if frame[c].notna().any()]
    if not stats:
        raise DataSourceError("The source response contains no numeric statistics.")
    frame = frame[META + stats].replace([float("inf"), float("-inf")], float("nan"))
    return frame.drop_duplicates(["game_id", "entity_id"]).sort_values(
        ["season", "event_date", "week", "game_id", "entity_id"], na_position="last"
    ).reset_index(drop=True)


def load_nba(query, client):
    season = f"{query.season}-{str(query.season + 1)[-2:]}"
    payload = client.json("https://stats.nba.com/stats/leaguegamelog", params={
        "Counter": 1000, "Direction": "ASC", "LeagueID": "00",
        "PlayerOrTeam": "T" if query.entity_type == "team" else "P",
        "Season": season, "SeasonType": "Regular Season" if query.season_type == "regular" else "Playoffs",
        "Sorter": "DATE", "DateFrom": "", "DateTo": "",
    }, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/",
                "Origin": "https://www.nba.com", "Accept": "application/json"})
    try:
        result = next(r for r in payload["resultSets"] if r["name"] == "LeagueGameLog")
        raw = pd.DataFrame(result["rowSet"], columns=result["headers"])
        if raw.empty:
            return normalize(raw, query)
        frame = pd.DataFrame({
            "game_id": raw["GAME_ID"], "event_date": raw["GAME_DATE"],
            "team": raw["TEAM_ABBREVIATION"], "opponent": raw["MATCHUP"].str.split().str[-1],
            "entity_id": raw["TEAM_ID" if query.entity_type == "team" else "PLAYER_ID"].astype(str),
            "entity_name": raw["TEAM_NAME" if query.entity_type == "team" else "PLAYER_NAME"],
            "position": raw.get("POSITION") if query.entity_type == "player" else None,
        })
        frame["matchup"] = [f"{opponent} @ {team}" if "vs." in matchup else f"{team} @ {opponent}"
                            for team, opponent, matchup in zip(frame["team"], frame["opponent"], raw["MATCHUP"])]
        for name in ("PTS", "REB", "AST", "STL", "BLK", "TOV", "MIN", "FGM", "FGA",
                     "FG_PCT", "FG3M", "FG3A", "FG3_PCT", "FTM", "FTA", "FT_PCT",
                     "OREB", "DREB", "PF", "PLUS_MINUS"):
            if name in raw:
                frame[slug(name)] = pd.to_numeric(raw[name], errors="coerce")
    except (KeyError, TypeError, StopIteration, ValueError) as exc:
        raise DataSourceError("NBA Stats returned an unexpected schema. Its endpoint may have changed.") from exc
    return normalize(frame, query)


def nfl_team_scoring(frame, schedule):
    """Attach actual final PF/PA by game and team; unavailable scores stay missing."""
    result = frame.copy()
    result["pf"], result["pa"] = float("nan"), float("nan")
    required = {"game_id", "home_team", "away_team", "home_score", "away_score"}
    if schedule is None or not required <= set(schedule):
        return result
    # Do not choose arbitrarily between duplicate game identities.
    games = schedule.drop_duplicates("game_id", keep=False).set_index("game_id")
    games = games.reindex(frame["game_id"].astype(str)).set_axis(frame.index)
    aliases = {"WAS": "WSH", "LA": "LAR", "STL": "LAR", "OAK": "LV", "SD": "LAC", "JAC": "JAX"}
    def canonical(column):
        return column.astype("string").replace(aliases)
    team, opponent = canonical(frame["team"]), canonical(frame["opponent"])
    home, away = canonical(games["home_team"]), canonical(games["away_team"])
    is_home = team.eq(home) & opponent.eq(away)
    is_away = team.eq(away) & opponent.eq(home)
    scores = games[["home_score", "away_score"]].apply(pd.to_numeric, errors="coerce")
    valid = ((is_home | is_away).fillna(False) & scores.notna().all(axis=1) &
             scores.ge(0).all(axis=1) & scores.mod(1).eq(0).all(axis=1))
    result["pf"] = scores["home_score"].where(is_home, scores["away_score"]).where(valid)
    result["pa"] = scores["away_score"].where(is_home, scores["home_score"]).where(valid)
    return result


def load_nfl(query, client):
    kind = query.entity_type
    url = f"https://github.com/nflverse/nflverse-data/releases/download/stats_{kind}/stats_{kind}_week_{query.season}.csv"
    try:
        raw = pd.read_csv(StringIO(client.get(url).text), dtype={"player_id": str, "game_id": str}, low_memory=False)
        raw = raw[raw["season_type"] == ("REG" if query.season_type == "regular" else "POST")].copy()
        if query.week is not None:
            raw = raw[raw["week"] == query.week]
        if kind == "player":
            # nflverse includes unassigned events with no player identity.
            # They belong in team totals, not in a player's history.
            raw = raw[raw["player_id"].notna()].copy()
        if raw.empty:
            return normalize(raw, query)
        team_col = "team" if "team" in raw else "recent_team"
        frame = pd.DataFrame({
            "week": raw["week"], "game_id": raw["game_id"], "team": raw[team_col],
            "opponent": raw["opponent_team"],
            "entity_id": raw["player_id"] if kind == "player" else raw[team_col],
            "entity_name": raw["player_display_name"].fillna(raw["player_id"]) if kind == "player" else raw[team_col],
            "position": raw.get("position") if kind == "player" else None,
        })
        # The nflverse game ID encodes away/home order; dates come from its schedule.
        parts = frame["game_id"].str.split("_")
        frame["matchup"] = parts.str[-2] + " @ " + parts.str[-1]
        schedule = None
        try:
            schedule = pd.read_csv(StringIO(client.get(
                "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv").text),
                dtype={"game_id": str}, usecols=lambda column: column in {
                    "game_id", "gameday", "away_team", "home_team", "home_score", "away_score"})
            dates = schedule.drop_duplicates("game_id").set_index("game_id")["gameday"]
            frame["event_date"] = frame["game_id"].map(dates)
        except (DataSourceError, KeyError, ValueError):
            # A failed schedule download must never invent dates or discard box scores.
            frame["event_date"] = None
        excluded = set(META) | {"player_id", "player_name", "player_display_name",
                                "headshot_url", "position", "position_group", "recent_team",
                                "opponent_team", "jersey_number"}
        if kind == "team":
            excluded.update({"pf", "pa"})
        stats = {name: pd.to_numeric(raw[name], errors="coerce") for name in raw if name not in excluded}
        frame = pd.concat([frame, pd.DataFrame(stats, index=raw.index)], axis=1)
        if kind == "team":
            frame = nfl_team_scoring(frame, schedule)
    except (KeyError, ValueError, pd.errors.ParserError) as exc:
        raise DataSourceError("nflverse returned an unexpected schema. Its dataset may have changed.") from exc
    result = normalize(frame, query)
    if kind == "team":
        # Persist the scoring schema even if the schedule was unavailable. The
        # selectors exclude missing values; don't repeatedly refresh a fresh cache.
        for name in ("pf", "pa"):
            if name not in result:
                result[name] = float("nan")
    return result


def load_nba_archive(query, client):
    """NBA Stats box scores published by SportsDataverse, keyed by ending year.

    Use the schedule to attach actual dates and opponents. Player rows without
    minutes are DNP records and must not become zero-stat appearances.
    """
    base = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/"
    year = query.season + 1
    kind = query.entity_type
    try:
        raw = pd.read_csv(StringIO(client.get(
            f"{base}nba_stats_{kind}_boxscores/{kind}_boxscores_{year}.csv").text),
            dtype={"game_id": str, "team_id": str, "person_id": str})
        schedule = pd.read_csv(BytesIO(client.get(
            f"{base}nba_stats_schedules/nba_schedule_{year}.csv.gz").content),
            compression="gzip", dtype={"game_id": str})
        raw = raw[pd.to_numeric(raw["season_type_id"], errors="coerce") ==
                  (2 if query.season_type == "regular" else 4)].copy()
        if raw.empty:
            return normalize(raw, query)
        raw["game_id"] = raw["game_id"].str.zfill(10)
        schedule["game_id"] = schedule["game_id"].str.zfill(10)
        raw = raw.merge(schedule[["game_id", "game_date", "home_team_abbreviation", "away_team_abbreviation"]]
                        .drop_duplicates("game_id"), on="game_id", how="inner", validate="many_to_one")
        minutes = raw["minutes"].astype("string").str.split(":", expand=True)
        raw["min"] = pd.to_numeric(minutes[0], errors="coerce")
        if 1 in minutes:
            raw["min"] += pd.to_numeric(minutes[1], errors="coerce").fillna(0) / 60
        if kind == "player":
            raw = raw[raw["min"] > 0].copy()
        if raw.empty:
            return normalize(raw, query)
        frame = pd.DataFrame({
            "game_id": raw["game_id"], "event_date": raw["game_date"], "team": raw["team_tricode"],
            "opponent": raw["away_team_abbreviation"].where(
                raw["team_tricode"] == raw["home_team_abbreviation"], raw["home_team_abbreviation"]),
            "entity_id": raw["person_id"] if kind == "player" else raw["team_id"],
            "entity_name": (raw["first_name"].fillna("") + " " + raw["family_name"].fillna("")).str.strip()
            if kind == "player" else raw["team_city"] + " " + raw["team_name"],
            "min": raw["min"],
            "position": raw.get("position") if kind == "player" else None,
            "matchup": raw["away_team_abbreviation"] + " @ " + raw["home_team_abbreviation"],
        })
        names = {"points": "pts", "rebounds_total": "reb", "assists": "ast", "steals": "stl",
                 "blocks": "blk", "turnovers": "tov", "field_goals_made": "fgm", "field_goals_attempted": "fga",
                 "field_goals_percentage": "fg_pct", "three_pointers_made": "fg3m", "three_pointers_attempted": "fg3a",
                 "three_pointers_percentage": "fg3_pct", "free_throws_made": "ftm", "free_throws_attempted": "fta",
                 "free_throws_percentage": "ft_pct", "rebounds_offensive": "oreb", "rebounds_defensive": "dreb",
                 "fouls_personal": "pf", "plus_minus_points": "plus_minus"}
        for original, canonical in names.items():
            if original in raw:
                frame[canonical] = pd.to_numeric(raw[original], errors="coerce")
        frame = normalize(frame, query)
        frame.attrs["source"] = "NBA Stats via SportsDataverse"
        return frame
    except (KeyError, TypeError, ValueError, pd.errors.MergeError) as exc:
        raise DataSourceError("The NBA archive returned an unexpected box-score or schedule schema.") from exc


def load_ncaaf(query, client):
    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        raise DataSourceError("Set CFBD_API_KEY in .env or your environment, then load NCAAF again. Get a key at collegefootballdata.com/key.")
    params = {"year": query.season, "seasonType": query.season_type, "classification": "fbs"}
    if query.team.strip():
        params["team"] = query.team.strip()
    if query.week is not None:
        params["week"] = query.week
    headers = {"Authorization": f"Bearer {key}"}
    base = "https://api.collegefootballdata.com"
    games = client.json(base + "/games", params=params, headers=headers)
    boxes = client.json(base + "/games/teams", params=params, headers=headers)
    try:
        completed = {str(game["id"]): game for game in games if game.get("completed") is True}
        rows = []
        for box in boxes:
            game = completed.get(str(box["id"]))
            if not game:
                continue
            for team in box["teams"]:
                team_name = team["team"]
                if query.team.strip() and team_name.casefold() != query.team.strip().casefold():
                    continue
                home = team_name == game["homeTeam"]
                common = {"game_id": str(game["id"]), "event_date": game["startDate"],
                          "week": game["week"], "team": team_name,
                          "opponent": game["awayTeam"] if home else game["homeTeam"],
                          "matchup": f"{game['awayTeam']} @ {game['homeTeam']}"}
                rows.append({**common, "entity_id": str(team["teamId"]), "entity_name": team_name,
                             **numeric_stats({"points": team.get("points"), **{
                                 s["category"]: s["stat"] for s in team["stats"]}})})
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError("CollegeFootballData returned an unexpected schema. Its API may have changed.") from exc
    return normalize(pd.DataFrame(rows), query)


def fetch(query, client=None):
    client = client or HttpClient()
    current_nba_season=date.today().year-(date.today().month<7)
    if query.sport=='NBA' and query.season>=current_nba_season:
        # The current NBA Stats release can lag behind opening night, while
        # stats.nba.com may time out. Published ESPN boxes include single games.
        from .nba_espn import load as load_nba_espn
        try:return load_nba_espn(query,client)
        except DataSourceError:pass
    if query.sport == "NBA" and 1996 <= query.season < date.today().year:
        try:
            return load_nba_archive(query, client)
        except DataSourceError:
            # Newly published or older seasons may need the original endpoint.
            pass
    return {"NBA": load_nba, "NFL": load_nfl, "NCAAF": load_ncaaf}[query.sport](query, client)
