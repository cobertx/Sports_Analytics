"""Offline regressions for bounded cache reuse, refreshes and equivalent filtering."""
import json
from copy import deepcopy
from contextlib import closing
from dataclasses import asdict
import os
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
from models.Player_Stats.Usage_Opportunity.markets import prop_key, quote_index
from models.Player_Stats.Usage_Opportunity.prediction_service import UsageOpportunityMarketService
from models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service import OpponentAdjustedMarketService
from models.Player_Stats.Pace_Game_Environment.prediction_service import PaceGameEnvironmentMarketService
from sports_analytics.nba_markets import NbaMarketService
from sports_analytics.usage_log import ApiUsageLog


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


class MarketPerformanceChecks(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rows = [dict(group='Points', market='Points', market_key=market, subject='Player One',
            selection=side, line=line, odds=-110, updated='Not reported')
            for market in ('player_points', 'player_points_alternate', 'player_rebounds_alternate')
            for line in (10., 10.5) for side in ('Over', 'Under')]
        self.snapshot = dict(sport='NBA', game_id='123', event_id='a'*32, home_team='Boston Celtics',
            away_team='Detroit Pistons', pulled_at='2026-03-01T12:00:00Z',
            commence_time='2026-03-02T12:00:00Z', cost=2, rows=self.rows)
        dates = pd.date_range('2025-10-01', periods=35, freq='3D', tz='UTC')
        self.frame = pd.DataFrame(dict(game_id=list(map(str, range(35))), event_date=dates,
            available_at=dates+pd.Timedelta(days=1), entity_id='one', entity_name='Player One',
            team='BOS', pts=[15+i%9 for i in range(35)], reb=[i%7 for i in range(35)], min=30.))

    def test_whp_reuses_exact_forecasts_but_rechecks_history_quotes_and_audit(self):
        repo=Mock(root=self.root/'whp')
        repo.load.side_effect=lambda *args:self.frame.copy()
        service=WHPMarketService(repo)
        with patch.object(service,'_player',wraps=service._player) as resolve:
            first=service.forecast(self.snapshot)
            self.assertEqual(resolve.call_count,1)
            self.assertEqual(len(first.forecasts),2)
            second=service.forecast(deepcopy(self.snapshot))
            self.assertEqual(asdict(first),asdict(second))
            self.assertEqual(resolve.call_count,1)
            key=prop_key(self.rows[0])
            second.forecasts[key].diagnostics['training_games']=-999
            self.assertEqual(asdict(service.forecast(self.snapshot)),asdict(first))
            self.frame.loc[0,'pts']+=50
            revised=service.forecast(self.snapshot)
            self.assertEqual(resolve.call_count,2)
            self.assertNotEqual(revised.forecasts[key].diagnostics['input_sha256'],
                                first.forecasts[key].diagnostics['input_sha256'])
            self.snapshot['rows'][0]['odds']=-120
            service.forecast(self.snapshot)
            self.assertEqual(resolve.call_count,3)
            for path in (repo.root/'evaluations').glob('*.json'):path.unlink()
            service.forecast(self.snapshot)
            self.assertEqual(resolve.call_count,4)
            self.assertTrue(list((repo.root/'evaluations').glob('*.json')))
        for i in range(9):
            service.forecast({**self.snapshot,'game_id':str(i)})
        self.assertEqual(len(service._memory),8)

    def test_index_matches_original_opposite_quote_rules_including_duplicates_and_aliases(self):
        rows = [*self.rows, dict(self.rows[0])]
        indexed = quote_index(iter(rows))
        for row in rows:
            side = {'Over':'Under', 'Under':'Over'}[row['selection']]
            expected = [r['odds'] for r in rows if prop_key(r)==prop_key(row)
                        and r['line']==row['line'] and r['selection']==side]
            self.assertEqual(indexed.get((*prop_key(row),row['line'],side), []), expected)
            self.assertEqual(compare_price(None,row['odds'],expected[0] if len(expected)==1 else None),
                compare_price(None,row['odds'],indexed[(*prop_key(row),row['line'],side)][0]
                              if len(indexed[(*prop_key(row),row['line'],side)])==1 else None))

    def test_joint_methods_resolve_a_missing_player_once_and_keep_every_quote_error(self):
        teams = self.frame.assign(team='BOS',entity_id='BOS',entity_name='BOS')
        teams = pd.concat([teams, teams.assign(team='DET',entity_id='DET',entity_name='DET')],ignore_index=True)
        modules = ['Usage_Opportunity', 'Opponent_Adjusted_Matchup', 'Pace_Game_Environment']
        classes = [UsageOpportunityMarketService, OpponentAdjustedMarketService, PaceGameEnvironmentMarketService]
        for name, cls in zip(modules,classes):
            with self.subTest(method=name):
                repo=Mock(root=self.root/name)
                repo.load.side_effect=lambda *args:(self.frame.copy(),teams.copy())
                module=f'models.Player_Stats.{name}.prediction_service'
                # Preparation/model fits are independent of failed identity resolution.
                with patch(module+'.match_player',side_effect=ValueError('No unique player')) as match, \
                     patch('models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service.feature_rows',return_value=pd.DataFrame()), \
                     patch('models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service.ScheduleAdjustedDefense.fit'), \
                     patch('models.Player_Stats.Pace_Game_Environment.prediction_service.volume_rows',return_value=teams), \
                     patch('models.Player_Stats.Pace_Game_Environment.prediction_service.PaceModel.fit'):
                    result=cls(repo).forecast(self.snapshot)
                    self.assertEqual(match.call_count,1)
                self.assertFalse(result.forecasts)
                self.assertEqual(set(result.errors.values()),{'No unique player'})
                self.assertEqual(set(result.errors),{prop_key(row) for row in self.rows})
                audit=json.loads(next((repo.root/'forecasts'/'nba').glob('*.json')).read_text(encoding='utf-8'))
                self.assertEqual(len(audit['evaluations']),len(self.rows))
                for row, actual in zip(self.rows,audit['evaluations']):
                    side={'Over':'Under','Under':'Over'}[row['selection']]
                    opposite=[r['odds'] for r in self.rows if prop_key(r)==prop_key(row)
                              and r['line']==row['line'] and r['selection']==side]
                    expected=compare_price(None,row['odds'],opposite[0] if len(opposite)==1 else None)
                    self.assertEqual({key:actual[key] for key in expected},expected)

    def test_history_reuse_invalidates_on_csv_database_account_and_log_changes(self):
        api=Mock();api.account_id.return_value='account'
        log=ApiUsageLog(self.root/'API Log'/'usage.csv');log.ensure()
        service=NbaMarketService(api,self.root/'markets.sqlite3',self.root/'markets',log)
        with closing(service._connect()) as db, db:
            # An unrelated malformed payload must never be decoded for this game.
            db.execute("INSERT INTO nba_snapshots VALUES (?,?,?,?,?,?,?)",
                       ('other','account','999','done','2026-01-01','invalid JSON',None))
        service._export_csv(self.snapshot,'account')
        service.history('123')  # Repairs the audit before establishing a revision.
        service.history('123')
        with patch.object(service,'_saved_account',wraps=service._saved_account) as read:
            first=service.history('123');first[0]['rows'][0]['odds']=777
            self.assertEqual(service.history('123')[0]['rows'][0]['odds'],-110)
            read.assert_not_called()
            csv_path=service.csv_directory/self.snapshot['csv_file']
            stamp=csv_path.stat()
            replacement=csv_path.with_suffix('.tmp')
            replacement.write_bytes(csv_path.read_bytes().replace(b',-110,',b',-120,'))
            os.utime(replacement,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
            replacement.replace(csv_path)
            self.assertEqual(service.history('123')[0]['rows'][0]['odds'],-120)
            self.assertEqual(read.call_count,1)
        log.path.unlink()
        service.history('123')
        self.assertEqual(len(log.records('account')),1)
        newer={**self.snapshot,'pulled_at':'2026-03-01T13:00:00Z'}
        with closing(service._connect()) as db, db:
            db.execute("INSERT INTO nba_snapshots VALUES (?,?,?,?,?,?,?)",
                       ('new','account','123','done',newer['pulled_at'],json.dumps(newer),None))
        self.assertEqual(service.history('123')[0]['pulled_at'],newer['pulled_at'])
        api.account_id.return_value='another-account'
        self.assertEqual(service.history('123'),[])
        api.get.assert_not_called()


if __name__ == '__main__':
    main()
