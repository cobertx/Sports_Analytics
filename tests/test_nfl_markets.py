"""Fixed-cost NFL pulls: event identity, complete ladders, click-only requests, and snapshots."""

import json
import csv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sports_analytics.dashboard import create_app, game_card
from sports_analytics.data import DataSourceError
from sports_analytics.nfl_markets import NFL_MARKETS, NflMarketService, match_event, snapshot_data
from sports_analytics.service import DataService
from sports_analytics.market_view import market_sections, recent_market_data, selection_text
from sports_analytics.display_time import format_eastern
from sports_analytics.dashboard import schedule_blocks

EVENT_ID = "a" * 32
GAME = {"id": "401873011", "start": "2026-10-25T17:00:00+00:00",
        "away": {"name": "New York Jets", "abbreviation": "NYJ"},
        "home": {"name": "Los Angeles Rams", "abbreviation": "LAR"},
        "time_tbd": False, "venue": "Test Stadium", "location": "Los Angeles", "neutral": False, "token": "click-token"}
EVENT = {"id": EVENT_ID, "sport_key": "americanfootball_nfl", "commence_time": "2026-10-25T17:00:00Z",
         "away_team": "New York Jets", "home_team": "Los Angeles Rams"}


def payload():
    markets = [
        {"key": "alternate_spreads", "outcomes": [{"name": "Los Angeles Rams", "point": -3.5, "price": -115},
                                                   {"name": "New York Jets", "point": 3.5, "price": -105}]},
        {"key": "player_pass_yds_alternate", "outcomes": [
            {"name": selection, "description": player, "point": point, "price": price}
            for player in ("First QB", "Second QB") for point in (175.5, 200.5, 225.5)
            for selection, price in (("Over", -120), ("Under", 100))]},
        {"key": "player_tds", "outcomes": [
            {"name": "Over", "description": "Runner", "point": point, "price": price}
            for point, price in ((.5, -110), (1.5, 350), (2.5, 1200))]},
        {"key": "player_anytime_td", "outcomes": [{"name": "Runner", "price": -110}]},
    ]
    return {**EVENT, "bookmakers": [{"key": "fanduel", "last_update": "2026-10-19T12:00:00Z", "markets": markets}]}


class NflMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = Mock()
        self.api.account_id.return_value = "account-fingerprint"
        self.events = Mock(headers={"x-requests-last": "0"})
        self.events.json.return_value = [EVENT]
        self.odds = Mock(headers={"x-requests-last": "4"})
        self.odds.json.return_value = payload()
        self.api.get.side_effect = [self.events, self.odds]
        self.service = NflMarketService(self.api, Path(self.tmp.name) / "nfl.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_seventeen_keys_with_no_extra_td_or_main_markets(self):
        expected = {"alternate_spreads", "alternate_totals", "player_pass_yds_alternate", "player_pass_tds_alternate",
                    "player_pass_attempts_alternate", "player_pass_completions_alternate", "player_pass_interceptions_alternate",
                    "player_reception_yds_alternate", "player_receptions_alternate", "player_reception_tds_alternate",
                    "player_rush_yds_alternate", "player_rush_attempts_alternate", "player_rush_tds_alternate",
                    "player_anytime_td", "player_1st_td", "player_last_td", "player_tds"}
        self.assertEqual(set(NFL_MARKETS), expected)
        snapshot = self.service.pull(GAME, "click-1")
        self.assertEqual(self.api.get.call_count, 2)
        self.assertEqual(self.api.get.call_args_list[0].args, ("sports/americanfootball_nfl/events",))
        self.assertEqual(self.api.get.call_args.args, (f"sports/americanfootball_nfl/events/{EVENT_ID}/odds",))
        params = self.api.get.call_args.kwargs["params"]
        self.assertEqual(params["bookmakers"], "fanduel")
        self.assertEqual(set(params["markets"].split(",")), expected)
        self.assertNotIn("regions", params)
        self.assertEqual(params["oddsFormat"], "american")
        self.assertEqual(snapshot["event_id"], EVENT_ID)
        self.assertNotEqual(snapshot["event_id"], GAME["id"])
        self.assertEqual(snapshot["cost"], 4)

    def test_all_players_thresholds_and_sides_are_saved_with_td_milestones(self):
        snapshot = self.service.pull(GAME, "click-1")
        passing = [r for r in snapshot["rows"] if r["group"] == "Passing"]
        self.assertEqual(len(passing), 12)
        self.assertEqual({r["subject"] for r in passing}, {"First QB", "Second QB"})
        self.assertEqual({r["selection"] for r in passing}, {"Over", "Under"})
        self.assertEqual({r["line"] for r in passing}, {175.5, 200.5, 225.5})
        self.assertEqual({r["td_threshold"] for r in snapshot["rows"] if r["market_key"] == "player_tds"},
                         {"1+ TDs", "2+ TDs", "3+ TDs"})
        self.assertEqual(len(snapshot["missing"]), 13)

    def test_snapshot_persists_across_restarts_and_repeated_click_is_not_charged_again(self):
        first = self.service.pull(GAME, "click-1")
        restarted = NflMarketService(self.api, self.service.path)
        self.assertEqual(restarted.pull(GAME, "click-1"), first)
        self.assertEqual(self.api.get.call_count, 2)
        self.assertEqual(restarted.latest([GAME["id"]])[GAME["id"]], first)
        self.api.get.side_effect = [self.events, self.odds]
        restarted.pull(GAME, "click-2")
        self.assertEqual(self.api.get.call_count, 4)

    def test_ambiguous_or_wrong_game_never_sends_paid_request(self):
        for events in ([], [EVENT, EVENT], [{**EVENT, "commence_time": "2026-11-01T17:00:00Z"}],
                       [{**EVENT, "away_team": "Kansas City Chiefs"}]):
            with self.subTest(events=events):
                self.api.get.reset_mock()
                self.api.get.side_effect = None
                self.events.json.return_value = events
                self.api.get.return_value = self.events
                with self.assertRaisesRegex(DataSourceError, "No odds request was sent"):
                    self.service.pull(GAME, "click-1")
                self.api.get.assert_called_once_with("sports/americanfootball_nfl/events")

    def test_short_names_and_neutral_designations_match_without_guessing(self):
        alternate = {**EVENT, "home_team": "NY Jets", "away_team": "LA Rams"}
        self.assertEqual(match_event([alternate], GAME), alternate)

    def test_failed_paid_request_is_not_retried_even_after_restart(self):
        self.api.get.side_effect = [self.events, DataSourceError("Connection timeout; request outcome unknown")]
        with self.assertRaises(DataSourceError):
            self.service.pull(GAME, "click-1")
        restarted = NflMarketService(self.api, self.service.path)
        with self.assertRaises(DataSourceError):
            restarted.pull(GAME, "click-1")
        self.assertEqual(self.api.get.call_count, 2)

    def test_other_bookmakers_unrequested_markets_and_unreported_cost_are_not_fabricated(self):
        raw = payload()
        raw["bookmakers"].append({"key": "draftkings", "markets": [{"key": "alternate_totals", "outcomes": [{"name": "Over", "price": 100, "point": 40.5}]}]})
        raw["bookmakers"][0]["markets"].append({"key": "player_tds_over", "outcomes": [{"name": "Runner", "price": 100}]})
        snapshot = snapshot_data(raw, EVENT, GAME["id"], {})
        self.assertIsNone(snapshot["cost"])
        self.assertNotIn("alternate_totals", snapshot["returned"])
        self.assertNotIn("player_tds_over", snapshot["returned"])
        raw["bookmakers"] = []
        snapshot = snapshot_data(raw, EVENT, GAME["id"], {"X-Requests-Last": "0"})
        self.assertEqual(snapshot["rows"], [])
        self.assertEqual(snapshot["cost"], 0)
        self.assertEqual(len(snapshot["missing"]), 17)

    def test_different_accounts_do_not_share_saved_snapshots(self):
        self.service.pull(GAME, "click-1")
        self.api.account_id.return_value = "other-account"
        self.assertEqual(self.service.latest([GAME["id"]]), {})

    def test_csv_filename_and_every_outcome_and_original_response_round_trip(self):
        snapshot = self.service.pull(GAME, "click-csv")
        path = self.service.csv_directory / snapshot["csv_file"]
        self.assertRegex(path.name, r"^nfl_lar_nyj_2026-10-25_\d{8}T\d{12}Z\.csv$")
        with path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), len(snapshot["rows"]))
        self.assertEqual(json.loads(rows[0]["api_response_json"]), payload())
        self.assertEqual({row["subject"] for row in rows}, {row["subject"] for row in snapshot["rows"]})
        self.assertEqual(rows[0]["home_abbreviation"], "LAR")
        self.assertEqual(rows[0]["away_abbreviation"], "NYJ")
        self.assertEqual(len(list(self.service.csv_directory.glob("*.tmp"))), 0)
        # CSVs remain sufficient after loss of the optional SQLite index.
        self.service.path.unlink()
        restarted = NflMarketService(self.api, self.service.path)
        restored = restarted.history(GAME["id"])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["rows"], snapshot["rows"])
        self.assertEqual(restored[0]["raw_response"], payload())
        self.assertEqual(self.api.get.call_count, 2)

    def test_csv_uses_eastern_game_date_for_evening_games_and_preserves_large_response(self):
        game = deepcopy(GAME)
        game["start"] = "2026-10-20T00:15:00+00:00"
        event = {**EVENT, "commence_time": game["start"]}
        self.events.json.return_value = [event]
        raw = {**payload(), "commence_time": game["start"], "extra_provider_data": "x" * 200000}
        self.odds.json.return_value = raw
        snapshot = self.service.pull(game, "evening-game")
        self.assertIn("nfl_lar_nyj_2026-10-19_", snapshot["csv_file"])
        self.service.path.unlink()
        restored = NflMarketService(self.api, self.service.path).history(GAME["id"])[0]
        self.assertEqual(restored["raw_response"], raw)

    def test_each_new_pull_writes_a_separate_csv_and_empty_responses_remain_recoverable(self):
        first = self.service.pull(GAME, "first")
        self.odds.json.return_value = {**EVENT, "bookmakers": []}
        self.api.get.side_effect = [self.events, self.odds]
        second = self.service.pull(GAME, "second")
        self.assertNotEqual(first["csv_file"], second["csv_file"])
        self.assertEqual(len(list(self.service.csv_directory.glob("*.csv"))), 2)
        self.service.path.unlink()
        history = NflMarketService(self.api, self.service.path).history(GAME["id"])
        self.assertEqual(history[0]["rows"], [])
        self.assertEqual(len(history), 2)
        self.assertEqual(self.api.get.call_count, 4)


class PullButtonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.markets = Mock()
        self.snapshot = snapshot_data(payload(), EVENT, GAME["id"], {"x-requests-last": "4"})
        self.markets.pull.return_value = self.snapshot
        self.markets.latest.return_value = {}
        self.markets.history.return_value = [self.markets.pull.return_value]
        self.app = create_app(DataService(Path(self.tmp.name) / "stats.sqlite3"), Mock(), Mock(), Mock(), self.markets)
        self.client = self.app.server.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, clicks=1, sport="NFL", page="market"):
        key = next(k for k in self.app.callback_map if "nfl-live-markets" in k)
        config = self.app.callback_map[key]
        button_id = {"type": "nfl-pull", "game": GAME["id"]}
        state = config["state"]
        return self.client.post("/_dash-update-component", json={
            "output": key, "outputs": {"id": {"type": "nfl-live-markets", "game": GAME["id"]}, "property": "children"},
            "inputs": [{"id": button_id, "property": "n_clicks", "value": clicks}],
            "state": [{"id": {"type": "nfl-game-context", "game": GAME["id"]}, "property": "data", "value": GAME},
                      {**state[1], "value": sport}, {**state[2], "value": page}],
            "changedPropIds": [json.dumps(button_id, sort_keys=True, separators=(",", ":")) + ".n_clicks"],
        })

    def test_only_nfl_cards_have_pull_button_next_to_details(self):
        for sport in ("NFL", "NBA", "NCAAF"):
            card = game_card(GAME, sport)
            buttons = [node for node in card._traverse() if isinstance(getattr(node, "id", None), dict) and node.id.get("type") == "nfl-pull"]
            self.assertEqual(len(buttons), 1 if sport == "NFL" else 0)
        self.markets.pull.assert_not_called()

    def test_initial_render_hidden_pages_and_other_sports_never_pull(self):
        for args in ({"clicks": 0}, {"sport": "NBA"}, {"sport": "NCAAF"}, {"page": "historical"}):
            self.assertEqual(self.callback(**args).status_code, 204)
        self.markets.pull.assert_not_called()

    def test_one_click_updates_latest_time_without_inline_markets(self):
        response = self.callback()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.markets.pull.assert_called_once_with(GAME, f"{GAME['id']}:click-token:1")
        text = response.get_data(as_text=True)
        self.assertIn("Latest data:", text)
        self.assertIn("market-data-recent", text)
        self.assertIn("latest-pull-timestamp", text)
        self.assertRegex(text, r"\b(?:EST|EDT)\b")
        self.assertNotIn("First QB", text)
        self.assertNotIn("Unavailable markets", text)

    def test_failure_keeps_previous_snapshot_time(self):
        self.markets.latest.return_value = {GAME["id"]: self.markets.pull.return_value}
        self.markets.pull.side_effect = DataSourceError("Check ODDS_API_KEY")
        response = self.callback()
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Latest data:", text)
        self.assertIn("ODDS_API_KEY", text)
        self.assertNotIn("First QB", text)

    def test_recency_uses_elapsed_time_and_excludes_24_hour_old_or_future_pulls(self):
        now = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
        for age, expected in [(timedelta(0), True), (timedelta(hours=23, minutes=59), True),
                              (timedelta(hours=24), False), (timedelta(days=2), False),
                              (timedelta(minutes=-1), False)]:
            with self.subTest(age=age):
                self.assertEqual(recent_market_data({"pulled_at": (now - age).isoformat()}, now), expected)
        self.assertTrue(recent_market_data({"pulled_at": "2026-10-20T02:00:00+03:00"}, now))
        for missing in (None, {}, {"pulled_at": "invalid"}):
            self.assertFalse(recent_market_data(missing, now))

    def test_failed_pull_preserves_old_data_background_marker(self):
        self.snapshot["pulled_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.markets.latest.return_value = {GAME["id"]: self.snapshot}
        self.markets.pull.side_effect = DataSourceError("Source unavailable")
        response = self.callback()
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("market-data-old", text)
        self.assertNotIn("market-data-recent", text)
        self.assertIn("Source unavailable", text)

    def detail_callback(self, token, changed=None, **values):
        key = next(key for key in self.app.callback_map if token in key)
        config = self.app.callback_map[key]
        outputs = config["output"]
        if isinstance(outputs, list):
            outputs = [{"id": item.component_id, "property": item.component_property} for item in outputs]
        else:
            outputs = {"id": outputs.component_id, "property": outputs.component_property}
        snapshot = self.snapshot
        settings = {"market-url.hash": "#nfl-game-" + GAME["id"], "sport.value": "NFL", "workspace-page.value": "market",
                    "market-schedule.data": [GAME], "market-snapshot.value": snapshot["pulled_at"], "market-category.value": "Passing",
                    "market-detail-context.data": GAME, "market-detail-pull.n_clicks": 1, "market-refresh-signal.data": None}
        settings.update(values)
        def properties(items):
            return [{**item, "value": settings.get(item["id"] + "." + item["property"])} for item in items]
        return self.client.post("/_dash-update-component", json={"output": key, "outputs": outputs,
            "inputs": properties(config["inputs"]), "state": properties(config["state"]), "changedPropIds": [changed] if changed else []})

    def test_card_links_to_detail_and_shows_saved_timestamp_without_markets(self):
        card = game_card(GAME, "NFL", self.markets.history.return_value[0])
        nodes = list(card._traverse())
        self.assertTrue(any(getattr(node, "href", "") == "#nfl-game-" + GAME["id"] for node in nodes))
        self.assertTrue(any("Latest data:" in str(getattr(node, "children", "")) for node in nodes))
        self.assertFalse(any(getattr(node, "className", "") == "sportsbook-market" for node in nodes))

    def test_game_detail_opens_saved_snapshot_and_back_restores_list_without_api_pull(self):
        response = self.detail_callback("market-list.style")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        view = response.json["response"]
        self.assertEqual(view["market-list"]["style"], {"display": "none"})
        self.assertEqual(view["market-game-detail"]["style"], {"display": "block"})
        self.assertEqual(len(view["market-snapshot"]["options"]), 1)
        returned = self.detail_callback("market-list.style", **{"market-url.hash": "#nfl-games"}).json["response"]
        self.assertEqual(returned["market-list"]["style"], {"display": "block"})
        self.markets.pull.assert_not_called()

    def test_sportsbook_categories_only_contain_returned_markets(self):
        response = self.detail_callback("market-category.children")
        self.assertEqual(response.status_code, 200)
        data = response.json["response"]
        tabs = data["market-category"]["children"]
        self.assertEqual({item["props"]["value"] for item in tabs}, {"Game", "Passing", "Touchdowns"})
        self.assertNotIn("Unavailable", response.get_data(as_text=True))
        sections = self.detail_callback("saved-market-sections.children")
        self.assertEqual(sections.status_code, 200)
        body = sections.get_data(as_text=True)
        self.assertIn("First QB - Alt Passing Yds", body)
        self.assertIn("market-price", body)
        self.assertIn("Passing Yards", body)
        self.assertNotIn("DataTable", body)
        self.markets.pull.assert_not_called()

    def test_alternate_integer_threshold_labels_are_exact_without_odds_invention(self):
        row = {"subject": "Quarterback", "selection": "Over", "line": 174.5, "market_key": "player_pass_yds_alternate"}
        self.assertEqual(selection_text(row), "Quarterback 175+ Passing Yards")
        self.assertEqual(selection_text({**row, "line": 175}), "Quarterback 176+ Passing Yards")
        self.assertEqual(selection_text({**row, "selection": "Under"}), "Quarterback Under 174.5 Passing Yards")

    def test_all_displayed_times_use_consistent_numeric_edt_and_local_dates(self):
        self.assertEqual(format_eastern("2026-10-19T23:35:23+00:00"), "10/19/2026 19:35:23 EDT")
        self.assertEqual(format_eastern("2026-12-19T23:35:23+00:00"), "12/19/2026 19:35:23 EDT")
        game = {**GAME, "start": "2026-10-20T00:15:00+00:00"}
        for sport in ("NBA", "NFL", "NCAAF"):
            nodes = list(game_card(game, sport)._traverse())
            time = next(node for node in nodes if getattr(node, "className", "") == "game-time")
            self.assertEqual(time.children, "10/19/2026 20:15:00 EDT")
            section = schedule_blocks([game], sport)[0]
            self.assertIn("10/19/2026", section.children[0].children)

    def test_detail_pull_requires_an_explicit_click_in_the_selected_game(self):
        for values in ({"market-detail-pull.n_clicks": 0}, {"sport.value": "NBA"},
                       {"workspace-page.value": "historical"}, {"market-url.hash": "#nfl-games"}):
            self.assertEqual(self.detail_callback("market-refresh-signal.data", **values).status_code, 204)
        self.markets.pull.assert_not_called()
        response = self.detail_callback("market-refresh-signal.data", changed="market-detail-pull.n_clicks")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.markets.pull.assert_called_once_with(GAME, f"detail:{GAME['id']}:click-token:1")
        signal = response.json["response"]["market-refresh-signal"]["data"]
        self.assertEqual(signal["game_id"], GAME["id"])
        self.assertEqual(signal["pulled_at"], self.snapshot["pulled_at"])

    def test_new_detail_snapshot_is_selected_but_manual_history_selection_is_preserved(self):
        newer = {**self.snapshot, "pulled_at": "2026-10-21T01:00:00+00:00", "csv_file": "new_snapshot.csv"}
        self.markets.history.return_value = [newer, self.snapshot]
        signal = {"game_id": GAME["id"], "snapshot": newer["csv_file"], "pulled_at": newer["pulled_at"]}
        response = self.detail_callback("market-list.style", changed="market-refresh-signal.data", **{"market-refresh-signal.data": signal})
        self.assertEqual(response.json["response"]["market-snapshot"]["value"], "new_snapshot.csv")
        response = self.detail_callback("market-list.style", changed="market-schedule.data", **{"market-refresh-signal.data": signal})
        self.assertEqual(response.json["response"]["market-snapshot"]["value"], self.snapshot["pulled_at"])
        self.markets.pull.assert_not_called()

    def test_detail_clicks_write_new_csvs_and_reopening_only_reads_saved_data(self):
        api = Mock()
        api.account_id.return_value = "test-account"
        events = Mock()
        events.json.return_value = [EVENT]
        odds = Mock(headers={"x-requests-last": "4"})
        odds.json.return_value = payload()
        api.get.side_effect = [events, odds, events, odds]
        service = NflMarketService(api, Path(self.tmp.name) / "saved.sqlite3", Path(self.tmp.name) / "market_data")
        self.app = create_app(DataService(Path(self.tmp.name) / "stats.sqlite3"), Mock(), Mock(), Mock(), service)
        self.client = self.app.server.test_client()
        for clicks in (1, 2):
            response = self.detail_callback("market-refresh-signal.data", changed="market-detail-pull.n_clicks",
                                            **{"market-detail-pull.n_clicks": clicks})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(list(service.csv_directory.glob("*.csv"))), 2)
        self.assertEqual(api.get.call_count, 4)
        opened = self.detail_callback("market-list.style")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(len(opened.json["response"]["market-snapshot"]["options"]), 2)
        self.assertEqual(api.get.call_count, 4)

    def test_detail_pull_status_does_not_follow_user_to_a_different_game(self):
        result = {"game_id": GAME["id"], "error": "Source unavailable"}
        response = self.detail_callback("market-detail-pull-status.children", **{"market-refresh-signal.data": result})
        self.assertIn("Source unavailable", response.get_data(as_text=True))
        response = self.detail_callback("market-detail-pull-status.children", **{"market-refresh-signal.data": result,
                                                                               "market-url.hash": "#nfl-game-999"})
        self.assertEqual(response.json["response"]["market-detail-pull-status"]["children"], [])


if __name__ == "__main__":
    unittest.main()
