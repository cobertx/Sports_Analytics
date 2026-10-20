"""EPA paper formulas, signed ratings, chronological forecasts and method switching."""

from dataclasses import asdict
import json
import math
from pathlib import Path
from statistics import NormalDist
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from Models.EPA import EPAConfig, EPAError, EPAForecast, EPAModel, expected_points_added, scoring_bridge
from Models.PPP import PPPConfig, PPPForecast
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.epa_service import EPAMarketService
from sports_analytics.nfl_model_data import epa_features
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.service import DataService


def observations():
    rng = np.random.default_rng(17)
    teams = ["SF", "WSH", "CHI", "NE"]
    strength = {"SF": .12, "WSH": -.08, "CHI": -.04, "NE": 0}
    records = []
    for week in range(24):
        date = pd.Timestamp("2024-09-01", tz="UTC") + pd.Timedelta(weeks=week)
        others = [teams[(week + i) % 3 + 1] for i in range(3)]
        for index, (home, away) in enumerate([(teams[0], others[0]), (others[1], others[2])]):
            game = []
            for side, (team, opponent) in enumerate([(home, away), (away, home)]):
                plays = int(rng.integers(52, 74))
                passes = round(.6 * plays)
                advantage = strength[team] - .5 * strength[opponent]
                pass_epa = advantage + .04 + rng.normal(0, .06)
                rush_epa = advantage - .06 + rng.normal(0, .06)
                points = max(0, round(11 * 1.8 + 1.3 * plays * advantage + rng.normal(0, 4)))
                game.append({"game_id": f"fixture_{week}_{index}", "season": 2024, "week": week + 1,
                    "event_date": date.isoformat(), "available_at": (date + pd.Timedelta(days=1, hours=12)).isoformat(),
                    "team": team, "opponent": opponent, "is_home": int(side == 0), "neutral": 0,
                    "offensive_points": points, "non_offensive_points": 2, "points": points + 2,
                    "offensive_plays": plays, "offensive_drives": 11, "off_epa_plays": plays,
                    "off_pass_epa_plays": passes, "off_rush_epa_plays": plays - passes,
                    "off_pass_epa_sum": pass_epa * passes, "off_rush_epa_sum": rush_epa * (plays - passes)})
            for side, row in enumerate(game):
                opponent = game[1-side]
                row.update(defensive_plays=opponent["offensive_plays"], defensive_drives=11,
                           offensive_points_allowed=opponent["offensive_points"], points_allowed=opponent["points"])
                for column in ("epa_plays", "pass_epa_plays", "rush_epa_plays", "pass_epa_sum", "rush_epa_sum"):
                    row["def_" + column] = opponent["off_" + column]
            records.extend(game)
    return pd.DataFrame(records)


def snapshot():
    return {"game_id": "123", "event_id": "b" * 32, "home_team": "San Francisco 49ers",
            "away_team": "Washington Commanders", "pulled_at": "2026-10-19T23:35:23+00:00",
            "commence_time": "2026-10-20T00:15:00Z", "rows": [
                {"group": "Game", "market_key": "alternate_spreads", "market": "Alternate spreads",
                 "subject": "San Francisco 49ers", "selection": "San Francisco 49ers", "line": -3.5,
                 "odds": -110, "updated": "Not reported"},
                {"group": "Game", "market_key": "alternate_totals", "market": "Alternate points total",
                 "subject": "Over", "selection": "Over", "line": 45.5, "odds": -110, "updated": "Not reported"}]}


