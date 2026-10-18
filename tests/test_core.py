"""Offline contract and integration checks. No live API requests or credentials."""

from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError, HttpClient, Query, load_nba, load_ncaaf, load_nfl, normalize
from sports_analytics.forecast import rolling_baseline
from sports_analytics.service import DataService


def observations():
    return normalize(pd.DataFrame({
        "game_id": [f"00{n}" for n in range(1, 8)], "entity_id": ["001"] * 7,
        "entity_name": ["Test team"] * 7, "team": ["ABC"] * 7, "opponent": ["DEF"] * 7,
        "event_date": pd.date_range("2024-10-01", periods=7, tz="UTC"),
        "pts": [10, 20, 30, 40, 50, 60, 70],
    }), Query("NBA", 2024))


class ProviderTests(unittest.TestCase):
    def test_nba_player_and_team_identifiers(self):
        headers = ["GAME_ID", "GAME_DATE", "TEAM_ID", "TEAM_ABBREVIATION", "TEAM_NAME",
                   "PLAYER_ID", "PLAYER_NAME", "MATCHUP", "PTS", "REB"]
        client = Mock()
        client.json.return_value = {"resultSets": [{"name": "LeagueGameLog", "headers": headers,
            "rowSet": [["0022400001", "2024-10-22", 1, "BOS", "Boston Celtics", 2, "Player", "BOS vs. NYK", 30, 7]]}]}
        for kind, expected in (("team", "1"), ("player", "2")):
            frame = load_nba(Query("NBA", 2024, kind), client)
            self.assertEqual(frame.iloc[0].entity_id, expected)
            self.assertEqual(frame.iloc[0].game_id, "0022400001")
            self.assertEqual(frame.iloc[0].opponent, "NYK")
            self.assertEqual(frame.iloc[0].pts, 30)
            self.assertEqual(client.json.call_args.kwargs["params"]["Season"], "2024-25")

    def test_nfl_week_and_phase_filters_keep_ids(self):
        client = Mock()
        client.get.return_value.text = ("season,week,team,season_type,game_id,opponent_team,player_id,player_display_name,passing_yards\n"
                                       "2024,1,KC,REG,2024_01_BAL_KC,BAL,00-001,Quarterback,291\n"
                                       "2024,1,BAL,REG,2024_01_BAL_KC,KC,,,0\n"
                                       "2024,2,KC,REG,2024_02_CIN_KC,CIN,00-001,Quarterback,151\n"
                                       "2024,19,KC,POST,2024_19_HOU_KC,HOU,00-001,Quarterback,177\n")
        frame = load_nfl(Query("NFL", 2024, "player", week=1), client)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0].entity_id, "00-001")
        self.assertEqual(frame.iloc[0].passing_yards, 291)
        self.assertTrue(frame.event_date.isna().all())
        self.assertNotIn("player_id", frame.columns)
        post = load_nfl(Query("NFL", 2024, "team", "postseason"), client)
        self.assertEqual(post.iloc[0].entity_id, "KC")
        self.assertEqual(post.iloc[0].week, 19)

    @patch.dict(os.environ, {"CFBD_API_KEY": "test-secret"})
    def test_ncaaf_flattens_player_stats_and_omits_incomplete_games(self):
        games = [{"id": n, "completed": n == 1, "week": 1, "startDate": "2024-08-31T16:00:00Z",
                  "homeTeam": "Michigan", "awayTeam": "Opponent"} for n in (1, 2)]
        team = {"team": "Michigan", "categories": [{"name": "passing", "types": [
            {"name": "YDS", "athletes": [{"id": "001", "name": "Player", "stat": "230"}]},
            {"name": "C/ATT", "athletes": [{"id": "001", "name": "Player", "stat": "20/30"}]}]}]}
        client = Mock()
        client.json.side_effect = [games, [{"id": n, "teams": [team]} for n in (1, 2)]]
        frame = load_ncaaf(Query("NCAAF", 2024, "player", team="Michigan"), client)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0].passing_yds, 230)
        self.assertEqual(frame.iloc[0].passing_completions, 20)
        self.assertEqual(frame.iloc[0].passing_attempts, 30)
        self.assertEqual(frame.iloc[0].opponent, "Opponent")
        self.assertEqual(client.json.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")

    @patch.dict(os.environ, {"CFBD_API_KEY": "test-secret"})
    def test_ncaaf_team_stats_preserve_score_and_split_efficiency(self):
        game = {"id": 1, "completed": True, "week": 0, "startDate": "2024-08-24T16:00:00Z",
                "homeTeam": "Michigan", "awayTeam": "Opponent"}
        box = {"id": 1, "teams": [
            {"teamId": 130, "team": "Michigan", "points": 28, "stats": [
                {"category": "totalYards", "stat": "400"}, {"category": "thirdDownEff", "stat": "5-12"},
                {"category": "possessionTime", "stat": "32:11"}]},
            {"teamId": 1, "team": "Opponent", "points": 10, "stats": []}]}
        client = Mock()
        client.json.side_effect = [[game], [box]]
        frame = load_ncaaf(Query("NCAAF", 2024, team="Michigan"), client)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0].points, 28)
        self.assertEqual(frame.iloc[0].total_yards, 400)
        self.assertEqual(frame.iloc[0].third_down_attempts, 12)
        self.assertNotIn("possession_time", frame)

    def test_empty_and_changed_schemas_are_actionable(self):
        client = Mock()
        client.json.return_value = {"resultSets": [{"name": "LeagueGameLog", "headers": [], "rowSet": []}]}
        with self.assertRaisesRegex(DataSourceError, "No completed"):
            load_nba(Query("NBA", 2024), client)
        client.json.return_value = {"changed": True}
        with self.assertRaisesRegex(DataSourceError, "schema"):
            load_nba(Query("NBA", 2024), client)

    @patch.dict(os.environ, {}, clear=True)
    def test_ncaaf_requires_key_without_network_request(self):
        client = Mock()
        with self.assertRaisesRegex(DataSourceError, "CFBD_API_KEY"):
            load_ncaaf(Query("NCAAF", 2024, week=1), client)
        client.json.assert_not_called()

    def test_query_validation(self):
        for kwargs in ({"sport": "MLB", "season": 2024}, {"sport": "NFL", "season": 1900},
                       {"sport": "NCAAF", "season": 2024}, {"sport": "NFL", "season": 2024, "week": 26}):
            with self.assertRaises(ValueError):
                Query(**kwargs)

    def test_http_does_not_expose_credentials(self):
        client = HttpClient()
        client.session.get = Mock(return_value=Mock(status_code=401))
        with self.assertRaises(DataSourceError) as error:
            client.get("https://example.com", headers={"Authorization": "Bearer secret"})
        self.assertNotIn("secret", str(error.exception))


