"""Persistent, account-scoped API audit history, readable in Excel as CSV."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import os
from pathlib import Path
import tempfile
import threading

from requests.structures import CaseInsensitiveDict

from .data import DataSourceError
from .display_time import eastern_datetime, format_eastern
from .odds_api import credit_headers

SPORTS = ("NBA", "NFL", "NCAAF")
FIELDS = ["usage_id", "account_id", "timestamp_utc", "timestamp_edt", "sport", "game_id", "event_id",
          "game", "game_start_utc", "bookmaker", "markets", "credits", "account_used", "account_remaining",
          "status", "source"]
_LOCK = threading.RLock()


def request_cost(headers):
    headers = CaseInsensitiveDict(headers or {})
    try:
        return credit_headers({"x-requests-used": headers.get("x-requests-last"), "x-requests-remaining": 0})[0]
    except DataSourceError:
        return None


class ApiUsageLog:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        """The OS releases this lock if the app exits; protects multiple workers."""
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != FIELDS:
                raise ValueError("API usage CSV has an unexpected header; restore its original columns.")
            rows = list(reader)
        for row in rows:
            # Reject damaged entries instead of silently reporting incomplete totals.
            if row["sport"] not in SPORTS or not row["usage_id"] or not row["account_id"]:
                raise ValueError("API usage CSV contains an invalid record.")
            eastern_datetime(row["timestamp_utc"])
            for key in ("credits", "account_used", "account_remaining"):
                row[key] = int(row[key]) if row[key] else None
                if row[key] is not None and row[key] < 0:
                    raise ValueError("API usage CSV contains a negative credit count.")
        return rows

    def _write(self, rows):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=self.path.parent,
                                             suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                writer = csv.DictWriter(file, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def ensure(self):
        with self._locked():
            if not self.path.exists():
                self._write([])

    def records(self, account):
        accounts = {account} if isinstance(account, str) else set(account)
        with self._locked():
            if not self.path.exists():
                self._write([])
            rows = self._read()
        return sorted((row for row in rows if row["account_id"] in accounts),
                      key=lambda row: row["timestamp_utc"], reverse=True)

    def record(self, *, usage_id, account, sport, game_id, event_id, home_team, away_team,
               game_start, timestamp, markets, headers=None, cost=None, status="Saved", source="Live pull"):
        if sport not in SPORTS:
            raise ValueError("Unsupported sport for API usage log.")
        headers = CaseInsensitiveDict(headers or {})
        try:
            used, remaining = credit_headers(headers)
        except DataSourceError:
            used = remaining = None
        charge = request_cost(headers) if cost is None else cost
        if charge is not None and (isinstance(charge, bool) or not isinstance(charge, int) or charge < 0):
            raise ValueError("Credits must be a nonnegative integer or unknown.")
        row = dict(zip(FIELDS, [usage_id, account, timestamp, format_eastern(timestamp), sport, str(game_id), event_id,
                               f"{away_team} @ {home_team}", game_start, "fanduel", "; ".join(markets), charge,
                               used, remaining, status, source]))
        with self._locked():
            rows = self._read()
            if any(item["usage_id"] == usage_id and item["account_id"] == account for item in rows):
                return False
            self._write([*rows, row])
        return True

    def import_snapshot(self, snapshot, account):
        identity = f"{account}:{snapshot['event_id']}:{snapshot['pulled_at']}"
        recorded = self.record(usage_id=snapshot.get("usage_log_id") or hashlib.sha256(identity.encode()).hexdigest(),
                           account=account, sport=snapshot.get("sport", "NFL"), game_id=snapshot["game_id"], event_id=snapshot["event_id"],
                           home_team=snapshot["home_team"], away_team=snapshot["away_team"],
                           game_start=snapshot["commence_time"], timestamp=snapshot["pulled_at"],
                           markets=snapshot.get("requested", []), headers=snapshot.get("usage_headers"),
                           cost=snapshot.get("cost"), source="Saved snapshot")
        for audit in snapshot.get('usage_attempts', []):
            self.record(**audit)
        return recorded


def usage_summary(records, now=None, account_labels=None):
    """Calendar months and daily buckets use the dashboard's fixed EDT timezone."""
    today = eastern_datetime(now or datetime.now(timezone.utc))
    monthly = {sport: {"credits": 0, "pulls": 0, "unknown": 0} for sport in SPORTS}
    daily = {}
    history = []
    unknown = 0
    for row in records:
        stamp = eastern_datetime(row["timestamp_utc"])
        cost = row["credits"]
        if (stamp.year, stamp.month) == (today.year, today.month):
            item = monthly[row["sport"]]
            item["pulls"] += 1
            item["credits"] += cost if cost is not None else 0
            item["unknown"] += cost is None
        day = daily.setdefault(stamp.date(), dict.fromkeys(SPORTS, 0))
        if cost is None:
            unknown += 1
        else:
            day[row["sport"]] += cost
        history.append({"timestamp": format_eastern(row["timestamp_utc"]), "sport": row["sport"],
                        "account": (account_labels or {}).get(row['account_id'], 'Connected key'),
                        "game": row["game"], "credits": cost, "charge": "Unreported" if cost is None else "Reported",
                        "markets": row["markets"], "status": row["status"], "source": row["source"],
                        "game_time": format_eastern(row["game_start_utc"]) if row["game_start_utc"] else "",
                        "game_id": row["game_id"], "event_id": row["event_id"]})
    series = []
    if daily:
        day, end = min(daily), max(daily)
        while day <= end:
            series.append({"date": day.isoformat(), **daily.get(day, dict.fromkeys(SPORTS, 0))})
            day += timedelta(days=1)
    return {"month": today.strftime("%B %Y"), "monthly": monthly, "daily": series,
            "history": history, "unknown": unknown}
