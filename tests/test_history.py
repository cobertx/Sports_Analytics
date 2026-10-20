"""Tests for career coverage, source identities, game dates, and matchup labels."""

from io import StringIO
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from sports_analytics.career import Career, CareerService, nba_season, nfl_season, season_value
from sports_analytics.dashboard import career_figure, create_app, performance_figure
from sports_analytics.data import DataSourceError, Query, load_nba, load_nba_archive, load_nfl, nfl_team_scoring, normalize
from sports_analytics.forecast import rolling_baseline
from sports_analytics.service import DataService


def game_frame(kind="player"):
    query = Query("NBA", 2025, kind)
    return normalize(pd.DataFrame({
        "game_id": ["001", "002", "003", "004"], "entity_id": ["201939"] * 4,
        "entity_name": ["Stephen Curry"] * 4, "team": ["GSW"] * 4,
        "opponent": ["TOR", "BOS", "TOR", "LAL"], "event_date": pd.date_range("2025-11-01", periods=4),
        "matchup": ["GSW @ TOR", "BOS @ GSW", "TOR @ GSW", "GSW @ LAL"], "pts": [20, 30, 10, 25],
    }), query)


class GameLabelTests(unittest.TestCase):
    def test_stat_and_next_baseline_are_bars_while_prior_five_game_mean_stays_a_line(self):
        frame = game_frame()
        extra = frame.iloc[:2].copy()
        extra["game_id"] = ["005", "006"]
        extra["event_date"] = pd.to_datetime(["2025-11-05", "2025-11-06"], utc=True)
        extra["pts"] = [30, 35]
        frame = pd.concat([frame, extra], ignore_index=True)
        for sport, metric in [("NBA", "pts"), ("NFL", "pf"), ("NFL", "pa"), ("NFL", "passing_yards")]:
            selected = frame.copy()
            selected["sport"], selected[metric] = sport, selected["pts"]
            history, prediction, *_ = rolling_baseline(selected, metric, 5)
            figure = performance_figure(history, metric, 5, prediction)
            with self.subTest(sport=sport, metric=metric):
                self.assertEqual([trace.type for trace in figure.data], ["bar", "scatter", "bar"])
                self.assertEqual(figure.data[1].mode, "lines")
                self.assertEqual(figure.data[1].name, "Prior 5-game mean")
                self.assertEqual(figure.data[1].y[-1], 23)
                self.assertEqual(list(figure.data[2].y), [26])
                self.assertEqual(list(figure.data[2].x), [7])
                self.assertIn("Matchup TBD", figure.layout.xaxis.ticktext[-1])
        history, *_ = rolling_baseline(frame.iloc[:2], "pts", 5)
        self.assertEqual([trace.type for trace in performance_figure(history, "pts", 5, None).data], ["bar", "scatter"])

    def test_axis_and_hover_show_date_and_away_home_matchup(self):
        history, forecast, *_ = rolling_baseline(game_frame(), "pts", 3)
        figure = performance_figure(history, "pts", 3, forecast)
        self.assertEqual(figure.layout.xaxis.ticktext[0], "2025-11-01<br>GSW @ TOR")
        self.assertEqual(figure.layout.xaxis.ticktext[1], "2025-11-02<br>BOS @ GSW")
        self.assertEqual(tuple(figure.data[0].customdata[2]), ("2025-11-03", "TOR @ GSW"))
        self.assertIn("Matchup TBD", figure.layout.xaxis.ticktext[-1])
        self.assertNotIn("2025", figure.layout.xaxis.ticktext[-1])

    def test_missing_dates_are_not_invented(self):
        frame = game_frame()
        frame["event_date"] = pd.NaT
        frame["week"] = [1, 2, 3, 4]
        history, *_ = rolling_baseline(frame, "pts", 3)
        figure = performance_figure(history, "pts", 3, None)
        self.assertIn("2025 · Week 1", figure.layout.xaxis.ticktext[0])

    def test_nba_home_and_away_rows_use_same_matchup_order(self):
        client = Mock()
        client.json.return_value = {"resultSets": [{"name": "LeagueGameLog", "headers":
            ["GAME_ID", "GAME_DATE", "TEAM_ID", "TEAM_ABBREVIATION", "TEAM_NAME", "MATCHUP", "PTS"],
            "rowSet": [["001", "2025-11-01", 1, "GSW", "Warriors", "GSW @ TOR", 100],
                       ["001", "2025-11-01", 2, "TOR", "Raptors", "TOR vs. GSW", 95]]}]}
        frame = load_nba(Query("NBA", 2025), client)
        self.assertEqual(frame.matchup.tolist(), ["GSW @ TOR", "GSW @ TOR"])

    def test_nfl_dates_join_from_schedule_by_game_id(self):
        client = Mock()
        stats = "season,week,team,season_type,game_id,opponent_team,passing_yards\n2025,1,DAL,REG,2025_01_DAL_PHI,PHI,200\n"
        schedule = "game_id,gameday,away_team,home_team\n2025_01_DAL_PHI,2025-09-04,DAL,PHI\n"
        client.get.side_effect = [Mock(text=stats), Mock(text=schedule)]
        frame = load_nfl(Query("NFL", 2025), client)
        self.assertEqual(frame.iloc[0].matchup, "DAL @ PHI")
        self.assertEqual(frame.iloc[0].event_date.strftime("%Y-%m-%d"), "2025-09-04")

    def test_nfl_team_scoring_uses_final_opposing_scores_with_shutouts_aliases_and_postseason(self):
        stats = ("season,week,team,season_type,game_id,opponent_team,passing_yards\n"
                 "2025,1,DAL,REG,2025_01_DAL_WAS,WSH,200\n"
                 "2025,1,WSH,REG,2025_01_DAL_WAS,DAL,250\n"
                 "2025,19,DAL,POST,2025_19_DAL_PHI,PHI,300\n")
        schedule = ("game_id,gameday,away_team,home_team,away_score,home_score\n"
                    "2025_01_DAL_WAS,2025-09-04,DAL,WAS,0,27\n"
                    "2025_19_DAL_PHI,2026-01-11,DAL,PHI,21,24\n")
        client = Mock()
        client.get.side_effect = [Mock(text=stats), Mock(text=schedule)]
        frame = load_nfl(Query("NFL", 2025), client).set_index("team")
        self.assertEqual((frame.loc["DAL", "pf"], frame.loc["DAL", "pa"]), (0, 27))
        self.assertEqual((frame.loc["WSH", "pf"], frame.loc["WSH", "pa"]), (27, 0))
        client.get.side_effect = [Mock(text=stats), Mock(text=schedule)]
        playoffs = load_nfl(Query("NFL", 2025, "team", "postseason", week=19), client)
        self.assertEqual(len(playoffs), 1)
        self.assertEqual((playoffs.iloc[0].pf, playoffs.iloc[0].pa), (21, 24))

    def test_scoring_missing_invalid_or_ambiguous_results_are_never_zero_filled(self):
        frame = pd.DataFrame({"game_id": ["g"], "team": ["DAL"], "opponent": ["PHI"]})
        schedule = pd.DataFrame({"game_id": ["g"], "away_team": ["DAL"], "home_team": ["PHI"],
                                 "away_score": [20], "home_score": [20]})
        tied = nfl_team_scoring(frame, schedule)
        self.assertEqual((tied.iloc[0].pf, tied.iloc[0].pa), (20, 20))
        cases = [None, schedule.drop(columns="home_score"), pd.concat([schedule, schedule]),
                 schedule.assign(home_score=float("nan")), schedule.assign(away_score=-1),
                 schedule.assign(home_score=20.5), schedule.assign(home_team="NYG")]
        for index, source in enumerate(cases):
            with self.subTest(index=index):
                result = nfl_team_scoring(frame, source)
                self.assertTrue(result[["pf", "pa"]].isna().all().all())

    def test_nfl_players_do_not_receive_team_scoring_and_missing_schedule_keeps_other_stats(self):
        client = Mock()
        stats = ("season,week,team,season_type,game_id,opponent_team,passing_yards,player_id,player_display_name,position\n"
                 "2025,1,DAL,REG,2025_01_DAL_PHI,PHI,200,001,Test Player,QB\n")
        client.get.side_effect = [Mock(text=stats), DataSourceError("Schedule unavailable")]
        player = load_nfl(Query("NFL", 2025, "player"), client)
        self.assertNotIn("pf", player)
        self.assertNotIn("pa", player)
        client.get.side_effect = [Mock(text=stats), DataSourceError("Schedule unavailable")]
        team = load_nfl(Query("NFL", 2025), client)
        self.assertEqual(team.iloc[0].passing_yards, 200)
        self.assertTrue(team[["pf", "pa"]].isna().all().all())

    def test_nba_archive_home_player_still_shows_visitor_first(self):
        client = Mock()
        stats = ("team_id,team_city,team_name,team_tricode,person_id,first_name,family_name,minutes,points,game_id,season_type_id\n"
                 "2,Toronto,Raptors,TOR,001,Home,Player,25:00,20,22500001,2\n")
        schedule = "game_id,game_date,home_team_abbreviation,away_team_abbreviation\n22500001,2025-11-01,TOR,GSW\n"
        client.get.side_effect = [Mock(text=stats), Mock(content=gzip.compress(schedule.encode()))]
        frame = load_nba_archive(Query("NBA", 2025, "player"), client)
        self.assertEqual(frame.iloc[0].matchup, "GSW @ TOR")
        self.assertEqual(frame.iloc[0].team, "TOR")


