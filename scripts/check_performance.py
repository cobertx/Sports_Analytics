"""Offline regressions for bounded cache reuse, refreshes and equivalent filtering."""
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch
import zlib

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError, Query
from sports_analytics.metrics import relevant_metrics
from sports_analytics.service import DataService
from models.Player_Stats.Weighted_Historical_Performance.prediction_service import WHPMarketService
from models.Player_Stats.Weighted_Historical_Performance.market_comparison import compare_price


class PerformanceChecks(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = DataService(self.root / 'sports.sqlite3')
        self.query = Query('NBA', 2025, 'player')
        self.key = self.service.key(self.query)
        self.frame = pd.DataFrame(dict(entity_id=['001', '002'], entity_name=['One', 'Two'],
            matchup=['A @ B', 'A @ B'], position=['G', 'F'], pts=[20., float('nan')],
            event_date=pd.date_range('2025-11-01', periods=2, tz='UTC').astype('datetime64[ns, UTC]')))
        self.service._decode.cache_clear()
        self.addCleanup(self.service._decode.cache_clear)
        self.addCleanup(self.service._read.cache_clear)
        self.save(self.frame)

    def save(self, frame, stamp='2020-01-01T00:00:00+00:00'):
        payload = zlib.compress(frame.to_json(orient='table', date_format='iso', index=False).encode())
        with self.service.connect() as db:
            db.execute('INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?)',
                (self.key, json.dumps(self.query.parameters()), 'fixture', stamp, payload))

    def test_repeated_payload_decodes_once_and_callers_cannot_mutate_cached_frame(self):
        with patch('sports_analytics.service.pd.read_json', wraps=pd.read_json) as decode:
            first = self.service.cached(self.key)
            first.frame.loc[0, 'pts'] = -999
            first.frame.loc[0, 'entity_name'] = 'Changed'
            first.notice = 'Changed'
            second = self.service.cached(self.key)
            third = self.service.read(self.key)
            self.assertEqual(decode.call_count, 1)
        pd.testing.assert_frame_equal(second.frame, self.frame)
        pd.testing.assert_frame_equal(third.frame, self.frame)
        self.assertEqual((second.source, second.notice, second.status), ('fixture', '', 'cached'))

    def test_payload_revision_with_same_timestamp_is_not_a_cache_hit(self):
        self.service.cached(self.key)
        revised = self.frame.assign(pts=[77., 88.])
        self.save(revised)
        pd.testing.assert_frame_equal(self.service.cached(self.key).frame, revised)
        self.assertEqual(self.service._decode.cache_info().misses, 2)

    def test_expiry_refresh_and_stale_fallback_still_work(self):
        self.service.read(self.key)
        revised = self.frame.assign(pts=[21., 22.])
        with patch('sports_analytics.service.fetch', return_value=revised) as fetch:
            loaded = self.service.load(self.query)
            fetch.assert_called_once_with(self.query)
        self.assertEqual(loaded.status, 'downloaded')
        pd.testing.assert_frame_equal(self.service.read(self.key).frame, revised)
        with patch('sports_analytics.service.fetch', side_effect=DataSourceError('offline')) as fetch:
            self.assertEqual(self.service.load(self.query).status, 'cached')
            fetch.assert_not_called()
            stale = self.service.load(self.query, refresh=True)
            self.assertEqual((stale.status, stale.notice), ('stale cache', 'offline'))
            pd.testing.assert_frame_equal(stale.frame, revised)

    def test_corrupt_payload_cannot_hide_behind_memory_cache_and_refresh_repairs_it(self):
        self.service.cached(self.key)
        with self.service.connect() as db:
            db.execute('UPDATE datasets SET payload=? WHERE key=?', (b'invalid zlib', self.key))
        with self.assertRaises(DataSourceError): self.service.cached(self.key)
        with patch('sports_analytics.service.fetch', return_value=self.frame):
            self.service.load(self.query, refresh=True)
        pd.testing.assert_frame_equal(self.service.cached(self.key).frame, self.frame)
        for i in range(6):
            self.service._decode(zlib.compress(self.frame.assign(pts=i).to_json(orient='table', index=False).encode()))
        self.assertEqual(self.service._decode.cache_info().currsize, 4)

    def test_metric_order_zero_stats_missing_values_and_positions_are_preserved(self):
        frame = pd.DataFrame(dict(position=['QB', 'QB'], passing_yards=[0., None], rushing_yards=[0., 0.],
            receiving_yards=[None, 5.], def_tackles_solo=[None, None], touchdowns=[0, 0],
            irrelevant=['1', '2'], other_stat=[0., 0.]))
        self.assertEqual(relevant_metrics(frame, 'NFL', 'player'),
            ['passing_yards', 'rushing_yards', 'receiving_yards', 'touchdowns'])
        self.assertEqual(relevant_metrics(frame.assign(position='WR'), 'NFL', 'player'),
            ['receiving_yards', 'touchdowns'])
        self.assertEqual(relevant_metrics(frame.assign(position=None), 'NFL', 'player'), ['receiving_yards'])
        self.assertEqual(relevant_metrics(frame, 'NBA', 'player'),
            ['passing_yards', 'rushing_yards', 'receiving_yards', 'touchdowns', 'other_stat'])
        self.assertEqual(relevant_metrics(frame.iloc[:0], 'NFL', 'player'), [])

    def test_indexed_quotes_preserve_no_vig_audit_and_ambiguous_duplicate_rules(self):
        rows = [dict(subject=name, market_key=market, line=line, selection=side, odds=odds)
            for name, market, line, side, odds in [
                ('One', 'player_1st_td', None, 'Yes', 200), ('One', 'player_1st_td', None, 'No', -250),
                ('One', 'player_last_td', None, 'Yes', 300), ('One', 'player_last_td', None, 'No', -400),
                ('One', 'player_last_td', None, 'No', -400), ('Two', 'player_1st_td', None, 'Yes', 100)]]
        snapshot = dict(sport='NFL', game_id='1', pulled_at='2026-10-01T12:00:00Z',
            commence_time='2026-10-02T12:00:00Z', home_team='New England Patriots', away_team='Buffalo Bills', rows=rows)
        repository = Mock(root=self.root)
        repository.load.return_value = pd.DataFrame(dict(entity_name=['One'],
            event_date=pd.to_datetime(['2026-09-01'], utc=True), available_at=pd.to_datetime(['2026-09-02'], utc=True)))
        WHPMarketService(repository).forecast(snapshot)
        audit = json.loads(next((self.root / 'evaluations').glob('*.json')).read_text(encoding='utf-8'))
        for row, actual in zip(rows, audit['evaluations']):
            opposite = {'Yes': 'No', 'No': 'Yes'}[row['selection']]
            matches = [r['odds'] for r in rows if r['subject'] == row['subject'] and r['market_key'] == row['market_key']
                       and r['line'] == row['line'] and r['selection'] == opposite]
            expected = compare_price(None, row['odds'], matches[0] if len(matches) == 1 else None)
            self.assertEqual({k: actual[k] for k in expected}, expected)


if __name__ == '__main__':
    main()