class ForecastTests(unittest.TestCase):
    def test_walk_forward_uses_only_prior_games(self):
        history, prediction, mae, count = rolling_baseline(observations().sample(frac=1), "pts", 3)
        self.assertTrue(history.baseline.iloc[:3].isna().all())
        self.assertEqual(history.baseline.iloc[3], 20)
        self.assertEqual(prediction, 60)
        self.assertEqual(mae, 20)
        self.assertEqual(count, 4)
        changed = observations()
        changed.loc[changed.index[-1], "pts"] = 9999
        revised, *_ = rolling_baseline(changed, "pts", 3)
        pd.testing.assert_series_equal(history.baseline, revised.baseline)

    def test_insufficient_history_and_missing_values(self):
        frame = observations().head(3)
        frame.loc[frame.index[1], "pts"] = float("nan")
        history, prediction, mae, count = rolling_baseline(frame, "pts", 3)
        self.assertEqual(len(history), 2)
        self.assertIsNone(prediction)
        self.assertIsNone(mae)
        self.assertEqual(count, 0)


class ServiceAndDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DataService(Path(self.tmp.name) / "test.sqlite3")
        self.query = Query("NBA", 2024)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("sports_analytics.service.fetch")
    def test_cache_round_trip_and_explicit_refresh(self, fetch):
        fetch.return_value = observations()
        loaded = self.service.load(self.query)
        cached = self.service.load(self.query)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(cached.status, "cached")
        self.assertEqual(cached.frame.game_id.iloc[0], "001")
        self.assertEqual(cached.frame.entity_id.iloc[0], "001")
        self.assertEqual(cached.frame.event_date.iloc[0], loaded.frame.event_date.iloc[0])
        self.service.load(self.query, refresh=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.service.read(loaded.key).frame.pts.sum(), 280)

    @patch("sports_analytics.service.fetch")
    def test_refresh_failure_keeps_saved_data_with_warning(self, fetch):
        fetch.return_value = observations()
        self.service.load(self.query)
        fetch.side_effect = DataSourceError("Offline")
        result = self.service.load(self.query, refresh=True)
        self.assertEqual(result.status, "stale cache")
        self.assertEqual(result.notice, "Offline")
        self.assertEqual(len(result.frame), 7)

    @patch("sports_analytics.service.fetch")
    def test_http_layout_and_callbacks_through_loaded_dataset(self, fetch):
        fetch.return_value = observations()
        app = create_app(self.service)
        client = app.server.test_client()
        for route in ("/", "/_dash-layout", "/_dash-dependencies", "/assets/style.css"):
            with client.get(route) as route_response:
                self.assertEqual(route_response.status_code, 200)
        fetch.assert_not_called()
        load_output = next(key for key in app.callback_map if "dataset.data" in key)
        load_response = client.post("/_dash-update-component", json={
            "output": load_output,
            "outputs": [{"id": "dataset", "property": "data"}, {"id": "load-status", "property": "children"},
                        {"id": "dataset-summary", "property": "children"}, {"id": "export", "property": "disabled"}],
            "changedPropIds": ["load.n_clicks"], "inputs": [
                {"id": "load", "property": "n_clicks", "value": 1}, {"id": "refresh", "property": "n_clicks", "value": 0},
                {"id": "sport", "property": "value", "value": "NBA"}, {"id": "season", "property": "value", "value": 2024},
                {"id": "entity-type", "property": "value", "value": "team"},
                {"id": "season-type", "property": "value", "value": "regular"},
                {"id": "college-team", "property": "value", "value": None}, {"id": "week", "property": "value", "value": None}],
            "state": []})
        self.assertEqual(load_response.status_code, 200, load_response.get_data(as_text=True))
        dataset = load_response.json["response"]["dataset"]["data"]
        self.assertFalse(load_response.json["response"]["export"]["disabled"])
        analyze_output = next(key for key in app.callback_map if "trend.figure" in key)
        response = client.post("/_dash-update-component", json={
            "output": analyze_output,
            "outputs": [{"id": "trend", "property": "figure"}, {"id": "forecast-summary", "property": "children"},
                        {"id": "game-log", "property": "data"}, {"id": "game-log", "property": "columns"}],
            "changedPropIds": ["dataset.data"], "inputs": [
                {"id": "dataset", "property": "data", "value": dataset}, {"id": "entity", "property": "value", "value": "001"},
                {"id": "metric", "property": "value", "value": "pts"}, {"id": "window", "property": "value", "value": 3}], "state": []})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.json["response"]
        self.assertEqual(len(result["game-log"]["data"]), 7)
        self.assertEqual(len(result["trend"]["figure"]["data"]), 3)
        export_response = client.post("/_dash-update-component", json={
            "output": "download.data", "outputs": {"id": "download", "property": "data"},
            "changedPropIds": ["export.n_clicks"], "inputs": [{"id": "export", "property": "n_clicks", "value": 1}],
            "state": [{"id": "dataset", "property": "data", "value": dataset}]})
        self.assertEqual(export_response.status_code, 200)
        exported = pd.read_csv(StringIO(export_response.json["response"]["download"]["data"]["content"]))
        self.assertEqual(len(exported), 7)


if __name__ == "__main__":
    unittest.main()
