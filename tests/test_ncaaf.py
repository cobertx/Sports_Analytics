"""NCAAF team-only source and public API regressions."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sports_analytics.career import CareerService
from sports_analytics.data import DataSourceError, Query, load_ncaaf


class CollegeTeamTests(unittest.TestCase):
    def test_player_queries_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "NCAAF supports team statistics only"):
            Query("NCAAF", 2025, "player", team="Michigan")

    @patch.dict(os.environ, {"CFBD_API_KEY": "test-key"})
    def test_team_import_uses_only_game_metadata_and_team_box_scores(self):
        games = [{"id": 1, "completed": True, "startDate": "2025-09-06T16:00:00Z", "week": 2,
                  "homeTeam": "Michigan", "awayTeam": "Oklahoma"},
                 {"id": 2, "completed": False, "homeTeam": "Michigan", "awayTeam": "Other"}]
        teams = [{"teamId": 130, "team": "Michigan", "points": 30, "stats": [
                    {"category": "rushingYards", "stat": "150"},
                    {"category": "completionAttempts", "stat": "20-30"},
                    {"category": "thirdDownEff", "stat": "4-10"}]},
                 {"teamId": 201, "team": "Oklahoma", "points": 20, "stats": []}]
        for scope in ({"team": "Michigan"}, {"week": 2}):
            with self.subTest(scope=scope):
                client = Mock()
                client.json.side_effect = [games, [{"id": 1, "teams": teams}, {"id": 2, "teams": teams}]]
                frame = load_ncaaf(Query("NCAAF", 2025, **scope), client)
                self.assertEqual([call.args[0] for call in client.json.call_args_list], [
                    "https://api.collegefootballdata.com/games", "https://api.collegefootballdata.com/games/teams"])
                for call in client.json.call_args_list:
                    self.assertEqual(call.kwargs["params"], {
                        "year": 2025, "seasonType": "regular", "classification": "fbs", **scope})
                self.assertEqual(set(frame["entity_type"]), {"team"})
                self.assertEqual(set(frame["matchup"]), {"Oklahoma @ Michigan"})
                self.assertEqual(set(frame["entity_name"]), {"Michigan"} if "team" in scope else {"Michigan", "Oklahoma"})
                michigan = frame[frame["entity_id"] == "130"].iloc[0]
                self.assertEqual(michigan.points, 30)
                self.assertEqual(michigan.rushing_yards, 150)
                self.assertEqual(michigan.completions, 20)
                self.assertEqual(michigan.attempts, 30)
                self.assertEqual(michigan.third_down_conversions, 4)
                self.assertEqual(michigan.event_date.strftime("%Y-%m-%d"), "2025-09-06")

    def test_career_service_rejects_unsupported_sport_before_cache_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CareerService(Path(tmp) / "career.sqlite3")
            with patch.object(service, "_cached_table") as cache, patch("sports_analytics.career.HttpClient") as client:
                for call in (lambda: service.load("NCAAF", "001", "Player", "points", "regular"),
                             lambda: service.available_seasons("NCAAF", "001", "Player"),
                             lambda: service.season("NCAAF", 2025, "regular")):
                    with self.assertRaisesRegex(DataSourceError, "only for NBA and NFL"):
                        call()
                cache.assert_not_called()
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
