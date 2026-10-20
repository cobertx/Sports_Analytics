"""Upcoming schedule filtering, caching, and sport/page navigation regressions."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.schedule import FANDUEL_LINKS, Schedule, ScheduleService, scheduled_games
from sports_analytics.service import DataService

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
END = NOW + timedelta(days=7)


def event(identity="101", start="2026-10-20T19:00Z", state="pre", name="STATUS_SCHEDULED"):
    return {"id": identity, "date": start,
            "status": {"type": {"name": name, "state": state, "completed": state == "post"}},
            "competitions": [{"date": start, "timeValid": True, "neutralSite": False,
                "venue": {"fullName": "Test Arena", "address": {"city": "Toronto", "country": "Canada"}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Toronto Raptors", "abbreviation": "TOR"}},
                    {"homeAway": "away", "team": {"displayName": "Golden State Warriors", "abbreviation": "GSW"}}]}]}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ScheduleService(Path(self.tmp.name) / "schedules.sqlite3")
        self.client = Mock()
        self.client.json.return_value = {"events": [event()]}
        self.http = patch("sports_analytics.schedule.HttpClient", return_value=self.client)
        self.http.start()

    def tearDown(self):
        self.http.stop()
        self.tmp.cleanup()

    def test_only_upcoming_scheduled_games_are_sorted_and_deduplicated(self):
        events = [event("later", "2026-10-22T19:00Z"), event(), event(),
                  event("past", "2026-10-18T19:00Z"), event("live", state="in"), event("final", state="post"),
                  event("postponed", name="STATUS_POSTPONED"), event("canceled", name="STATUS_CANCELED"),
                  event("outside", "2026-11-01T19:00Z"), {"id": "malformed"}]
        games = scheduled_games(events, NOW, END)
        self.assertEqual([g["id"] for g in games], ["101", "later"])
        self.assertEqual(games[0]["away"]["abbreviation"], "GSW")
        self.assertEqual(games[0]["home"]["abbreviation"], "TOR")
        self.assertEqual(games[0]["location"], "Toronto, Canada")

    def test_tbd_and_neutral_games_keep_provider_labels(self):
        raw = event(name="STATUS_SCHEDULED_TBD")
        raw["competitions"][0].update(timeValid=False, neutralSite=True)
        game = scheduled_games([raw], NOW, END)[0]
        self.assertTrue(game["time_tbd"])
        self.assertTrue(game["neutral"])

    def test_all_sports_use_correct_feed_and_college_includes_all_fbs(self):
        for sport, route in [("NBA", "basketball/nba"), ("NFL", "football/nfl"), ("NCAAF", "football/college-football")]:
            with self.subTest(sport=sport):
                self.client.reset_mock()
                result = self.service.load(sport, now=NOW)
                self.assertEqual(len(result.games), 1)
                self.assertEqual(self.client.json.call_count, 8)
                for call in self.client.json.call_args_list:
                    self.assertIn(route + "/scoreboard", call.args[0])
                    self.assertEqual(len(call.kwargs["params"]["dates"]), 8)
                    self.assertEqual(call.kwargs["params"].get("groups"), 80 if sport == "NCAAF" else None)

    def test_fresh_cache_is_shared_and_expired_games_are_filtered_on_read(self):
        self.client.json.return_value = {"events": [event(start="2026-10-19T12:30Z")]}
        self.service.load("NBA", now=NOW)
        self.client.reset_mock()
        other = ScheduleService(self.service.path, ttl_minutes=120)
        self.assertEqual(len(other.load("NBA", now=NOW + timedelta(minutes=1)).games), 1)
        self.assertEqual(other.load("NBA", now=NOW + timedelta(hours=1)).games, [])
        self.client.json.assert_not_called()

    def test_late_us_games_after_midnight_utc_are_not_missed(self):
        midnight = NOW.replace(hour=0)
        def download(_url, params):
            return {"events": [event(start="2026-10-19T01:00Z")] if params["dates"] == "20261018" else []}
        self.client.json.side_effect = download
        games = self.service.load("NBA", now=midnight).games
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["start"], "2026-10-19T01:00:00+00:00")

    def test_schedule_window_includes_late_games_on_the_last_eastern_day(self):
        self.client.json.return_value = {"events": [event(start="2026-10-26T03:30Z")]}
        games = self.service.load("NBA", now=NOW).games
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["start"], "2026-10-26T03:30:00+00:00")

    def test_manual_refresh_bypasses_cache_and_reports_stale_fallback(self):
        self.service.load("NBA", now=NOW)
        self.client.reset_mock()
        self.client.json.side_effect = DataSourceError("Offline")
        result = self.service.load("NBA", refresh=True, now=NOW + timedelta(minutes=1))
        self.assertEqual(self.client.json.call_count, 8)
        self.assertEqual(len(result.games), 1)
        self.assertEqual(len(result.stale_days), 8)
        self.assertEqual(result.failed_days, [])

    def test_partial_failure_reports_missing_dates_and_complete_failure_raises(self):
        def download(_url, params):
            if params["dates"] == "20261019":
                raise DataSourceError("Offline")
            return {"events": [event()]}
        self.client.json.side_effect = download
        result = self.service.load("NFL", now=NOW)
        self.assertEqual(result.failed_days, ["2026-10-19"])
        self.assertEqual(len(result.games), 1)
        self.client.json.side_effect = DataSourceError("Offline")
        with self.assertRaisesRegex(DataSourceError, "could not be loaded"):
            self.service.load("NCAAF", now=NOW)

    def test_malformed_response_is_a_source_error_not_an_empty_schedule(self):
        self.client.json.return_value = {"unexpected": []}
        with self.assertRaises(DataSourceError):
            self.service.load("NBA", now=NOW)


class MarketPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = DataService(Path(self.tmp.name) / "historical.sqlite3")
        self.schedule = Mock()
        self.schedule.load.return_value = Schedule(scheduled_games([event()], NOW, END), NOW.isoformat(), [], [])
        self.app = create_app(self.data, Mock(), self.schedule)
        self.client = self.app.server.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, token, changed=None, **values):
        key = next(k for k in self.app.callback_map if token in k)
        config = self.app.callback_map[key]
        outputs = config["output"]
        if isinstance(outputs, list):
            outputs = [{"id": o.component_id, "property": o.component_property} for o in outputs]
        else:
            outputs = {"id": outputs.component_id, "property": outputs.component_property}
        defaults = {"sport.value": "NBA", "workspace-page.value": "market", "market-days.value": 7,
                    "market-refresh.n_clicks": 0, "market-timer.n_intervals": 0}
        defaults.update(values)
        def props(items):
            return [{**item, "value": defaults.get(item["id"] + "." + item["property"])} for item in items]
        return self.client.post("/_dash-update-component", json={
            "output": key, "outputs": outputs, "inputs": props(config["inputs"]), "state": props(config["state"]),
            "changedPropIds": [changed] if changed else [],
        })

    def test_layout_has_ordered_toolbar_pages_and_no_initial_schedule_request(self):
        response = self.client.get("/_dash-layout")
        self.assertEqual(response.status_code, 200)
        nodes = {node.id: node for node in self.app.layout._traverse() if hasattr(node, "id")}
        self.assertEqual([o["value"] for o in nodes["workspace-page"].options], ["historical", "market"])
        self.assertEqual(nodes["workspace-page"].value, "historical")
        self.assertEqual(nodes["market-page"].style, {"display": "none"})
        self.schedule.load.assert_not_called()

    def test_all_sports_have_both_pages_and_matching_market_links(self):
        for sport in ("NBA", "NFL", "NCAAF"):
            for page in ("historical", "market"):
                with self.subTest(sport=sport, page=page):
                    response = self.callback("historical-page.style", **{"sport.value": sport, "workspace-page.value": page})
                    self.assertEqual(response.status_code, 200)
                    data = response.json["response"]
                    self.assertEqual(data["market-page"]["style"]["display"], "block" if page == "market" else "none")
                    self.assertEqual(data["historical-page"]["style"]["display"], "grid" if page == "historical" else "none")
                    self.assertEqual(data["fanduel-link"]["href"], FANDUEL_LINKS[sport])
                    self.assertEqual(data["market-timer"]["disabled"], page != "market")

    def test_sport_switch_and_refresh_load_matching_cards(self):
        for sport in ("NBA", "NFL", "NCAAF"):
            response = self.callback("market-games.children", **{"sport.value": sport})
            self.assertEqual(response.status_code, 200)
            self.schedule.load.assert_called_with(sport, 7, refresh=False)
            body = json.dumps(response.json)
            self.assertIn("Golden State Warriors", body)
            self.assertIn("Toronto Raptors", body)
            self.assertIn("Test Arena", body)
        self.callback("market-games.children", changed="market-refresh.n_clicks", **{"market-refresh.n_clicks": 1})
        self.schedule.load.assert_called_with("NBA", 7, refresh=True)

    def test_hidden_pages_do_not_download_unrelated_sources(self):
        response = self.callback("market-games.children", **{"workspace-page.value": "historical"})
        self.assertEqual(response.status_code, 204)
        self.schedule.load.assert_not_called()
        with patch.object(self.data, "load") as load:
            response = self.callback("dataset.data")
        self.assertEqual(response.status_code, 204)
        load.assert_not_called()

    def test_empty_failure_and_partial_coverage_are_distinguished(self):
        self.schedule.load.return_value = Schedule([], NOW.isoformat(), ["2026-10-19"], ["2026-10-20"])
        response = self.callback("market-games.children")
        self.assertIn("No upcoming games found", json.dumps(response.json))
        status = response.json["response"]["market-status"]["children"]
        self.assertIn("Saved schedules", status)
        self.assertIn("Incomplete coverage", status)
        self.schedule.load.side_effect = DataSourceError("Offline")
        response = self.callback("market-games.children")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Schedule unavailable", json.dumps(response.json))


if __name__ == "__main__":
    unittest.main()