class CareerTests(unittest.TestCase):
    def test_nfl_season_summaries_preserve_player_id_and_rate_units(self):
        client = Mock()
        client.get.return_value.text = ("player_id,recent_team,games,passing_yards,fg_pct\n"
                                        "00-001,KC,17,5000,85.5\n,KC,1,0,0\n")
        frame = nfl_season(2025, "postseason", client)
        self.assertEqual(frame.iloc[0].entity_id, "00-001")
        self.assertEqual(frame.iloc[0].passing_yards, 5000)
        self.assertEqual(frame.iloc[0].fg_pct, 85.5)
        self.assertEqual(len(frame), 1)
        self.assertIn("stats_player_post_2025.csv", client.get.call_args.args[0])

    def test_nba_summaries_choose_base_totals_and_combined_team_row(self):
        csv = ("player_id,team_id,team_abbreviation,season_type,measure_type,per_mode,gp,pts\n"
               "001,1,GSW,regular-season,base,pergame,10,20\n"
               "001,1,GSW,regular-season,base,totals,10,200\n"
               "001,0,TOT,regular-season,base,totals,20,450\n"
               "001,1,GSW,playoffs,base,totals,5,100\n"
               "001,1,GSW,regular-season,advanced,totals,10,999\n")
        client = Mock()
        client.get.return_value.text = csv
        frame = nba_season(2025, "regular", client)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0].entity_id, "001")
        self.assertEqual(frame.iloc[0].pts, 450)
        self.assertEqual(frame.iloc[0].team, "TOT")
        self.assertIn("2026.csv", client.get.call_args.args[0])

    def test_missing_season_stays_a_gap_in_career_line(self):
        career = Career(pd.DataFrame({"season": [2010, 2012], "value": [20, 40], "team": ["GSW", "GSW"]}),
                        [2010, 2011, 2012], [2011], "regular", "NBA Stats")
        figure = career_figure(career, "NBA", "pts", "Player")
        self.assertEqual(list(figure.data[0].y), [20, None, 40])
        self.assertFalse(figure.data[0].connectgaps)

    def test_rates_are_not_added_across_transfers(self):
        rows = pd.DataFrame({"fg_pct": [50.0, 100.0], "fg_att": [3, 1]})
        self.assertEqual(season_value(rows, "fg_pct"), 62.5)
        self.assertEqual(season_value(rows, "fg_att"), 4)

    def test_career_load_checks_all_available_years_and_matches_id_across_teams(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CareerService(Path(tmp) / "career.sqlite3")
            service.available_seasons = Mock(return_value=[2009, 2010, 2011])
            frames = {
                2009: pd.DataFrame({"entity_id": ["001", "002"], "team": ["GSW", "TOR"], "pts": [100, 999]}),
                2010: pd.DataFrame({"entity_id": ["001"], "team": ["BOS"], "pts": [150]}),
            }
            def season(sport, year, phase):
                if year == 2011:
                    raise DataSourceError("Unavailable")
                return frames[year]
            service.season = Mock(side_effect=season)
            result = service.load("NBA", "001", "Same name", "pts", "regular")
            self.assertEqual(result.frame.value.tolist(), [100, 150])
            self.assertEqual(result.frame.team.tolist(), ["GSW", "BOS"])
            self.assertEqual(result.failed, [2011])
            self.assertEqual(service.season.call_count, 3)

    def test_summary_cache_is_shared_between_players_and_persists_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "career.sqlite3"
            service = CareerService(path)
            download = Mock(return_value=pd.DataFrame({"entity_id": pd.Series(["001"], dtype="string"), "pts": [10]}))
            service._cached_table("season", download)
            service._cached_table("season", download)
            read = CareerService(path)._cached_table("season", download)
            self.assertEqual(read.iloc[0].entity_id, "001")
            download.assert_called_once()


class DashboardHistoryTests(unittest.TestCase):
    def test_career_panel_only_in_player_mode_and_callback_uses_selected_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DataService(Path(tmp) / "stats.sqlite3")
            career = Mock()
            career.load.return_value = Career(pd.DataFrame({"season": [2009, 2025], "value": [100, 200], "team": ["GSW", "GSW"]}),
                                              list(range(1996, 2026)), [], "regular", "NBA Stats")
            with patch("sports_analytics.service.fetch", return_value=game_frame()):
                loaded = service.load(Query("NBA", 2025, "player"))
            app = create_app(service, career)
            client = app.server.test_client()
            client.get("/_dash-layout").close()
            response = client.post("/_dash-update-component", json={
                "output": "career-panel.style", "outputs": {"id": "career-panel", "property": "style"},
                "inputs": [{"id": "entity-type", "property": "value", "value": "team"},
                           {"id": "sport", "property": "value", "value": "NBA"}],
                "changedPropIds": ["entity-type.value"], "state": []})
            self.assertEqual(response.json["response"]["career-panel"]["style"], {"display": "none"})
            key = next(k for k in app.callback_map if "career-trend.figure" in k)
            dataset = {"key": loaded.key, "sport": "NBA", "season": 2025, "kind": "player", "phase": "regular"}
            response = client.post("/_dash-update-component", json={
                "output": key, "outputs": [{"id": "career-trend", "property": "figure"}, {"id": "career-status", "property": "children"}],
                "inputs": [{"id": "dataset", "property": "data", "value": dataset},
                           {"id": "entity", "property": "value", "value": "201939"},
                           {"id": "metric", "property": "value", "value": "pts"}],
                "changedPropIds": ["entity.value"], "state": []})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            career.load.assert_called_once_with("NBA", "201939", "Stephen Curry", "pts", "regular")
            self.assertIn("2 recorded seasons", response.json["response"]["career-status"]["children"])

    def test_existing_cache_refreshes_to_add_matchups(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DataService(Path(tmp) / "stats.sqlite3")
            query = Query("NBA", 2025, "player")
            with patch("sports_analytics.service.fetch", side_effect=[game_frame().drop(columns="matchup"), game_frame()]) as fetch:
                service.load(query)
                upgraded = service.load(query)
                self.assertEqual(upgraded.frame.matchup.iloc[0], "GSW @ TOR")
                service.load(query)
                self.assertEqual(fetch.call_count, 2)

    def test_existing_nfl_team_cache_refreshes_once_for_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DataService(Path(tmp) / "stats.sqlite3")
            query = Query("NFL", 2025, "team")
            old = normalize(pd.DataFrame({"game_id": ["g"], "entity_id": ["DAL"], "entity_name": ["DAL"],
                                          "team": ["DAL"], "opponent": ["PHI"], "passing_yards": [200]}), query)
            upgraded = old.assign(pf=24., pa=20.)
            with patch("sports_analytics.service.fetch", side_effect=[old, upgraded]) as fetch:
                initial = service.load(query)
                result = service.load(query)
                self.assertEqual(result.key, initial.key)
                self.assertEqual((result.frame.iloc[0].pf, result.frame.iloc[0].pa), (24, 20))
                service.load(query)
                self.assertEqual(fetch.call_count, 2)
            with patch("sports_analytics.service.fetch", return_value=old.assign(pf=float("nan"), pa=float("nan"))) as fetch:
                service.load(query, refresh=True)
                service.load(query)
                fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
