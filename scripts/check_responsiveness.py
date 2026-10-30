"""Exact forecast reuse, independent fits and loading feedback at its output."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from os import utime
import sys
import tempfile
from threading import Event
from time import perf_counter,sleep
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

import pandas as pd
from dash import dcc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from models.Game_Outcome.PPP.PPP import PPPConfig,PPPForecast
from sports_analytics.data import DataSourceError
from sports_analytics.loading import MESSAGES,indicator
from sports_analytics.nba_model_data import NbaModelData,aggregate_team_games
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.page_cache import forecast_revision
from sports_analytics.backtesting_service import source_revisions,implementation_signature,file_revision
import check_mispricing as fixtures
from check_mispricing import components


class ResponsivenessChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.data=Mock(directory=self.root)
        self.data.prepare.return_value=(pd.DataFrame(),[])
        self.model=Mock(config=PPPConfig())
        self.model.predict.side_effect=lambda frame,home,away,*a,**k:PPPForecast(home,away,25,20,10,15)
        self.service=PPPMarketService(self.data,self.model)
        self.snapshot=dict(home_team='Baltimore Ravens',away_team='Cleveland Browns',
            pulled_at='2026-10-21T12:00:00Z',commence_time='2026-10-25T17:00:00Z')

    def test_slow_fit_does_not_block_cached_or_independent_games_and_same_key_fits_once(self):
        expected=asdict(self.service.forecast(self.snapshot))
        slow={**self.snapshot,'pulled_at':'2026-10-21T13:00:00Z'}
        other={**self.snapshot,'pulled_at':'2026-10-21T14:00:00Z'}
        entered,release=Event(),Event()
        def predict(frame,home,away,cutoff,**kwargs):
            if 'T13:' in cutoff:
                entered.set()
                if not release.wait(10):raise AssertionError('Release the blocked fit')
            return PPPForecast(home,away,25,20,10,15)
        self.model.predict.side_effect=predict
        with ThreadPoolExecutor(max_workers=4) as pool:
            blocked=pool.submit(self.service.forecast,slow)
            try:
                self.assertTrue(entered.wait(2))
                duplicate=pool.submit(self.service.forecast,slow)
                start=perf_counter()
                cached=pool.submit(self.service.forecast,self.snapshot).result(timeout=2)
                self.assertEqual(asdict(cached),expected)
                independent=pool.submit(self.service.forecast,other).result(timeout=2)
                self.assertEqual(independent.margin_mean,5)
                self.assertFalse(blocked.done());self.assertFalse(duplicate.done())
                print(f'Cached game plus independent forecast: {1000*(perf_counter()-start):.1f} ms while another fit was blocked.')
            finally:release.set()
            self.assertIs(blocked.result(timeout=2),duplicate.result(timeout=2))
        self.assertEqual(self.model.predict.call_count,3)
        restarted=PPPMarketService(self.data,Mock(config=PPPConfig()))
        with patch.object(restarted.model,'predict',side_effect=AssertionError('Reuse the exact archived forecast')):
            self.assertEqual(asdict(restarted.forecast(self.snapshot)),expected)

    def test_failed_forecast_releases_waiters_and_allows_retry(self):
        self.model.predict.side_effect=DataSourceError('Unavailable source')
        with self.assertRaises(DataSourceError):self.service.forecast(self.snapshot)
        self.assertFalse(self.service._pending)
        self.model.predict.side_effect=lambda *a,**k:PPPForecast('BAL','CLE',25,20,10,15)
        self.assertEqual(self.service.forecast(self.snapshot).home_mean,25)

    def test_backtest_revision_scan_excludes_generated_trees_and_tracks_real_inputs_and_code(self):
        base=self.root/'data/modeling/player_stats'
        for name in ['imports','evaluations','forecasts']:(base/name).mkdir(parents=True)
        source=base/'imports/nfl.csv';source.write_text('pts\n20\n',encoding='utf-8')
        (base/'evaluations/a.json').write_text('{}',encoding='utf-8')
        (base/'forecasts/a.json').write_text('{}',encoding='utf-8')
        revision=source_revisions(self.root)
        self.assertEqual(len(revision),1)
        # Same-size writes can share a timestamp on Windows; advance it explicitly.
        stamp=source.stat()
        source.write_text('pts\n25\n',encoding='utf-8')
        utime(source,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1_000_000_000))
        self.assertNotEqual(revision,source_revisions(self.root))
        code=self.root/'model.py';code.write_text('VERSION=1',encoding='utf-8')
        first=implementation_signature((file_revision(code),))
        stamp=code.stat()
        code.write_text('VERSION=2',encoding='utf-8')
        utime(code,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1_000_000_000))
        self.assertNotEqual(first,implementation_signature((file_revision(code),)))

    def test_nba_preparation_reuses_exact_content_and_rebuilds_revised_or_deleted_files(self):
        frame=pd.DataFrame(dict(game_id=['001','001'],team=['BOS','NYK'],opponent=['NYK','BOS'],
            matchup=['NYK @ BOS']*2,event_date=pd.to_datetime(['2026-10-20']*2,utc=True),
            pts=[110,100],fga=[90,85],fta=[20,20],oreb=[10,10],tov=[12,12]))
        dataset=SimpleNamespace(frame=frame,key='nba-2026',source='fixture',fetched_at='2026-10-21T00:00:00Z',notice='')
        source=Mock()
        def load(query,refresh=False):
            if query.season_type!='regular':raise DataSourceError('No postseason yet')
            return dataset
        source.load.side_effect=load
        data=NbaModelData(self.root/'nba',source)
        with patch('sports_analytics.nba_model_data.aggregate_team_games',wraps=aggregate_team_games) as aggregate:
            first,_=data.prepare([2026]);self.assertEqual(aggregate.call_count,1)
            paths=list(data.directory.glob('*'));revisions=data._revisions(paths)
            first.loc[0,'points']=-999
            second,_=data.prepare([2026])
            self.assertEqual(aggregate.call_count,1)
            self.assertEqual(data._revisions(paths),revisions)
            self.assertEqual(second.points.tolist(),[110,100])
            # Source corrections with an unchanged retrieval timestamp still invalidate.
            dataset.frame=frame.assign(pts=[111,100])
            changed,_=data.prepare([2026],refresh=True)
            self.assertEqual(aggregate.call_count,2);self.assertEqual(changed.points.tolist(),[111,100])
            (data.directory/'team_games_2026_regular.csv').unlink()
            data.prepare([2026]);self.assertEqual(aggregate.call_count,3)
        self.assertTrue(any(call.kwargs.get('refresh') for call in source.load.call_args_list))

    def test_repeated_nba_season_preparation_is_identical_without_reaggregating_or_rewriting(self):
        rows=[]
        for game in range(300):
            for team,opponent,score in [('BOS','NYK',110),('NYK','BOS',100)]:
                rows.append(dict(game_id=str(game),team=team,opponent=opponent,matchup='NYK @ BOS',
                    event_date='2025-11-01T00:00:00Z',pts=score,fga=90,fta=20,oreb=10,tov=12))
        dataset=SimpleNamespace(frame=pd.DataFrame(rows),key='nba',source='fixture',fetched_at='2026-01-01T00:00:00Z',notice='')
        source=Mock()
        def load(query,refresh=False):
            if query.season_type!='regular':raise DataSourceError('No postseason')
            return dataset
        source.load.side_effect=load;data=NbaModelData(self.root/'nba',source)
        start=perf_counter();first,_=data.prepare([2025]);cold=perf_counter()-start
        revisions=data._revisions(list(data.directory.glob('*')))
        with patch('sports_analytics.nba_model_data.aggregate_team_games',side_effect=AssertionError('Do not repeat unchanged aggregation')):
            start=perf_counter();warm,_=data.prepare([2025]);elapsed=perf_counter()-start
        pd.testing.assert_frame_equal(first,warm)
        self.assertEqual(data._revisions(list(data.directory.glob('*'))),revisions)
        print(f'NBA preparation (300 games, identical output): first {1000*cold:.1f} ms; repeat {1000*elapsed:.1f} ms.')

    def fixture(self):
        fixture=fixtures.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def finish_view(self,fixture,category='Game',criteria=None,player_method='WHP',previous=None):
        history=previous
        for _ in range(200):
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-category' if history is None else 'market-model-poll')):
                result=fixture.cb['show_saved_markets'](category,fixture.saved['pulled_at'],'#nfl-game-123',criteria,'PPP',player_method,history,'market',0)
            if result[1] is not None and isinstance(result[1],dict):history=result[1]
            if history and not history.get('pending'):return result
            sleep(.01)
        self.fail('Page calculation did not finish')

    def test_filter_thresholds_and_unrelated_methods_do_not_refit_warm_game_forecasts(self):
        fixture=self.fixture()
        with patch('sports_analytics.dashboard.market_sections',return_value='same view'):
            self.finish_view(fixture)
            before=fixture.game_services['PPP'].forecast.call_count
            self.finish_view(fixture,criteria=dict(probability=.8,edge=.1),player_method='PGE')
            self.assertEqual(fixture.game_services['PPP'].forecast.call_count,before)
            fixture.saved['rows'][-1]['line']=47.5
            self.finish_view(fixture)
            self.assertEqual(fixture.game_services['PPP'].forecast.call_count,before+1)

    def test_joint_player_forecast_is_shared_across_categories_and_displayed_category_is_current(self):
        fixture=self.fixture()
        with patch('sports_analytics.dashboard.market_sections',return_value='same view'):
            self.finish_view(fixture,'Passing',player_method='UO')
            result=self.finish_view(fixture,'Receiving',player_method='UO')
            self.assertEqual(fixture.player_services['UO'].forecast.call_count,1)
            self.assertEqual(result[1]['player']['UO']['category'],'Receiving')
            self.finish_view(fixture,'Passing',player_method='WHP')
            self.finish_view(fixture,'Receiving',player_method='WHP')
            self.assertEqual(fixture.player_services['WHP'].forecast.call_count,2)

    def test_history_edits_invalidate_page_reuse_but_generated_audits_do_not(self):
        fixture=self.fixture();root=self.root/'history';(root/'imports').mkdir(parents=True)
        history=root/'imports/nfl.csv';history.write_text('pts\n20\n',encoding='utf-8')
        source=fixture.player_services['UO']
        source.repository=SimpleNamespace(root=root,data=SimpleNamespace(path=self.root/'sports.sqlite3'))
        before=forecast_revision(source,'NFL')
        with patch('sports_analytics.dashboard.market_sections',return_value='same view'):
            self.finish_view(fixture,'Passing',player_method='UO')
            history.write_text('pts\n25\n',encoding='utf-8')
            self.assertNotEqual(forecast_revision(source,'NFL'),before)
            self.finish_view(fixture,'Receiving',player_method='UO')
            self.assertEqual(source.forecast.call_count,2)
            (root/'evaluations').mkdir();(root/'evaluations/a.json').write_text('{}',encoding='utf-8')
            self.finish_view(fixture,'Passing',player_method='UO')
            self.assertEqual(source.forecast.call_count,2)
        original=forecast_revision(source,'NFL')
        Path(str(source.repository.data.path)+'-wal').write_bytes(b'new history')
        self.assertNotEqual(forecast_revision(source,'NFL'),original)

    def test_cold_input_creation_does_not_restart_a_pending_forecast_on_every_poll(self):
        fixture=self.fixture();root=self.root/'cold';(root/'imports').mkdir(parents=True)
        source=fixture.player_services['UO'];source.repository=SimpleNamespace(root=root)
        forecast=source.forecast.return_value
        def prepare(snapshot):
            (root/'imports/nfl.csv').write_text('pts\n20\n',encoding='utf-8')
            return forecast
        source.forecast.side_effect=prepare
        with patch('sports_analytics.dashboard.market_sections',return_value='same view'):
            self.finish_view(fixture,'Passing',player_method='UO')
        self.assertEqual(source.forecast.call_count,1)

    def test_loading_indicators_are_local_visible_and_follow_the_actual_callback_output(self):
        fixture=self.fixture();nodes=list(components(fixture.app.layout))
        loaders=[node for node in nodes if isinstance(node,dcc.Loading)]
        self.assertGreater(len(loaders),25)
        for loader in loaders:
            self.assertFalse(loader.show_initially)
            self.assertEqual(loader.overlay_style,{'visibility':'visible'})
            self.assertEqual(loader.parent_className,'action-loading')
            self.assertEqual(loader.custom_spinner.role,'status')
            self.assertIn('action-loading-overlay',loader.custom_spinner.className)
            self.assertFalse(getattr(loader,'fullscreen',False))
        for identity in ['kalshi-quick-status','market-detail-pull-status','performance-results','load-status','pnl-import-message']:
            self.assertTrue(any(any(getattr(child,'id',None)==identity for child in components(loader.children)) for loader in loaders),identity)
        identifiers=[node.id for node in nodes if isinstance(getattr(node,'id',None),str)]
        self.assertEqual(len(identifiers),len(set(identifiers)))
        for identity in ['load-status','trend','information-status','game-information-content','kalshi-quote-status','polymarket-quote-status']:
            self.assertIn(identity+'-loading',identifiers)
            callback=fixture.app.callback_map[identity+'-loading.display']
            self.assertNotIn('callback',callback,'Background feedback must not require another HTTP request')
        with fixture.app.server.test_client() as client:
            self.assertEqual(client.get('/_dash-layout').status_code,200)
            self.assertEqual(client.get('/assets/loading.css').status_code,200)
        self.assertIn('kalshi-quick-status',MESSAGES)
        css=(Path(__file__).resolve().parents[1]/'sports_analytics/assets/loading.css').read_text(encoding='utf-8')
        # Dash uses parent_className twice: wrapper -> content -> actual child.
        # A descendant :last-child rule also disables the entire content child.
        self.assertNotIn('.action-loading > div:last-child { pointer-events: none;',css)
        self.assertIn('.action-loading > div:has(> .action-loading-overlay) { pointer-events: none;',css)
        self.assertNotIn('action-loading-overlay',indicator('Inline market progress').className)


if __name__=='__main__':main()
