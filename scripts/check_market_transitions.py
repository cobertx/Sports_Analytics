"""Retain market views, skip hidden work, and reuse only current game catalogs."""
from pathlib import Path
import sys
import tempfile
from time import time
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from sports_analytics.dashboard import create_app
from sports_analytics.kalshi_analysis import FILTER
from sports_analytics.mispricing import filter_context
from sports_analytics import kalshi_view
import check_kalshi as fixtures


class MarketTransitionChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.schedule=Mock();self.markets=Mock();self.exchange=Mock();self.poly=Mock()
        self.markets.latest.return_value={}
        self.source=Mock(path=Path(self.tmp.name)/'cache.sqlite3')
        self.app=create_app(service=self.source,schedule_service=self.schedule,nfl_market_service=self.markets,
                            kalshi_service=self.exchange,polymarket_service=self.poly)
        self.cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in self.app.callback_map.values() if m.get('callback')}
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external HTTP'))
        self.network.start();self.addCleanup(self.network.stop)

    def test_fanduel_departure_preserves_controls_snapshot_and_results_without_io(self):
        with self.assertRaises(PreventUpdate):
            self.cb['open_game']('#nfl-kalshi-market','NFL','kalshi',[],None,'saved.csv',{'id':'123'})
        with self.assertRaises(PreventUpdate):self.cb['snapshot_categories']('saved.csv','#nfl-kalshi-market','Game')
        with self.assertRaises(PreventUpdate):
            self.cb['show_saved_markets']('Game','saved.csv','#nfl-kalshi-market',None,'PPP','WHP',None,'kalshi')
        with self.assertRaises(PreventUpdate):
            self.cb['quick_filter_markets'](None,'Mispricing','#nfl-kalshi-market','saved.csv',['PPP'],['WHP'],75,5,None,'kalshi')
        self.markets.history.assert_not_called()
        shown=self.cb['open_game']('#nfl-games','NFL','market',[],None,'saved.csv',{'id':'123'})
        self.assertTrue(all(value is no_update for value in shown[2:]))

    def test_return_to_fanduel_game_does_not_reload_snapshots_or_rebuild_header(self):
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_prop_ids={'market-url.hash':'market-url'})):
            result=self.cb['open_game']('#nfl-game-123','NFL','market',[],None,'saved.csv',dict(sport='NFL',id='123',token='same'))
        self.assertTrue(all(value is no_update for value in result));self.markets.history.assert_not_called()
        history=dict(context=['#nfl-game-123','saved.csv'],view_key=['Game','PPP','WHP',None])
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-url')):
            shown=self.cb['show_saved_markets']('Game','saved.csv','#nfl-game-123',None,'PPP','WHP',history,'market')
        self.assertEqual(shown,(no_update,no_update));self.markets.history.assert_not_called()
        request=filter_context('#nfl-game-123','saved.csv',['PPP'],['WHP'],dict(probability=.75,edge=.05))
        previous=dict(context=['#nfl-game-123','saved.csv'],filter_key=request)
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-url')):
            filtered=self.cb['quick_filter_markets'](request,'Mispricing','#nfl-game-123','saved.csv',['PPP'],['WHP'],75,5,previous,'market')
        self.assertEqual(filtered,(no_update,no_update));self.markets.history.assert_not_called()

    def test_fanduel_catalog_scope_expires_and_manual_refresh_always_works(self):
        scope=dict(sport='NFL',days=7,checked_at=time())
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='workspace-page')):
            with self.assertRaises(PreventUpdate):self.cb['upcoming_games']('market','NFL',7,0,0,None,[],scope)
        self.schedule.load.assert_not_called();self.markets.latest.assert_not_called()
        self.schedule.load.return_value=SimpleNamespace(games=[],fetched_at='2026-10-24T00:00:00Z',stale_days=[],failed_days=[])
        for trigger,stale_scope,days in [('workspace-page',dict(scope,checked_at=time()-301),7),
                                         ('workspace-page',scope,14),('market-refresh',scope,7),('market-timer',scope,7)]:
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id=trigger)):
                result=self.cb['upcoming_games']('market','NFL',days,1,1,None,[],stale_scope)
            self.assertEqual(result[-1]['sport'],'NFL');self.assertEqual(result[-1]['days'],days)
            self.assertGreater(result[-1]['checked_at'],scope['checked_at']-1)
        self.assertEqual(self.schedule.load.call_count,4)

    def test_exchange_catalogs_reuse_current_window_and_keep_manual_and_timer_updates(self):
        for provider,service in [('kalshi',self.exchange),('polymarket',self.poly)]:
            previous=dict(sport='NFL',window_days=7,checked_at=time(),games=[],fetched_at='2026-10-24T00:00:00Z',notices=[])
            service.games.return_value=previous
            with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
                with self.assertRaises(PreventUpdate):self.cb[provider+'_games'](provider,'NFL',7,0,0,previous)
            service.games.assert_not_called()
            for trigger in [provider+'-list-refresh',provider+'-list-timer']:
                with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id=trigger)):
                    self.cb[provider+'_games'](provider,'NFL',7,1,1,previous)
            self.assertEqual(service.games.call_count,2)
            with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
                self.cb[provider+'_games'](provider,'NFL',14,1,1,previous)
            self.assertEqual(service.games.call_count,3)

    def test_leaving_exchange_games_never_clears_retained_context_or_tabs(self):
        for provider,href in [('kalshi','#nfl-polymarket-market'),('polymarket','#nfl-kalshi-market')]:
            with self.assertRaises(PreventUpdate):self.cb[provider+'_game']({'sport':'NFL','games':[]},href,'NFL',{'id':'retained'},'Passing Props')
            with self.assertRaises(PreventUpdate):self.cb[provider+'_game']({'sport':'NFL','games':[]},'#nfl-'+provider+'-market','NFL',{'id':'retained'},'Passing Props')

    def test_exchange_list_entry_and_hidden_render_do_not_request_or_build_quotes(self):
        for provider,service,g in [('kalshi',self.exchange,dict(sport='NFL',id=fixtures.GAME)),
                                   ('polymarket',self.poly,dict(sport='NFL',id='123'))]:
            with self.assertRaises(PreventUpdate):
                self.cb[provider+'_quotes'](provider,'NFL',g,'Game Lines','live',0,0,5,'#nfl-'+provider+'-market')
            with self.assertRaises(PreventUpdate):
                self.cb[provider+'_table']({},g,'Game Lines',False,False,None,'historical','#historical-team')
            service.quotes.assert_not_called()
        g=dict(sport='NFL',id=fixtures.GAME)
        with self.assertRaises(PreventUpdate):
            self.cb['kalshi_models'](None,None,g,'PPP','WHP','live','kalshi',FILTER,None,'#nfl-kalshi-market')

    def test_local_visibility_assets_and_callback_are_served_without_heavy_inputs(self):
        meta=self.app.callback_map['app-shell.className']
        self.assertNotIn('callback',meta)
        self.assertNotIn('kalshi-quotes',{item['id'] for item in meta['inputs']})
        self.assertNotIn('kalshi-models',{item['id'] for item in meta['inputs']})
        with self.app.server.test_client() as client:
            for url in ['/assets/market_transition.js','/assets/market_transition.css','/_dash-dependencies']:
                with client.get(url) as response:self.assertEqual(response.status_code,200)


if __name__=='__main__':main()
