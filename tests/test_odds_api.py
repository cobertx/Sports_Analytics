"""Account quota accuracy, key handling, and Odds API dashboard integration."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from sports_analytics.dashboard import create_app, credit_figure
from sports_analytics.data import DataSourceError
from sports_analytics.odds_api import CreditUsage, OddsApiService, credit_headers
from sports_analytics.service import DataService

KEY = "private-test-key-do-not-render"
STAMP = "2026-10-19T12:00:00+00:00"


class OddsApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"ODDS_API_KEY": KEY})
        self.environment.start()
        self.session = Mock()
        self.response = Mock(status_code=200, headers={"x-requests-used": "125", "x-requests-remaining": "375"})
        self.session.get.return_value = self.response
        self.service = OddsApiService(self.session)

    def tearDown(self):
        self.environment.stop()

    def test_usage_comes_from_account_headers_on_the_free_endpoint(self):
        usage = self.service.usage()
        self.assertEqual((usage.used, usage.remaining, usage.total), (125, 375, 500))
        self.session.get.assert_called_once_with("https://api.the-odds-api.com/v4/sports/",
                                                 params={"apiKey": KEY}, timeout=(5, 20), allow_redirects=False)
        self.assertNotIn(KEY, repr(usage))
        self.assertIn("+00:00", usage.checked_at)

    def test_odds_response_updates_credit_chart_cache_without_another_request(self):
        self.response.headers = {"x-requests-used": "142", "x-requests-remaining": "358", "x-requests-last": "17"}
        self.service.get("sports/americanfootball_nfl/events/" + "a" * 32 + "/odds", {"bookmakers": "fanduel"})
        usage = self.service.usage()
        self.assertEqual((usage.used, usage.remaining), (142, 358))
        self.assertEqual(self.session.get.call_count, 1)

    def test_header_case_decimal_format_and_zero_values_are_supported(self):
        self.assertEqual(credit_headers({"X-Requests-Used": "0", "X-Requests-Remaining": "500.0"}), [0, 500])
        self.assertEqual(credit_headers({"x-requests-used": "500", "x-requests-remaining": "0"}), [500, 0])
        for bad in ({}, {"x-requests-used": "5"}, {"x-requests-used": "NaN", "x-requests-remaining": "10"},
                    {"x-requests-used": "1.5", "x-requests-remaining": "10"},
                    {"x-requests-used": "1", "x-requests-remaining": "-1"}):
            with self.subTest(headers=bad), self.assertRaises(DataSourceError):
                credit_headers(bad)

    def test_cache_expiry_manual_refresh_and_provider_quota_reset(self):
        with patch("sports_analytics.odds_api.time.monotonic", return_value=100) as clock:
            first = self.service.usage()
            self.assertEqual(self.service.usage(), first)
            self.assertEqual(self.session.get.call_count, 1)
            clock.return_value = 401
            self.response.headers = {"x-requests-used": "500", "x-requests-remaining": "0"}
            self.assertEqual(self.service.usage().remaining, 0)
            self.assertEqual(self.session.get.call_count, 2)
            self.response.headers = {"x-requests-used": "0", "x-requests-remaining": "500"}
            reset = self.service.usage(refresh=True)
            self.assertEqual((reset.used, reset.remaining), (0, 500))
            self.assertEqual(self.session.get.call_count, 3)

    def test_missing_key_does_not_make_a_request_or_show_cached_account(self):
        self.service.usage()
        self.session.reset_mock()
        with patch.dict(os.environ, {"ODDS_API_KEY": ""}):
            with self.assertRaisesRegex(DataSourceError, "Set ODDS_API_KEY"):
                self.service.usage()
        self.session.get.assert_not_called()

    def test_failed_refresh_marks_saved_snapshot_and_key_switch_discards_it(self):
        self.service.usage()
        self.session.get.side_effect = requests.ConnectionError("A private request URL contains " + KEY)
        saved = self.service.usage(refresh=True)
        self.assertTrue(saved.stale)
        self.assertEqual(saved.used, 125)
        self.assertNotIn(KEY, saved.notice)
        with patch.dict(os.environ, {"ODDS_API_KEY": "different-account"}):
            with self.assertRaises(DataSourceError) as failure:
                self.service.usage()
        self.assertNotIn(KEY, str(failure.exception))

    def test_http_errors_are_credential_safe_and_not_shown_as_zero_usage(self):
        for code in (401, 403, 429, 500, 302):
            with self.subTest(code=code):
                self.response.status_code = code
                self.response.text = KEY
                with self.assertRaises(DataSourceError) as failure:
                    self.service.usage(refresh=True)
                self.assertNotIn(KEY, str(failure.exception))

    def test_donut_uses_exact_counts_and_handles_full_empty_and_zero_allocations(self):
        for used, remaining in ((125, 375), (0, 500), (500, 0)):
            with self.subTest(used=used):
                figure = credit_figure(CreditUsage(used, remaining, STAMP))
                self.assertEqual(list(figure.data[0].values), [used, remaining])
                self.assertEqual(list(figure.data[0].labels), ["Used", "Unused"])
                self.assertGreater(figure.data[0].hole, 0)
        figure = credit_figure(CreditUsage(0, 0, STAMP))
        self.assertEqual(len(figure.data), 0)
        self.assertIn("zero credit allocation", figure.layout.annotations[0].text)


class OddsApiPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = DataService(Path(self.tmp.name) / "historical.sqlite3")
        self.schedule = Mock()
        self.odds = Mock()
        self.odds.usage.return_value = CreditUsage(125, 375, STAMP)
        self.app = create_app(self.data, Mock(), self.schedule, self.odds)
        self.client = self.app.server.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, token, changed=None, **values):
        key = next(key for key in self.app.callback_map if token in key)
        config = self.app.callback_map[key]
        outputs = config["output"]
        if isinstance(outputs, list):
            outputs = [{"id": o.component_id, "property": o.component_property} for o in outputs]
        else:
            outputs = {"id": outputs.component_id, "property": outputs.component_property}
        defaults = {"sport.value": "ODDS_API", "workspace-page.value": "historical", "entity-type.value": "player",
                    "odds-refresh.n_clicks": 0, "odds-timer.n_intervals": 0}
        defaults.update(values)
        def props(items):
            return [{**item, "value": defaults.get(item["id"] + "." + item["property"])} for item in items]
        return self.client.post("/_dash-update-component", json={
            "output": key, "outputs": outputs, "inputs": props(config["inputs"]), "state": props(config["state"]),
            "changedPropIds": [changed] if changed else [],
        })

    def test_new_top_section_is_hidden_until_selected_and_keeps_existing_sports(self):
        response = self.client.get("/_dash-layout")
        self.assertEqual(response.status_code, 200)
        nodes = {node.id: node for node in self.app.layout._traverse() if hasattr(node, "id")}
        self.assertEqual([tab.value for tab in nodes["sport"].children], ["NBA", "NFL", "NCAAF", "ODDS_API"])
        self.assertEqual(nodes["odds-api-page"].style, {"display": "none"})
        self.odds.usage.assert_not_called()
        for sport in ("ODDS_API", "NBA", "NFL", "NCAAF"):
            data = self.callback("historical-page.style", **{"sport.value": sport}).json["response"]
            self.assertEqual(data["odds-api-page"]["style"]["display"], "block" if sport == "ODDS_API" else "none")
            self.assertEqual(data["sport-pages"]["style"]["display"], "none" if sport == "ODDS_API" else "grid")
            self.assertEqual(data["odds-timer"]["disabled"], sport != "ODDS_API")

    def test_open_refresh_and_timer_return_real_counters_and_donut(self):
        response = self.callback("odds-usage.figure")
        self.assertEqual(response.status_code, 200)
        data = response.json["response"]
        self.assertEqual(data["odds-usage"]["figure"]["data"][0]["values"], [125, 375])
        self.assertIn("Connected", data["odds-status"]["children"])
        self.odds.usage.assert_called_with(refresh=False)
        self.callback("odds-usage.figure", changed="odds-refresh.n_clicks", **{"odds-refresh.n_clicks": 1})
        self.odds.usage.assert_called_with(refresh=True)
        self.callback("odds-usage.figure", changed="odds-timer.n_intervals", **{"odds-timer.n_intervals": 1})
        self.odds.usage.assert_called_with(refresh=False)

    def test_missing_connection_is_actionable_and_has_no_fabricated_values(self):
        self.odds.usage.side_effect = DataSourceError("Set ODDS_API_KEY in .env")
        response = self.callback("odds-usage.figure")
        self.assertEqual(response.status_code, 200)
        data = response.json["response"]
        self.assertEqual(data["odds-usage"]["figure"]["data"], [])
        self.assertIn("ODDS_API_KEY", data["odds-status"]["children"])

    def test_account_key_is_absent_from_layout_and_usage_callback(self):
        with patch.dict(os.environ, {"ODDS_API_KEY": KEY}):
            self.assertNotIn(KEY, self.client.get("/_dash-layout").get_data(as_text=True))
            self.assertNotIn(KEY, self.callback("odds-usage.figure").get_data(as_text=True))

    def test_odds_section_does_not_load_historical_or_schedule_data(self):
        with patch.object(self.data, "load") as load:
            self.assertEqual(self.callback("dataset.data").status_code, 204)
        load.assert_not_called()
        self.assertEqual(self.callback("market-games.children", **{"workspace-page.value": "market"}).status_code, 204)
        self.assertEqual(self.callback("source-info.children").status_code, 204)
        self.assertEqual(self.callback("entity-type.options").status_code, 204)
        self.schedule.load.assert_not_called()
        self.assertEqual(self.callback("odds-usage.figure", **{"sport.value": "NBA"}).status_code, 204)
        self.odds.usage.assert_not_called()

    def test_failed_refresh_is_labeled_as_a_saved_snapshot(self):
        self.odds.usage.return_value = CreditUsage(125, 375, STAMP, stale=True, notice="Source unavailable")
        data = self.callback("odds-usage.figure").json["response"]
        self.assertIn("Saved usage snapshot", data["odds-status"]["children"])
        self.assertIn("Source unavailable", data["odds-status"]["children"])


if __name__ == "__main__":
    unittest.main()
