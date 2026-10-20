"""Team -> player -> statistic category -> statistic callback regressions."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from sports_analytics.career import Career
from sports_analytics.dashboard import ALL_TEAMS, create_app
from sports_analytics.data import Query, normalize
from sports_analytics.metrics import metric_label, statistic_categories
from sports_analytics.service import DataService


def football_players():
    return normalize(pd.DataFrame({
        "game_id": ["001", "002", "003", "004"], "entity_id": ["qb", "qb", "rb", "k"],
        "entity_name": ["Alex Quarterback", "Alex Quarterback", "Ben Runner", "Chris Kicker"],
        "team": ["KC", "BUF", "KC", "BUF"], "opponent": ["BUF", "KC", "BUF", "KC"],
        "position": ["QB", "QB", "RB", "K"], "event_date": pd.date_range("2025-09-01", periods=4),
        "passing_yards": [250, 280, 0, 0], "passing_tds": [2, 3, 0, 0], "passing_interceptions": [0, 0, 0, 0],
        "completions": [20, 25, 0, 0], "attempts": [30, 34, 0, 0], "carries": [4, 5, 18, 0],
        "rushing_yards": [20, 25, 80, 0], "receiving_yards": [0, 0, 30, 0], "targets": [0, 0, 3, 0],
        "fg_made": [0, 0, 0, 3], "fg_att": [0, 0, 0, 3],
    }), Query("NFL", 2025, "player"))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DataService(Path(self.tmp.name) / "stats.sqlite3")
        self.career = Mock()
        self.career.load.return_value = Career(pd.DataFrame({"season": [2024, 2025], "value": [4000, 4500],
                                                            "team": ["KC", "BUF"]}),
                                              [2024, 2025], [], "regular", "nflverse")
        self.app = create_app(self.service, self.career)
        self.client = self.app.server.test_client()
        self.client.get("/_dash-layout").close()
        self.dataset = self.save(football_players(), "NFL", "player")

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, frame, sport, kind):
        with patch("sports_analytics.service.fetch", return_value=frame):
            result = self.service.load(Query(sport, 2025, kind))
        return {"key": result.key, "sport": sport, "kind": kind, "season": 2025, "phase": "regular"}

    def callback(self, token, **values):
        key = next(key for key in self.app.callback_map if token in key)
        config = self.app.callback_map[key]
        output = config["output"]
        if isinstance(output, list):
            outputs = [{"id": item.component_id, "property": item.component_property} for item in output]
        else:
            outputs = {"id": output.component_id, "property": output.component_property}
        defaults = {"dataset.data": self.dataset, "entity.value": "qb", "player-team.value": ALL_TEAMS,
                    "metric-group.value": "passing", "metric.value": "passing_yards", "window.value": 3,
                    "entity-type.value": "player", "sport.value": "NFL", "workspace-page.value": "historical"}
        defaults.update(values)
        def properties(items):
            return [{**item, "value": defaults.get(item["id"] + "." + item["property"])} for item in items]
        with patch("sports_analytics.service.fetch", side_effect=AssertionError("Filters must not download source data")):
            response = self.client.post("/_dash-update-component", json={
                "output": key, "outputs": outputs, "inputs": properties(config["inputs"]),
                "state": properties(config["state"]), "changedPropIds": [],
            })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["response"]

    def test_team_is_first_filter_and_only_visible_in_player_mode(self):
        ids = [component.id for component in self.app.layout._traverse() if hasattr(component, "id")]
        self.assertLess(ids.index("player-team"), ids.index("entity"))
        self.assertLess(ids.index("entity"), ids.index("metric-group"))
        self.assertLess(ids.index("metric-group"), ids.index("metric"))
        player = self.callback("player-team-field.style")
        self.assertEqual(player["player-team-field"]["style"], {"display": "block"})
        self.assertEqual(player["entity-label"]["children"], "Player")
        team = self.callback("player-team-field.style", **{"entity-type.value": "team"})
        self.assertEqual(team["player-team-field"]["style"], {"display": "none"})
        self.assertEqual(team["entity-label"]["children"], "Team")

    def test_team_options_and_player_list_respect_selected_team(self):
        teams = self.callback("player-team.options", **{"player-team.value": "invalid"})["player-team"]
        self.assertEqual({o["value"] for o in teams["options"]}, {ALL_TEAMS, "KC", "BUF"})
        self.assertEqual(teams["value"], ALL_TEAMS)
        roster = self.callback("entity.options", **{"player-team.value": "KC", "entity.value": "k"})["entity"]
        self.assertEqual({o["value"] for o in roster["options"]}, {"qb", "rb"})
        self.assertEqual(roster["value"], "qb")
        preserved = self.callback("entity.options", **{"player-team.value": "KC", "entity.value": "rb"})["entity"]
        self.assertEqual(preserved["value"], "rb")

    def test_category_and_statistic_options_keep_position_filtering_and_zero_values(self):
        categories = self.callback("metric-group.options")["metric-group"]
        self.assertEqual({o["value"] for o in categories["options"]}, {"passing", "rushing"})
        passing = self.callback("metric.options", **{"metric.value": "rushing_yards"})["metric"]
        self.assertEqual({o["value"] for o in passing["options"]},
                         {"passing_yards", "passing_tds", "passing_interceptions", "completions", "attempts"})
        self.assertEqual(passing["value"], "passing_yards")
        self.assertEqual(next(o["label"] for o in passing["options"] if o["value"] == "passing_tds"), "Passing touchdowns")
        rushing = self.callback("metric.options", **{"metric-group.value": "rushing"})["metric"]
        self.assertEqual({o["value"] for o in rushing["options"]}, {"carries", "rushing_yards"})
        self.assertEqual(rushing["value"], "rushing_yards")

    def test_switching_player_resets_incompatible_category_and_statistic(self):
        categories = self.callback("metric-group.options", **{"entity.value": "rb"})["metric-group"]
        self.assertEqual(categories["value"], "rushing")
        self.assertEqual({o["value"] for o in categories["options"]}, {"rushing", "receiving"})
        kicker_categories = self.callback("metric-group.options", **{"entity.value": "k"})["metric-group"]
        self.assertEqual(kicker_categories["value"], "kicking")
        kicker = self.callback("metric.options", **{"entity.value": "k", "metric-group.value": "kicking"})["metric"]
        self.assertEqual({o["value"] for o in kicker["options"]}, {"fg_made", "fg_att"})
        self.assertEqual(kicker["value"], "fg_made")

    def test_team_filter_limits_game_log_for_traded_player_but_not_career(self):
        filtered = self.callback("trend.figure", **{"player-team.value": "KC"})
        self.assertEqual(len(filtered["game-log"]["data"]), 1)
        self.assertEqual(filtered["game-log"]["data"][0]["team"], "KC")
        unfiltered = self.callback("trend.figure")
        self.assertEqual(len(unfiltered["game-log"]["data"]), 2)
        self.callback("career-trend.figure", **{"player-team.value": "KC"})
        self.career.load.assert_called_once_with("NFL", "qb", "Alex Quarterback", "passing_yards", "regular")

    def test_team_mode_ignores_the_hidden_player_team_filter(self):
        frame = normalize(pd.DataFrame({"game_id": ["001", "001"], "entity_id": ["KC", "BUF"],
                                        "entity_name": ["KC", "BUF"], "team": ["KC", "BUF"],
                                        "passing_yards": [250, 200]}), Query("NFL", 2025))
        dataset = self.save(frame, "NFL", "team")
        teams = self.callback("entity.options", **{"dataset.data": dataset, "player-team.value": "KC"})["entity"]
        self.assertEqual({o["value"] for o in teams["options"]}, {"KC", "BUF"})

    def test_nba_groups_and_shooting_options(self):
        frame = normalize(pd.DataFrame({"game_id": ["001"], "entity_id": ["nba-player"],
                                        "entity_name": ["Player"], "team": ["GSW"], "position": ["G"],
                                        "pts": [20], "ast": [5], "reb": [6], "stl": [0],
                                        "fgm": [7], "fga": [15], "fg_pct": [.467]}), Query("NBA", 2025, "player"))
        dataset = self.save(frame, "NBA", "player")
        values = {"dataset.data": dataset, "entity.value": "nba-player"}
        category = self.callback("metric-group.options", **values)["metric-group"]
        self.assertEqual(category["value"], "scoring")
        self.assertEqual({o["value"] for o in category["options"]}, {"scoring", "playmaking", "rebounding", "defense", "shooting"})
        shooting = self.callback("metric.options", **values, **{"metric-group.value": "shooting"})["metric"]
        self.assertEqual({o["value"] for o in shooting["options"]}, {"fgm", "fga", "fg_pct"})
        trend = self.callback("trend.figure", **values, **{"metric.value": "pts"})
        columns = {column["id"] for column in trend["game-log"]["columns"]}
        self.assertNotIn("game_id", columns)
        self.assertNotIn("week", columns)
        self.assertNotIn("week", trend["game-log"]["data"][0])

    def test_nfl_team_scoring_options_and_game_log_columns(self):
        frame = normalize(pd.DataFrame({"game_id": ["g1", "g2"], "entity_id": ["KC", "KC"],
                                        "entity_name": ["KC", "KC"], "team": ["KC", "KC"],
                                        "opponent": ["BUF", "DAL"], "week": [1, 2],
                                        "event_date": ["2025-09-01", "2025-09-08"],
                                        "pf": [24, 0], "pa": [20, 27], "passing_yards": [250, 200]}), Query("NFL", 2025))
        dataset = self.save(frame, "NFL", "team")
        values = {"dataset.data": dataset, "entity.value": "KC", "metric-group.value": "scoring", "metric.value": "pf"}
        categories = self.callback("metric-group.options", **values)["metric-group"]["options"]
        self.assertIn({"label": "Scoring", "value": "scoring"}, categories)
        scoring = self.callback("metric.options", **values)["metric"]["options"]
        self.assertEqual(scoring, [{"label": "PF (Points for)", "value": "pf"}, {"label": "PA (Points against)", "value": "pa"}])
        for metric in ["pf", "pa"]:
            result = self.callback("trend.figure", **{**values, "metric.value": metric})
            columns = {column["id"] for column in result["game-log"]["columns"]}
            self.assertNotIn("game_id", columns)
            self.assertIn("week", columns)
            self.assertTrue(all("game_id" not in row for row in result["game-log"]["data"]))
        self.assertIn("game_id", self.service.read(dataset["key"]).frame)
        self.assertEqual(metric_label("pf", "NBA"), "Personal fouls")

    def test_empty_dataset_disables_dependent_filters(self):
        for token, component in [("player-team.options", "player-team"), ("entity.options", "entity"),
                                 ("metric-group.options", "metric-group"), ("metric.options", "metric")]:
            result = self.callback(token, **{"dataset.data": None})[component]
            self.assertTrue(result["disabled"])
            self.assertEqual(result["options"], [])

    def test_college_team_statistics_use_the_matching_football_groups(self):
        groups = statistic_categories(["completions", "attempts", "rushing_yards", "penalty_yards"], "NCAAF")
        self.assertEqual({o["label"] for o in groups}, {"Passing", "Rushing", "Penalties"})
        self.assertEqual(statistic_categories(["points"], "NCAAF"), [{"label": "Scoring", "value": "scoring"}])

    def test_switching_to_college_removes_players_and_resets_to_team(self):
        college = self.callback("entity-type.options", **{"sport.value": "NCAAF"})["entity-type"]
        self.assertEqual(college["options"], [{"label": "Teams", "value": "team"}])
        self.assertEqual(college["value"], "team")
        for sport in ("NBA", "NFL"):
            with self.subTest(sport=sport):
                modes = self.callback("entity-type.options", **{"sport.value": sport})["entity-type"]
                self.assertEqual({o["value"] for o in modes["options"]}, {"team", "player"})
                self.assertEqual(modes["value"], "player")

    def test_college_hides_player_filters_and_career_even_before_mode_resets(self):
        identity = self.callback("player-team-field.style", **{"sport.value": "NCAAF"})
        self.assertEqual(identity["player-team-field"]["style"], {"display": "none"})
        self.assertEqual(identity["entity-label"]["children"], "Team")
        career = self.callback("career-panel.style", **{"sport.value": "NCAAF"})
        self.assertEqual(career["career-panel"]["style"], {"display": "none"})

    def test_obsolete_college_player_selection_cannot_read_stats_or_load_career(self):
        obsolete = {**self.dataset, "sport": "NCAAF"}
        with patch.object(self.service, "read", side_effect=AssertionError("Unsupported dataset must not be read")):
            for token, component in [("entity.options", "entity"), ("player-team.options", "player-team"),
                                     ("metric-group.options", "metric-group"), ("metric.options", "metric")]:
                result = self.callback(token, **{"dataset.data": obsolete})[component]
                self.assertTrue(result["disabled"])
                self.assertEqual(result["options"], [])
            trend = self.callback("trend.figure", **{"dataset.data": obsolete})
            self.assertEqual(trend["game-log"]["data"], [])
            career = self.callback("career-trend.figure", **{"dataset.data": obsolete})
            self.assertEqual(career["career-status"]["children"], "")
        self.career.load.assert_not_called()

    def test_college_load_uses_teams_while_previous_player_mode_is_resetting(self):
        query = Query("NCAAF", 2025, team="Michigan")
        frame = normalize(pd.DataFrame({"game_id": ["001"], "entity_id": ["130"],
                                        "entity_name": ["Michigan"], "team": ["Michigan"], "points": [30]}), query)
        with patch("sports_analytics.service.fetch", return_value=frame):
            loaded = self.service.load(query)
        with patch.object(self.service, "load", return_value=loaded) as load:
            result = self.callback("dataset.data", **{
                "sport.value": "NCAAF", "season.value": 2025, "season-type.value": "regular",
                "college-team.value": "Michigan", "week.value": None, "load.n_clicks": 0, "refresh.n_clicks": 0})
        self.assertEqual(load.call_args.args[0], query)
        dataset = result["dataset"]["data"]
        self.assertEqual(dataset["kind"], "team")
        self.assertFalse(result["export"]["disabled"])
        teams = self.callback("entity.options", **{"dataset.data": dataset})["entity"]
        self.assertEqual(teams["options"], [{"label": "Michigan", "value": "130"}])
        stats = self.callback("metric.options", **{"dataset.data": dataset, "entity.value": "130",
                                                   "metric-group.value": "scoring"})["metric"]
        self.assertEqual(stats["value"], "points")


if __name__ == "__main__":
    unittest.main()
