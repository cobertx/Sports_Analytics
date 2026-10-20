"""Actual credit accounting, persistence, local reporting, and calendar boundaries."""

import csv
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sports_analytics.dashboard import create_app, usage_figures
from sports_analytics.data import DataSourceError
from sports_analytics.nfl_markets import NFL_MARKETS, NflMarketService
from sports_analytics.service import DataService
from sports_analytics.usage_log import ApiUsageLog, FIELDS, request_cost, usage_summary

EVENT = {"id": "b" * 32, "sport_key": "americanfootball_nfl", "home_team": "Los Angeles Rams",
         "away_team": "New York Jets", "commence_time": "2026-10-25T17:00:00Z"}
GAME = {"id": "123", "start": EVENT["commence_time"], "home": {"name": EVENT["home_team"]},
        "away": {"name": EVENT["away_team"]}}


class UsageLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = ApiUsageLog(self.root / "API Log" / "api_credit_usage.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, identity="one", sport="NFL", timestamp="2026-10-19T23:35:23+00:00", **kwargs):
        values = dict(usage_id=identity, account="test-account", sport=sport, timestamp=timestamp,
                      game_id="123", event_id=EVENT["id"], home_team=EVENT["home_team"], away_team=EVENT["away_team"],
                      game_start=EVENT["commence_time"], markets=list(NFL_MARKETS), headers={"x-requests-last": "4"})
        values.update(kwargs)
        return self.log.record(**values)

    def service(self):
        api = Mock()
        api.account_id.return_value = "test-account"
        discovery = Mock()
        discovery.json.return_value = [EVENT]
        response = Mock(headers={"x-requests-last": "4", "x-requests-used": "142", "x-requests-remaining": "358"})
        response.json.return_value = {**EVENT, "bookmakers": []}
        api.get.side_effect = [discovery, response]
        return NflMarketService(api, self.root / "cache" / "nfl.sqlite3", self.root / "market_data", self.log), api, response

    def test_excel_readable_csv_has_context_exact_charge_and_edt_timestamp(self):
        self.log.ensure()
        with self.log.path.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(next(csv.reader(file)), FIELDS)
        self.assertTrue(self.record())
        row = self.log.records("test-account")[0]
        self.assertEqual(row["credits"], 4)  # Never assume the requested 17 markets cost 17.
        self.assertEqual(row["sport"], "NFL")
        self.assertEqual(row["game"], "New York Jets @ Los Angeles Rams")
        self.assertEqual(row["timestamp_edt"], "10/19/2026 19:35:23 EDT")
        self.assertEqual(row["markets"].split("; "), list(NFL_MARKETS))

    def test_deduplication_and_account_isolation_survive_restart(self):
        self.record()
        self.assertFalse(self.record())
        restarted = ApiUsageLog(self.log.path)
        self.assertEqual(len(restarted.records("test-account")), 1)
        self.assertEqual(restarted.records("different-account"), [])
        self.record(account="different-account")
        self.assertEqual(len(restarted.records("different-account")), 1)
        self.assertEqual(len(restarted.records("test-account")), 1)

    def test_edt_calendar_months_daily_gaps_and_unknown_costs(self):
        self.record("october", "NFL", "2026-11-01T03:30:00+00:00")  # Still October in EDT.
        self.record("november", "NBA", "2026-11-01T04:30:00+00:00", cost=7)
        self.record("college", "NCAAF", "2026-11-03T15:00:00+00:00", cost=3)
        self.record("unknown", "NFL", "2026-11-03T18:00:00+00:00", headers={})
        summary = usage_summary(self.log.records("test-account"), datetime(2026, 11, 5, tzinfo=timezone.utc))
        self.assertEqual(summary["month"], "November 2026")
        self.assertEqual(summary["monthly"]["NBA"]["credits"], 7)
        self.assertEqual(summary["monthly"]["NCAAF"]["credits"], 3)
        self.assertEqual(summary["monthly"]["NFL"], {"credits": 0, "pulls": 1, "unknown": 1})
        self.assertEqual(summary["daily"][0]["date"], "2026-10-31")
        self.assertEqual(summary["daily"][2], {"date": "2026-11-02", "NBA": 0, "NFL": 0, "NCAAF": 0})
        self.assertIsNone(summary["history"][0]["credits"])
        self.assertEqual(summary["history"][0]["charge"], "Unreported")

    def test_missing_invalid_and_zero_charge_headers_are_distinct(self):
        for value in ("-1", "1.5", "NaN", "Infinity", None):
            self.assertIsNone(request_cost({"x-requests-last": value}))
        self.assertEqual(request_cost({"X-Requests-Last": "0.0"}), 0)
        self.record("zero", headers={"x-requests-last": "0"})
        self.record("unknown", headers={})
        rows = self.log.records("test-account")
        self.assertEqual([row["credits"] for row in rows], [0, None])

    def test_saved_pulls_are_logged_once_and_reading_never_spends_credits(self):
        service, api, _ = self.service()
        snapshot = service.pull(GAME, "one-click")
        self.assertEqual(self.log.records("test-account")[0]["source"], "Live pull")
        service.pull(GAME, "one-click")
        service.latest(["123"])
        service.export_existing()
        self.assertEqual(api.get.call_count, 2)
        self.assertEqual(len(self.log.records("test-account")), 1)

        # Rebuild an accidentally removed log from the saved CSV, even without SQLite.
        self.log.path.unlink()
        recovered = NflMarketService(api, self.root / "new.sqlite3", service.csv_directory, self.log)
        self.assertEqual(recovered.history("123")[0]["pulled_at"], snapshot["pulled_at"])
        self.assertEqual(self.log.records("test-account")[0]["credits"], 4)
        self.assertEqual(api.get.call_count, 2)

    def test_malformed_paid_response_is_still_accounted_without_a_retry(self):
        service, api, response = self.service()
        response.json.side_effect = ValueError("Bad JSON")
        with self.assertRaises(DataSourceError):
            service.pull(GAME, "one-click")
        row = self.log.records("test-account")[0]
        self.assertEqual(row["credits"], 4)
        self.assertEqual(row["status"], "Failed / not saved")
        with self.assertRaises(DataSourceError):
            service.pull(GAME, "one-click")
        self.assertEqual(api.get.call_count, 2)
        self.assertEqual(len(self.log.records("test-account")), 1)

        self.log.path.unlink()
        self.assertEqual(service.export_existing(), 0)
        self.assertEqual(self.log.records("test-account")[0]["credits"], 4)
        self.assertEqual(api.get.call_count, 2)

    def test_network_failure_is_unknown_and_http_failure_uses_reported_charge(self):
        for identity, cost in (("timeout", None), ("http", 0)):
            service, api, _ = self.service()
            error = DataSourceError("Request failed")
            if cost == 0:
                error.usage_headers = {"x-requests-last": "0"}
            discovery = Mock()
            discovery.json.return_value = [EVENT]
            api.get.side_effect = [discovery, error]
            with self.assertRaises(DataSourceError):
                service.pull(GAME, identity)
            row = next(row for row in self.log.records("test-account") if row["credits"] == cost)
            self.assertEqual(row["credits"], cost)
        self.assertEqual(len(self.log.records("test-account")), 2)

    def test_unmatched_game_does_not_create_a_paid_usage_record(self):
        service, api, _ = self.service()
        with self.assertRaises(DataSourceError):
            service.pull({**GAME, "home": {"name": "Wrong team"}}, "no-match")
        self.assertEqual(self.log.records("test-account"), [])
        self.assertEqual(api.get.call_count, 1)

    def test_damaged_csv_does_not_silently_reset_or_replace_history(self):
        self.log.ensure()
        self.log.path.write_text("wrong,header\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.log.records("test-account")
        with self.assertRaises(ValueError):
            self.record()
        self.assertEqual(self.log.path.read_text(), "wrong,header\n")

    def test_figures_use_actual_monthly_sport_totals_and_daily_series(self):
        for sport, cost in (("NBA", 2), ("NFL", 4), ("NCAAF", 1)):
            self.record(sport, sport=sport, cost=cost)
        summary = usage_summary(self.log.records("test-account"), datetime(2026, 10, 20, tzinfo=timezone.utc))
        pie, line = usage_figures(summary)
        self.assertEqual(list(pie.data[0].labels), ["NBA", "NFL", "NCAAF"])
        self.assertEqual(list(pie.data[0].values), [2, 4, 1])
        self.assertEqual([trace.name for trace in line.data], ["Total", "NBA", "NFL", "NCAAF"])
        self.assertEqual(list(line.data[0].y), [7])
        self.assertEqual(list(line.data[2].y), [4])
        empty_pie, empty_line = usage_figures(usage_summary([]))
        self.assertFalse(empty_pie.data)
        self.assertFalse(empty_line.data)

    def test_page_reports_saved_history_independently_of_network_connection(self):
        self.record(timestamp=datetime.now(timezone.utc).isoformat())
        odds = Mock()
        odds.account_id.return_value = "test-account"
        odds.usage.side_effect = DataSourceError("Offline")
        markets = Mock()
        app = create_app(DataService(self.root / "historical.sqlite3"), Mock(), Mock(), odds, markets, self.log)
        client = app.server.test_client()
        key = next(key for key in app.callback_map if "odds-composition.figure" in key)
        config = app.callback_map[key]
        defaults = {"sport.value": "ODDS_API", "odds-refresh.n_clicks": 0, "odds-timer.n_intervals": 0,
                    "market-refresh-signal.data": None}
        response = client.post("/_dash-update-component", json={"output": key,
            "outputs": [{"id": item.component_id, "property": item.component_property} for item in config["output"]],
            "inputs": [{**item, "value": defaults[item["id"] + "." + item["property"]]} for item in config["inputs"]],
            "state": [], "changedPropIds": ["sport.value"]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.json["response"]
        self.assertEqual(data["odds-composition"]["figure"]["data"][0]["values"], [0, 4, 0])
        self.assertEqual(len(data["odds-history-table"]["data"]), 1)
        self.assertIn("EDT", data["odds-history-table"]["data"][0]["timestamp"])
        self.assertNotIn("account_id", response.get_data(as_text=True))
        odds.get.assert_not_called()
        odds.usage.assert_not_called()
        markets.pull.assert_not_called()
        nodes = {node.id: node for node in app.layout._traverse() if hasattr(node, "id")}
        self.assertTrue({"odds-composition", "odds-sport-summary", "odds-history-chart", "odds-history-table"} <= nodes.keys())


if __name__ == "__main__":
    unittest.main()
