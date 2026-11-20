"""Offline NBA injury and lineup model, market, and replay checks."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dashboard import create_app
from app.sections.backtesting.backtesting_view import find_model
from app.sections.markets import quick_methods
from models.Player_Stats.Injury_Lineup_Adjusted import InjuryLineupConfig, InjuryLineupMarketService
from models.Player_Stats.Injury_Lineup_Adjusted.rates import LineupRates
from models.Player_Stats.Playing_Time_Rotation import PlayingTimeRotationMarketService
from models.Player_Stats.Usage_Opportunity.historical_data import eligible
from sports_analytics.availability import AvailabilityStore
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.data import DataSourceError
from scripts.callback_fixtures import callback_functions
from check_playing_time_rotation import inputs


class InjuryLineupChecks(TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.players, self.teams = inputs()
        role = self.players.entity_id.eq("BOS1") & self.players.event_date.dt.day.mod(2).eq(0)
        self.players.loc[role, ["fga", "fgm", "fg3a", "fg3m", "ast"]] = [22, 10, 8, 4, 6]
        self.cutoff = pd.Timestamp("2025-11-14T20:00:00Z")
        self.snapshot = dict(sport="NBA", game_id="123", home_team="Boston Celtics", away_team="New York Knicks",
                             pulled_at=self.cutoff.isoformat(), commence_time="2025-11-14T23:00:00Z",
                             rows=[dict(subject=subject, market_key="player_points_alternate", market="Points",
                                        line=20, selection="Over", odds=-110, group="Points")
                                   for subject in ("BOS Player 0", "BOS Player 1")])
        config = InjuryLineupConfig()
        self.config = replace(config, baseline=replace(config.baseline, simulations=1200))
        self.lineups = self.root / "lineups"
        self.lineups.mkdir()
        self.availability = AvailabilityStore(directory=self.root / "availability",
                                              fetch=Mock(side_effect=AssertionError("No live reports in replay")))
        self.repository = Mock(root=self.root, load=Mock(return_value=(self.players, self.teams)))
        self.service = InjuryLineupMarketService(self.repository, self.config,
                                                availability=self.availability, lineup_directory=self.lineups)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No external HTTP"))
        network.start()
        self.addCleanup(network.stop)

    def save_report(self, when, player="BOS Player 0", status="Out"):
        when = pd.Timestamp(when)
        packet = dict(sport="NBA", fetched_at=when.isoformat(), reports=[
            dict(team="Boston Celtics", player=player, status=status,
                 reported_at=(when-pd.Timedelta(minutes=1)).isoformat())])
        archive = self.availability.directory / "archive/nba" / when.strftime("%Y-%m-%d")
        archive.mkdir(parents=True, exist_ok=True)
        (archive / (when.strftime("%H%M%S")+".json")).write_text(json.dumps(packet), encoding="utf-8")
        return packet

    def scenarios(self):
        return dict(available_at=(self.cutoff-pd.Timedelta(minutes=2)).isoformat(), scenarios=[
            dict(label="Limited", probability=.35, teams={"BOS": {
                "active": ["BOS Player 0"], "minutes_caps": {"BOS Player 0": 20}}}),
            dict(label="Out", probability=.65, teams={"BOS": {"out": ["BOS Player 0"]},
                                                    "NYK": {"out": ["NYK Player 0"]}})])

    def test_joint_scenarios_conserve_minutes_respect_caps_and_mix_props(self):
        snapshot = dict(self.snapshot, lineup_scenarios=self.scenarios())
        state = self.service._availability(snapshot, self.cutoff, ["BOS", "NYK"])
        players, teams = eligible(self.players, self.cutoff, "NBA"), eligible(self.teams, self.cutoff, "NBA")
        roster = self.service._roster(players, ["BOS", "NYK"])
        rotations = {}
        for club in ("BOS", "NYK"):
            source = roster[roster.team.eq(club)]
            rotations[club], _ = self.service._team_model(players[players.team.eq(club)], teams[teams.team.eq(club)],
                source, set(source.entity_id), self.cutoff, club, 391, state)
            np.testing.assert_allclose(np.column_stack(list(rotations[club].minutes.values())).sum(axis=1),
                                       240, rtol=0, atol=1e-8)
        limited = rotations["BOS"].minutes["BOS0"]
        np.testing.assert_array_equal(limited > 0, rotations["NYK"].minutes["NYK0"] > 0)
        self.assertLessEqual(limited.max(), 20.)
        result = self.service.forecast(snapshot)
        fitted = result.forecasts["BOS Player 0", "player_points"]
        self.assertAlmostEqual(fitted.diagnostics["play_probability"], .35, delta=.05)
        self.assertAlmostEqual(fitted.diagnostics["unconditional_mean"],
                               fitted.mean*fitted.diagnostics["play_probability"])
        estimate = fitted.market(fitted.quantile(.5), "Over")
        self.assertGreater(estimate.push, 0)
        self.assertAlmostEqual(estimate.win+estimate.loss+estimate.push, 1)
        teammate = result.forecasts["BOS Player 1", "player_points"]
        evidence = teammate.diagnostics["lineup_evidence"]
        self.assertGreater(evidence["role_conditioned_weight"], 0)
        self.assertGreater(evidence["fg3a_conditioned_fraction"], 0)

    def test_archives_and_supplied_versions_respect_cutoffs_and_cache(self):
        past = self.save_report(self.cutoff-pd.Timedelta(minutes=10), status="Questionable")
        future = self.save_report(self.cutoff+pd.Timedelta(minutes=1), status="Out")
        (self.availability.directory/"nba.json").write_text(json.dumps(future), encoding="utf-8")
        versions = [self.scenarios(), dict(self.scenarios(), available_at=future["fetched_at"],
                                         scenarios=[dict(label="Future", probability=1)])]
        (self.lineups/"123.json").write_text(json.dumps(versions), encoding="utf-8")
        result = self.service.forecast(self.snapshot)
        self.assertEqual(result.availability["fetched_at"], past["fetched_at"])
        self.assertEqual(result.availability["scenarios_at"], versions[0]["available_at"])
        self.assertEqual(result.availability["excluded"], [])
        self.assertIn(("BOS Player 0", "player_points"), result.forecasts)
        with patch.object(self.service, "_simulate", side_effect=AssertionError("Reuse the exact inputs")):
            self.assertEqual(self.service.forecast(self.snapshot).forecasts, result.forecasts)
        self.availability.fetch.assert_not_called()

    def test_status_corrections_supersede_earlier_report_rows(self):
        stamp = self.cutoff-pd.Timedelta(minutes=3)
        packet = self.save_report(stamp)
        packet["reports"].append(dict(packet["reports"][0], status="Active", reported_at=stamp.isoformat()))
        (self.availability.directory/"nba.json").write_text(json.dumps(packet), encoding="utf-8")
        forecast = self.service.forecast(self.snapshot)
        self.assertEqual(forecast.availability["excluded"], [])
        self.assertIn(("BOS Player 0", "player_points"), forecast.forecasts)

    def test_infeasible_rotations_and_invalid_probabilities_are_explicit(self):
        packet = dict(self.scenarios(), scenarios=[dict(label="Too few", probability=1.,
                      teams={"BOS":{"out":["BOS Player 3", "BOS Player 4", "BOS Player 5"]}})])
        result = self.service.forecast(dict(self.snapshot, lineup_scenarios=packet))
        self.assertFalse(result.forecasts)
        self.assertIn("Too few", result.errors["BOS Player 0", "player_points"])
        packet["scenarios"][0]["probability"] = .6
        with self.assertRaisesRegex(DataSourceError, "sum to one"):
            self.service.forecast(dict(self.snapshot, lineup_scenarios=packet))

    def test_no_eligible_lineup_inputs_reuses_rotation_baseline(self):
        prior = PlayingTimeRotationMarketService(self.repository, self.config, persist=False).forecast(self.snapshot)
        result = self.service.forecast(self.snapshot)
        self.assertEqual(prior.forecasts.keys(), result.forecasts.keys())
        for key, forecast in result.forecasts.items():
            self.assertEqual(forecast.values, prior.forecasts[key].values)
            self.assertEqual(forecast.probabilities, prior.forecasts[key].probabilities)

    def test_sparse_joint_evidence_uses_one_supported_with_without_cohort(self):
        dates = pd.date_range("2025-11-01", periods=8, tz="UTC")
        ids = [str(i) for i in range(len(dates))]
        games = pd.DataFrame({"event_date":dates}, index=ids)
        minutes = pd.DataFrame({"known":[30]*4+[0]*4, "new":[0]*7+[12]}, index=ids)
        counts = self.config.baseline.simulations
        context = LineupRates(minutes, games, {"known":np.zeros(counts), "new":np.zeros(counts)},
                              ["known","new"], {"known":dates[0], "new":dates[-1]}, self.config)
        selected = list(context._matching(pd.DataFrame({"game_id":ids}), np.full(8, .125)))
        self.assertEqual(len(selected), 1)
        _, weights, effective = selected[0]
        np.testing.assert_array_equal(weights, [0,0,0,0,.25,.25,.25,.25])
        self.assertEqual(effective, 4)

    def test_nba_market_and_walk_forward_entry_points(self):
        self.assertIsNotNone(find_model("NBA", ("player-stats", "ila")))
        self.assertIsNone(find_model("NFL", ("player-stats", "ila")))
        self.assertIn("ILA", quick_methods.player_methods("NBA"))
        self.assertNotIn("ILA", quick_methods.player_methods("NCAAF"))
        app = create_app(injury_lineup_service=self.service,
                         nba_market_service=Mock(history=Mock(return_value=[self.snapshot])))
        callbacks = callback_functions(app)
        self.assertEqual(callbacks["player_forecast_methods"]("#nba-game-123", "ILA")[1], "ILA")
        sections, history = callbacks["show_saved_markets"]("Points", self.snapshot["pulled_at"],
                                                            "#nba-game-123", None, "PPP", "ILA", None)
        self.assertIn("ILA mean", str(sections))
        self.assertTrue(history["player"]["ILA"]["latest_date"])
        report = self.save_report("2025-11-13T22:45:00Z")
        self.save_report("2025-11-13T23:01:00Z", status="Healthy")
        replay = BacktestingService(root=self.root/"backtesting")
        with (patch.object(replay, "player_inputs", return_value=self.players),
              patch.object(replay, "game_inputs", return_value=(self.teams, None)),
              patch.object(replay, "player_service", return_value=self.service),
              patch.object(replay, "phase_map", return_value={})):
            result = replay.run("NBA", "player-stats", "ila", "BOS1", "player_points",
                                start="2025-11-13", end="2025-11-14", limit=1, line=20,
                                config_overrides={"baseline": {"simulations": 1200}})
        record = result["records"][0]
        self.assertEqual(record["injury_report_at"], report["fetched_at"])
        self.assertLess(record["latest_training"], record["date"][:10])
        self.assertGreater(record["lineup_role_weight"], 0)
        self.assertIsNotNone(replay.saved("NBA", "player-stats", "ila", result["run_id"]))


if __name__ == "__main__":
    main()
