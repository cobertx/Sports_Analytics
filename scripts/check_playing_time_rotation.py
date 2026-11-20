"""Offline NBA rotation, prop, replay, and dashboard integration checks."""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dashboard import create_app
from app.sections.backtesting.backtesting_results import render
from app.sections.backtesting.backtesting_view import find_model
from app.sections.markets import quick_methods
from models.Player_Stats.Playing_Time_Rotation import PlayingTimeRotationMarketService, RotationConfig
from models.Player_Stats.Playing_Time_Rotation.rotation import draw_rotation, project_minutes
from models.Player_Stats.Usage_Opportunity.historical_data import eligible
from scripts.callback_fixtures import callback_functions
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.data import DataSourceError


def inputs():
    players, teams = [], []
    for index, date in enumerate(pd.date_range("2025-11-01 23:00", periods=13, freq="D", tz="UTC")):
        for club, opponent, home in [("BOS", "NYK", 1), ("NYK", "BOS", 0)]:
            game = dict(game_id=f"002250{index:04d}", team=club, opponent=opponent, is_home=home,
                        event_date=date, available_at=date+pd.Timedelta(hours=5), season=2025)
            teams.append(dict(game, entity_id=club, entity_name=club, min=240, fga=90, fgm=45, fg3a=36,
                              fg3m=18, fta=24, ftm=18, oreb=12, dreb=30, ast=24, stl=12, blk=6, tov=12))
            slots = range(6) if club != "BOS" or index % 2 == 0 else range(1, 7)
            for slot in slots:
                players.append(dict(game, entity_id=f"{club}{slot}", entity_name=f"{club} Player {slot}", min=40,
                                    fga=15, fgm=8 if slot == 0 else 7, fg3a=6, fg3m=3, fta=4, ftm=3,
                                    pts=22 if slot == 0 else 20, oreb=2, dreb=5, reb=7, ast=4, stl=2, blk=1, tov=2))
    return pd.DataFrame(players), pd.DataFrame(teams)


