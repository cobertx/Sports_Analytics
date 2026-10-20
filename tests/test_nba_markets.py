"""NBA snapshot billing/isolation, forecast cutoffs and Dash market workflow."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from Models.Game_Outcome.PPP.NBA_PPP import NBAPPPConfig, NBAPPPModel, matchup_ppp
from Models.Game_Outcome.PPP.PPP import PPPError, PPPForecast, implied_probability
from Models.Game_Outcome.EPA.NBA_EPA import NBAEPAForecast
from sports_analytics.dashboard import create_app, game_card
from sports_analytics.data import DataSourceError
from sports_analytics.market_view import market_sections
from sports_analytics.nba_markets import NBA_MARKETS, NbaMarketService
from sports_analytics.nba_model_data import aggregate_team_games
from sports_analytics.nba_ppp_service import NBAPPPMarketService
from sports_analytics.nfl_markets import NflMarketService, match_event, snapshot_data
from sports_analytics.service import DataService


GAME = {"id": "123", "sport": "NBA", "start": "2026-10-25T23:00:00Z", "time_tbd": False, "neutral": False,
        "away": {"name": "Los Angeles Lakers", "abbreviation": "LAL"},
        "home": {"name": "Golden State Warriors", "abbreviation": "GSW"},
        "venue": "Test arena", "location": "San Francisco", "token": "test-click"}
EVENT = {"id": "b" * 32, "sport_key": "basketball_nba", "commence_time": GAME["start"],
         "home_team": GAME["home"]["name"], "away_team": GAME["away"]["name"]}


def payload():
    return {**EVENT, "bookmakers": [{"key": "fanduel", "markets": [
        {"key": "alternate_spreads", "outcomes": [{"name": EVENT["home_team"], "point": -3.5, "price": -110},
                                                   {"name": EVENT["away_team"], "point": 3.5, "price": -110}]},
        {"key": "alternate_totals", "outcomes": [{"name": side, "point": line, "price": -110}
                                                    for side in ["Over", "Under"] for line in [190.5, 270.5]]},
        {"key": "player_points_alternate", "outcomes": [
            {"name": side, "description": "Test player", "point": line, "price": -115}
            for side in ["Over", "Under"] for line in [19.5, 24.5]]}]}]}


def snapshot():
    saved = snapshot_data(payload(), EVENT, GAME["id"], {"x-requests-last": "3"}, NBA_MARKETS, "NBA")
    saved.update(pulled_at="2026-10-19T23:00:00+00:00", game=GAME)
    return saved


def forecast():
    return PPPForecast("GSW", "LAL", 130, 105, 13, 16,
                       {"as_of": "2026-10-19T23:00:00+00:00", "training_games": 1000, "validation_games": 300})


def observations(days=100):
    """Synthetic observations for deterministic math tests only."""
    rng = np.random.default_rng(71)
    teams, rows = ["GSW", "LAL", "BOS", "NYK"], []
    for day in range(days):
        date = pd.Timestamp("2025-10-01", tz="UTC") + pd.Timedelta(days=day)
        home, away = teams[day % 4], teams[(day + 1 + (day // 4) % 2) % 4]
        pace = float(rng.integers(92, 107))
        scores = rng.integers(90, 132, size=2)
        for i, (team, opponent) in enumerate([(home, away), (away, home)]):
            rows.append({"game_id": f"test_{day}", "team": team, "opponent": opponent,
                         "event_date": date.isoformat(), "available_at": (date + pd.Timedelta(days=2)).isoformat(),
                         "points": float(scores[i]), "points_allowed": float(scores[1-i]),
                         "possessions": pace, "is_home": int(i == 0)})
    return pd.DataFrame(rows)


class NBASnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = Mock()
        self.api.account_id.return_value = "account-fingerprint"
        self.events = Mock()
        self.events.json.return_value = [EVENT]
        self.odds = Mock(headers={"x-requests-last": "3", "x-requests-used": "20", "x-requests-remaining": "480"})
        self.odds.json.return_value = payload()
        self.api.get.side_effect = [self.events, self.odds]
        self.markets = NbaMarketService(self.api, self.root / "markets.sqlite3", self.root / "market_data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_nba_only_package_cost_log_new_files_and_restart_without_requests(self):
        first = self.markets.pull(GAME, "click-1")
        self.assertEqual(self.api.get.call_args_list[0].args, ("sports/basketball_nba/events",))
        self.assertEqual(self.api.get.call_args.args, (f"sports/basketball_nba/events/{EVENT['id']}/odds",))
        params = self.api.get.call_args.kwargs["params"]
        self.assertEqual(params["bookmakers"], "fanduel")
        self.assertEqual(set(params["markets"].split(",")), set(NBA_MARKETS))
        self.assertEqual(len(NBA_MARKETS), 13)
        self.assertFalse(any("pass_" in key or "rush_" in key for key in NBA_MARKETS))
        self.assertTrue(first["csv_file"].startswith("nba_gsw_lal_2026-10-25_"))
        self.assertEqual(first["sport"], "NBA")
        rows = self.markets.usage_log.records("account-fingerprint")
        self.assertEqual([(row["sport"], row["credits"]) for row in rows], [("NBA", 3)])
        self.assertEqual(self.markets.pull(GAME, "click-1"), first)
        self.assertEqual(self.api.get.call_count, 2)
        # Identical IDs and the same SQLite file cannot expose another sport.
        nfl = NflMarketService(self.api, self.markets.path, self.markets.csv_directory, self.markets.usage_log)
        self.assertEqual(nfl.history(GAME["id"]), [])
        # CSV files alone recover the snapshot after SQLite is lost.
        self.markets.path.unlink()
        restored = NbaMarketService(self.api, self.markets.path, self.markets.csv_directory, self.markets.usage_log)
        self.assertEqual(restored.history(GAME["id"])[0]["raw_response"], payload())
        self.assertEqual(len(restored.usage_log.records("account-fingerprint")), 1)
        self.assertEqual(self.api.get.call_count, 2)
        self.api.get.side_effect = [self.events, self.odds]
        second = restored.pull(GAME, "click-2")
        self.assertNotEqual(first["csv_file"], second["csv_file"])
        self.assertEqual(len(restored.history(GAME["id"])), 2)

    def test_wrong_sport_or_ambiguous_event_is_never_charged_and_clippers_alias_matches(self):
        for events in [[{**EVENT, "sport_key": "americanfootball_nfl"}], [EVENT, EVENT]]:
            self.events.json.return_value = events
            self.api.get.side_effect = [self.events]
            with self.assertRaisesRegex(DataSourceError, "No odds request"):
                self.markets.pull(GAME, "unused")
        clippers = {**GAME, "home": {"name": "LA Clippers"}}
        event = {**EVENT, "home_team": "Los Angeles Clippers"}
        self.assertEqual(match_event([event], clippers, "basketball_nba"), event)

    def test_failed_nba_pull_logs_actual_charge_without_retry(self):
        self.odds.json.return_value = {"bad": "schema"}
        with self.assertRaises(DataSourceError):
            self.markets.pull(GAME, "failed")
        self.assertEqual(self.api.get.call_count, 2)
        row = self.markets.usage_log.records("account-fingerprint")[0]
        self.assertEqual((row["sport"], row["credits"], row["status"]), ("NBA", 3, "Failed / not saved"))
        with self.assertRaises(DataSourceError):
            self.markets.pull(GAME, "failed")
        self.assertEqual(self.api.get.call_count, 2)


class NBAForecastTests(unittest.TestCase):
    def test_source_pairs_shared_possessions_and_incomplete_games(self):
        frame = pd.DataFrame([
            {"game_id": "001", "team": "GSW", "opponent": "LAL", "matchup": "LAL @ GSW", "event_date": "2025-10-01",
             "pts": 110, "fga": 90, "fta": 20, "oreb": 10, "tov": 12},
            {"game_id": "001", "team": "LAL", "opponent": "GSW", "matchup": "LAL @ GSW", "event_date": "2025-10-01",
             "pts": 105, "fga": 88, "fta": 25, "oreb": 9, "tov": 11},
            {"game_id": "002", "team": "GSW", "opponent": "BOS", "matchup": "BOS @ GSW", "event_date": "2025-10-02",
             "pts": 120, "fga": 90, "fta": 20, "oreb": 10, "tov": 12},
        ])
        result = aggregate_team_games(frame)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result.game_id), {"001"})
        self.assertTrue(np.allclose(result.possessions, (100.8 + 101) / 2))
        self.assertEqual(list(result.points_allowed), [105, 110])
        self.assertEqual(list(result.is_home), [1, 0])
        self.assertEqual(result.available_at.iloc[0], "2025-10-03T00:00:00+00:00")
        with self.assertRaises(DataSourceError):
            aggregate_team_games(pd.concat([frame, frame.iloc[:1]]))
        self.assertAlmostEqual(matchup_ppp(1.18, 1.14, 1.12), 1.20)

    def test_predictions_hold_out_dates_and_ignore_future_scores_and_pace(self):
        model = NBAPPPModel(NBAPPPConfig(minimum_training_games=20, minimum_team_games=4,
                                        minimum_calibration_games=15, maximum_calibration_games=30))
        frame = observations()
        as_of = "2026-01-01T00:00:00+00:00"
        first = model.predict(frame, "GSW", "LAL", as_of)
        poisoned = frame.copy()
        future = pd.to_datetime(poisoned.available_at, utc=True).ge(pd.Timestamp(as_of))
        poisoned.loc[future, ["points", "points_allowed", "possessions"]] = 10000
        second = model.predict(poisoned, "GSW", "LAL", as_of)
        self.assertEqual(asdict(first), asdict(second))
        self.assertEqual(first.diagnostics["validation_games"], 30)
        self.assertGreater(first.margin_sd, 0)
        # Integers have a refund mass; Over and Under use one distribution.
        over, under = first.total(220), first.total(220, "Under")
        self.assertGreater(over.push, 0)
        self.assertAlmostEqual(over.probability + under.probability, 1)
        self.assertAlmostEqual(first.spread("GSW", -3.5).probability + first.spread("LAL", 3.5).probability, 1)
        self.assertEqual(first.spread("GSW", 0).push, 0)
        self.assertGreater(first.spread("GSW", -3).push, 0)
        self.assertAlmostEqual(sum(asdict(first.spread("GSW", 3)).values()), 1)
        bad = frame.copy()
        bad.loc[0, "points_allowed"] += 1
        with self.assertRaises(PPPError):
            model.predict(bad, "GSW", "LAL", as_of)

    def test_snapshot_cutoff_season_and_archived_forecast_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Mock(directory=Path(directory))
            data.prepare.return_value = (observations(), [])
            model = Mock(config=NBAPPPConfig())
            model.predict.return_value = forecast()
            service = NBAPPPMarketService(data, model)
            saved = snapshot()
            saved["pulled_at"] = "2026-10-26T12:00:00+00:00"
            result = service.forecast(saved)
            data.prepare.assert_called_once_with([2025, 2026])
            self.assertEqual(model.predict.call_args.args[3], "2026-10-25T23:00:00+00:00")
            self.assertTrue(result.diagnostics["snapshot_after_kickoff"])
            self.assertEqual(asdict(NBAPPPMarketService(data, model).forecast(saved)), asdict(result))
            model.predict.assert_called_once()


class NBADashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nfl, self.nba, self.ppp, self.epa, self.nba_ppp, self.odds = [Mock() for _ in range(6)]
        self.nba_epa = Mock()
        self.nba_epa.forecast.return_value = NBAEPAForecast("GSW", "LAL", 100, 130, 13, 16,
                                                          {"as_of": "2026-10-19T23:00:00+00:00"})
        self.saved = snapshot()
        self.nba.history.return_value = [self.saved]
        self.nba.latest.return_value = {GAME["id"]: self.saved}
        self.nba.pull.return_value = self.saved
        self.nba_ppp.forecast.return_value = forecast()
        self.app = create_app(DataService(Path(self.tmp.name) / "stats.sqlite3"), Mock(), Mock(), self.odds, self.nfl,
                              ppp_service=self.ppp, epa_service=self.epa, nba_market_service=self.nba, nba_ppp_service=self.nba_ppp,
                              nba_epa_service=self.nba_epa)
        self.client = self.app.server.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, token, changed=None, **values):
        key = next(key for key in self.app.callback_map if token in key)
        config = self.app.callback_map[key]
        outputs = config["output"]
        outputs = ([{"id": item.component_id, "property": item.component_property} for item in outputs] if isinstance(outputs, list)
                   else {"id": outputs.component_id, "property": outputs.component_property})
        settings = {"market-url.hash": "#nba-game-123", "sport.value": "NBA", "workspace-page.value": "market",
                    "market-schedule.data": [GAME], "market-refresh-signal.data": None, "market-snapshot.value": self.saved["pulled_at"],
                    "market-detail-context.data": GAME, "market-detail-pull.n_clicks": 1, "market-category.value": "Game",
                    "market-applied-criteria.data": {"probability": .75, "edge": .05}, "market-forecast-method.value": "PPP"}
        settings.update(values)
        def props(items):
            return [{**item, "value": settings.get(item["id"] + "." + item["property"])} for item in items]
        response = self.client.post("/_dash-update-component", json={"output": key, "outputs": outputs,
                                    "inputs": props(config["inputs"]), "state": props(config["state"]),
                                    "changedPropIds": [changed] if changed else []})
        self.assertIn(response.status_code, [200, 204], response.get_data(as_text=True))
        return response

    def test_route_headers_categories_and_snapshot_reads_never_pull(self):
        restored = self.callback("sport.value").json["response"]
        self.assertEqual(restored["sport"]["value"], "NBA")
        detail_response = self.callback("market-list.style")
        detail = detail_response.get_data(as_text=True)
        self.assertIn("NBA", detail)
        self.assertIn("Golden State Warriors", detail)
        self.assertIn("/images/nba/team-logos/", str(detail_response.json))
        self.assertNotIn("espncdn", detail)
        categories = self.callback("market-category.children").get_data(as_text=True)
        self.assertIn("Game Lines", categories)
        self.assertIn("Points", categories)
        self.assertNotIn("Passing Props", categories)
        methods = self.callback("market-forecast-method.options").json["response"]
        self.assertEqual(methods["market-forecast-method"]["value"], "PPP")
        self.assertFalse(methods["market-forecast-method"]["options"][1].get("disabled", False))
        self.nba.pull.assert_not_called()
        self.nfl.history.assert_not_called()
        self.odds.get.assert_not_called()

    def test_nba_values_edges_all_four_charts_and_criteria_in_both_groups(self):
        text = self.callback("saved-market-sections.children").get_data(as_text=True)
        self.assertIn("PPP fair odds", text)
        expected = forecast().spread("GSW", -3.5).probability - implied_probability(-110)
        self.assertIn(f"{expected * 100:+.2f} pp", text)
        sections = market_sections(self.saved, "Game", forecast(), criteria={"probability": .75, "edge": .05})
        for section in [section for section in sections if getattr(section, "className", "").startswith("sportsbook-market")]:
            self.assertTrue(any("market-criteria-match" in getattr(node, "className", "") for node in section._traverse()))
        graphs = [node for node in sections[-1]._traverse() if hasattr(node, "figure")]
        self.assertEqual(len(graphs), 4)
        self.assertTrue(all([trace.name for trace in graph.figure.data[:2]] ==
                            ["PPP probability", "Market implied probability"] for graph in graphs))
        self.assertTrue(any(any(trace.name == "Meets criteria" for trace in graph.figure.data) for graph in graphs))
        props = self.callback("saved-market-sections.children", **{"market-category.value": "Points"}).get_data(as_text=True)
        self.assertIn("Test player 20+ Points", props)
        self.assertNotIn("market-criteria-match", props)
        self.nba_ppp.forecast.assert_called_once()
        self.ppp.forecast.assert_not_called()
        self.epa.forecast.assert_not_called()
        self.nba.pull.assert_not_called()
        selected = str(self.callback("saved-market-sections.children", **{"market-forecast-method.value": "EPA"}).json)
        self.assertIn("EPA fair odds", selected)
        self.assertIn("event-based", selected)
        self.nba_epa.forecast.assert_called_once()
        self.nba_epa.forecast.side_effect = DataSourceError("NBA EPA input unavailable")
        failure = str(self.callback("saved-market-sections.children", **{"market-forecast-method.value": "EPA"}).json)
        self.assertIn("NBA EPA input unavailable", failure)
        self.assertIn("Criteria could not be evaluated", failure)
        self.nba_ppp.forecast.assert_called_once()

    def test_apply_opens_game_lines_and_reports_matches_or_zero_matches(self):
        applied = self.callback("market-applied-criteria.data", changed="market-apply-criteria.n_clicks",
                                **{"market-apply-criteria.n_clicks": 1, "market-probability-threshold.value": 75,
                                   "market-edge-threshold.value": 5, "market-category.value": "Points"})
        result = applied.json["response"]
        self.assertEqual(result["market-category"]["value"], "Game")
        self.assertEqual(result["market-applied-criteria"]["data"], {"probability": .75, "edge": .05})
        matches = str(self.callback("saved-market-sections.children").json)
        self.assertIn("qualifying selections", matches)
        self.assertIn("market-criteria-match", matches)
        unmatched = str(self.callback("saved-market-sections.children", **{"market-applied-criteria.data": {"probability": .99, "edge": 1}}).json)
        self.assertIn("No markets match these criteria", unmatched)
        self.assertNotIn("market-criteria-match", unmatched)
        self.assertIn("market-criteria-no-match", unmatched)
        self.assertIn("no match", unmatched)
        self.nba.pull.assert_not_called()
        self.odds.get.assert_not_called()

    def test_detail_pull_requires_explicit_click_and_matching_sport(self):
        self.assertEqual(self.callback("market-refresh-signal.data", **{"market-detail-pull.n_clicks": 0}).status_code, 204)
        self.assertEqual(self.callback("market-refresh-signal.data", **{"sport.value": "NFL"}).status_code, 204)
        self.nba.pull.assert_not_called()
        result = self.callback("market-refresh-signal.data").json["response"]["market-refresh-signal"]["data"]
        self.assertEqual(result["sport"], "NBA")
        self.nba.pull.assert_called_once_with(GAME, "detail:123:test-click:1")
        self.nfl.pull.assert_not_called()

    def test_nba_card_button_and_pull_updates_only_timestamp(self):
        nodes = list(game_card(GAME, "NBA", self.saved)._traverse())
        self.assertTrue(any(getattr(node, "href", "") == "#nba-game-123" for node in nodes))
        self.assertTrue(any(getattr(node, "id", None) == {"type": "nba-pull", "game": "123"} for node in nodes))
        key = next(key for key in self.app.callback_map if "nba-live-markets" in key)
        button = {"type": "nba-pull", "game": "123"}
        body = {"output": key, "outputs": {"id": {"type": "nba-live-markets", "game": "123"}, "property": "children"},
                "inputs": [{"id": button, "property": "n_clicks", "value": 1}],
                "state": [{"id": {"type": "nba-game-context", "game": "123"}, "property": "data", "value": GAME},
                          {"id": "sport", "property": "value", "value": "NBA"},
                          {"id": "workspace-page", "property": "value", "value": "market"}],
                "changedPropIds": [json.dumps(button, sort_keys=True, separators=(",", ":")) + ".n_clicks"]}
        response = self.client.post("/_dash-update-component", json=body)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("Latest data:", response.get_data(as_text=True))
        self.assertNotIn("Test player", response.get_data(as_text=True))
        self.nba.pull.assert_called_once()
        self.nfl.pull.assert_not_called()


if __name__ == "__main__":
    unittest.main()
