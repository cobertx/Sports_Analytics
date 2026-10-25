"""Public ESPN schedules, cached by sport and day independently of historical data."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .data import DataSourceError, HttpClient
from .display_time import EASTERN
from .cache_refresh import RefreshQueue

ROUTES = {"NBA": "basketball/nba", "NFL": "football/nfl", "NCAAF": "football/college-football"}
SCHEDULE_LINKS = {sport: f"https://www.espn.com/{route.split('/')[-1]}/schedule" for sport, route in ROUTES.items()}
FANDUEL_LINKS = {"NBA": "https://sportsbook.fanduel.com/navigation/nba",
                 "NFL": "https://sportsbook.fanduel.com/navigation/nfl",
                 "NCAAF": "https://sportsbook.fanduel.com/navigation/ncaaf"}


@dataclass
class Schedule:
    games: list[dict]
    fetched_at: str
    stale_days: list[str]
    failed_days: list[str]
    updating: bool = False


def scheduled_games(events, now, end):
    """Use designated away/home teams; exclude live, final, canceled, and postponed games."""
    games = {}
    for event in events:
        try:
            competition = event["competitions"][0]
            status = competition.get("status", event.get("status", {})).get("type", {})
            if status.get("state") != "pre" or status.get("completed") or status.get("name") in {
                    "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_POSTPONED", "STATUS_SUSPENDED"}:
                continue
            start = datetime.fromisoformat(competition.get("date", event["date"]).replace("Z", "+00:00"))
            if start.tzinfo is None:
                continue
            start = start.astimezone(timezone.utc)
            if not now <= start < end:
                continue
            teams = {team["homeAway"]: team for team in competition["competitors"]}
            def side(key):
                team = teams[key]["team"]
                return {"name": team["displayName"], "abbreviation": team.get("abbreviation", "")}
            venue = competition.get("venue") or {}
            address = venue.get("address") or {}
            games[str(event["id"])] = {
                "id": str(event["id"]), "start": start.isoformat(), "away": side("away"), "home": side("home"),
                "time_tbd": competition.get("timeValid") is False or status.get("name") == "STATUS_SCHEDULED_TBD",
                "venue": venue.get("fullName") or "Venue to be announced",
                "location": ", ".join(str(address[key]) for key in ("city", "state", "country") if address.get(key)),
                "neutral": bool(competition.get("neutralSite")),
            }
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            # A single incomplete listing must not hide the rest of the schedule.
            continue
    return sorted(games.values(), key=lambda game: (game["start"], game["id"]))


class ScheduleService:
    def __init__(self, path, ttl_minutes=5, workers=4,background=False):
        self.path = Path(path)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.workers = workers
        self.background=background
        self._refresh=RefreshQueue(workers=workers)

    def _day(self, sport, day, now, refresh):
        key = f"{sport}:{day:%Y%m%d}"
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            saved = db.execute("SELECT fetched_at, payload FROM schedules WHERE key = ?", (key,)).fetchone()
        cached = None
        if saved:
            try:
                stamp = datetime.fromisoformat(saved[0])
                cached = json.loads(saved[1])
                if not isinstance(cached, list):
                    cached = None
                elif not refresh and now - stamp < self.ttl:
                    return cached, saved[0], False
            except (ValueError, TypeError):
                cached = None
        if cached is not None and not refresh and self.background:
            def update():
                _,_,stale=self._day(sport,day,now,True)
                if stale:raise DataSourceError('ESPN refresh failed; saved schedules are still available.')
            state=self._refresh.request(key,update)
            return cached,saved[0],bool(state['error'])
        try:
            # The public feed supports single dates; date ranges may return HTTP 400.
            params = {"dates": day.strftime("%Y%m%d"), "limit": 1000}
            if sport == "NCAAF":
                params["groups"] = 80  # All FBS conferences, including unranked teams.
            payload = HttpClient().json(f"https://site.api.espn.com/apis/site/v2/sports/{ROUTES[sport]}/scoreboard", params=params)
            events = payload["events"]
            if not isinstance(events, list):
                raise TypeError("Expected an event list")
        except (DataSourceError, KeyError, TypeError) as exc:
            if cached is not None:
                return cached, saved[0], True
            raise DataSourceError("The ESPN schedule could not be loaded. Refresh to retry.") from exc
        stamp = now.isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("INSERT OR REPLACE INTO schedules VALUES (?, ?, ?)", (key, stamp, json.dumps(events)))
        return events, stamp, False

    def load(self, sport, days=7, refresh=False, now=None):
        if sport not in ROUTES or days not in (7, 14):
            raise ValueError("Choose NBA, NFL, or NCAAF and a 7- or 14-day schedule.")
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # Keep whole Eastern calendar days, including evening games after midnight UTC.
        dates = [now.astimezone(EASTERN).date() + timedelta(days=offset) for offset in range(-1, days)]
        end = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), EASTERN).astimezone(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS schedules (key TEXT PRIMARY KEY, fetched_at TEXT, payload TEXT)")
        events, stamps, stale, failed, updating_days = [], [], [], [], []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            tasks = {pool.submit(self._day, sport, day, now, refresh): day for day in dates}
            for task in as_completed(tasks):
                day = tasks[task].isoformat()
                try:
                    daily, stamp, was_stale = task.result()
                    events.extend(daily)
                    stamps.append(stamp)
                    if was_stale:
                        stale.append(day)
                    elif self.background and not refresh and now-datetime.fromisoformat(stamp)>=self.ttl:
                        updating_days.append(day)
                except DataSourceError:
                    failed.append(day)
        if len(failed) == len(dates):
            raise DataSourceError("Upcoming games could not be loaded from ESPN. Check connectivity and refresh to retry.")
        updating=bool(updating_days) or any(self._refresh.pending(f'{sport}:{day:%Y%m%d}') for day in dates)
        return Schedule(scheduled_games(events, now, end), min(stamps), sorted(stale), sorted(failed),updating)