class EPAModelTests(unittest.TestCase):
    def test_signed_reward_and_scoring_bridge_match_paper(self):
        self.assertAlmostEqual(expected_points_added(2.1, 4), 1.9)
        self.assertAlmostEqual(expected_points_added(2.1, 3, possession_changed=True), -5.1)
        self.assertAlmostEqual(expected_points_added(2.1, 0, reward=7), 4.9)
        self.assertAlmostEqual(scoring_bridge(11, 6, .15, 2, 1.1), 32.89)
        self.assertLess(scoring_bridge(11, 6, -.1, 2, 1.1), 22)

    def test_paper_nfl_covariance_example_and_push_pricing(self):
        forecast = EPAForecast("A", "B", 24.8, 21.5, math.sqrt(145), math.sqrt(217))
        self.assertAlmostEqual(forecast.total_mean, 46.3)
        self.assertAlmostEqual(forecast.margin_mean, 3.3)
        self.assertAlmostEqual(forecast.normal_over(44.5), .548627, places=6)
        self.assertAlmostEqual(NormalDist().cdf(forecast.margin_mean / forecast.margin_sd), .608, places=3)
        over, under = forecast.total(45), forecast.total(45, "Under")
        self.assertGreater(over.push, 0)
        self.assertAlmostEqual(over.win + under.win + over.push, 1)
        self.assertAlmostEqual(over.probability + under.probability, 1)

    def test_negative_epa_is_retained_and_missing_values_are_not_zero(self):
        plays = pd.DataFrame({"play_type": ["pass", "run", "qb_spike", "qb_kneel"], "epa": [1.9, -5.1, -.2, -.5]})
        values = epa_features(plays, plays)
        self.assertEqual(values["off_epa_plays"], 4)
        self.assertAlmostEqual(values["off_epa_sum"], -3.9)
        self.assertAlmostEqual(values["off_pass_epa_sum"], 1.7)
        self.assertAlmostEqual(values["def_rush_epa_sum"], -5.6)
        missing = epa_features(plays.drop(columns="epa"), plays.drop(columns="epa"))
        self.assertEqual(missing["off_epa_plays"], 0)
        self.assertTrue(math.isnan(missing["off_epa_sum"]))

    def test_ratings_center_both_team_effects_and_fit_the_scoring_bridge(self):
        model = EPAModel(EPAConfig(minimum_training_games=8, minimum_team_games=3, minimum_calibration_games=10))
        frame = model._history(observations(), "2025-03-01T12:00:00Z")
        ratings = model._fit(frame)
        n = len(ratings["teams"])
        for beta in ratings["coefficients"].values():
            self.assertAlmostEqual(float(beta[2:2+n].sum()), 0)
            self.assertAlmostEqual(float(beta[2+n:].sum()), 0)
        self.assertTrue(np.isfinite(ratings["bridge"]).all())
        self.assertGreater(ratings["bridge"][0], 0)
        self.assertGreater(ratings["bridge"][1], 0)
        means, detail = model._means(frame, ratings, "SF", "WSH")
        self.assertGreater(means[0], means[1])
        self.assertGreater(detail["SF"]["epa_advantage"], detail["WSH"]["epa_advantage"])

    def test_walk_forward_epa_forecasts_exclude_future_results(self):
        model = EPAModel(EPAConfig(minimum_training_games=8, minimum_team_games=3, minimum_calibration_games=10))
        frame = observations()
        result = model.predict(frame, "SF", "WSH", "2025-03-01T12:00:00Z")
        self.assertIsInstance(result, EPAForecast)
        self.assertIn("EPA", result.diagnostics["method"])
        residuals = result.diagnostics["validation_residuals"]
        self.assertAlmostEqual(result.total_sd, np.std([row["home_error"] + row["away_error"] for row in residuals], ddof=1))
        future = frame.copy()
        future["game_id"] += "_future"
        future["event_date"] = "2025-04-01T00:00:00Z"
        future["available_at"] = "2025-04-02T12:00:00Z"
        future["points"] = 999
        future["off_pass_epa_sum"] = 999
        other = model.predict(pd.concat([frame, future]), "SF", "WSH", "2025-03-01T12:00:00Z")
        self.assertEqual(asdict(result), asdict(other))

    def test_incomplete_epa_cannot_produce_a_fallback_forecast(self):
        frame = observations()
        frame.loc[0, "off_epa_plays"] -= 1
        with self.assertRaises(EPAError):
            EPAModel().predict(frame, "SF", "WSH", "2025-03-01T12:00:00Z")
        with self.assertRaises(EPAError):
            EPAModel().predict(frame.drop(columns="off_pass_epa_sum"), "SF", "WSH", "2025-03-01T12:00:00Z")


class EPAIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ppp_result = PPPForecast("SF", "WSH", 20, 20, 13, 14, {"as_of": snapshot()["pulled_at"]})
        self.epa_result = EPAForecast("SF", "WSH", 40, 10, 13, 14, {"as_of": snapshot()["pulled_at"]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_epa_archives_are_separate_from_ppp_and_reload_as_epa(self):
        data = Mock(directory=self.root)
        data.prepare.return_value = (pd.DataFrame(), [])
        ppp_model, epa_model = Mock(config=PPPConfig()), Mock(config=EPAConfig())
        ppp_model.predict.return_value, epa_model.predict.return_value = self.ppp_result, self.epa_result
        ppp, epa = PPPMarketService(data, ppp_model), EPAMarketService(data, epa_model)
        ppp.forecast(snapshot())
        first = epa.forecast(snapshot())
        reloaded = EPAMarketService(data, epa_model).forecast(snapshot())
        self.assertIsInstance(reloaded, EPAForecast)
        self.assertEqual(asdict(first), asdict(reloaded))
        self.assertEqual(len(list((self.root / "forecasts").glob("ppp_*.json"))), 1)
        self.assertEqual(len(list((self.root / "forecasts").glob("epa_*.json"))), 1)
        epa_model.predict.assert_called_once()

    def test_switching_method_changes_columns_edge_charts_and_criteria_without_odds_pull(self):
        markets, ppp, epa, odds = Mock(), Mock(), Mock(), Mock()
        markets.history.return_value = [snapshot()]
        ppp.forecast.return_value, epa.forecast.return_value = self.ppp_result, self.epa_result
        app = create_app(DataService(self.root / "data.sqlite3"), Mock(), Mock(), odds, markets, ppp_service=ppp, epa_service=epa)
        nodes = {node.id: node for node in app.layout._traverse() if hasattr(node, "id")}
        self.assertEqual(nodes["market-forecast-method"].value, "PPP")
        self.assertEqual([item["value"] for item in nodes["market-forecast-method"].options], ["PPP", "EPA"])
        key = next(key for key in app.callback_map if "saved-market-sections.children" in key)
        config = app.callback_map[key]
        client = app.server.test_client()
        def render(method):
            values = {"market-category": "Game", "market-snapshot": snapshot()["pulled_at"], "market-url": "#nfl-game-123",
                      "market-applied-criteria": {"probability": .75, "edge": .05}, "market-forecast-method": method}
            response = client.post("/_dash-update-component", json={"output": key,
                "outputs": {"id": "saved-market-sections", "property": "children"},
                "inputs": [{**item, "value": values[item["id"]]} for item in config["inputs"]], "state": [],
                "changedPropIds": ["market-forecast-method.value"]})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_data(as_text=True)
        first = render("PPP")
        self.assertIn("PPP fair odds", first)
        self.assertNotIn("market-criteria-match", first)
        epa.forecast.assert_not_called()
        second = render("EPA")
        self.assertIn("EPA fair odds", second)
        self.assertIn("EPA probability", second)
        self.assertIn("EPA forecast:", second)
        self.assertNotIn("PPP probability", second)
        self.assertIn("market-criteria-match", second)
        expected_edge = self.epa_result.spread("SF", -3.5).probability - 110 / 210
        self.assertIn(f"{expected_edge*100:+.2f} pp", second)
        ppp.forecast.assert_called_once()
        epa.forecast.assert_called_once_with(snapshot())
        epa.forecast.side_effect = DataSourceError("EPA coverage missing")
        failure = render("EPA")
        self.assertIn("EPA coverage missing", failure)
        self.assertIn("EPA fair odds", failure)
        self.assertNotIn("market-criteria-match", failure)
        ppp.forecast.assert_called_once()
        markets.pull.assert_not_called()
        odds.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
