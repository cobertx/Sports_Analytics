"""Offline checks for the NBA additive ratings and scoring-rate simulation."""
from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, main

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_nba_pace import history
from models.Game_Outcome.Adjusted_Offensive_Defensive_Efficiency import (
    NBAAdjustedEfficiencyConfig, NBAAdjustedEfficiencyForecast, NBAAdjustedEfficiencyModel,
)
from models.Game_Outcome.Adjusted_Offensive_Defensive_Efficiency.NBA_AODE import simulate
from models.Game_Outcome.Pace_Possession import NBAPaceConfig, NBAPaceModel
from sports_analytics.backtesting_service import BacktestingService, evaluated_records
from sports_analytics.backtesting_view import find_model
from sports_analytics.mispricing import selected_methods
from sports_analytics.quick_methods import choices, prompt_key, scope


class AdjustedEfficiencyChecks(TestCase):
    def test_weighted_fit_and_additive_matchup_formula(self):
        model = NBAAdjustedEfficiencyModel()
        teams = pd.DataFrame(dict(points=[121., 113., 104.], points_allowed=[102., 116., 107.],
                                  possessions=[102., 94., 90.], neutral=[False, True, False]))
        home, away, recency = np.array([0, 2, 0]), np.array([1, 0, 2]), np.array([.2, .7, 1.])
        league, effects = model._fit_efficiency(teams, 3, home, away, recency)
        design, rates, weights = [], [], []
        for row, host, visitor, weight in zip(teams.itertuples(index=False), home, away, recency):
            advantage = .0 if row.neutral else .5
            for offense, defense, points, venue in [(host, visitor, row.points, advantage),
                                                    (visitor, host, row.points_allowed, -advantage)]:
                features = np.zeros(7)
                features[offense], features[3 + defense], features[-1] = 1, -1, venue
                design.append(features)
                rates.append(points / row.possessions * 100)
                weights.append(weight * row.possessions / 100)
        design, rates, weights = map(np.asarray, (design, rates, weights))
        center = np.average(rates, weights=weights)
        penalty = np.diag([model.config.efficiency_ridge] * 6 + [model.config.venue_ridge])
        expected = np.linalg.lstsq(np.vstack([design * np.sqrt(weights[:, None]), np.sqrt(penalty)]),
                                   np.r_[(rates - center) * np.sqrt(weights), np.zeros(7)], rcond=None)[0]
        self.assertAlmostEqual(league, center)
        np.testing.assert_allclose(effects, expected)
        example = (115., np.array([2., -1., 3., -1., 2.]))
        np.testing.assert_allclose(model._matchup_rates(example, 2, 0, 1, False), [1.19, 1.10])
        np.testing.assert_allclose(model._matchup_rates(example, 2, 0, 1, True), [1.18, 1.11])

    def test_shared_draws_settle_integer_and_half_point_lines(self):
        parameters = [99., 3.5, [1.19, 1.10], [.01, -.01], [[.01, .002], [.002, .014]], 6000, 2026]
        scores, ties = simulate(*parameters)
        home, away = scores.T
        forecast = NBAAdjustedEfficiencyForecast.from_parameters("BOS", "NYK", parameters, {})
        self.assertGreater(ties, 0)
        self.assertFalse(np.any(home == away))
        self.assertEqual(forecast.spread("BOS", -5.5).win, np.mean(home - away > 5.5))
        self.assertEqual(forecast.total(225).push, np.mean(home + away == 225))
        self.assertEqual(forecast.team_total("NYK", 109.5).loss, np.mean(away < 109.5))
        self.assertEqual(forecast.total(225.5).push, 0)
        self.assertEqual(asdict(forecast), asdict(NBAAdjustedEfficiencyForecast.from_parameters("BOS", "NYK", parameters, {})))
        forecast.apply_score_adjustment(np.array([5., 0.]), np.eye(2))
        np.testing.assert_allclose(forecast.diagnostics["simulation"][4], np.asarray(parameters[4]) + np.eye(2) / 99**2)
        self.assertGreater(forecast.spread("BOS", 0).win, np.mean(home > away))

    def test_cutoffs_shared_pace_and_saved_backtest(self):
        frame = history()
        cutoff = frame.event_date.sort_values().unique()[-2]
        delayed = frame.game_id.eq("10-0")
        revised = frame.copy()
        revised.loc[delayed, "available_at"] = cutoff
        revised.loc[delayed | revised.event_date.ge(cutoff), ["points", "points_allowed"]] += 20
        settings = dict(minimum_training_games=8, minimum_team_games=3,
                        minimum_calibration_games=7, maximum_calibration_games=16, simulations=2000)
        model = NBAAdjustedEfficiencyModel(NBAAdjustedEfficiencyConfig(**settings))
        historical = frame[~delayed]
        base = model.predict(historical, "A", "B", cutoff)
        self.assertEqual(asdict(base), asdict(model.predict(revised, "A", "B", cutoff)))
        pace = NBAPaceModel(NBAPaceConfig(**settings)).predict(historical, "A", "B", cutoff)
        self.assertEqual(base.diagnostics["projected_possessions"], pace.diagnostics["projected_possessions"])
        self.assertNotEqual(base.masses, pace.masses)
        self.assertEqual(set(base.diagnostics["adjusted_ratings"]), {"A", "B"})
        with TemporaryDirectory() as directory:
            service = BacktestingService(root=directory, now=lambda: pd.Timestamp("2026-01-01", tz="UTC"))
            service.game_inputs = lambda *_: (frame, None)
            report = service.run("NBA", "game-outcome", "aode", entity="A", limit=2, config_overrides=settings)
            self.assertEqual(report["skipped"], [])
            self.assertEqual(len(report["records"]), 2)
            self.assertEqual(service.latest("NBA", "game-outcome", "aode")["run_id"], report["run_id"])
            for row in evaluated_records(report, "total"):
                self.assertEqual([row["lower"], row["upper"]], row["total_interval"])

    def test_aode_is_available_only_for_nba(self):
        key = prompt_key(dict(market_key="alternate_totals", line=220.5))
        for sport in ("NBA", "NFL", "NCAAF"):
            context = scope("market", sport, "123")
            ids = [dict(scope=context, prompt=key)]
            if sport == "NBA":
                self.assertEqual(selected_methods(["AODE"], ["WHP"], sport), (["AODE"], ["WHP"]))
                self.assertEqual(choices([["AODE"]], ids, context), {key: ["AODE"]})
                self.assertIsNotNone(find_model(sport, ("game-outcome", "aode")))
            else:
                self.assertIsNone(find_model(sport, ("game-outcome", "aode")))
                with self.assertRaises(ValueError):
                    selected_methods(["AODE"], ["WHP"], sport)
                with self.assertRaises(ValueError):
                    choices([["AODE"]], ids, context)


if __name__ == "__main__":
    main()
