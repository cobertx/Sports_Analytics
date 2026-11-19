"""Offline NBA PACE model, settlement, history and app wiring checks."""
from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_backtesting import team_history
from scripts.callback_fixtures import callback_functions
from models.Game_Outcome.Adjusted_Offensive_Defensive_Efficiency import NBAAdjustedEfficiencyForecast
from models.Game_Outcome.Pace_Possession import NBAPaceConfig, NBAPaceForecast, NBAPaceModel
from models.Game_Outcome.Pace_Possession.NBA_PACE import simulate
from sports_analytics.backtesting_service import BacktestingService, evaluated_records
from sports_analytics.backtesting_view import find_model
from sports_analytics.comparison import selected_methods as comparison_methods
from sports_analytics.dashboard import create_app
from sports_analytics.kalshi_analysis import quick_calculate
from sports_analytics.mispricing import rank_markets, selected_methods
from sports_analytics.nba_model_data import aggregate_team_games, with_player_minutes
from sports_analytics.quick_methods import choices, game_methods, game_options, prompt_key, scope
from sports_analytics.summary_filtering import calculate_filter


def history():
    frame = team_history()
    frame["elapsed_minutes"] = np.where(frame.week.mod(7).eq(0), 53, 48)
    frame["possessions"] = (96 + frame.week.mod(7)) * frame.elapsed_minutes / 48
    frame[["points", "points_allowed"]] += 82
    frame["season_type"] = "regular"
    return frame


def config():
    return NBAPaceConfig(minimum_training_games=8, minimum_team_games=3,
                         minimum_calibration_games=7, maximum_calibration_games=16, simulations=2000)


