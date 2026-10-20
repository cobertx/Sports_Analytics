"""PPP formula verification, scoring attribution, leakage checks and saved UI."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import NormalDist
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd
from dash import html

from Models.Game_Outcome.PPP.PPP import PPPConfig, PPPError, PPPForecast, PPPModel, fair_american_odds, implied_probability, matchup_ppp
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.market_view import _comparison_figure, criteria_match, market_sections, ppp_comparison
from sports_analytics.nfl_model_data import NflModelData, aggregate_team_games
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.service import DataService


def forecast():
    return PPPForecast("SF", "WSH", 32.736, 20.862, 13, 13.5,
                       {"as_of": "2026-10-19T23:35:23+00:00", "training_games": 100, "validation_games": 40})


def snapshot():
    rows = [{"group": "Game", "market_key": "alternate_spreads", "market": "Alternate spreads",
             "subject": team, "selection": team, "line": line, "odds": odds, "updated": "Not reported"}
            for team, line, odds in [("San Francisco 49ers", -3.5, -110), ("Washington Commanders", 3.5, -110)]]
    rows.extend({"group": "Game", "market_key": "alternate_totals", "market": "Alternate points total",
                 "subject": side, "selection": side, "line": line, "odds": -115, "updated": "Not reported"}
                for side in ["Over", "Under"] for line in [45, 45.5, 51.5])
    return {"home_team": "San Francisco 49ers", "away_team": "Washington Commanders", "event_id": "a" * 32,
            "game_id": "123", "pulled_at": "2026-10-19T23:35:23+00:00", "commence_time": "2026-10-20T00:15:00Z",
            "rows": rows}


def observations(weeks=24):
    """Synthetic observations exclusively for deterministic tests, never app data."""
    rows = []
    rng = np.random.default_rng(42)
    teams = ["SF", "WSH", "CHI", "NE"]
    for week in range(weeks):
        date = pd.Timestamp("2024-09-01", tz="UTC") + pd.Timedelta(weeks=week)
        pairs = [(teams[week % 3 + 1], teams[0]), (teams[(week + 1) % 3 + 1], teams[(week + 2) % 3 + 1])]
        for i, (home, away) in enumerate(pairs):
            plays = rng.integers(50, 76, size=2)
            scores = rng.integers(10, 36, size=2)
            for side, (team, opponent) in enumerate([(home, away), (away, home)]):
                rows.append({"game_id": f"test_{week}_{i}", "season": 2024, "week": week + 1,
                    "team": team, "opponent": opponent, "event_date": date.isoformat(),
                    "available_at": (date + pd.Timedelta(days=1, hours=12)).isoformat(),
                    "is_home": int(side == 0), "neutral": 0, "offensive_points": int(scores[side]),
                    "offensive_points_allowed": int(scores[1-side]), "points": int(scores[side]) + 2,
                    "points_allowed": int(scores[1-side]) + 2, "non_offensive_points": 2,
                    "offensive_plays": int(plays[side]), "defensive_plays": int(plays[1-side]),
                    "offensive_drives": 11, "defensive_drives": 11})
    return pd.DataFrame(rows)


class PPPMathTests(unittest.TestCase):
    def test_pdf_sections_two_to_four_worked_example(self):
        self.assertAlmostEqual(matchup_ppp(.48, .44, .4), .528)
        self.assertAlmostEqual(matchup_ppp(.36, .38, .4), .342)
        model = forecast()
        self.assertAlmostEqual(62 * .528, model.home_mean)
        self.assertAlmostEqual(61 * .342, model.away_mean)
        self.assertAlmostEqual(model.total_mean, 53.598)
        self.assertAlmostEqual(model.margin_mean, 11.874)
        self.assertAlmostEqual(model.normal_over(51.5), .5617, places=4)
        self.assertAlmostEqual(NormalDist().cdf(model.margin_mean / model.margin_sd), .8195, places=4)

    def test_odds_conversion_and_round_trip(self):
        self.assertAlmostEqual(implied_probability(-110), 110 / 210)
        self.assertAlmostEqual(implied_probability(150), .4)
        self.assertEqual(fair_american_odds(.5), 100)
        for probability in [.01, .4, .5, .56, .99]:
            self.assertAlmostEqual(implied_probability(fair_american_odds(probability)), probability)
        self.assertIsNone(fair_american_odds(0))
        self.assertIsNone(fair_american_odds(1))
        for invalid in [0, 99, -99, float("nan"), float("inf")]:
            with self.assertRaises(PPPError):
                implied_probability(invalid)

    def test_opposite_spreads_and_totals_preserve_push_probability(self):
        model = forecast()
        for line in [3, 3.5, 0, -3]:
            home, away = model.spread("SF", line), model.spread("WSH", -line)
            self.assertAlmostEqual(home.win + home.loss + home.push, 1)
            self.assertAlmostEqual(home.win, away.loss)
            self.assertAlmostEqual(home.push, away.push)
            self.assertAlmostEqual(home.probability + away.probability, 1)
        for line in [0, 45, 45.5, 150.5]:
            over, under = model.total(line), model.total(line, "Under")
            self.assertAlmostEqual(over.win + under.win + over.push, 1)
            self.assertAlmostEqual(over.probability + under.probability, 1)
            self.assertEqual(over.push > 0, float(line).is_integer())
        self.assertGreater(model.total(45).push, 0)
        self.assertAlmostEqual(model.total(45.5).push, 0)

    def test_line_probabilities_monotonic_and_out_of_range_inputs_fail(self):
        model = forecast()
        totals = [model.total(line).probability for line in np.arange(10.5, 100, 1)]
        spreads = [model.spread("SF", line).probability for line in np.arange(-30.5, 31, 1)]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertEqual(spreads, sorted(spreads))
        with self.assertRaises(PPPError):
            model.spread("UNKNOWN", 3)
        with self.assertRaises(PPPError):
            model.total(math.inf)
        with self.assertRaises(PPPError):
            PPPForecast("SF", "WSH", 20, 20, 0, 0)

    def test_walk_forward_residual_variance_is_used_and_future_results_cannot_leak(self):
        config = PPPConfig(minimum_training_games=8, minimum_team_games=3, minimum_calibration_games=10)
        model = PPPModel(config)
        frame = observations()
        first = model.predict(frame, "SF", "WSH", "2025-03-01T12:00:00Z")
        errors = first.diagnostics["validation_residuals"]
        margins = [row["home_error"] - row["away_error"] for row in errors]
        totals = [row["home_error"] + row["away_error"] for row in errors]
        self.assertAlmostEqual(first.margin_sd, np.std(margins, ddof=1))
        self.assertAlmostEqual(first.total_sd, np.std(totals, ddof=1))
        future = frame.copy()
        future["game_id"] += "_future"
        future["event_date"] = "2025-04-01T00:00:00Z"
        future["available_at"] = "2025-04-02T12:00:00Z"
        future["points"] = 999
        second = model.predict(pd.concat([frame, future]), "SF", "WSH", "2025-03-01T12:00:00Z")
        self.assertEqual(asdict(first), asdict(second))
        self.assertGreater(first.diagnostics["validation_games"], 10)

    def test_missing_team_or_insufficient_calibration_has_no_estimated_fallback(self):
        frame = observations(4)
        with self.assertRaises(PPPError):
            PPPModel().predict(frame, "SF", "WSH", "2025-03-01T12:00:00Z")


class ModelingDataTests(unittest.TestCase):
    def inputs(self):
        def row(play_id, team, kind="pass", **kwargs):
            return {"game_id": "test", "play_id": play_id, "posteam": team, "defteam": "WSH" if team == "SF" else "SF",
                    "play_type": kind, "drive": 1 if team == "SF" else 2, "touchdown": 0, "td_team": None,
                    "extra_point_result": None, "two_point_conv_result": None, "two_point_attempt": 0,
                    "field_goal_result": None, **kwargs}
        pbp = pd.DataFrame([
            row(1, "SF", touchdown=1, td_team="SF"), row(2, "SF", "extra_point", extra_point_result="good"),
            row(3, "WSH", "run"), row(4, "WSH", touchdown=1, td_team="SF"),
            row(5, "SF", "extra_point", extra_point_result="good"),
            row(6, "WSH", "run", touchdown=1, td_team="WSH"),
            row(7, "WSH", two_point_attempt=1, two_point_conv_result="success"),
            row(8, "SF", "field_goal", field_goal_result="made"), row(9, "SF", "qb_kneel"),
            row(10, "SF", "no_play", touchdown=1, td_team="SF"), row(11, "SF")])
        pbp["total_home_score"], pbp["total_away_score"] = 17, 10
        games = pd.DataFrame([{"game_id": "test", "season": 2025, "week": 1, "game_type": "REG", "gameday": "2025-09-01",
                               "home_team": "SF", "away_team": "WAS", "home_score": 17, "away_score": 10}])
        box = pd.DataFrame([{"game_id": "test", "team": team, "attempts": 2} for team in ["SF", "WAS"]])
        snaps = pd.DataFrame([{"game_id": "test", "team": team, "offense_snaps": 3, "defense_snaps": 3}
                              for team in ["SF", "WAS"] for _ in range(3)])
        return pbp, games, box, snaps

    def test_play_counts_scoring_and_joined_sources_are_model_ready(self):
        frame = aggregate_team_games(*self.inputs()).set_index("team")
        self.assertEqual(frame.loc["SF", "offensive_plays"], 3)
        self.assertEqual(frame.loc["WSH", "offensive_plays"], 3)
        self.assertEqual(frame.loc["SF", "defensive_plays"], frame.loc["WSH", "offensive_plays"])
        self.assertEqual(frame.loc["SF", "offensive_points"], 10)
        self.assertEqual(frame.loc["SF", "non_offensive_points"], 7)
        self.assertEqual(frame.loc["WSH", "offensive_points"], 8)
        self.assertEqual(frame.loc["WSH", "non_offensive_points"], 2)
        self.assertEqual(frame.loc["SF", "box_attempts"], 2)
        self.assertEqual(frame.loc["SF", "max_player_offense_snaps"], 3)
        self.assertTrue(frame["snap_coverage"].all())
        self.assertTrue(frame["box_coverage"].eq("both").all())

    def test_duplicate_and_inconsistent_scoring_are_rejected(self):
        pbp, games, box, snaps = self.inputs()
        with self.assertRaises(DataSourceError):
            aggregate_team_games(pd.concat([pbp, pbp.iloc[:1]]), games, box, snaps)
        games["home_score"] = 1
        with self.assertRaises(DataSourceError):
            aggregate_team_games(pbp, games, box, snaps)

    def test_incomplete_play_by_play_cannot_use_completed_game_final_score(self):
        pbp, games, box, snaps = self.inputs()
        pbp.loc[pbp.index[-1], "total_home_score"] = 16
        with self.assertRaisesRegex(DataSourceError, "final score"):
            aggregate_team_games(pbp, games, box, snaps)

    def test_failed_free_source_refresh_retains_cached_data_with_a_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Mock()
            client.get.side_effect = DataSourceError("Source unavailable")
            data = NflModelData(Path(temporary), client)
            raw = data.directory / "raw" / "games.csv"
            raw.parent.mkdir()
            raw.write_text("game_id,season\nexample,2025\n")
            raw.with_suffix(".source.json").write_text(json.dumps({
                "fetched_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}))
            self.assertEqual(data._source("games.csv", "https://example.invalid/games.csv"), raw)
            self.assertIn("source refresh failed", data._source_notices[0])
            self.assertIn("example", raw.read_text())


class PPPMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_freezes_snapshot_estimate_and_reuses_it_after_restart(self):
        data = Mock(directory=self.root)
        data.prepare.return_value = (pd.DataFrame(), [])
        model = Mock(config=PPPConfig())
        model.predict.return_value = forecast()
        service = PPPMarketService(data, model)
        saved = snapshot()
        first = service.forecast(saved)
        self.assertEqual(service.forecast(saved), first)
        model.predict.assert_called_once()
        self.assertEqual(model.predict.call_args.args[3], "2026-10-19T23:35:23+00:00")
        restarted = PPPMarketService(data, model)
        self.assertEqual(asdict(restarted.forecast(saved)), asdict(first))
        self.assertEqual(data.prepare.call_count, 1)
        self.assertEqual(model.predict.call_count, 1)

    def test_post_kickoff_quotes_still_use_pregame_cutoff(self):
        data = Mock(directory=self.root)
        data.prepare.return_value = (pd.DataFrame(), [])
        model = Mock(config=PPPConfig())
        model.predict.return_value = forecast()
        saved = {**snapshot(), "pulled_at": "2026-10-20T01:00:00Z"}
        result = PPPMarketService(data, model).forecast(saved)
        self.assertEqual(model.predict.call_args.args[3], "2026-10-20T00:15:00+00:00")
        self.assertTrue(result.diagnostics["snapshot_after_kickoff"])

    def test_all_saved_lines_have_probabilities_and_comparison_curves(self):
        saved = snapshot()
        rendered = market_sections(saved, "Game", forecast())
        text = repr(rendered)
        for expected in ["Market implied", "PPP probability", "PPP fair odds", "52.38%", "Push", "Spread probabilities", "Total points probabilities"]:
            self.assertIn(expected, text)
        self.assertEqual(text.count("className='market-outcome market-line-comparison'"), len(saved["rows"]))
        figure = _comparison_figure(saved, forecast(), "alternate_totals", "Over")
        self.assertEqual(len(figure.data), 2)
        self.assertEqual(list(figure.data[1].x), [45, 45.5, 51.5])
        self.assertEqual(list(figure.data[0].x), list(figure.data[1].x))
        self.assertEqual(list(figure.data[0].y), [forecast().total(line, "Over").probability for line in [45, 45.5, 51.5]])

    def test_four_charts_separate_team_and_total_outcomes_with_two_filled_lines(self):
        from dash import dcc
        view = ppp_comparison(snapshot(), forecast())
        figures = [node.figure for node in view._traverse() if isinstance(node, dcc.Graph)]
        self.assertEqual(len(figures), 4)
        self.assertEqual(list(figures[0].data[0].x), [-3.5])
        self.assertEqual(list(figures[1].data[0].x), [3.5])
        self.assertEqual(list(figures[2].data[0].y), [forecast().total(line, "Over").probability for line in [45, 45.5, 51.5]])
        self.assertEqual(list(figures[3].data[0].y), [forecast().total(line, "Under").probability for line in [45, 45.5, 51.5]])
        for figure in figures:
            self.assertEqual([trace.name for trace in figure.data], ["PPP probability", "Market implied probability"])
            self.assertNotEqual(figure.data[0].line.color, figure.data[1].line.color)
            for trace in figure.data:
                self.assertIn("lines", trace.mode)
                self.assertEqual(trace.fill, "tozeroy")
                self.assertIn("0.16", trace.fillcolor)
                self.assertFalse(trace.connectgaps)

    def test_spread_labels_name_the_team_and_missing_quotes_are_not_fabricated(self):
        saved = snapshot()
        saved.update(home_team="Baltimore Ravens", away_team="Cincinnati Bengals")
        rows = [{**row, "subject": "Baltimore Ravens", "selection": "Baltimore Ravens", "line": line, "odds": odds}
                for row, line, odds in [(saved["rows"][0], -3.5, -110), (saved["rows"][0], -4.5, None)]]
        saved["rows"] = rows
        result = PPPForecast("BAL", "CIN", 24, 20, 13, 13.5)
        figure = _comparison_figure(saved, result, "alternate_spreads", "Baltimore Ravens")
        self.assertEqual(list(figure.layout.xaxis.ticktext), ["Baltimore - 4.5", "Baltimore - 3.5"])
        self.assertEqual(list(figure.data[1].y), [None, implied_probability(-110)])
        self.assertIsNone(_comparison_figure(saved, result, "alternate_spreads", "Cincinnati Bengals"))
        unavailable = _comparison_figure(saved, None, "alternate_spreads", "Baltimore Ravens")
        self.assertEqual(len(unavailable.data), 1)
        self.assertEqual(unavailable.data[0].name, "Market implied probability")

    def test_edge_is_on_right_and_is_a_signed_percentage_point_difference(self):
        saved = snapshot()
        sections = market_sections(saved, "Game", forecast())
        header = next(node for node in sections[0]._traverse()
                      if getattr(node, "className", "") == "market-comparison-heading market-line-comparison")
        self.assertEqual(header.children[-1].children, "Edge")
        rows = [node for node in sections[0]._traverse()
                if getattr(node, "className", "") == "market-outcome market-line-comparison"]
        home = next(node for node in rows if "San Francisco" in node.children[0].children)
        expected = forecast().spread("SF", -3.5).probability - 110 / 210
        self.assertEqual(home.children[-1].children, f"{100 * expected:+.2f} pp")
        away = next(node for node in rows if "Washington" in node.children[0].children)
        self.assertTrue(away.children[-1].children.startswith("-"))

    def test_highlighting_requires_both_strict_thresholds(self):
        criteria = {"probability": .75, "edge": .05}
        self.assertTrue(criteria_match(.8, .06, criteria))
        for probability, edge in [(.7, .1), (.9, .01), (.75, .06), (.9, .05), (.8, .8 - .75), (None, .1), (.8, None)]:
            with self.subTest(probability=probability, edge=edge):
                self.assertFalse(criteria_match(probability, edge, criteria))
        self.assertFalse(criteria_match(.9, .2, None))

    def test_matching_rows_stay_collapsed_and_missing_estimates_never_highlight(self):
        saved = snapshot()
        sections = market_sections(saved, "Game", forecast(), criteria={"probability": .6, "edge": .05})
        # A qualifying spread and Over must both highlight on the same Apply.
        groups = [section for section in sections if isinstance(section, html.Details)]
        for section, expected in zip(groups, ["San Francisco", "Over"]):
            matches = [node for node in section._traverse()
                       if "market-criteria-match" in getattr(node, "className", "")]
            self.assertTrue(matches)
            self.assertTrue(all(expected in node.children[0].children for node in matches))
        # An Under at a high total must also qualify, alongside the spread.
        saved["rows"].append({**saved["rows"][-1], "line": 70.5})
        sections = market_sections(saved, "Game", forecast(), criteria={"probability": .6, "edge": .05})
        groups = [section for section in sections if isinstance(section, html.Details)]
        total_matches = [node.children[0].children for node in groups[1]._traverse()
                         if "market-criteria-match" in getattr(node, "className", "")]
        self.assertTrue(any("Over" in label for label in total_matches))
        self.assertTrue(any("Under" in label for label in total_matches))
        # Put all selections under one market so qualifying rows occur under Show more.
        from Models.Game_Outcome.PPP.PPP import MarketProbability
        certain = Mock()
        certain.total.return_value = MarketProbability(.9, .1, 0)
        certain.spread.return_value = MarketProbability(.9, .1, 0)
        # Skip the comparison charts; this checks the grouped row presentation.
        extended = {**saved, "rows": [{**saved["rows"][2], "group": "Test", "line": i + .5} for i in range(10)]}
        sections = market_sections(extended, "Test", certain, criteria={"probability": .75, "edge": .05})
        more = next(node for node in sections[0]._traverse() if getattr(node, "className", "") == "more-market-lines")
        self.assertFalse(more.open)
        next_game = market_sections({**extended, "game_id": "456"}, "Test", certain,
                                    criteria={"probability": .75, "edge": .05})[0]
        self.assertNotEqual(sections[0].key, next_game.key)
        self.assertEqual(sum("market-criteria-match" in getattr(node, "className", "") for node in sections[0]._traverse()), 10)
        unavailable = repr(market_sections(saved, "Game", criteria={"probability": 0, "edge": -1}))
        self.assertNotIn("market-criteria-match", unavailable)
        self.assertIn("52.38%", unavailable)

    def test_applied_criteria_highlight_each_team_over_and_under_in_the_charts(self):
        from dash import dcc
        saved = snapshot()
        saved["rows"].append({**saved["rows"][-1], "line": 70.5})
        criteria = {"probability": .6, "edge": .05}
        for method in ["PPP", "EPA", "ELO"]:
            sections = market_sections(saved, "Game", forecast(), criteria=criteria, method=method)
            figures = [node.figure for section in sections for node in section._traverse() if isinstance(node, dcc.Graph)]
            self.assertEqual(len(figures), 4)
            for figure, key, group in zip(figures,
                    ["alternate_spreads", "alternate_spreads", "alternate_totals", "alternate_totals"],
                    [saved["home_team"], saved["away_team"], "Over", "Under"]):
                rows = sorted([row for row in saved["rows"] if row["market_key"] == key
                               and (row["subject"] if key == "alternate_spreads" else row["selection"]) == group],
                              key=lambda row: row["line"])
                expected = []
                for row in rows:
                    p = (forecast().spread("SF" if group == saved["home_team"] else "WSH", row["line"])
                         if key == "alternate_spreads" else forecast().total(row["line"], group)).probability
                    expected.append(p if p > .6 and p-implied_probability(row["odds"]) > .05 else None)
                highlighted = [trace for trace in figure.data if trace.name == "Meets criteria"]
                if any(p is not None for p in expected):
                    self.assertEqual(len(highlighted), 1)
                    self.assertEqual(list(highlighted[0].y), expected)
                    self.assertFalse(highlighted[0].connectgaps)
                    self.assertEqual(highlighted[0].line.color, "#f6c667")
                    self.assertEqual(highlighted[0].fill, "tozeroy")
                else:
                    self.assertFalse(highlighted)

    def test_chart_highlights_require_applied_criteria_and_never_bridge_nonmatches_or_missing_quotes(self):
        from Models.Game_Outcome.PPP.PPP import MarketProbability
        saved = snapshot()
        base = saved["rows"][0]
        saved["rows"] = [{**base, "line": line, "odds": odds}
                         for line, odds in [(-5.5, -110), (-4.5, -10000), (-3.5, -110), (-2.5, None)]]
        result = Mock()
        result.spread.return_value = MarketProbability(.9, .1, 0)
        args = (saved, result, "alternate_spreads", saved["home_team"])
        self.assertEqual(len(_comparison_figure(*args).data), 2)
        highlighted = _comparison_figure(*args, criteria={"probability": .75, "edge": .05}).data[-1]
        self.assertEqual(highlighted.name, "Meets criteria")
        self.assertEqual(list(highlighted.y), [.9, None, .9, None])
        self.assertFalse(highlighted.connectgaps)
        self.assertEqual(len(_comparison_figure(*args, criteria={"probability": .9, "edge": .05}).data), 2)
        self.assertEqual(len(_comparison_figure(saved, None, "alternate_spreads", saved["home_team"],
                                             criteria={"probability": 0, "edge": -1}).data), 1)

    def test_no_match_marks_each_evaluated_section_red_and_preserves_matching_sections(self):
        from Models.Game_Outcome.PPP.PPP import MarketProbability
        saved = snapshot()
        result = Mock()
        result.spread.return_value = MarketProbability(.9, .1, 0)
        result.total.return_value = MarketProbability(.1, .9, 0)
        criteria = {"probability": .75, "edge": .05}
        # This test concerns the market sections, not the chart forecast object.
        saved["rows"] = [{**row, "group": "Test"} for row in saved["rows"]]
        sections = market_sections(saved, "Test", result, criteria=criteria)
        self.assertIn("market-criteria-group", sections[0].className)
        self.assertNotIn("market-criteria-no-match", sections[0].className)
        self.assertIn("market-criteria-no-match", sections[1].className)
        self.assertNotIn("market-criteria-group", sections[1].className)
        self.assertIn("no match", repr(sections[1].children[0]))
        for estimate in [result, None]:
            untouched = repr(market_sections(saved, "Test", estimate))
            self.assertNotIn("market-criteria-no-match", untouched)
        unavailable = repr(market_sections(saved, "Test", criteria=criteria))
        self.assertNotIn("market-criteria-no-match", unavailable)
        self.assertNotIn("no match", unavailable)
        no_quotes = {**saved, "rows": [{**row, "odds": None} for row in saved["rows"]]}
        self.assertNotIn("market-criteria-no-match", repr(market_sections(no_quotes, "Test", result, criteria=criteria)))
        empty = market_sections(snapshot(), "Game", forecast(), criteria={"probability": 1, "edge": 1})
        self.assertIn("criteria-evaluation-no-match", empty[0].className)
        self.assertEqual(sum("market-criteria-no-match" in getattr(section, "className", "") for section in empty), 2)

    def test_extra_selection_control_has_more_and_less_labels_and_keeps_the_first_six(self):
        saved = snapshot()
        saved["rows"] = [{**saved["rows"][0], "group": "Test", "line": i + .5} for i in range(10)]
        section = market_sections(saved, "Test")[0]
        first_six = next(node for node in section.children if getattr(node, "className", "") == "market-outcomes")
        self.assertEqual(len(first_six.children), 6)
        more = section.children[-1]
        self.assertEqual(more.className, "more-market-lines")
        self.assertFalse(more.open)
        labels = more.children[0].children
        self.assertEqual(labels[0].children, "Show more · 4 selections")
        self.assertEqual(labels[0].className, "market-show-more")
        self.assertEqual(labels[1].children, "Show less")
        self.assertEqual(labels[1].className, "market-show-less")
        self.assertEqual(len(more.children[1].children), 4)
        saved["rows"] = saved["rows"][:6]
        self.assertIsNone(market_sections(saved, "Test")[0].children[-1])

    def test_criteria_controls_default_to_75_and_5_and_only_apply_on_button_click(self):
        app = create_app(DataService(self.root / "data.sqlite3"), Mock(), Mock(), Mock(), Mock(), ppp_service=Mock())
        nodes = {node.id: node for node in app.layout._traverse() if hasattr(node, "id")}
        self.assertEqual(nodes["market-probability-threshold"].value, 75)
        self.assertEqual(nodes["market-edge-threshold"].value, 5)
        self.assertEqual(nodes["market-game-detail"].children[0].className, "panel market-criteria")
        self.assertEqual(nodes["market-game-detail"].children[1].className, "sportsbook-view")
        key = next(key for key in app.callback_map if "market-applied-criteria.data" in key)
        config = app.callback_map[key]
        self.assertEqual(config["inputs"], [{"id": "market-apply-criteria", "property": "n_clicks"}])
        render = app.callback_map[next(key for key in app.callback_map if "saved-market-sections.children" in key)]
        self.assertFalse(any(item["id"] in {"market-probability-threshold", "market-edge-threshold"} for item in render["inputs"]))
        client = app.server.test_client()
        def apply(probability, edge, clicks=1):
            return client.post("/_dash-update-component", json={"output": key,
                "outputs": [{"id": item.component_id, "property": item.component_property} for item in config["output"]],
                "inputs": [{"id": "market-apply-criteria", "property": "n_clicks", "value": clicks}],
                "state": [{"id": "market-probability-threshold", "property": "value", "value": probability},
                          {"id": "market-edge-threshold", "property": "value", "value": edge}],
                "changedPropIds": ["market-apply-criteria.n_clicks"]})
        self.assertEqual(apply(75, 5, 0).status_code, 204)
        response = apply(75, 5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response"]["market-applied-criteria"]["data"], {"probability": .75, "edge": .05})
        self.assertIn("all calculated markets (alternate spreads and alternate totals)",
                      response.json["response"]["market-criteria-status"]["children"])
        for probability, edge in [(None, 5), (101, 5), (-1, 5), (75, 101), (75, -101)]:
            response = apply(probability, edge)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("market-applied-criteria", response.json["response"])
            self.assertIn("Previous criteria remain applied", response.get_data(as_text=True))

    def test_saved_game_callback_uses_selected_snapshot_without_odds_requests(self):
        markets, ppp = Mock(), Mock()
        markets.history.return_value = [snapshot()]
        ppp.forecast.return_value = forecast()
        odds = Mock()
        app = create_app(DataService(self.root / "data.sqlite3"), Mock(), Mock(), odds, markets, ppp_service=ppp)
        key = next(key for key in app.callback_map if "saved-market-sections.children" in key)
        config = app.callback_map[key]
        values = {"market-category": "Game", "market-snapshot": snapshot()["pulled_at"], "market-url": "#nfl-game-123",
                  "market-applied-criteria": None, "market-forecast-method": "PPP"}
        body = {"output": key, "outputs": {"id": "saved-market-sections", "property": "children"},
                "inputs": [{**item, "value": values[item["id"]]} for item in config["inputs"]],
                "state": [], "changedPropIds": ["market-category.value"]}
        client = app.server.test_client()
        response = client.post("/_dash-update-component", json=body)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("PPP fair odds", response.get_data(as_text=True))
        ppp.forecast.assert_called_once_with(snapshot())
        odds.get.assert_not_called()
        markets.pull.assert_not_called()
        next(item for item in body["inputs"] if item["id"] == "market-applied-criteria")["value"] = {"probability": .6, "edge": .05}
        body["changedPropIds"] = ["market-applied-criteria.data"]
        response = client.post("/_dash-update-component", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIn("market-criteria-match", response.get_data(as_text=True))
        odds.get.assert_not_called()
        markets.pull.assert_not_called()
        ppp.forecast.side_effect = DataSourceError("Missing modeling data")
        response = client.post("/_dash-update-component", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Missing modeling data", response.get_data(as_text=True))
        self.assertIn("52.38%", response.get_data(as_text=True))
        odds.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
