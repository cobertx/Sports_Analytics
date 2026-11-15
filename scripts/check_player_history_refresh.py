"""Offline regressions for current box scores, historical isolation and recency."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError
from sports_analytics.service import DataService
from sports_analytics.player_history_refresh import refresh_live_history
from sports_analytics.whp_service import WHPMarketService
from models.Player_Stats.Weighted_Historical_Performance.core import WHPConfig, WeightedHistoricalPerformance


NOW=pd.Timestamp('2026-11-13T17:00:00Z')
SNAPSHOT=dict(sport='NBA',commence_time='2026-11-14T00:00:00Z',pulled_at=NOW.isoformat())


class HistoryRefreshChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=DataService(Path(self.tmp.name)/'history.sqlite3')
        self.source=WHPMarketService(SimpleNamespace(data=self.data))
        self.result=SimpleNamespace(status='cached',fetched_at=NOW.isoformat(),notice='')

    def test_current_whp_and_simulation_sources_refresh_the_required_public_inputs(self):
        with patch.object(self.data,'load',return_value=self.result) as load:
            refresh_live_history(SNAPSHOT,self.source,now=NOW)
            self.assertEqual([(c.args[0].entity_type,c.args[0].season) for c in load.call_args_list],[('player',2026)])
            load.reset_mock()
            simulation=SimpleNamespace(repository=SimpleNamespace(data=self.data))
            refresh_live_history(SNAPSHOT,simulation,now=NOW)
            self.assertEqual([c.args[0].entity_type for c in load.call_args_list],['player','team'])

    def test_failed_refresh_does_not_silently_quote_probabilities_from_stale_inputs(self):
        stale=SimpleNamespace(status='stale cache',fetched_at='2026-10-31T00:00:00Z',notice='Source offline.')
        with patch.object(self.data,'load',return_value=stale):
            with self.assertRaisesRegex(DataSourceError,'Current player probabilities are unavailable'):
                refresh_live_history(SNAPSHOT,self.source,now=NOW)

    def test_historical_snapshots_finished_games_and_college_never_download(self):
        with patch.object(self.data,'load',side_effect=AssertionError('Unexpected download')):
            refresh_live_history(dict(SNAPSHOT,pulled_at='2026-11-01T00:00:00Z'),self.source,now=NOW)
            refresh_live_history(dict(SNAPSHOT,commence_time='2026-11-13T16:00:00Z'),self.source,now=NOW)
            refresh_live_history(dict(SNAPSHOT,sport='NCAAF'),self.source,now=NOW)

    def test_recent_failures_lower_probability_more_than_the_same_older_failures(self):
        dates=pd.date_range('2026-10-01',periods=30,tz='UTC')
        frame=pd.DataFrame(dict(game_id=[str(i) for i in range(30)],event_date=dates,
            available_at=dates+pd.Timedelta(hours=8),season=2026,value=30))
        model=WeightedHistoricalPerformance(replace(WHPConfig(),prior_strength=0))
        recent=frame.copy();recent.loc[27:,'value']=10
        older=frame.copy();older.loc[10:12,'value']=10
        new=model.forecast(recent,NOW);old=model.forecast(older,NOW)
        self.assertLess(new.mean,old.mean)
        self.assertLess(new.market(19.5).win,old.market(19.5).win)
        self.assertGreater(new.weights[-1],new.weights[-6])
        self.assertAlmostEqual(new.weights[-1]/new.weights[-6],2)
        self.assertAlmostEqual(new.market(19.5).win+new.market(19.5,'Under').win,1)
        self.assertTrue(np.isfinite(new.variance))


if __name__=='__main__':main()
