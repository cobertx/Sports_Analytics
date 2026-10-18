"""Regression tests for the load -> entity options -> metric options flow."""

from pathlib import Path
import gzip
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError, Query, load_nba_archive, normalize
from sports_analytics.service import DataService


class DropdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DataService(Path(self.tmp.name) / "stats.sqlite3")
        self.app = create_app(self.service)
        self.client = self.app.server.test_client()
        self.client.get("/_dash-layout").close()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self, token, inputs, state=None, changed=None):
        key = next(k for k in self.app.callback_map if token in k)
        outputs = self.app.callback_map[key]["output"]
        outputs = [{"id": o.component_id, "property": o.component_property} for o in outputs]
        response = self.client.post("/_dash-update-component", json={
            "output": key, "outputs": outputs, "changedPropIds": changed or [],
            "inputs": [{"id": name, "property": prop, "value": value} for name, prop, value in inputs],
            "state": [{"id": name, "property": prop, "value": value} for name, prop, value in (state or [])],
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["response"]

    def load(self, sport, kind="team", changed=None):
        return self.callback("dataset.data", [
            ("load", "n_clicks", 0), ("refresh", "n_clicks", 0), ("sport", "value", sport),
            ("season", "value", 2024), ("entity-type", "value", kind),
            ("season-type", "value", "regular"), ("college-team", "value", "Michigan"), ("week", "value", None),
        ], changed=changed)

    def options(self, dataset, entity=None, metric=None):
        return self.callback("entity.options", [("dataset", "data", dataset)],
                             [("entity", "value", entity), ("metric", "value", metric)], ["dataset.data"])

    @staticmethod
    def fixture(query):
        metric = {"NBA": "pts", "NFL": "passing_yards", "NCAAF": "points"}[query.sport]
        return normalize(pd.DataFrame({
            "game_id": ["001", "002"], "entity_id": [f"{query.sport}-{query.entity_type}"] * 2,
            "entity_name": ["Michigan" if query.entity_type == "team" else "Player name"] * 2,
            "team": ["Michigan"] * 2, "opponent": ["Opponent"] * 2, "week": [1, 2], metric: [10, 20],
        }), query)

    @patch("sports_analytics.service.fetch")
    def test_initial_page_and_every_league_and_entity_type_populate_without_load_click(self, fetch):
        fetch.side_effect = self.fixture
        for sport in ("NBA", "NFL", "NCAAF"):
            for kind in ("team", "player"):
                with self.subTest(sport=sport, kind=kind):
                    # Empty changedPropIds reproduces the initial browser callback.
                    loaded = self.load(sport, kind)
                    dataset = loaded["dataset"]["data"]
                    self.assertIsNotNone(dataset)
                    selected = self.options(dataset, entity="previous-page-id", metric="previous-page-stat")
                    self.assertFalse(selected["entity"]["disabled"])
                    self.assertFalse(selected["metric"]["disabled"])
                    self.assertEqual(selected["entity"]["value"], f"{sport}-{kind}")
                    self.assertEqual(len(selected["entity"]["options"]), 1)
                    self.assertEqual(len(selected["metric"]["options"]), 1)
                    self.assertIn(selected["metric"]["value"], {o["value"] for o in selected["metric"]["options"]})
        self.assertEqual(fetch.call_count, 6)

    @patch("sports_analytics.service.fetch")
    def test_filter_changes_auto_load_and_preserve_valid_selection(self, fetch):
        fetch.side_effect = self.fixture
        for changed in ("sport.value", "season.value", "entity-type.value", "season-type.value", "college-team.value", "week.value"):
            loaded = self.load("NBA", changed=[changed])
            options = self.options(loaded["dataset"]["data"], "NBA-team", "pts")
            self.assertEqual(options["entity"]["value"], "NBA-team")
            self.assertEqual(options["metric"]["value"], "pts")
        # Further scope visits use the already loaded cache.
        self.assertEqual(fetch.call_count, 1)

    @patch("sports_analytics.service.fetch", side_effect=DataSourceError("Set CFBD_API_KEY"))
    def test_unavailable_source_disables_selectors_and_shows_reason(self, fetch):
        loaded = self.load("NCAAF")
        self.assertIn("CFBD_API_KEY", loaded["load-status"]["children"])
        options = self.options(loaded["dataset"]["data"])
        self.assertTrue(options["entity"]["disabled"])
        self.assertTrue(options["metric"]["disabled"])
        self.assertIn("source status", options["entity"]["placeholder"])


class ArchiveTests(unittest.TestCase):
    def test_nba_archive_filters_phase_joins_dates_and_excludes_dnp(self):
        columns = "team_id,team_city,team_name,team_tricode,person_id,first_name,family_name,minutes,points,game_id,season_type_id\n"
        csv = (columns + "1,Boston,Celtics,BOS,001,First,Player,30:30,20,22500001,2\n"
               "1,Boston,Celtics,BOS,002,Second,Player,,0,22500001,2\n"
               "1,Boston,Celtics,BOS,001,First,Player,30:00,40,42500001,4\n")
        schedule = "game_id,game_date,home_team_abbreviation,away_team_abbreviation\n22500001,2025-10-22,BOS,NYK\n42500001,2026-04-20,BOS,NYK\n"
        for kind in ("team", "player"):
            client = Mock()
            client.get.side_effect = [Mock(text=csv), Mock(content=gzip.compress(schedule.encode()))]
            frame = load_nba_archive(Query("NBA", 2025, kind), client)
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0].game_id, "0022500001")
            self.assertEqual(frame.iloc[0].opponent, "NYK")
            self.assertEqual(frame.iloc[0]["min"], 30.5)
            self.assertEqual(frame.iloc[0].pts, 20)
            self.assertEqual(frame.iloc[0].event_date.date().isoformat(), "2025-10-22")
            self.assertEqual(frame.attrs["source"], "NBA Stats via SportsDataverse")


if __name__ == "__main__":
    unittest.main()