class PlayingTimeRotationChecks(TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.players, self.teams = inputs()
        self.cutoff = pd.Timestamp("2025-11-14T20:00:00Z")
        self.snapshot = dict(sport="NBA", game_id="123", home_team="Boston Celtics", away_team="New York Knicks",
                             pulled_at=self.cutoff.isoformat(), commence_time="2025-11-14T23:00:00Z",
                             rows=[dict(subject="BOS Player 0", market_key=market, market=market, line=line, selection="Over", odds=-110,
                                        group="NBA Player Props") for market,line in
                                   [("player_points_alternate", 20), ("player_points_rebounds_assists", 29)]])
        self.config = replace(RotationConfig(), baseline=replace(RotationConfig().baseline, simulations=1000))
        self.repository = Mock(root=self.root, load=Mock(return_value=(self.players, self.teams)))
        self.service = PlayingTimeRotationMarketService(repository=self.repository, config=self.config)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No external HTTP"))
        network.start()
        self.addCleanup(network.stop)

    def test_rotation_draws_cover_team_minutes_and_keep_dnp_separate(self):
        with self.assertRaises(DataSourceError):
            project_minutes(np.full((1, 4), 40), np.full((1, 4), 48))
        players = eligible(self.players, self.cutoff, "NBA")
        teams = eligible(self.teams, self.cutoff, "NBA")
        club = players[players.team.eq("BOS")]
        rotation = draw_rotation(club, teams[teams.team.eq("BOS")], club.entity_id, self.cutoff,
                                 self.config, np.random.default_rng(8))
        minutes = np.column_stack(list(rotation.minutes.values()))
        np.testing.assert_allclose(minutes.sum(axis=1), 240, atol=1e-8, rtol=0)
        self.assertTrue(((minutes >= 0) & (minutes <= 48)).all())
        self.assertTrue((rotation.minutes["BOS0"] == 0).any())
        self.assertTrue((rotation.minutes["BOS0"] > 0).any())

    def test_prop_distribution_cache_and_cutoff(self):
        result = self.service.forecast(self.snapshot)
        fitted = result.forecasts["BOS Player 0", "player_points"]
        self.assertAlmostEqual(sum(fitted.probabilities), 1)
        self.assertGreater(fitted.diagnostics["play_probability"], 0)
        self.assertLess(fitted.diagnostics["play_probability"], 1)
        self.assertAlmostEqual(fitted.diagnostics["minutes_mean"],
                               fitted.diagnostics["play_probability"]*fitted.diagnostics["minutes_if_playing"])
        self.assertAlmostEqual(fitted.diagnostics["unconditional_mean"], fitted.mean*fitted.diagnostics["play_probability"])
        price = fitted.market(fitted.quantile(.5), "Over")
        self.assertGreater(price.push, 0)
        self.assertAlmostEqual(price.win+price.loss+price.push, 1)
        combo = result.forecasts["BOS Player 0", "player_points_rebounds_assists"]
        self.assertGreater(combo.mean, fitted.mean)
        with patch.object(self.service, "_simulate", side_effect=AssertionError("Reuse the timestamped forecast")):
            self.assertEqual(self.service.forecast(self.snapshot).forecasts, result.forecasts)
        future = self.players.iloc[:1].assign(game_id="future", event_date=self.cutoff-pd.Timedelta(hours=1),
                                              available_at=self.cutoff, min=99, fga=400)
        self.repository.load.return_value = (pd.concat([self.players, future], ignore_index=True), self.teams)
        self.service.persist = False
        self.assertEqual(self.service.forecast(self.snapshot).forecasts, result.forecasts)

    def test_availability_reallocates_only_using_reports_at_the_cutoff(self):
        packet = dict(fetched_at=self.cutoff.isoformat(), reports=[
            dict(team="Boston Celtics", player="BOS Player 1", status="Out",
                 reported_at=(self.cutoff-pd.Timedelta(hours=1)).isoformat())])
        availability = Mock(load=Mock(return_value=(packet, None)))
        self.service.availability = availability
        adjusted = self.service.forecast(self.snapshot).forecasts["BOS Player 0", "player_points"]
        self.assertAlmostEqual(adjusted.diagnostics["minutes_if_playing"], 48, places=7)
        packet["fetched_at"] = (self.cutoff+pd.Timedelta(seconds=1)).isoformat()
        baseline = self.service.forecast(self.snapshot).forecasts["BOS Player 0", "player_points"]
        self.assertAlmostEqual(baseline.diagnostics["minutes_if_playing"], 40, places=7)

    def test_nba_market_and_backtest_entry_points(self):
        self.assertIsNotNone(find_model("NBA", ("player-stats", "ptr")))
        self.assertIsNone(find_model("NFL", ("player-stats", "ptr")))
        markets = Mock(history=Mock(return_value=[self.snapshot]))
        app = create_app(playing_time_rotation_service=self.service, nba_market_service=markets)
        callbacks = callback_functions(app)
        callback = callbacks["player_forecast_methods"]
        self.assertEqual(callback("#nba-game-123", "PTR")[1], "PTR")
        self.assertEqual(callback("#nfl-game-123", "PTR")[1], "WHP")
        self.assertIn("PTR", quick_methods.player_methods("NBA"))
        self.assertNotIn("PTR", quick_methods.player_methods("NCAAF"))
        sections, history = callbacks["show_saved_markets"]("NBA Player Props", self.snapshot["pulled_at"],
                                                            "#nba-game-123", None, "PPP", "PTR", None)
        self.assertIn("PTR probability", str(sections))
        self.assertIn("PTR mean", str(sections))
        self.assertTrue(history["player"]["PTR"]["latest_date"])
        replay = BacktestingService(root=self.root/"backtesting")
        with (patch.object(replay, "player_inputs", return_value=self.players),
              patch.object(replay, "game_inputs", return_value=(self.teams, None)),
              patch.object(replay, "player_service", return_value=self.service),
              patch.object(replay, "phase_map", return_value={})):
            result = replay.run("NBA", "player-stats", "ptr", "BOS0", "player_points",
                                start="2025-11-12", end="2025-11-14", limit=1, line=20,
                                config_overrides={"baseline":{"simulations":1000}})
        self.assertEqual(len(result["records"]), 1)
        record = result["records"][0]
        self.assertEqual(record["actual_minutes"], 40)
        self.assertLess(record["latest_training"], record["date"][:10])
        self.assertAlmostEqual(record["projected_minutes"], 40)
        self.assertIn("MINUTES MAE", str(render(result)))
        self.assertIsNotNone(replay.saved("NBA", "player-stats", "ptr", result["run_id"]))


if __name__ == "__main__":
    main()
