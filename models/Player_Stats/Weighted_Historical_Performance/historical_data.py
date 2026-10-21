"""Explicit history preparation; rendering forecasts only reads saved inputs.

NBA/NFL reuse the application's public adapters. NCAAF uses a separate CFBD
player adapter so the Historical Data team-only interface is preserved.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

import pandas as pd

from sports_analytics.data import DataSourceError, HttpClient, Query
from sports_analytics.service import DataService

ROOT = Path(__file__).resolve().parents[3] / "data" / "modeling" / "player_stats" / "whp"


def identity_name(value):
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).strip().split()
    while words and words[-1] in {"jr", "sr", "ii", "iii", "iv"}:
        words.pop()
    return " ".join(words)


def seasons_for(sport, start):
    date = pd.Timestamp(start)
    season = date.year - int(date.month < (10 if sport == "NBA" else 3))
    return [season - 1, season]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, allow_nan=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def model_frame(frame):
    data = frame.copy()
    if "passing_interceptions" in data:
        observed = pd.to_numeric(data["passing_interceptions"], errors="coerce")
        data["interceptions"] = (pd.to_numeric(data["interceptions"], errors="coerce").combine_first(observed)
                                  if "interceptions" in data else observed)
    data["event_date"] = pd.to_datetime(data["event_date"], utc=True, errors="coerce", format="mixed")
    # Box scores provide start times/dates, not publication timestamps. This
    # conservative lag excludes the entire game until the following UTC day
    # (or 8h after an explicit start). It is an availability proxy, not a PIT feed.
    dates = data["event_date"]
    proxy = dates + pd.Timedelta(hours=8)
    midnight = dates.eq(dates.dt.normalize())
    proxy = proxy.where(~midnight, dates + pd.Timedelta(days=1))
    if "available_at" in data:
        data["available_at"] = pd.to_datetime(data["available_at"], utc=True, errors="coerce", format="mixed").fillna(proxy)
    else:
        data["available_at"] = proxy
    return data.dropna(subset=["entity_id", "entity_name", "event_date", "available_at"])


CFBD_FIELDS = {
    ("passing", "YDS"): "passing_yards", ("passing", "TD"): "passing_tds",
    ("passing", "INT"): "interceptions", ("rushing", "CAR"): "carries",
    ("rushing", "YDS"): "rushing_yards", ("rushing", "TD"): "rushing_tds",
    ("receiving", "REC"): "receptions", ("receiving", "YDS"): "receiving_yards",
    ("receiving", "TD"): "receiving_tds",
}


def cfbd_player_rows(games, boxes, team):
    """Join completed games by IDs and retain explicit per-athlete observations."""
    rows = {}
    try:
        complete = {str(g["id"]): g for g in games if g.get("completed") is True}
        for box in boxes:
            game = complete.get(str(box["id"]))
            if game is None:
                continue
            for side in box["teams"]:
                if identity_name(side["team"]) != identity_name(team):
                    continue
                home = identity_name(side["team"]) == identity_name(game["homeTeam"])
                for category in side["categories"]:
                    cat = category["name"].lower()
                    for stat in category["types"]:
                        label = stat["name"].upper()
                        target = CFBD_FIELDS.get((cat, label))
                        if target is None and not (cat == "passing" and label in {"C/ATT", "COMP/ATT"}):
                            continue
                        for athlete in stat["athletes"]:
                            key = (str(box["id"]), str(athlete["id"]))
                            row = rows.setdefault(key, {"game_id": key[0], "entity_id": key[1],
                                "entity_name": athlete["name"], "team": side["team"],
                                "opponent": game["awayTeam"] if home else game["homeTeam"],
                                "event_date": game["startDate"], "season": game.get("season"),
                                "season_type": game.get("seasonType", "unknown"),
                                "position": {"passing": "QB", "rushing": "RB", "receiving": "WR"}.get(cat),
                                "participation_status": "observed box-score participant"})
                            text = str(athlete["stat"]).replace(",", "").strip()
                            if target:
                                value = pd.to_numeric(text, errors="coerce")
                                if pd.notna(value):
                                    if target in row and row[target] != value:
                                        raise DataSourceError("CFBD returned conflicting player/game observations.")
                                    row[target] = float(value)
                            else:
                                match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
                                if match:
                                    row.update(completions=float(match[1]), attempts=float(match[2]))
    except (KeyError, TypeError, ValueError):
        raise DataSourceError("CFBD player box scores returned an unexpected schema.") from None
    return pd.DataFrame(rows.values())


class PlayerHistoryRepository:
    def __init__(self, root=None, data=None, client=None, schedule_path=None,college_directory=None):
        self.root = Path(root or ROOT)
        # An explicitly isolated repository must not read the user's global
        # college CSVs. Callers can still share a deliberate CSV directory.
        self.college_directory=(college_directory if college_directory is not None else
                                Path(root)/'college_csv' if root is not None else None)
        self.data = data or DataService()
        self.client = client or HttpClient()
        self.schedule_path = Path(schedule_path or Path(__file__).resolve().parents[3] / "data/modeling/nfl/raw/games.csv")

    def _path(self, season, team):
        # Hash the full canonical school identity; avoid filesystem characters
        # and collisions between schools that have the same abbreviation.
        name = sha256(identity_name(team).encode()).hexdigest()[:16]
        return self.root / "ncaaf" / f"{season}_{name}.json"

    def _school(self, display):
        key = os.environ.get("CFBD_API_KEY", "").strip()
        if not key:
            raise DataSourceError("Set CFBD_API_KEY in .env to prepare NCAAF player histories.")
        payload = self.client.json("https://api.collegefootballdata.com/teams/fbs",
                                   headers={"Authorization": f"Bearer {key}"})
        aliases = {}
        try:
            for team in payload:
                school = team["school"]
                for alias in [school, f"{school} {team.get('mascot') or ''}", *(team.get("altName1"), team.get("altName2"), team.get("altName3"))]:
                    if alias:
                        aliases.setdefault(identity_name(alias), set()).add(school)
        except (KeyError, TypeError):
            raise DataSourceError("CFBD returned an unexpected school directory schema.") from None
        matches = aliases.get(identity_name(display), set())
        if len(matches) != 1:
            raise DataSourceError(f"Could not uniquely map college team {display} to a CFBD school.")
        return next(iter(matches))

    def prepare(self, sport, start, teams=(), refresh=False):
        notices, total = [], 0
        years = seasons_for(sport, start)
        if sport in {"NBA", "NFL"}:
            for year in years:
                for phase in ["regular", "postseason"]:
                    query = Query(sport, year, "player", phase)
                    try:
                        cached = self.data.cached(self.data.key(query))
                        missing_dates = cached is not None and pd.to_datetime(cached.frame["event_date"], errors="coerce").isna().any()
                        result = (cached if cached and not refresh and not missing_dates
                                  else self.data.load(query, refresh=refresh or missing_dates))
                        total += len(result.frame)
                        if result.notice:
                            notices.append(result.notice)
                    except DataSourceError as exc:
                        notices.append(f"{sport} {year} {phase}: {exc}")
        elif sport == "NCAAF":
            if len(teams) != 2:
                raise DataSourceError("Choose a complete college matchup before preparing player histories.")
            schools = {display: self._school(display) for display in teams}
            key = os.environ.get("CFBD_API_KEY", "").strip()
            headers = {"Authorization": f"Bearer {key}"}
            for display, school in schools.items():
                for year in years:
                    path = self._path(year, display)
                    if path.exists() and not refresh:
                        total += len(json.loads(path.read_text(encoding="utf-8"))["rows"])
                        continue
                    params = {"year": year, "team": school, "seasonType": "both"}
                    try:
                        games = self.client.json("https://api.collegefootballdata.com/games", params=params, headers=headers)
                        boxes = self.client.json("https://api.collegefootballdata.com/games/players", params=params, headers=headers)
                        frame = cfbd_player_rows(games, boxes, school)
                        if frame.empty:
                            notices.append(f"No completed player boxes for {school} {year}.")
                            continue
                        frame["season"] = year
                        atomic_json(path, {"sport": sport, "school": school, "display_name": display,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "source": "CollegeFootballData /games/players", "rows": json.loads(frame.to_json(orient="records"))})
                        total += len(frame)
                    except DataSourceError as exc:
                        notices.append(f"{school} {year}: {exc}")
        else:
            raise DataSourceError("Choose NBA, NFL or NCAAF.")
        if not total:
            raise DataSourceError("No player history prepared. " + " ".join(notices))
        return f"{sport}: {total:,} saved player-game rows available across seasons {years[0]} and {years[1]}. " + " ".join(dict.fromkeys(notices))

    def load(self, sport, start, teams=()):
        """Read-only: no automatic download, refresh, or CFBD request."""
        frames = []
        years=seasons_for(sport,start)
        if sport=='NCAAF':
            from sports_analytics.ncaaf_model_data import NcaafModelData,seasons_for_start
            college=NcaafModelData(directory=self.college_directory)
            years=seasons_for_start(start)
        for year in years:
            if sport in {"NBA", "NFL"}:
                for phase in ["regular", "postseason"]:
                    query = Query(sport, year, "player", phase)
                    saved = self.data.cached(self.data.key(query))
                    if saved:
                        frames.append(saved.frame)
            else:
                for team in teams:
                    path = self._path(year, team)
                    csv_path=None
                    if (college.directory/'schools.json').exists():
                        try:csv_path=college.player_path(year,college.resolve_school(team))
                        except DataSourceError:pass
                    if csv_path is not None and csv_path.exists():
                        frame=pd.read_csv(csv_path,dtype={'entity_id':str,'game_id':str})
                        frame['market_team']=team
                        frames.append(frame)
                    elif path.exists():
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        frame = pd.DataFrame(payload["rows"])
                        frame["market_team"] = team
                        frames.append(frame)
        if sport=='NCAAF' and frames:
            known_ids=set(pd.concat(frames,ignore_index=True).entity_id.astype(str))
            for entity_id in known_ids:
                token=re.sub(r'[^0-9a-zA-Z_-]','',entity_id)
                transfer=college.directory/'transfers'/f'{token}_{years[-1]}.csv'
                if transfer.exists():
                    frame=pd.read_csv(transfer,dtype={'game_id':str,'entity_id':str})
                    frame=frame[frame.entity_id.eq(entity_id)&pd.to_numeric(frame.season,errors='coerce').isin(years)]
                    frames.append(frame)
        # Optional canonical imports cover authoritative total scoring TDs and
        # corrected participation records. See INPUT_SCHEMA.md.
        path = self.root / "imports" / f"{sport.lower()}.csv"
        if path.exists():
            imported = pd.read_csv(path, dtype={"entity_id": str, "game_id": str})
            if "season" in imported:
                season = pd.to_numeric(imported["season"], errors="coerce")
            else:
                dates = pd.to_datetime(imported["event_date"], utc=True, errors="coerce", format="mixed")
                season = dates.dt.year - dates.dt.month.lt(10 if sport == "NBA" else 3).astype(int)
            imported = imported[season.isin(years)]
            frames.append(imported)
        if not frames:
            raise DataSourceError("No saved player history. Click Prepare player history; this does not pull odds.")
        data = pd.concat(frames, ignore_index=True)
        if sport == "NFL" and self.schedule_path.exists():
            # Older player caches sometimes have scores but no dates after a
            # schedule failure. Recover dates from the already-saved nflverse
            # schedule, without downloading or inferring dates from game IDs.
            schedule = pd.read_csv(self.schedule_path, dtype={"game_id": str}, usecols=["game_id", "gameday"])
            schedule = schedule.drop_duplicates("game_id", keep=False).set_index("game_id")["gameday"]
            dates = pd.to_datetime(data["event_date"], utc=True, errors="coerce", format="mixed")
            mapped = pd.to_datetime(data["game_id"].astype(str).map(schedule), utc=True, errors="coerce")
            data["event_date"] = dates.fillna(mapped)
        data = model_frame(data)
        data["entity_id"], data["game_id"] = data["entity_id"].astype(str), data["game_id"].astype(str)
        duplicates = data.duplicated(["entity_id", "game_id"], keep=False)
        if duplicates.any():
            # Identical duplicate rows are safe, conflicting revisions are not.
            data = data.drop_duplicates()
            if data.duplicated(["entity_id", "game_id"]).any():
                raise DataSourceError("Conflicting player-game records found. Remove overlapping imports or duplicate source rows.")
        return data
