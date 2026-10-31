"""Offline cache, lazy-row, forecast identity and asynchronous scan contracts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Event,Lock
from time import perf_counter
from unittest import TestCase,main
from unittest.mock import patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash._utils import to_json
from models.replay import input_digest
from scripts.callback_fixtures import wire_value
from sports_analytics.frame_cache import FrameCache
from sports_analytics.lazy_markets import LazyMarkets,markets
from sports_analytics.market_view import market_sections
from sports_analytics.filter_jobs import FilterJobs
from sports_analytics.mispricing import filter_context,criteria_values,FILTER_CATEGORY,run_forecasts
from check_mispricing import components,finish_filter
import check_mispricing as fanduel_fixtures
import check_kalshi_analysis as kalshi_fixtures
from check_kalshi_analysis import FILTER,view


def post(app,name,values,trigger):
    key,meta=next((key,meta) for key,meta in app.callback_map.items()
                 if meta.get('callback') and meta['callback'].__wrapped__.__name__==name)
    outputs=meta['output']
    wire=([dict(id=o.component_id,property=o.component_property) for o in outputs] if isinstance(outputs,list)
          else dict(id=outputs.component_id,property=outputs.component_property))
    def value(item):return [] if item['id'].startswith('{') else wire_value(item,values)
    response=app.server.test_client().post('/_dash-update-component',json=dict(output=key,outputs=wire,
        inputs=[dict(**item,value=value(item)) for item in meta['inputs']],
        state=[dict(**item,value=value(item)) for item in meta['state']],changedPropIds=[trigger]))
    if response.status_code not in {200,204}:raise AssertionError(response.get_data(as_text=True))
    return response.get_json() if response.status_code==200 else {}


class CacheChecks(TestCase):
    def test_read_returns_copies_without_invalidating_derived_cache(self):
        import check_performance as fixtures
        fixture=fixtures.PerformanceChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        first=fixture.service.read(fixture.key);first.frame.loc[0,'pts']=-100
        self.assertEqual(fixture.service.read(fixture.key).frame.loc[0,'pts'],20.)

    def test_byte_and_count_bounds_oversized_bypass_and_clear(self):
        decode=lambda payload:pd.DataFrame({'value':[payload.decode()]*10})
        cache=FrameCache(decode,maxsize=2,maxbytes=2000)
        for payload in [b'a',b'b',b'c']:cache(payload)
        self.assertLessEqual(cache.cache_info().currsize,2)
        self.assertLessEqual(cache.cache_info().bytes,2000)
        self.assertEqual(cache(b'x'*1000).iloc[0,0],'x'*1000)
        self.assertNotIn(b'x'*1000,cache.values)
        cache.cache_clear();self.assertEqual(cache.cache_info().bytes,0)
        self.assertEqual(cache.cache_info().currsize,0)

    def test_same_payload_singleflight_and_unrelated_payloads_overlap(self):
        entered=Event();other=Event();release=Event();calls=[];guard=Lock()
        def decode(payload):
            with guard:calls.append(payload)
            (entered if payload==b'a' else other).set()
            if not release.wait(3):raise AssertionError('Decode was not released')
            return pd.DataFrame({'value':[payload]})
        cache=FrameCache(decode)
        with ThreadPoolExecutor(max_workers=3) as pool:
            try:
                first=pool.submit(cache,b'a');self.assertTrue(entered.wait(1))
                duplicate=pool.submit(cache,b'a');different=pool.submit(cache,b'b')
                self.assertTrue(other.wait(1))
            finally:release.set()
            self.assertIs(first.result(),duplicate.result());different.result()
        self.assertEqual(calls.count(b'a'),1);self.assertEqual(calls.count(b'b'),1)

    def test_decode_failure_is_retryable_and_clear_blocks_old_publication(self):
        entered=Event();release=Event()
        def decode(payload):
            entered.set();release.wait(2)
            return pd.DataFrame({'a':[1]})
        cache=FrameCache(decode)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(cache,b'a');self.assertTrue(entered.wait(1))
            cache.cache_clear();release.set();future.result()
        self.assertEqual(cache.cache_info().currsize,0)
        bad=FrameCache(lambda payload:json.loads(payload))
        for _ in range(2):
            with self.assertRaises(ValueError):bad(b'invalid')
        self.assertEqual(bad.cache_info().misses,2)

    def test_digest_tracks_schema_index_order_nanos_and_supports_objects(self):
        frame=pd.DataFrame(dict(a=[1.,2.],date=pd.to_datetime(['2026-01-01','2026-01-02'],utc=True)))
        key=input_digest(frame)
        self.assertEqual(len(key),64)
        self.assertEqual(key,input_digest(frame.copy()))
        for changed in [frame.iloc[::-1],frame[['date','a']],frame.set_axis([5,6]),frame.astype({'a':'float32'}),
                        frame.assign(a=[1.,3.]),frame.assign(date=frame.date+pd.Timedelta(nanoseconds=1))]:
            self.assertNotEqual(key,input_digest(changed))
        with patch.object(pd.DataFrame,'to_json',side_effect=AssertionError('No full-table JSON on the normal path')):
            self.assertEqual(key,input_digest(frame))
        objects=pd.DataFrame({'a':[{'nested':[1,2]}]})
        self.assertEqual(input_digest(objects),input_digest(objects.copy()))
        self.assertNotEqual(input_digest(objects),input_digest(pd.DataFrame({'a':[{'nested':[1,3]}]})))


class LazyRowChecks(TestCase):
    def snapshot(self):
        return dict(sport='NBA',game_id='123',home_team='Boston Celtics',away_team='Detroit Pistons',
            commence_time='2026-11-01T12:00:00Z',pulled_at='2026-10-30T12:00:00Z',
            rows=[dict(subject=f'Player {player}',group='Points',market='Points',market_key='player_points_alternate',
                       selection='Over',line=float(line),odds=100) for player in range(8) for line in range(140)])

    def test_initial_payload_reduction_headers_and_all_rows_reachable(self):
        snapshot=self.snapshot()
        eager=market_sections(snapshot,'Points',method='WHP',loading=True,lazy=False)
        lazy=market_sections(snapshot,'Points',method='WHP',loading=True)
        self.assertLess(len(to_json(lazy)),len(to_json(eager))*.25)
        headers=[c for c in components(lazy) if c.__class__.__name__=='Details']
        self.assertEqual(len(headers),8)
        rendered=[c for c in components(lazy) if getattr(c,'className','').startswith('market-outcome ')]
        self.assertEqual(len(rendered),12)
        bodies=[c for c in components(lazy) if isinstance(getattr(c,'id',None),dict) and c.id['type']=='lazy-market-body']
        self.assertEqual(getattr(bodies[2],'data-ready'),'false')
        for pages,count in [(0,6),(1,56),(2,106),(3,140)]:
            body,_,style,_=markets.read(bodies[2].id,pages)
            rows=[c for c in components(body) if getattr(c,'className','').startswith('market-outcome ')]
            self.assertEqual(len(rows),count)
            self.assertIn('Player 2',to_json(body));self.assertNotIn('Player 7',to_json(body))
            self.assertEqual(style,{'display':'none'} if count==140 else {})

    def test_expired_scope_does_not_load_another_games_rows(self):
        cache=LazyMarkets(capacity=1);old=cache.view()
        cache.panel(old,0,lambda count:'Old game',10,initial=False)
        new=cache.view();cache.panel(new,0,lambda count:'New game',10)
        expired=cache.read(dict(view=old,section=0),0)
        self.assertIn('expired',str(expired));self.assertNotIn('New game',str(expired))

    def test_live_source_replacement_wins_and_late_clicks_cannot_shrink_rows(self):
        cache=LazyMarkets();key=cache.view();entered=Event();release=Event()
        def old(count):
            entered.set();release.wait(2)
            return 'Old price'
        cache.panel(key,0,old,140,initial=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(cache.read,dict(view=key,section=0),2)
            self.assertTrue(entered.wait(1))
            cache.update(key,0,lambda count:f'Current price / {count} rows')
            release.set();value=future.result()
        self.assertEqual(value[0],'Current price / 106 rows')
        self.assertEqual(cache.read(dict(view=key,section=0),0)[0],'Current price / 106 rows')

    def test_snapshot_and_method_changes_create_a_new_exchange_scope(self):
        from check_kalshi_analysis import snapshot,game
        from sports_analytics.exchange_render import render
        original={**snapshot(),'game':game(),'view_scope':['live','PPP','WHP']}
        first=render(original)
        for scope in [['archive.json','PPP','WHP'],['live','EPA','WHP']]:
            changed=render({**original,'view_scope':scope},first[-1])
            self.assertNotEqual(changed[-1]['lazy_view'],first[-1]['lazy_view'])
            self.assertIsNot(changed[3],no_update)


class AsyncScanChecks(TestCase):
    def fixture(self,kind=fanduel_fixtures.MispricingChecks):
        fixture=kind();fixture.setUp();self.addCleanup(fixture.doCleanups);return fixture

    def test_fanduel_http_submit_is_nonblocking_and_results_remain_scoped(self):
        f=self.fixture();entered=Event();release=Event();self.addCleanup(release.set)
        original=f.game_services['PPP'].forecast.return_value
        def slow(snapshot):
            entered.set()
            if not release.wait(4):raise AssertionError('Fit was not released')
            return original
        f.game_services['PPP'].forecast.side_effect=slow
        location='#nfl-game-123';selection=f.saved['pulled_at']
        request=filter_context(location,selection,['PPP'],['WHP'],criteria_values(75,5))
        values={'mispricing-request':request,'market-category':FILTER_CATEGORY,'market-url':location,
            'market-snapshot':selection,'market-quick-game-methods':['PPP'],'market-quick-player-methods':['WHP'],
            'market-quick-probability':75,'market-quick-edge':5,'workspace-page':'market'}
        start=perf_counter();reply=post(f.app,'quick_filter_markets',values,'mispricing-request.data')
        self.assertLess(perf_counter()-start,1);self.assertTrue(entered.wait(1))
        history=reply['response']['mispricing-forecast-history']['data']
        self.assertTrue(history['pending']);self.assertLess(len(json.dumps(history)),3000)
        self.assertNotIn('snapshot',history);values['mispricing-forecast-history']=history
        values['mispricing-request']={**request,'sequence':2}
        duplicate=post(f.app,'quick_filter_markets',values,'mispricing-request.data')
        self.assertEqual(duplicate['response']['mispricing-forecast-history']['data']['job'],history['job'])
        values['mispricing-forecast-history']=duplicate['response']['mispricing-forecast-history']['data']
        f.game_services['PPP'].forecast.assert_called_once()
        values['market-quick-probability']=80
        changed=post(f.app,'quick_filter_markets',values,'market-quick-probability.value')
        self.assertFalse(changed['response']['mispricing-forecast-history']['data']['pending'])
        self.assertIn('Click Quick Filtering',str(changed))
        values['market-quick-probability']=75
        values['market-url']='#nfl-game-456'
        stale=post(f.app,'quick_filter_markets',values,'market-url.hash')
        self.assertIn('Click Quick Filtering',str(stale));self.assertNotIn('Top 3 by edge',str(stale))
        release.set();f.app._filter_jobs.tasks.queue.tasks.join()
        values['market-url']=location
        finished=post(f.app,'quick_filter_markets',values,'market-quick-poll.n_intervals')
        self.assertIn('Top 3 by edge',str(finished))
        self.assertFalse(finished['response']['mispricing-forecast-history']['data'].get('pending',False))
        self.assertEqual(finished['response']['mispricing-forecast-history']['data']['filter_key']['sequence'],2)
        values['mispricing-request']=filter_context(location,selection,['PPP'],['WHP'],criteria_values(80,5))
        values['market-quick-probability']=80
        post(f.app,'quick_filter_markets',values,'mispricing-request.data')
        f.app._filter_jobs.tasks.queue.tasks.join()
        f.game_services['PPP'].forecast.assert_called_once()
        f.player_services['WHP'].forecast.assert_called_once()
        f.player_services['WHP'].config={'lookback':99}
        post(f.app,'quick_filter_markets',values,'mispricing-request.data')
        f.app._filter_jobs.tasks.queue.tasks.join()
        self.assertEqual(f.player_services['WHP'].forecast.call_count,2)

    def test_kalshi_quote_fetch_is_background_and_scan_store_contains_no_quotes(self):
        f=self.fixture(kalshi_fixtures.KalshiFilterCallbacks);entered=Event();release=Event();self.addCleanup(release.set)
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(4):raise AssertionError('Quotes were not released')
            return deepcopy(f.snapshot)
        f.exchange.all_quotes.side_effect=slow
        start=perf_counter();scan,_=f.cb['kalshi_quick_scan'](1,0,-100,['PPP'],['WHP'],f.game,FILTER,'live','kalshi',view.href('NFL',f.game['id']))
        self.assertLess(perf_counter()-start,1);self.assertTrue(entered.wait(1))
        self.assertNotIn('snapshot',scan);self.assertNotIn('ranked',scan);self.assertLess(len(json.dumps(scan)),3000)
        values={'kalshi-quick-scan':scan,'kalshi-context':f.game,'kalshi-quick-game-methods':['PPP'],
            'kalshi-quick-player-methods':['WHP'],'kalshi-quick-probability':1,'kalshi-quick-edge':-100,
            'kalshi-snapshot':'live','kalshi-category':FILTER,'workspace-page':'kalshi'}
        changed=post(f.app,'kalshi_quick_results',values,'kalshi-quick-probability.value')
        self.assertFalse(changed['sideUpdate']['kalshi-quick-filter']['disabled'])
        self.assertTrue(changed['sideUpdate']['kalshi-quick-poll']['disabled'])
        release.set();completed=finish_filter(f.app,scan)
        self.assertIn('ranked',completed);self.assertTrue(completed['ranked']['matches'])

    def test_two_filter_jobs_overlap_and_method_pool_remains_bounded(self):
        jobs=FilterJobs();self.addCleanup(jobs.tasks.queue.shutdown)
        entered=[Event(),Event()];release=Event();self.addCleanup(release.set)
        def work(index):
            entered[index].set()
            if not release.wait(3):raise AssertionError('Jobs did not overlap')
            return index
        jobs.tasks.read('a',lambda:work(0));jobs.tasks.read('b',lambda:work(1))
        self.assertTrue(all(event.wait(1) for event in entered));release.set();jobs.tasks.queue.tasks.join()
        self.assertEqual(jobs.tasks.peek('a'),0);self.assertEqual(jobs.tasks.peek('b'),1)
        guard=Lock();active=maximum=0;ready=Event();finish=Event();self.addCleanup(finish.set)
        def method():
            nonlocal active,maximum
            with guard:
                active+=1;maximum=max(maximum,active)
                if active==2:ready.set()
            finish.wait(2)
            with guard:active-=1
            return 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(run_forecasts,{'a':method,'b':method},'NBA')
            b=pool.submit(run_forecasts,{'c':method,'d':method},'NFL')
            self.assertTrue(ready.wait(1));finish.set();a.result();b.result()
        self.assertEqual(maximum,2)

    def test_failed_background_work_can_be_retried_explicitly(self):
        jobs=FilterJobs();self.addCleanup(jobs.tasks.queue.shutdown)
        def fail():raise ValueError('Missing source')
        jobs.tasks.read('same-request',fail);jobs.tasks.queue.tasks.join()
        self.assertIn('Missing source',jobs.tasks.queue.state('same-request')['error'])
        jobs.tasks.read('same-request',lambda:'Recovered',refresh=True,force=True)
        jobs.tasks.queue.tasks.join()
        self.assertEqual(jobs.tasks.peek('same-request'),'Recovered')


if __name__=='__main__':main()
