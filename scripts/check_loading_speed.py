"""Slow-source regressions: local navigation, refresh accuracy and independent reads."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError,Query
from sports_analytics.service import DataService
from sports_analytics.schedule import ScheduleService
from sports_analytics.dashboard import create_app
from sports_analytics.polymarket import GAMMA,PolymarketService
from sports_analytics.kalshi_view import team_aliases,team_identity
from sports_analytics.market_view import NBA_LOGO_FILES,NFL_LOGO_FILES,NCAAF_LOGO_FILES
import check_kalshi as kalshi_fixtures
import check_polymarket as polymarket_fixtures


class LoadingSpeedChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def queue(self,service):
        self.addCleanup(service._refresh.shutdown,wait=True)

    def history(self):
        service=DataService(self.root/'sports.sqlite3');self.queue(service)
        query=Query('NBA',2025,'player')
        frame=pd.DataFrame(dict(entity_id=['1'],entity_name=['Player One'],position=['G'],matchup=['A @ B'],
            team=['A'],opponent=['B'],game_id=['001'],pts=[25.],event_date=pd.to_datetime(['2025-10-20'],utc=True)))
        with patch('sports_analytics.service.fetch',return_value=frame):service.load(query,refresh=True)
        with service.connect() as db:db.execute('UPDATE datasets SET fetched_at=?',('2020-01-01T00:00:00+00:00',))
        return service,query,frame

    def test_history_renders_old_local_data_then_updates_without_waiting_or_repeated_downloads(self):
        service,query,frame=self.history();entered,release=Event(),Event()
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source was not released')
            return frame.assign(pts=40.)
        app=create_app(service=service)
        cb=next(m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__=='load_data')
        with patch('sports_analytics.service.fetch',side_effect=slow) as source:
            try:
                start=perf_counter()
                with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='workspace-page')):
                    first=cb(0,0,'NBA',2025,'player','regular',None,None,'historical',None,'#historical-player')
                elapsed=perf_counter()-start
                self.assertLess(elapsed,1);self.assertTrue(entered.wait(2));self.assertTrue(first[4])
                for _ in range(5):
                    cached=service.load(query,local_first=True)
                    self.assertEqual(cached.frame.pts.iloc[0],25.);self.assertIn('background',cached.notice)
                source.assert_called_once_with(query)
            finally:release.set()
            service._refresh.shutdown(wait=True)
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='historical-cache-poll')):
                second=cb(0,0,'NBA',2025,'player','regular',None,None,'historical',first[0],'#historical-player',1)
        self.assertFalse(second[4]);self.assertNotEqual(first[0]['revision'],second[0]['revision'])
        self.assertEqual(service.read(second[0]['key']).frame.pts.iloc[0],40.)
        print(f'Historical cached page: {1000*elapsed:.1f} ms while source was blocked.')

    def test_first_history_visit_returns_while_download_is_blocked_and_polls_once(self):
        service,query,frame=self.history()
        with service.connect() as db:db.execute('DELETE FROM datasets')
        entered,release=Event(),Event()
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Source was not released')
            return frame
        app=create_app(service=service)
        cb=next(m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__=='load_data')
        def load(previous=None):
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='historical-cache-poll')):
                return cb(0,0,'NBA',2025,'player','regular',None,None,'historical',previous,'#historical-player',1)
        with patch('sports_analytics.service.fetch',side_effect=slow) as source:
            try:
                started=perf_counter();first=load();elapsed=perf_counter()-started
                self.assertLess(elapsed,1)
                self.assertIsNone(first[0]);self.assertTrue(first[4]);self.assertTrue(entered.wait(2))
                for _ in range(3):self.assertTrue(load()[4])
                source.assert_called_once_with(query)
            finally:release.set()
            service._refresh.tasks.join()
            ready=load()
        self.assertFalse(ready[4]);self.assertIsNotNone(ready[0])
        self.assertEqual(service.read(ready[0]['key']).frame.pts.iloc[0],25.)
        print(f'First historical visit: {1000*elapsed:.1f} ms while initial download was blocked.')

    def test_cold_history_source_failure_stops_pending_and_refresh_can_retry(self):
        service=DataService(self.root/'cold.sqlite3');self.queue(service)
        query=Query('NBA',2025,'player')
        with patch('sports_analytics.service.fetch',side_effect=DataSourceError('Source offline')):
            self.assertEqual(service.load(query,local_first=True).status,'loading cache')
            service._refresh.tasks.join()
            with self.assertRaisesRegex(DataSourceError,'Source offline'):service.load(query,local_first=True)
        frame=pd.DataFrame(dict(entity_id=['1'],position=['G'],matchup=['A @ B'],pts=[20]))
        with patch('sports_analytics.service.fetch',return_value=frame):
            self.assertEqual(service.load(query,refresh=True,local_first=True).status,'downloaded')
        self.assertEqual(service.load(query,local_first=True).frame.pts.iloc[0],20)

    def test_refresh_failure_preserves_original_timestamp_backs_off_and_manual_refresh_bypasses_it(self):
        service,query,frame=self.history()
        with patch('sports_analytics.service.fetch',side_effect=DataSourceError('Offline')) as source:
            service.load(query,local_first=True)
            service._refresh.shutdown(wait=True)
            for _ in range(4):
                result=service.load(query,local_first=True)
                self.assertEqual(result.status,'stale cache');self.assertIn('Offline',result.notice)
                self.assertEqual(result.fetched_at,'2020-01-01T00:00:00+00:00')
            self.assertEqual(source.call_count,1)
        with patch('sports_analytics.service.fetch',return_value=frame.assign(pts=33.)) as source:
            refreshed=service.load(query,refresh=True,local_first=True)
        source.assert_called_once_with(query);self.assertEqual(refreshed.frame.pts.iloc[0],33.)

    def test_kalshi_cached_quotes_return_immediately_and_refresh_once_without_changing_timestamps(self):
        fixture=kalshi_fixtures.KalshiChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        service=fixture.service;self.queue(service)
        first=service.quotes(fixture.game);fixture.clock+=timedelta(seconds=3);service.background=True
        entered,release=Event(),Event();original=fixture.client.json.side_effect
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('Kalshi source was not released')
            result=original(*args,**kwargs)
            for event in result.get('events',[]):
                for row in event.get('markets',[]):row['yes_ask_dollars']='0.65'
            return result
        fixture.client.reset_mock();fixture.client.json.side_effect=slow
        try:
            start=perf_counter();cached=service.quotes(fixture.game);elapsed=perf_counter()-start
            self.assertLess(elapsed,1);self.assertTrue(entered.wait(2))
            self.assertEqual(cached['rows'],first['rows']);self.assertEqual(cached['fetched_at'],first['fetched_at'])
            self.assertTrue(cached['updating'])
            self.assertIn('background',' '.join(cached['notices']))
            for _ in range(4):service.quotes(fixture.game)
            self.assertEqual(fixture.client.json.call_count,1)
        finally:release.set()
        service._refresh.shutdown(wait=True)
        updated=service.quotes(fixture.game)
        self.assertEqual(updated['rows'][0]['yes_ask'],.65);self.assertFalse(updated['updating'])
        print(f'Kalshi cached quotes: {1000*elapsed:.1f} ms while source was blocked.')

    def test_polymarket_reuses_discovery_after_clock_changes_and_process_restart(self):
        fixture=polymarket_fixtures.PolymarketChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        service=fixture.service;self.queue(service)
        first=service.games('NBA');gets=fixture.client.json.call_count
        fixture.clock+=timedelta(seconds=30)
        self.assertEqual(service.games('NBA')['games'],first['games'])
        self.assertEqual(fixture.client.json.call_count,gets)
        restarted=PolymarketService(service.directory,service.archive,fixture.client,now=lambda:fixture.clock)
        self.queue(restarted);fixture.client.reset_mock()
        self.assertEqual(restarted.games('NBA')['games'],first['games']);fixture.client.json.assert_not_called()
        # Broad cached request bounds never include games outside the exact window.
        service.games('NBA',refresh=True);self.assertTrue(fixture.client.json.called)

    def test_polymarket_quotes_are_local_first_and_unrelated_requests_do_not_share_http_lock(self):
        fixture=polymarket_fixtures.PolymarketChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        service=fixture.service;self.queue(service);game=fixture.game()
        first=service.quotes(game);fixture.clock+=timedelta(seconds=3);service.background=True
        entered,release=Event(),Event();original=fixture.client.session.post.side_effect
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('Polymarket books were not released')
            return original(*args,**kwargs)
        fixture.client.session.post.reset_mock();fixture.client.session.post.side_effect=slow
        try:
            start=perf_counter();cached=service.quotes(game);elapsed=perf_counter()-start
            self.assertLess(elapsed,1);self.assertTrue(entered.wait(2));self.assertEqual(cached['fetched_at'],first['fetched_at'])
            self.assertTrue(cached['updating'])
            self.assertIn('background',' '.join(cached['notices']))
            with ThreadPoolExecutor(max_workers=1) as pool:
                other=pool.submit(service._request,GAMMA+'/sports',{'independent':True})
                self.assertIsInstance(other.result(timeout=1)['data'],list)
            for _ in range(3):service.quotes(game)
            self.assertEqual(fixture.client.session.post.call_count,1)
        finally:release.set()
        service._refresh.shutdown(wait=True)
        updated=service.quotes(game)
        self.assertGreater(updated['fetched_at'],first['fetched_at']);self.assertFalse(updated['updating'])
        print(f'Polymarket cached quotes: {1000*elapsed:.1f} ms while source was blocked.')

    def test_saved_schedule_appears_before_background_daily_updates_finish(self):
        service=ScheduleService(self.root/'schedule.sqlite3');self.queue(service)
        now=datetime(2026,10,24,20,tzinfo=timezone.utc)
        with patch('sports_analytics.schedule.HttpClient.json',return_value={'events':[]}):service.load('NFL',7,now=now)
        service.background=True;now+=timedelta(minutes=6);entered,release=Event(),Event()
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('Schedule source was not released')
            return {'events':[]}
        with patch('sports_analytics.schedule.HttpClient.json',side_effect=slow):
            try:
                start=perf_counter();result=service.load('NFL',7,now=now);elapsed=perf_counter()-start
                self.assertLess(elapsed,1);self.assertTrue(entered.wait(2));self.assertTrue(result.updating)
            finally:release.set()
            service._refresh.shutdown(wait=True)
        with patch('sports_analytics.schedule.HttpClient.json',side_effect=AssertionError('Use updated local dates')):
            self.assertFalse(service.load('NFL',7,now=now).updating)
        print(f'Cached weekly schedule: {1000*elapsed:.1f} ms while source was blocked.')

    def test_indexed_team_aliases_preserve_canonical_names_and_ambiguous_or_unknown_teams(self):
        for sport,files in [('NBA',NBA_LOGO_FILES),('NFL',NFL_LOGO_FILES),('NCAAF',NCAAF_LOGO_FILES)]:
            index=team_aliases(sport)
            for filename in set(files.values()):
                name=Path(filename).stem
                self.assertEqual(team_identity(name,sport)['name'],name)
            team_identity.cache_clear()
            self.assertIs(team_aliases(sport),index)
        for name,sport in [('Los Angeles','NBA'),('Tigers','NCAAF'),('Unknown Club','NFL')]:
            self.assertEqual(team_identity(name,sport)['name'],name)


if __name__=='__main__':main()