class PaceChecks(TestCase):
    def test_one_joint_simulation_prices_pushes_team_totals_and_overtime(self):
        parameters = [99., 4., [1.17, 1.10], [0., 0.], [[100., 10.], [10., 110.]], 10000, 2026]
        scores, ties = simulate(parameters[0], parameters[1], np.array(parameters[2]), np.array(parameters[3]), *parameters[4:])
        home, away = scores.T
        self.assertGreater(ties, 0)
        self.assertFalse(np.any(home == away))
        forecast = NBAPaceForecast.from_parameters("BOS", "NYK", parameters, {})
        self.assertEqual(forecast.spread("BOS", 0).win, float(np.mean(home > away)))
        self.assertEqual(forecast.spread("NYK", 4).push, float(np.mean(away + 4 == home)))
        self.assertEqual(forecast.total(225.5).push, 0)
        self.assertEqual(forecast.total(225, "Under").win, float(np.mean(home + away < 225)))
        self.assertEqual(forecast.team_total("NYK", 109).push, float(np.mean(away == 109)))
        self.assertEqual(NBAPaceForecast(**asdict(forecast)).spread("BOS", -4), forecast.spread("BOS", -4))
        forecast.apply_score_adjustment(np.array([6., 0.]), np.eye(2))
        self.assertGreater(forecast.spread("BOS", 0).win, float(np.mean(home > away)))
        self.assertEqual(forecast.spread("BOS", 0).push, 0)

    def test_model_ignores_later_results_and_replay_uses_empirical_intervals(self):
        frame = history()
        cutoff = frame.event_date.sort_values().unique()[-2]
        modified = frame.copy()
        ineligible = modified.event_date.ge(cutoff)
        delayed = modified.game_id.eq("10-0")
        modified.loc[delayed, "available_at"] = cutoff
        modified.loc[ineligible | delayed, ["points", "points_allowed"]] += 40
        model = NBAPaceModel(config())
        expected = frame[~delayed]
        self.assertEqual(asdict(model.predict(expected, "A", "B", cutoff)),
                         asdict(model.predict(modified, "A", "B", cutoff)))
        with TemporaryDirectory() as directory:
            service = BacktestingService(root=directory, now=lambda: pd.Timestamp("2026-01-01", tz="UTC"))
            service.game_inputs = lambda *_: (frame, None)
            report = service.run("NBA", "game-outcome", "pace", entity="A", limit=2, config_overrides=asdict(config()))
            self.assertEqual(len(report["records"]), 2)
            self.assertEqual(report["skipped"], [])
            for row in evaluated_records(report, "total"):
                self.assertEqual([row["lower"], row["upper"]], row["total_interval"])
            self.assertEqual(service.latest("NBA", "game-outcome", "pace")["run_id"], report["run_id"])

    def test_espn_minutes_require_both_boxes_and_preserve_publication_time(self):
        stamp = pd.Timestamp("2026-11-01T00:00:00Z")
        teams, players = [], []
        for club, opponent, score in [("BOS", "NYK", 120), ("NYK", "BOS", 115)]:
            teams.append(dict(game_id="espn:1", team=club, opponent=opponent, matchup="NYK @ BOS",
                              event_date=stamp, available_at=stamp+pd.Timedelta(hours=5),
                              pts=score, fgm=35, ftm=15, fga=88, fta=22, oreb=9, tov=15))
            for number in range(6):
                players.append(dict(game_id="espn:1", team=club, min=45 if number < 5 else 40,
                                    pts=score if number == 0 else 0, fgm=35 if number == 0 else 0,
                                    ftm=15 if number == 0 else 0, available_at=stamp+pd.Timedelta(hours=6)))
        teams, players = pd.DataFrame(teams), pd.DataFrame(players)
        paired = aggregate_team_games(with_player_minutes(teams, players))
        self.assertEqual(paired.elapsed_minutes.tolist(), [53, 53])
        self.assertEqual(paired.available_at.unique().tolist(), [(stamp+pd.Timedelta(hours=6)).isoformat()])
        partial = aggregate_team_games(with_player_minutes(teams, players.iloc[:-1]))
        self.assertTrue(partial.elapsed_minutes.isna().all())

    def test_nba_only_forecast_and_filter_dropdowns(self):
        self.assertEqual(selected_methods(["PACE"], ["WHP"], "NBA"), (["PACE"], ["WHP"]))
        self.assertEqual(comparison_methods("game-outcome", ["pace"], "NBA"), ["pace"])
        self.assertIsNotNone(find_model("NBA", ("game-outcome", "pace")))
        key = prompt_key(dict(market_key="alternate_totals", line=220.5))
        for sport in ("NBA", "NFL", "NCAAF"):
            current = scope("market", sport, "123")
            if sport == "NBA":
                self.assertEqual(choices([["PACE"]], [dict(scope=current, prompt=key)], current), {key: ["PACE"]})
            else:
                with self.assertRaises(ValueError):
                    choices([["PACE"]], [dict(scope=current, prompt=key)], current)
                self.assertIsNone(find_model(sport, ("game-outcome", "pace")))
                with self.assertRaises(ValueError):
                    selected_methods(["PACE"], ["WHP"], sport)
        with TemporaryDirectory() as directory:
            app = create_app(service=Mock(path=Path(directory)/"cache.sqlite3"))
            with app.server.test_client() as client:
                self.assertEqual(client.get("/_dash-layout").status_code, 200)
                with client.get("/backtesting/docs/game-outcome/pace") as response:
                    self.assertEqual(response.status_code, 200)
            from check_navigation_pipeline import node, run_client
            if node():
                refs = [dict(output=prefix+"-quick-game-methods.options",
                             args=[sport, ["PPP", "EPA", "ELO", "PACE"]],
                             expected=[game_options(sport), list(game_methods(sport))])
                        for prefix in ("market", "kalshi", "summary") for sport in ("NBA", "NFL", "NCAAF")]
                self.assertEqual(run_client(app, references=refs, steps=[])["checks"], len(refs))

    def test_pace_prices_sportsbook_and_exchange_quick_filters(self):
        forecast = NBAPaceForecast.from_parameters(
            "BOS", "NYK", [99., 4., [1.17, 1.10], [0., 0.], [[100., 10.], [10., 110.]], 3000, 2026], {})
        self.check_market_prices("PACE", forecast, "nba_pace_service")

    def test_aode_prices_sportsbook_and_exchange_quick_filters(self):
        forecast = NBAAdjustedEfficiencyForecast.from_parameters(
            "BOS", "NYK", [99., 4., [1.17, 1.10], [0., 0.], [[.01, .001], [.001, .011]], 3000, 2026], {})
        self.check_market_prices("AODE", forecast, "nba_aode_service")

    def check_market_prices(self, method, forecast, service_arg):
        from check_market_summary import STAMP, START, binary

        home, away = "Boston Celtics", "New York Knicks"
        game_engine = Mock(return_value=(forecast, None))
        player_engine = Mock(return_value=(None, None, {}))
        criteria = dict(probability=0, edge=-1.)
        quote = dict(group="Game", market="Game Total", market_key="alternate_totals",
                     subject="Over", selection="Over", line=220.5, odds=-110)
        snapshot = dict(sport="NBA", game_id="123", pulled_at=STAMP, commence_time=START,
                        home_team=home, away_team=away, rows=[quote])
        sports_ranked = rank_markets(snapshot, forecast, None, criteria, method)
        self.assertEqual(sports_ranked["matches"][0]["probability"], forecast.total(220.5).probability)

        event_id = "KXNBAGAME-26OCT25NYKBOS"
        event = dict(sport="NBA", id=event_id, start=START, title=f"{away} at {home}",
                     events=[event_id], categories=["Game Lines"])
        exchange_quote = dict(binary(), event_ticker="KXNBATOTAL-TEST",
                              title="Over 220.5 points", line=220)
        exchange = dict(sport="NBA", game_id=event_id, fetched_at=STAMP, metadata_fetched_at=STAMP,
                        rows=[exchange_quote], notices=[])
        ranked, errors = quick_calculate(event, exchange, "live", [method], ["WHP"], criteria, game_engine, player_engine)
        self.assertEqual(errors, [])
        self.assertTrue(ranked["matches"])
        game_engine.assert_called_once()
        self.assertEqual(game_engine.call_args.args[1], method)

        game = dict(id="123", start=START, home=dict(name=home, abbreviation="BOS"),
                    away=dict(name=away, abbreviation="NYK"))
        polymarket = dict(exchange, game_id="100",
                          rows=[dict(binary(provider="Polymarket"), title="Over 220.5 points", line=220.5)])
        bundle = dict(id="pace-test", sport="NBA", game=game, providers={
            "FanDuel": dict(game=game, snapshot=snapshot),
            "DraftKings": dict(game=game, snapshot=dict(snapshot, bookmaker="draftkings")),
            "Kalshi": dict(game=event, snapshot=exchange),
            "Polymarket": dict(game=dict(event, id="100"), snapshot=polymarket)})
        ranked = calculate_filter(bundle, [method], ["WHP"], criteria, game_engine, player_engine)
        self.assertEqual({row["provider"] for row in ranked["matches"]}, set(bundle["providers"]))
        self.assertTrue(all(row["method"] == method for row in ranked["matches"]))

        with TemporaryDirectory() as directory:
            game_service = Mock()
            game_service.forecast.return_value = forecast
            availability = Mock()
            availability.revision.return_value = []
            availability.apply.side_effect = lambda snapshot, fitted, method: fitted
            app = create_app(service=Mock(path=Path(directory)/"cache.sqlite3"),
                             availability_service=availability, **{service_arg: game_service})
            callbacks = callback_functions(app)
            models, _ = callbacks["summary_models"](bundle, method, "WHP", 1, "market-summary", "NBA", "123", None)
            game_service.forecast.assert_called_once()
            self.assertEqual(models["method"], method)
            self.assertTrue(all(row["probability"] is not None for row in models["rows"]))


if __name__ == "__main__":
    main()
