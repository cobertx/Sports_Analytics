"""Offline checks for the Four Factors accounting, replay and market paths."""
from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, main

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_nba_pace as market_fixtures
from check_backtesting import team_history
from models.Game_Outcome.Four_Factors import NBAFourFactorsConfig, NBAFourFactorsModel
from models.Game_Outcome.Four_Factors.NBA_FF import (
    AWAY_EXPOSURE, AWAY_LATENT, COUNTS, EXPOSURE_COLUMNS, LATENT_COLUMNS,
    fit_factor_effects, from_latent, observed_factors, scoring_rate, simulate, to_latent,
)
from sports_analytics.backtesting_service import BacktestingService, evaluated_records
from app.sections.markets.mispricing import selected_methods
from sports_analytics.nba_model_data import FACTOR_INPUTS, aggregate_team_games
from app.sections.markets.quick_methods import choices, prompt_key, scope


def history():
    frame = team_history()
    rng = np.random.default_rng(834)
    n = len(frame)
    frame["fga"] = rng.integers(81, 100, n)
    frame["fgm"] = rng.integers(34, 47, n)
    frame["fg3m"] = rng.integers(9, 18, n)
    frame["fta"] = rng.integers(16, 32, n)
    frame["ftm"] = (frame.fta * rng.uniform(.65, .9, n)).astype(int)
    frame["oreb"] = rng.integers(7, 16, n)
    frame["dreb"] = rng.integers(28, 40, n)
    frame["tov"] = rng.integers(8, 19, n)
    frame["matchup"] = np.where(frame.is_home.eq(1), frame.opponent+" @ "+frame.team, frame.team+" @ "+frame.opponent)
    frame["pts"] = 2*frame.fgm + frame.fg3m + frame.ftm
    # Keep the fixture's paired final scores distinct.
    ties = frame.pts.eq(frame.groupby("game_id").pts.transform("mean")) & frame.is_home.eq(1)
    frame.loc[ties, ["ftm", "pts"]] += 1
    frame["min"] = np.where(frame.week.mod(7).eq(0), 265, 240)
    result = aggregate_team_games(frame, include_box_counts=True).assign(season_type="regular")
    for column in ("event_date", "available_at"):
        result[column] = pd.to_datetime(result[column], utc=True)
    return result


def config():
    return NBAFourFactorsConfig(minimum_training_games=8, minimum_team_games=3,
                                minimum_calibration_games=8, maximum_calibration_games=20, simulations=3000)


