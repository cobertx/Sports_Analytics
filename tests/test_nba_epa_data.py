"""Readiness checks must expose scoring gaps and never enable an untrained model."""

import csv
import gzip
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from sports_analytics.data import DataSourceError
from Models.Game_Outcome.EPA.nba_epa_data import SOURCES, archive_columns, archive_source, audit_archives, prepare_archive


def write_archive(path, kind, rows):
    fields = sorted(SOURCES[kind][2])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({name: row.get(name, "") for name in fields} for row in rows)


class NBAEPADataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.possessions, self.events = self.root / "poss.csv.gz", self.root / "pbp.csv.gz"
        self.rows = [dict(game_id="0022500001", period=1, possession_number=i, offense_team_id=offense,
                          defense_team_id=defense, points=points, count_as_possession=count,
                          start_order_index=i, end_order_index=i, possession_start_type="OffDeadball")
                     for i, offense, defense, points, count in [(1, 11, 22, 2, "true"), (2, 22, 11, 5, "true"), (3, 11, 22, 1, "false")]]
        self.rows.append({**self.rows[0], "game_id": "0012500001"})
        self.event_rows = [dict(game_id=game_id, period=1, order_index=1, possession_number=1,
                                seconds_remaining=700, action_type="2pt", score_home=3, score_away=5)
                           for game_id in ["0022500001", "0012500001"]]
        write_archive(self.possessions, "possessions", self.rows)
        write_archive(self.events, "play_by_play", self.event_rows)
        self.box_scores = pd.DataFrame([{"game_id": "0022500001", "entity_id": "11", "pts": 3, "team": "HOME", "matchup": "AWAY @ HOME"},
                                        {"game_id": "0022500001", "entity_id": "22", "pts": 5, "team": "AWAY", "matchup": "AWAY @ HOME"}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_season_translation_and_missing_cache_never_download_without_flag(self):
        filename, url = archive_source("play_by_play", 2025)
        self.assertEqual(filename, "nba_play_by_play_2026.csv.gz")
        self.assertIn("/nba_stats_pbp/", url)
        with self.assertRaisesRegex(DataSourceError, "--download"):
            prepare_archive(self.root, "possessions", 2025)

    def test_administrative_scoring_retained_and_preseason_excluded(self):
        result = audit_archives(self.possessions, self.events, self.box_scores)
        self.assertEqual(result["possession_rows"], 3)
        self.assertEqual(result["counted_possessions"], 2)
        self.assertEqual(result["points_in_non_counted_records"], 1)
        self.assertEqual(result["excluded_non_regular_postseason_possessions"], 1)
        self.assertEqual(result["event_rows"], 1)
        self.assertEqual(result["score_mismatches"], [])
        self.assertEqual(result["box_score_team_rows_compared"], 2)
        self.assertEqual(result["pbp_box_score_team_rows_compared"], 2)
        self.assertEqual(result["pbp_score_mismatches"], [])
        self.assertFalse(result["shot_clock_column_available"])
        self.assertFalse(result["tracking_available"])
        self.assertFalse(result["nba_epa_ready"])

    def test_gaps_duplicates_and_score_mismatches_remain_visible(self):
        self.rows.append(self.rows[0])
        write_archive(self.possessions, "possessions", self.rows)
        box = pd.concat([self.box_scores, pd.DataFrame([{"game_id": "0022500002", "entity_id": "11", "pts": 100}])])
        result = audit_archives(self.possessions, self.events, box)
        self.assertEqual(result["duplicate_possession_keys"], 1)
        self.assertEqual(result["score_mismatches"][0]["possession_points"], 5)
        self.assertEqual(len(result["box_score_team_rows_missing_possessions"]), 1)
        self.assertFalse(result["nba_epa_ready"])

    def test_schema_changes_are_rejected_before_data_can_be_used(self):
        with gzip.open(self.events, "wt", encoding="utf-8") as file:
            file.write("game_id,unexpected\n0022500001,1\n")
        with self.assertRaisesRegex(DataSourceError, "missing required columns"):
            archive_columns(self.events, "play_by_play")

    def test_event_score_disagreement_is_reported_even_if_possession_scores_match(self):
        self.event_rows.append({**self.event_rows[0], "order_index": 2, "score_home": 4})
        write_archive(self.events, "play_by_play", self.event_rows)
        result = audit_archives(self.possessions, self.events, self.box_scores)
        self.assertEqual(result["score_mismatches"], [])
        self.assertEqual(result["pbp_score_mismatches"][0]["pbp_points"], 4)
        self.assertFalse(result["nba_epa_ready"])


if __name__ == "__main__":
    unittest.main()
