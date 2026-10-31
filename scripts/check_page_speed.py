"""Navigation stays responsive during cold quote reads and slow forecasts."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
from time import perf_counter,sleep
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dash import Dash,no_update
from dash._utils import to_json
from dash.exceptions import PreventUpdate
from models.Game_Outcome.PPP.PPP import PPPForecast
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.market_view import market_sections
from sports_analytics.page_cache import PageTasks,QuotePages
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi_callbacks import register
from sports_analytics.market_summary_callbacks import register as register_summary
import check_mispricing as fixtures
import check_market_summary as summary_fixtures
from check_kalshi import NOW,GAME


class PageSpeedChecks(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=NOW
        self.game=dict(sport='NFL',id=GAME,title='Pittsburgh at New Orleans',start=NOW.isoformat())
        self.snapshot=dict(sport='NFL',game_id=GAME,category='Game Lines',rows=[dict(ticker='TEST',yes_ask=.6)],
            fetched_at=NOW.isoformat(),notices=[])
        self.source=Mock(directory=self.root/'source',background=True)
        self.source.quotes.return_value=self.snapshot
        self.source.history.return_value=[]

    def cache(self):
        cache=QuotePages(self.source,self.root/'views',now=lambda:self.clock)
        self.addCleanup(cache.tasks.queue.shutdown)
        return cache

    def test_cold_exchange_quotes_return_before_a_blocked_source_and_cache_exact_results(self):
        cache=self.cache();entered,release=Event(),Event();self.addCleanup(release.set)
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source never released')
            return deepcopy(self.snapshot)
        self.source.quotes.side_effect=slow
        start=perf_counter();first=cache.read(self.game,'Game Lines');elapsed=perf_counter()-start
        self.assertLess(elapsed,1);self.assertTrue(entered.wait(2))
        self.assertTrue(first['updating']);self.assertFalse(first['rows'])
        for _ in range(5):self.assertTrue(cache.read(self.game,'Game Lines')['updating'])
        self.source.quotes.assert_called_once_with(self.game,'Game Lines')
        release.set();cache.tasks.queue.tasks.join()
        result=cache.read(self.game,'Game Lines')
        self.assertEqual(result['rows'],self.snapshot['rows']);self.assertEqual(result['fetched_at'],self.snapshot['fetched_at'])
        self.assertFalse(result['updating']);self.assertEqual(len(list((self.root/'views').glob('*.json'))),1)
        print(f'Cold game navigation: {1000*elapsed:.1f} ms while the quote source was blocked.')

    def test_restarted_pages_use_local_quotes_while_source_refreshes(self):
        cache=self.cache();cache.remember(self.game,'Game Lines',deepcopy(self.snapshot))
        self.clock+=timedelta(minutes=5)
        restarted=self.cache();entered,release=Event(),Event();self.addCleanup(release.set)
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source never released')
            return {**self.snapshot,'fetched_at':self.clock.isoformat(),'rows':[dict(ticker='TEST',yes_ask=.7)]}
        self.source.quotes.side_effect=slow
        start=perf_counter();first=restarted.read(self.game,'Game Lines');elapsed=perf_counter()-start
        self.assertLess(elapsed,1);self.assertTrue(entered.wait(2))
        self.assertEqual(first['fetched_at'],self.snapshot['fetched_at']);self.assertEqual(first['rows'],self.snapshot['rows'])
        first['rows'][0]['yes_ask']=.1
        self.assertEqual(restarted.read(self.game,'Game Lines')['rows'][0]['yes_ask'],.6)
        release.set();restarted.tasks.queue.tasks.join()
        self.assertEqual(restarted.read(self.game,'Game Lines')['rows'][0]['yes_ask'],.7)
        print(f'Restarted game cached view: {1000*elapsed:.1f} ms while refresh was blocked.')

    def test_manual_pull_cannot_be_overwritten_by_an_older_background_read(self):
        cache=self.cache();cache.remember(self.game,'Game Lines',deepcopy(self.snapshot));self.clock+=timedelta(seconds=3)
        entered,release=Event(),Event();self.addCleanup(release.set)
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source never released')
            return deepcopy(self.snapshot)
        self.source.quotes.side_effect=slow
        cache.read(self.game,'Game Lines');self.assertTrue(entered.wait(2))
        manual={**self.snapshot,'fetched_at':self.clock.isoformat(),'rows':[dict(ticker='TEST',yes_ask=.8)]}
        cache.remember(self.game,'Game Lines',manual);release.set();cache.tasks.queue.tasks.join()
        self.assertEqual(cache.read(self.game,'Game Lines')['rows'],manual['rows'])
        self.assertEqual(self.cache().read(self.game,'Game Lines')['rows'],manual['rows'])

    def test_failed_refresh_retains_original_timestamp_and_backs_off(self):
        cache=self.cache();cache.remember(self.game,'Game Lines',deepcopy(self.snapshot));self.clock+=timedelta(seconds=3)
        self.source.quotes.side_effect=DataSourceError('Offline')
        cache.read(self.game,'Game Lines');cache.tasks.queue.tasks.join()
        for _ in range(4):
            result=cache.read(self.game,'Game Lines')
            self.assertFalse(result['updating']);self.assertIn('Offline',' '.join(result['notices']))
            self.assertEqual(result['fetched_at'],self.snapshot['fetched_at'])
        self.source.quotes.assert_called_once()

    def test_separate_games_complete_independently_and_forecasts_are_not_changed(self):
        tasks=PageTasks();self.addCleanup(tasks.queue.shutdown)
        entered,release=Event(),Event();self.addCleanup(release.set)
        def slow():
            entered.set()
            if not release.wait(5):raise AssertionError('Forecast never released')
            return ('unchanged A',.7654321)
        self.assertTrue(tasks.read('A',slow)[1]);self.assertTrue(entered.wait(2))
        expected=('unchanged B',.87654321)
        tasks.read('B',lambda:expected)
        deadline=perf_counter()+2
        while tasks.peek('B') is None and perf_counter()<deadline:sleep(.01)
        self.assertEqual(tasks.read('B',lambda:None)[0],expected)
        release.set();tasks.queue.tasks.join()
        self.assertEqual(tasks.read('A',lambda:None)[0],('unchanged A',.7654321))

    def test_slow_disk_write_does_not_lock_navigation_or_other_games(self):
        tasks=PageTasks();self.addCleanup(tasks.queue.shutdown)
        entered,release=Event(),Event();self.addCleanup(release.set)
        def write(value):
            entered.set()
            if not release.wait(5):raise AssertionError('Disk write never released')
        tasks.read('A',lambda:'ready quotes',publish=write)
        self.assertTrue(entered.wait(2))
        start=perf_counter();value,pending,_=tasks.read('A',lambda:None)
        self.assertLess(perf_counter()-start,1);self.assertEqual(value,'ready quotes')
        self.assertTrue(pending)
        tasks.read('B',lambda:'other game')
        deadline=perf_counter()+2
        while tasks.peek('B') is None and perf_counter()<deadline:sleep(.01)
        self.assertEqual(tasks.peek('B'),'other game')
        release.set();tasks.queue.tasks.join()

    def test_paused_exchange_finishes_initial_load_and_manual_pull_is_still_explicit(self):
        cache=self.cache();app=Dash(__name__,suppress_callback_exceptions=True)
        register(app,self.source,pages=cache)
        callback=next(m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback')
                      and m['callback'].__wrapped__.__name__=='kalshi_quotes')
        entered,release=Event(),Event();self.addCleanup(release.set)
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source never released')
            return deepcopy(self.snapshot)
        self.source.quotes.side_effect=slow
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-context')):
            first=callback('kalshi','NFL',self.game,'Game Lines','live',0,0,0,'#nfl-kalshi-game-'+GAME)
        self.assertTrue(entered.wait(2));self.assertTrue(first[0]['page_pending'])
        release.set();cache.tasks.queue.tasks.join()
        state=dict(game=GAME,category='Game Lines',updating=True,page_pending=True)
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-cache-poll')):
            final=callback('kalshi','NFL',self.game,'Game Lines','live',0,0,0,'#nfl-kalshi-game-'+GAME,1,state)
        self.assertEqual(final[0]['rows'],self.snapshot['rows']);self.assertFalse(final[0]['page_pending'])
        self.source.quotes.side_effect=None
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-pull')):
            callback('kalshi','NFL',self.game,'Game Lines','live',0,1,0,'#nfl-kalshi-game-'+GAME)
        self.source.quotes.assert_called_with(self.game,'Game Lines',refresh=True,save=True)

    def test_summary_quotes_do_not_wait_for_cold_exchange_pages_or_fit_incomplete_bundles(self):
        bundle=summary_fixtures.bundle();game=bundle['game'];entered,release=Event(),Event();self.addCleanup(release.set)
        catalog=dict(sport='NFL',games=[game])
        services={};pages={}
        for provider in ('Kalshi','Polymarket'):
            saved=bundle['providers'][provider]
            catalog[provider]=dict(games=[saved['game']])
            service=Mock();services[provider]=service
            snapshot={**saved['snapshot'],'category':'All Markets'}
            def load(game,snapshot=snapshot,provider=provider):
                if provider=='Kalshi':
                    entered.set()
                    if not release.wait(5):raise AssertionError('Summary source never released')
                return deepcopy(snapshot)
            service.all_quotes.side_effect=load
            pages[provider]=QuotePages(service,self.root/provider,now=lambda:self.clock)
            self.addCleanup(pages[provider].tasks.queue.shutdown)
        # Cleanup is LIFO: release the blocked source before joining workers.
        self.addCleanup(release.set)
        markets=Mock();markets.latest.return_value={game['id']:bundle['providers']['FanDuel']['snapshot']}
        game_fit=Mock();player_fit=Mock();app=Dash(__name__,suppress_callback_exceptions=True)
        register_summary(app,Mock(),{'NFL':markets},services['Kalshi'],services['Polymarket'],game_fit,player_fit,pages=pages)
        cb=callback_functions(app)
        start=perf_counter()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-game')):
            first,_=cb['summary_quotes'](game['id'],catalog,0,'market-summary','NFL',None)
        self.assertLess(perf_counter()-start,1);self.assertTrue(entered.wait(2));self.assertTrue(first['updating'])
        with self.assertRaises(PreventUpdate):
            cb['summary_models'](first,'PPP','WHP',0,'market-summary','NFL',game['id'],None)
        game_fit.assert_not_called();player_fit.assert_not_called()
        release.set()
        for page in pages.values():page.tasks.queue.tasks.join()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            final,_=cb['summary_quotes'](game['id'],catalog,0,'market-summary','NFL',first,1)
        self.assertFalse(final['updating'])
        self.assertEqual(final['providers']['Kalshi']['snapshot']['rows'],bundle['providers']['Kalshi']['snapshot']['rows'])

    def test_fanduel_http_quotes_appear_before_slow_forecast_and_poll_does_not_rebuild_while_waiting(self):
        fixture=fixtures.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        entered,release,finished=Event(),Event(),Event();self.addCleanup(release.set)
        forecast=PPPForecast('BAL','CLE',30,20,12,10,diagnostics=dict(last_game_date='2026-10-18',as_of=fixture.saved['pulled_at']))
        def slow(snapshot):
            entered.set()
            if not release.wait(5):raise AssertionError('Forecast never released')
            finished.set();return forecast
        fixture.game_services['PPP'].forecast.side_effect=slow
        key,meta=next((key,m) for key,m in fixture.app.callback_map.items() if m.get('callback')
                      and m['callback'].__wrapped__.__name__=='show_saved_markets')
        values={'market-category':'Game','market-snapshot':fixture.saved['pulled_at'],'market-url':'#nfl-game-123',
            'market-applied-criteria':None,'market-forecast-method':'PPP','player-forecast-method':'WHP',
            'market-forecast-history':None,'workspace-page':'market','market-model-poll':0}
        client=fixture.app.server.test_client()
        # Browser navigation follows initial layout loading. Measure the game
        # callback separately from Dash's one-time server/asset initialization.
        self.assertEqual(client.get('/_dash-layout').status_code,200)
        start=perf_counter()
        response=client.post('/_dash-update-component',json=dict(output=key,
            outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
            inputs=[dict(item,value=wire_value(item,values)) for item in meta['inputs']],
            state=[dict(item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=['market-url.hash']))
        elapsed=perf_counter()-start
        self.assertEqual(response.status_code,200,response.get_data(as_text=True));self.assertLess(elapsed,1)
        self.assertTrue(entered.wait(2));payload=response.get_json()['response']
        self.assertIn('Calculating',json.dumps(payload));self.assertNotIn('dash_core_components.Graph',json.dumps(payload))
        previous=payload['market-forecast-history']['data'];self.assertTrue(previous['pending'])
        cb=fixture.cb['show_saved_markets']
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-model-poll')), \
                patch.object(fixture.markets,'history',side_effect=AssertionError('A pending poll needs no disk reads')), \
                patch('sports_analytics.dashboard.forecast_revision',side_effect=AssertionError('A pending poll needs no file scans')):
            self.assertEqual(cb('Game',fixture.saved['pulled_at'],'#nfl-game-123',None,'PPP','WHP',previous,'market',1),(no_update,no_update))
        release.set();self.assertTrue(finished.wait(2));deadline=perf_counter()+3
        while perf_counter()<deadline:
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-model-poll')):
                result=cb('Game',fixture.saved['pulled_at'],'#nfl-game-123',None,'PPP','WHP',previous,'market',2)
            if result[1] is not no_update:break
            sleep(.01)
        self.assertFalse(result[1]['pending'])
        from check_exchange_updates import normalized
        self.assertEqual(normalized(result[0]),normalized(market_sections(fixture.saved,'Game',forecast,method='PPP')))
        fixture.game_services['PPP'].forecast.assert_called_once()
        print(f'FanDuel game HTTP render: {1000*elapsed:.1f} ms while the forecast was blocked.')


if __name__=='__main__':main()