class FourFactorsChecks(TestCase):
    def test_document_example_and_exact_box_score_identity(self):
        self.assertAlmostEqual(99.5 * scoring_rate(.560, .125, .140, .270, .790), 116.51, places=2)
        self.assertAlmostEqual(99.5 * scoring_rate(.535, .140, .118, .240, .780), 107.14, places=2)
        frame = history()
        self.assertTrue(FACTOR_INPUTS <= set(frame))
        factors, exposure, possessions = observed_factors(frame[list(COUNTS)].to_numpy(float))
        efg, turnover, share, ft_rate, ft_pct, opportunities = factors.T
        np.testing.assert_allclose(scoring_rate(efg, turnover, share*opportunities, ft_rate, ft_pct),
                                   frame.points.to_numpy()/possessions)
        np.testing.assert_allclose(from_latent(to_latent(factors)), factors)
        np.testing.assert_array_equal(exposure[:, 2], frame.oreb + frame.opponent_dreb)

    def test_sparse_factor_fit_matches_weighted_dense_reference(self):
        frame = history()
        model = NBAFourFactorsModel(config())
        history_rows = model._history(frame, frame.event_date.max()+pd.Timedelta(days=2))
        home = history_rows.loc[history_rows.is_home.eq(1)]
        teams = pd.Index(sorted(history_rows.team.unique()))
        hi, ai = teams.get_indexer(home.team), teams.get_indexer(home.opponent)
        weight = np.linspace(.15, 1., len(home))
        actual = fit_factor_effects(home, len(teams), hi, ai, weight, model.config.efficiency_ridge)
        n = len(teams)
        design = np.zeros((2*len(home), 2+2*n))
        rows = np.arange(len(home))
        design[:, 0] = 1
        design[rows, 1], design[rows+len(home), 1] = 1, -1
        design[rows, 2+hi] = design[rows+len(home), 2+ai] = 1
        design[rows, 2+n+ai] = design[rows+len(home), 2+n+hi] = 1
        values = np.vstack([home[list(LATENT_COLUMNS)], home[list(AWAY_LATENT)]])
        exposure = np.vstack([home[list(EXPOSURE_COLUMNS)], home[list(AWAY_EXPOSURE)]])
        weights = exposure / exposure.mean(axis=0) * np.tile(weight, 2)[:, None]
        penalty = np.eye(design.shape[1])*model.config.efficiency_ridge
        penalty[0, 0] = 0
        for k, column in enumerate(values.T):
            weighted = design*weights[:, k, None]
            expected = np.linalg.solve(design.T@weighted+penalty, weighted.T@column)
            np.testing.assert_allclose(actual[:, k], expected, atol=1e-12)

    def test_cutoff_joint_draws_and_replay_reports(self):
        frame = history()
        model = NBAFourFactorsModel(config())
        cutoff = frame.event_date.sort_values().unique()[-2]
        delayed = frame.game_id.eq("10-0")
        modified = frame.copy()
        modified.loc[delayed, "available_at"] = cutoff
        modified.loc[delayed | modified.event_date.ge(cutoff), "fgm"] = 100
        forecast = model.predict(frame.loc[~delayed], "A", "B", cutoff)
        self.assertEqual(asdict(forecast), asdict(model.predict(modified, "A", "B", cutoff)))
        scores, _, _ = simulate(forecast.diagnostics["simulation"])
        home, away = scores.T
        self.assertFalse(np.any(home == away))
        self.assertEqual(forecast.total(221).push, np.mean(home+away == 221))
        self.assertEqual(forecast.total(221.5).push, 0)
        self.assertEqual(forecast.team_total("A", 109.5).win, np.mean(home > 109.5))
        self.assertEqual(forecast.spread("A", -3.5).win, np.mean(home-away > 3.5))
        fit = forecast.diagnostics["matchup_factors"]["A"]
        self.assertAlmostEqual(fit["orb_per_fga"], fit["offensive_rebound_share"]*fit["rebound_opportunities_per_fga"])
        before = forecast.spread("A", 0).win
        forecast.apply_score_adjustment(np.array([6., 0.]), np.eye(2))
        self.assertGreater(forecast.spread("A", 0).win, before)
        with TemporaryDirectory() as folder:
            service = BacktestingService(root=folder, now=lambda: pd.Timestamp("2026-01-01", tz="UTC"))
            service.game_inputs = lambda *_: (frame, None)
            report = service.run("NBA", "game-outcome", "ff", entity="A", limit=2, config_overrides=asdict(config()))
            self.assertEqual(report["skipped"], [])
            self.assertEqual(len(report["records"]), 2)
            self.assertEqual(service.latest("NBA", "game-outcome", "ff")["run_id"], report["run_id"])
            for row in evaluated_records(report, "total"):
                self.assertEqual([row["lower"], row["upper"]], row["total_interval"])

    def test_tied_regulation_scenarios_receive_overtime(self):
        latent = to_latent(np.array([[.56, .125, .28, .27, .79, .5]]*2)).tolist()
        parameters = dict(pace=99.5, matchup_latent=latent, paired_errors=[[0.]*15, [1.]+[0.]*14],
                          rate_shift=[0., 0.], availability_covariance=[[0., 0.], [0., 0.]], draws=2000, seed=2026)
        scores, ties, _ = simulate(parameters)
        self.assertEqual(ties, parameters["draws"])
        self.assertFalse(np.any(scores[:, 0] == scores[:, 1]))
        self.assertTrue(np.all(scores.sum(axis=1) >= 232))

    def test_nba_market_filters_and_other_sport_rejection(self):
        current = scope("market", "NBA", "123")
        prompt = prompt_key(dict(market_key="alternate_totals", line=220.5))
        self.assertEqual(selected_methods(["FF"], ["WHP"], "NBA"), (["FF"], ["WHP"]))
        self.assertEqual(choices([["FF"]], [dict(scope=current, prompt=prompt)], current), {prompt:["FF"]})
        for sport in ("NFL", "NCAAF"):
            with self.assertRaises(ValueError):
                selected_methods(["FF"], ["WHP"], sport)
        frame = history().replace({"A": "BOS", "B": "NYK"})
        result = NBAFourFactorsModel(config()).predict(frame, "BOS", "NYK", frame.event_date.max()+pd.Timedelta(days=2))
        market_fixtures.PaceChecks.check_market_prices(self, "FF", result, "nba_four_factors_service")


if __name__ == "__main__":
    main()
