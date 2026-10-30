"""Offline quota failover, account attribution, combined balances and secret isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError
from sports_analytics.odds_api import OddsApiService
from sports_analytics.nfl_markets import NflMarketService
from sports_analytics.nba_markets import NbaMarketService
from sports_analytics.ncaaf_markets import NcaafMarketService
from sports_analytics.usage_log import ApiUsageLog, usage_summary
from sports_analytics.dashboard import create_app

PRIMARY='primary-test-secret'; BACKUP='backup-test-secret'
P=sha256(PRIMARY.encode()).hexdigest(); B=sha256(BACKUP.encode()).hexdigest()


def response(body=None, *, used=10, remaining=490, last=0, status=200, code=None, headers=True):
    payload={'error_code':code,'message':PRIMARY+' '+BACKUP} if code else body
    return SimpleNamespace(status_code=status,
        headers={'x-requests-used':str(used),'x-requests-remaining':str(remaining),'x-requests-last':str(last)} if headers else {},
        json=Mock(return_value=payload))


def exhausted(**kwargs):
    return response(status=401,code='OUT_OF_USAGE_CREDITS',used=500,remaining=0,**kwargs)


def fixture(service_class=NflMarketService):
    sport=service_class.sport
    home,away=('Baltimore Ravens','Cleveland Browns') if sport=='NFL' else ('Boston Celtics','New York Knicks') if sport=='NBA' else ('Michigan Wolverines','Ohio State Buckeyes')
    game=dict(sport=sport,id='123',start='2026-11-01T17:00:00Z',home=dict(name=home,abbreviation='HOME'),away=dict(name=away,abbreviation='AWAY'))
    event=dict(id='a'*32,sport_key=service_class.sport_key,home_team=home,away_team=away,commence_time=game['start'])
    body={**event,'bookmakers':[dict(key='fanduel',markets=[dict(key='alternate_totals',outcomes=[dict(name='Over',point=44.5,price=-110),dict(name='Under',point=44.5,price=-110)])])]}
    return game,event,body


class OddsAccountsChecks(TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        env=patch.dict(os.environ,{'ODDS_API_KEY':PRIMARY,'ODDS_API_KEY_2':BACKUP})
        env.start();self.addCleanup(env.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests in tests'))
        network.start();self.addCleanup(network.stop)
        self.session=Mock();self.api=OddsApiService(self.session)

    def keys_sent(self):return [c.kwargs['params']['apiKey'] for c in self.session.get.call_args_list]

    def market(self, service_class=NflMarketService):
        return service_class(self.api,self.root/(service_class.sport+'.sqlite3'),self.root/'markets',ApiUsageLog(self.root/'log.csv'))

    def test_usage_shows_both_accounts_and_combined_totals_without_paid_calls(self):
        self.session.get.side_effect=[response(used=100,remaining=400),response(used=200,remaining=800)]
        usage=self.api.usage()
        self.assertEqual((usage.used,usage.remaining,usage.total),(300,1200,1500))
        self.assertEqual(len(usage.accounts),2)
        self.assertTrue(usage.accounts[0].active);self.assertFalse(usage.accounts[1].active)
        self.assertEqual(self.keys_sent(),[PRIMARY,BACKUP])
        self.assertTrue(all(c.args[0].endswith('/sports/') for c in self.session.get.call_args_list))
        self.assertEqual(self.api.usage(),usage);self.assertEqual(self.session.get.call_count,2)

    def test_exhaustion_retries_once_on_backup_and_stays_there(self):
        self.session.get.side_effect=[exhausted(),response(last=17),response(last=17)]
        first=self.api.get('sports/americanfootball_nfl/events/event/odds')
        self.assertEqual(first.odds_account_id,B)
        self.assertEqual(len(first.odds_attempts),1)
        self.api.get('sports/americanfootball_nfl/events/another/odds')
        self.assertEqual(self.keys_sent(),[PRIMARY,BACKUP,BACKUP])
        self.assertEqual(self.api.account_id(),P)

    def test_known_empty_balance_skips_the_primary_paid_request(self):
        self.session.get.side_effect=[response(used=500,remaining=0),response(),response(last=17)]
        usage=self.api.usage()
        self.assertTrue(usage.accounts[0].exhausted);self.assertTrue(usage.accounts[1].active)
        self.api.get('sports/americanfootball_nfl/events/event/odds')
        self.assertEqual(self.keys_sent(),[PRIMARY,BACKUP,BACKUP])

    def test_primary_is_preferred_again_after_credits_replenish(self):
        self.session.get.side_effect=[exhausted(),response(last=17),response(used=0,remaining=500),response(used=17,remaining=483,last=17)]
        with patch('sports_analytics.odds_api.time.monotonic',return_value=100):
            self.api.get('sports/americanfootball_nfl/events/event/odds')
        with patch('sports_analytics.odds_api.time.monotonic',return_value=401):
            result=self.api.get('sports/americanfootball_nfl/events/another/odds')
        self.assertEqual(result.odds_account_id,P)
        self.assertEqual(self.keys_sent(),[PRIMARY,BACKUP,PRIMARY,PRIMARY])
        self.assertTrue(self.session.get.call_args_list[-2].args[0].endswith('/sports/'))

    def test_nonquota_failures_and_timeouts_are_not_repeated(self):
        for failure in [response(status=401,code='INVALID_KEY'),response(status=429,code='EXCEEDED_FREQ_LIMIT'),
                        response(status=403,code='HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN'),response(status=503),requests.Timeout(PRIMARY)]:
            with self.subTest(failure=type(failure).__name__):
                self.api=OddsApiService(self.session);self.session.reset_mock()
                self.session.get.side_effect=[failure]
                with self.assertRaises(DataSourceError) as caught:self.api.get('sports/americanfootball_nfl/events/event/odds')
                self.assertEqual(self.session.get.call_count,1)
                self.assertNotIn(PRIMARY,str(caught.exception));self.assertNotIn(BACKUP,str(caught.exception))

    def test_charged_quota_error_is_not_submitted_twice(self):
        self.session.get.side_effect=[exhausted(last=17)]
        with self.assertRaises(DataSourceError):self.api.get('sports/americanfootball_nfl/events/event/odds')
        self.assertEqual(self.session.get.call_count,1)

    def test_both_empty_keys_stop_without_a_loop(self):
        self.session.get.side_effect=[exhausted(),exhausted()]
        with self.assertRaisesRegex(DataSourceError,'All configured'):self.api.get('sports/americanfootball_nfl/events/event/odds')
        with self.assertRaises(DataSourceError):self.api.get('sports/americanfootball_nfl/events/event/odds')
        self.assertEqual(self.session.get.call_count,2)

    def test_identical_keys_are_counted_once_and_backup_can_be_used_alone(self):
        self.session.get.return_value=response()
        self.session.get.side_effect=None
        with patch.dict(os.environ,{'ODDS_API_KEY_2':PRIMARY}):
            self.assertEqual(len(self.api.usage().accounts),1)
        with patch.dict(os.environ,{'ODDS_API_KEY':'','ODDS_API_KEY_2':BACKUP}):
            self.assertEqual(self.api.account_ids(),[B])
            self.assertEqual(self.api.usage().accounts[0].account_id,B)

    def test_partial_and_stale_usage_is_labeled_and_not_fabricated(self):
        self.session.get.side_effect=[response(status=401,code='INVALID_KEY',headers=False),response(used=10,remaining=490)]
        usage=self.api.usage()
        self.assertEqual(usage.used,10);self.assertEqual(usage.remaining,490)
        self.assertIsNone(usage.accounts[0].usage);self.assertTrue(usage.stale)
        self.assertIn('unknown',usage.notice)
        self.session.get.side_effect=[response(used=20,remaining=480),response(used=30,remaining=470)]
        self.api.usage(refresh=True)
        self.session.get.side_effect=[requests.ConnectionError(PRIMARY),response(used=40,remaining=460)]
        usage=self.api.usage(refresh=True)
        self.assertEqual(usage.used,60);self.assertTrue(usage.accounts[0].usage.stale)
        self.assertNotIn(PRIMARY,usage.notice)

    def test_success_without_headers_does_not_reissue_odds(self):
        self.session.get.return_value=response({},headers=False)
        self.api.get('sports/americanfootball_nfl/events/event/odds')
        self.assertEqual(self.session.get.call_count,1)

    def test_concurrent_requests_switch_only_after_the_first_rejection(self):
        first=True
        def reply(url,**kwargs):
            nonlocal first
            if kwargs['params']['apiKey']==PRIMARY and first:
                first=False;return exhausted()
            return response(last=1)
        self.session.get.side_effect=reply
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda i:self.api.get(f'sports/americanfootball_nfl/events/{i}/odds'),range(4)))
        self.assertTrue(all(r.odds_account_id==B for r in results))
        self.assertEqual(self.keys_sent().count(PRIMARY),1)
        self.assertEqual(self.keys_sent().count(BACKUP),4)

    def test_actual_paying_account_is_saved_and_history_survives_switch_for_all_sports(self):
        for cls in (NflMarketService,NbaMarketService,NcaafMarketService):
            self.api=OddsApiService(self.session);self.session.reset_mock()
            game,event,body=fixture(cls)
            self.session.get.side_effect=[response([event]),exhausted(),response(body,used=17,remaining=483,last=17)]
            markets=self.market(cls)
            saved=markets.pull(game,'click-'+cls.sport)
            self.assertEqual(saved['api_account_id'],B)
            with closing(markets._connect()) as db:
                self.assertEqual(db.execute(f'SELECT account FROM {markets.table}').fetchone()[0],B)
            self.assertEqual(markets.history('123')[0]['event_id'],event['id'])
            self.assertEqual(markets.pull(game,'click-'+cls.sport)['csv_file'],saved['csv_file'])
            self.assertEqual(self.session.get.call_count,3)
            rows=markets.usage_log.records([P,B])
            sport_rows=[r for r in rows if r['sport']==cls.sport]
            self.assertEqual(sum(r['credits'] or 0 for r in sport_rows),17)
            self.assertEqual({r['account_id'] for r in sport_rows},{P,B})
        for path in self.root.rglob('*'):
            if path.is_file() and path.suffix in {'.csv','.json'}:
                contents=path.read_text(encoding='utf-8-sig')
                self.assertNotIn(PRIMARY,contents);self.assertNotIn(BACKUP,contents)

    def test_failed_backup_request_is_attributed_and_not_charged_twice(self):
        game,event,_=fixture();markets=self.market()
        self.session.get.side_effect=[response([event]),exhausted(),requests.Timeout(PRIMARY)]
        with self.assertRaises(DataSourceError):markets.pull(game,'failed')
        rows=markets.usage_log.records([P,B])
        self.assertEqual(len(rows),2)
        self.assertEqual(next(r['credits'] for r in rows if r['account_id']==P),0)
        self.assertIsNone(next(r['credits'] for r in rows if r['account_id']==B))
        with self.assertRaises(DataSourceError):markets.pull(game,'failed')
        self.assertEqual(self.session.get.call_count,3)

    def test_original_and_backup_snapshots_remain_visible_and_recover_the_combined_ledger(self):
        game,event,body=fixture();markets=self.market()
        self.session.get.side_effect=[response([event]),response(body,last=17),response([event]),exhausted(),response(body,last=17)]
        markets.pull(game,'first');markets.pull(game,'second')
        self.assertEqual(len(markets.history('123')),2)
        markets.usage_log.path.unlink()
        markets.export_existing()
        rows=markets.usage_log.records([P,B])
        self.assertEqual(len(rows),3);self.assertEqual(sum(r['credits'] for r in rows),34)
        with patch.dict(os.environ,{'ODDS_API_KEY_2':''}):
            self.assertEqual(len(markets.history('123')),1)

    def test_dashboard_combines_quota_and_history_without_exposing_keys(self):
        game,event,body=fixture();markets=self.market()
        self.session.get.side_effect=[response([event]),exhausted(),response(body,last=17)]
        markets.pull(game,'page')
        self.session.get.side_effect=[response(used=500,remaining=0),response(used=17,remaining=483)]
        app=create_app(service=Mock(path=self.root/'data.sqlite3'),odds_service=self.api,nfl_market_service=markets,
                       nba_market_service=Mock(),ncaaf_market_service=Mock(),api_usage_log=markets.usage_log,
                       schedule_service=Mock(),kalshi_service=Mock(),polymarket_service=Mock())
        callbacks={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback')}
        from dash import _callback_context
        with patch.object(_callback_context.CallbackContext,'triggered_id',new_callable=lambda:property(lambda _: 'odds-refresh')):
            graph,metrics,status=callbacks['odds_usage']('ODDS_API',1,0)
        self.assertEqual(list(graph.data[0].values),[517,483])
        from plotly.io import to_json
        from dash._utils import to_json as dash_json
        rendered=dash_json(metrics)
        self.assertIn('ODDS_API_KEY_2',rendered);self.assertIn('Primary',rendered);self.assertIn('Backup',rendered)
        self.assertNotIn(PRIMARY,rendered);self.assertNotIn(BACKUP,rendered)
        *_,history,status=callbacks['logged_usage']('ODDS_API',0,0,None)
        self.assertEqual(sum(r['credits'] for r in history),17)
        self.assertTrue(any('Backup' in r['account'] for r in history))
        with app.server.test_client() as client:
            layout=json.dumps(client.get('/_dash-layout').get_json())
            self.assertNotIn(PRIMARY,layout);self.assertNotIn(BACKUP,layout)
        self.assertEqual(self.session.get.call_count,5)


if __name__=='__main__':main()
