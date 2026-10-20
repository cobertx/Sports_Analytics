"""Click-triggered, FanDuel-only NFL snapshots using the fixed 17-market package."""

from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from zoneinfo import ZoneInfo

from requests.structures import CaseInsensitiveDict

from .data import DataSourceError
from .odds_api import credit_headers
from .usage_log import ApiUsageLog

# Exactly the package supplied by the user. Do not add main markets or
# player_tds_over: an extra distinct market can increase the credit charge.
NFL_MARKETS = {
    "alternate_spreads": ("Game", "Alternate spreads"),
    "alternate_totals": ("Game", "Alternate points total"),
    "player_pass_yds_alternate": ("Passing", "Alternate passing yards"),
    "player_pass_tds_alternate": ("Passing", "Alternate passing touchdowns"),
    "player_pass_attempts_alternate": ("Passing", "Alternate passing attempts"),
    "player_pass_completions_alternate": ("Passing", "Alternate completions"),
    "player_pass_interceptions_alternate": ("Passing", "Alternate interceptions"),
    "player_reception_yds_alternate": ("Receiving", "Alternate receiving yards"),
    "player_receptions_alternate": ("Receiving", "Alternate receptions"),
    "player_reception_tds_alternate": ("Receiving", "Alternate receiving touchdowns"),
    "player_rush_yds_alternate": ("Rushing", "Alternate rushing yards"),
    "player_rush_attempts_alternate": ("Rushing", "Alternate rush attempts"),
    "player_rush_tds_alternate": ("Rushing", "Alternate rushing touchdowns"),
    "player_anytime_td": ("Touchdowns", "Anytime touchdown scorer"),
    "player_1st_td": ("Touchdowns", "First touchdown scorer"),
    "player_last_td": ("Touchdowns", "Last touchdown scorer"),
    "player_tds": ("Touchdowns", "Total player touchdowns"),
}
NFL_TEAM_CODES = dict(zip([
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills", "Carolina Panthers",
    "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys", "Denver Broncos",
    "Detroit Lions", "Green Bay Packers", "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
    "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins",
    "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants", "New York Jets",
    "Philadelphia Eagles", "Pittsburgh Steelers", "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
    "Tennessee Titans", "Washington Commanders"],
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LAR MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WSH".split()))
ROW_FIELDS = ["group", "market_key", "market", "subject", "selection", "line", "td_threshold", "odds", "updated"]
CSV_FIELDS = ["game_id", "event_id", "home_team", "home_abbreviation", "away_team", "away_abbreviation",
              "game_date", "commence_time", "pulled_at", "bookmaker", "cost", *ROW_FIELDS,
              "snapshot_metadata_json", "api_response_json"]
csv.field_size_limit(max(csv.field_size_limit(), 64 * 1024 * 1024))
GAME_TIMEZONE = ZoneInfo("America/New_York")


def team_name(name):
    name = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    aliases = {"la rams": "los angeles rams", "la chargers": "los angeles chargers",
               "ny giants": "new york giants", "ny jets": "new york jets"}
    return aliases.get(name, name)


def match_event(events, game):
    try:
        teams = {team_name(game["away"]["name"]), team_name(game["home"]["name"])}
        start = datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
        if len(teams) != 2 or start.tzinfo is None:
            raise ValueError("Incomplete matchup")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DataSourceError("The selected game is incomplete. Refresh the NFL schedule.") from None
    candidates = []
    for event in events:
        try:
            event_start = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            if (event.get("sport_key") == "americanfootball_nfl" and
                    {team_name(event["away_team"]), team_name(event["home_team"])} == teams and
                    abs((event_start - start).total_seconds()) <= (36 if game.get("time_tbd") else 6) * 3600 and
                    re.fullmatch(r"[a-f0-9]{32}", str(event["id"]))):
                candidates.append(event)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if len(candidates) != 1:
        raise DataSourceError("Could not uniquely match this game to The Odds API. No odds request was sent; refresh the schedule and try again.")
    return candidates[0]


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def snapshot_data(payload, event, game_id, headers):
    headers = CaseInsensitiveDict(headers)
    if not isinstance(payload, dict) or payload.get("id") != event["id"] or not isinstance(payload.get("bookmakers"), list):
        raise DataSourceError("The odds response was incomplete. The request was not retried; check account usage before pulling again.")
    rows, returned = [], set()
    try:
        for bookmaker in payload["bookmakers"]:
            if bookmaker["key"] != "fanduel":
                continue
            for market in bookmaker["markets"]:
                key = market["key"]
                if key not in NFL_MARKETS:
                    continue
                group, label = NFL_MARKETS[key]
                for outcome in market["outcomes"]:
                    selection = str(outcome["name"])
                    point = number(outcome.get("point"))
                    milestone = ""
                    if key == "player_tds" and selection == "Over" and point is not None and point >= 0 and point % 1 == 0.5:
                        milestone = f"{int(point) + 1}+ TDs"
                    rows.append({"group": group, "market_key": key, "market": label,
                                 "subject": str(outcome.get("description") or selection),
                                 "selection": "Yes" if key in {"player_anytime_td", "player_1st_td", "player_last_td"} and not outcome.get("description") else selection,
                                 "line": point, "td_threshold": milestone, "odds": number(outcome.get("price")),
                                 "updated": market.get("last_update") or bookmaker.get("last_update") or "Not reported"})
                    returned.add(key)
    except (KeyError, TypeError, AttributeError):
        raise DataSourceError("The odds response had an unexpected market schema. It was not retried; check account usage before pulling again.") from None
    try:
        cost = credit_headers({"x-requests-used": headers.get("x-requests-last"), "x-requests-remaining": 0})[0]
    except DataSourceError:
        cost = None
    return {"game_id": str(game_id), "event_id": event["id"], "home_team": event["home_team"],
            "away_team": event["away_team"], "commence_time": event["commence_time"], "bookmaker": "fanduel",
            "pulled_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "cost": cost,
            "usage_headers": {key: headers.get(key) for key in ("x-requests-used", "x-requests-remaining", "x-requests-last")},
            "requested": list(NFL_MARKETS), "returned": sorted(returned),
            "missing": [key for key in NFL_MARKETS if key not in returned], "rows": rows}


class NflMarketService:
    def __init__(self, odds_api, path, csv_directory=None, usage_log=None):
        self.api = odds_api
        self.path = Path(path)
        self.csv_directory = Path(csv_directory) if csv_directory is not None else self.path.parent / "market_data"
        self.usage_log = usage_log or ApiUsageLog(self.csv_directory.parent / "API Log" / "api_credit_usage.csv")
        self._csv_cache = {}
        self._lock = threading.Lock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS nfl_snapshots (
                request_id TEXT PRIMARY KEY, account TEXT, game_id TEXT, state TEXT,
                pulled_at TEXT, payload TEXT, error TEXT)""")
        return connection

    def _export_csv(self, snapshot, account):
        """Atomic, readable outcome rows plus complete response JSON once per file."""
        game = snapshot.get("game", {})
        snapshot.setdefault("home_abbreviation", game.get("home", {}).get("abbreviation") or NFL_TEAM_CODES.get(snapshot["home_team"], "HOME"))
        snapshot.setdefault("away_abbreviation", game.get("away", {}).get("abbreviation") or NFL_TEAM_CODES.get(snapshot["away_team"], "AWAY"))
        played = datetime.fromisoformat(snapshot["commence_time"].replace("Z", "+00:00")).astimezone(timezone.utc)
        taken = datetime.fromisoformat(snapshot["pulled_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        snapshot.setdefault("game_date", played.astimezone(GAME_TIMEZONE).date().isoformat())
        home = re.sub(r"[^a-z0-9]", "", snapshot["home_abbreviation"].lower())
        away = re.sub(r"[^a-z0-9]", "", snapshot["away_abbreviation"].lower())
        filename = f"nfl_{home}_{away}_{snapshot['game_date']}_{taken:%Y%m%dT%H%M%S%fZ}.csv"
        snapshot["csv_file"] = filename
        destination = self.csv_directory / filename
        if destination.is_file():
            return
        self.csv_directory.mkdir(parents=True, exist_ok=True)
        metadata = {key: value for key, value in snapshot.items() if key not in {"rows", "raw_response", "account_id"}}
        metadata["account_id"] = account
        common = {key: snapshot.get(key, "") for key in CSV_FIELDS if key not in ROW_FIELDS}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=self.csv_directory,
                                             suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for index, row in enumerate(snapshot["rows"] or [{}]):
                    writer.writerow({**common, **{key: row.get(key, "") for key in ROW_FIELDS},
                                     "snapshot_metadata_json": json.dumps(metadata) if index == 0 else "",
                                     "api_response_json": json.dumps(snapshot.get("raw_response")) if index == 0 else ""})
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_csv(self, path):
        stamp = path.stat().st_mtime_ns
        cached = self._csv_cache.get(path.name)
        if cached and cached[0] == stamp:
            return cached[1]
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            first = next(reader)
            snapshot = json.loads(first["snapshot_metadata_json"])
            snapshot["rows"] = []
            for row in [first, *reader]:
                if not row.get("market_key"):
                    continue
                item = {key: row.get(key, "") for key in ROW_FIELDS}
                for key in ("line", "odds"):
                    item[key] = float(item[key]) if item[key] else None
                snapshot["rows"].append(item)
            snapshot["raw_response"] = json.loads(first.get("api_response_json") or "null")
            snapshot["csv_file"] = path.name
        self._csv_cache[path.name] = (stamp, snapshot)
        return snapshot

    def _saved(self, selected):
        try:
            account = self.api.account_id()
        except DataSourceError:
            return []
        saved = {}
        with closing(self._connect()) as db:
            rows = db.execute("SELECT game_id, payload FROM nfl_snapshots WHERE account = ? AND state = 'done' ORDER BY pulled_at DESC", (account,))
            for game_id, payload in rows:
                if selected is None or game_id in selected:
                    snapshot = json.loads(payload)
                    self._export_csv(snapshot, account)  # Also exports earlier SQLite-only pulls.
                    saved[(game_id, snapshot["pulled_at"])] = snapshot
        for path in self.csv_directory.glob("nfl_*.csv"):
            try:
                snapshot = self._read_csv(path)
                if snapshot.get("account_id") == account and (selected is None or snapshot["game_id"] in selected):
                    snapshot = {key: value for key, value in snapshot.items() if key != "account_id"}
                    saved[(snapshot["game_id"], snapshot["pulled_at"])] = snapshot
            except (OSError, ValueError, KeyError, StopIteration, TypeError, csv.Error):
                continue
        snapshots = sorted(saved.values(), key=lambda item: item["pulled_at"], reverse=True)
        try:
            for snapshot in snapshots:
                self.usage_log.import_snapshot(snapshot, account)
            with closing(self._connect()) as db:
                failed = db.execute("SELECT payload FROM nfl_snapshots WHERE account = ? AND state = 'failed' AND payload IS NOT NULL",
                                    (account,)).fetchall()
            for (audit,) in failed:
                self.usage_log.record(**json.loads(audit))
        except (OSError, ValueError, csv.Error):
            raise DataSourceError("Saved markets are available, but the API credit log could not be updated. Check data/API Log folder access and CSV format; do not pull again to repair the log.") from None
        return snapshots

    def latest(self, game_ids):
        saved = {}
        for snapshot in self._saved(set(map(str, game_ids))):
            saved.setdefault(snapshot["game_id"], snapshot)
        return saved

    def history(self, game_id):
        return self._saved({str(game_id)})

    def export_existing(self):
        """Backfill CSVs for completed pulls without making any API requests."""
        return len(self._saved(None))

    def pull(self, game, request_id):
        account = self.api.account_id()
        request_key = hashlib.sha256((account + ":" + str(request_id)).encode()).hexdigest()
        with self._lock, closing(self._connect()) as db:
            previous = db.execute("SELECT state, payload, error FROM nfl_snapshots WHERE request_id = ?", (request_key,)).fetchone()
            if previous:
                if previous[0] == "done":
                    snapshot = json.loads(previous[1])
                    self._export_csv(snapshot, account)
                    self.usage_log.import_snapshot(snapshot, account)
                    return snapshot
                raise DataSourceError(previous[2] or "This pull was already submitted. It will not be repeated automatically.")
            response = self.api.get("sports/americanfootball_nfl/events")
            try:
                events = response.json()
                if not isinstance(events, list):
                    raise ValueError("Expected events")
            except (ValueError, TypeError):
                raise DataSourceError("The Odds API returned an invalid event list. No odds request was sent.") from None
            event = match_event(events, game)
            # Reserve the click before the paid request, including across app restarts.
            # A timed-out response is never automatically resubmitted and recharged.
            with db:
                claimed = db.execute("INSERT OR IGNORE INTO nfl_snapshots VALUES (?, ?, ?, 'pending', ?, NULL, NULL)",
                                     (request_key, account, str(game["id"]), datetime.now(timezone.utc).isoformat())).rowcount
            if not claimed:
                raise DataSourceError("This pull was already submitted. No second odds request was sent.")
            taken = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            paid_headers = {}
            try:
                response = self.api.get(f"sports/americanfootball_nfl/events/{event['id']}/odds", params={
                    "bookmakers": "fanduel", "markets": ",".join(NFL_MARKETS), "oddsFormat": "american", "dateFormat": "iso"})
                paid_headers = response.headers
                try:
                    payload = response.json()
                except ValueError:
                    raise DataSourceError("The odds response was invalid. It was not retried; check account usage before pulling again.") from None
                snapshot = snapshot_data(payload, event, game["id"], response.headers)
                snapshot["pulled_at"] = taken
                snapshot["usage_log_id"] = request_key
                snapshot["game"] = {key: value for key, value in game.items() if key != "token"}
                snapshot["raw_response"] = payload
            except DataSourceError as exc:
                audit = dict(usage_id=request_key, account=account, sport="NFL", game_id=game["id"],
                             event_id=event["id"], home_team=event["home_team"], away_team=event["away_team"],
                             game_start=event["commence_time"], timestamp=taken, markets=list(NFL_MARKETS),
                             headers=dict(getattr(exc, "usage_headers", paid_headers)), status="Failed / not saved")
                with db:
                    db.execute("UPDATE nfl_snapshots SET state = 'failed', payload = ?, error = ? WHERE request_id = ?",
                               (json.dumps(audit), str(exc), request_key))
                try:
                    self.usage_log.record(**audit)
                except (OSError, ValueError, csv.Error):
                    raise DataSourceError(f"{exc} The API usage log could not be written. Check data/API Log access; do not retry this pull to repair the log.") from None
                raise
            with db:
                db.execute("UPDATE nfl_snapshots SET state = 'done', pulled_at = ?, payload = ? WHERE request_id = ?",
                           (snapshot["pulled_at"], json.dumps(snapshot), request_key))
            try:
                self._export_csv(snapshot, account)
                self.usage_log.record(usage_id=request_key, account=account, sport="NFL", game_id=game["id"],
                                      event_id=event["id"], home_team=event["home_team"], away_team=event["away_team"],
                                      game_start=event["commence_time"], timestamp=taken, markets=list(NFL_MARKETS),
                                      headers=paid_headers)
            except (OSError, ValueError, csv.Error):
                raise DataSourceError("The snapshot is saved in the local database, but CSV export or API logging failed. Check data/market_data and data/API Log access; reopen the page to repair without another pull.") from None
            return snapshot
