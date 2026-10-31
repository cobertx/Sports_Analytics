"""Registered client/server navigation contracts; no external requests."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import TestCase,main
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash._utils import to_json
from sports_analytics.navigation import (PAGES,navigate,sport_page_navigation,exchange_visibility,
    in_game_visibility,summary_visibility,filtered_visibility,research_visibility)
import check_market_transitions as transition_fixtures
from check_dashboard_efficiency import post
from models.season_weights import POLICY_VERSION

ROOT=Path(__file__).resolve().parents[1]
STEPS=[dict(label=label,hash=value) for label,value in [
    ('FanDuel to Kalshi','#nfl-kalshi-game-KXNFLGAME-TEST'),
    ('Kalshi to Polymarket','#nfl-polymarket-game-456'),
    ('Return to FanDuel','#nfl-game-123'),('Another saved game','#nfl-game-124'),
    ('Player history','#historical-player?sport=NFL&name=Player+A'),('Back to prompt','#nfl-game-123'),
    ('Revisit Kalshi','#nfl-kalshi-game-KXNFLGAME-TEST'),('Revisit Polymarket','#nfl-polymarket-game-456'),
    ('Return to FanDuel again','#nfl-game-123')]]
STEPS += [dict(label='Player category',set={'market-category':'Passing'}),
          dict(label='Return to game lines',set={'market-category':'Game'})]


def node():
    found=shutil.which('node')
    fallback=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    return found or (str(fallback) if fallback.is_file() else None)


def graph(app):
    return [dict(name=m['callback'].__wrapped__.__name__,inputs=m['inputs'],state=m['state'])
            for m in app.callback_map.values() if m.get('callback')]


def run_client(app,before=None,references=None):
    config=dict(asset=(ROOT/'sports_analytics/assets/market_transition.js').read_text(encoding='utf-8'),
        inline=app._inline_scripts,clients=[m for m in app._callback_list if m.get('clientside_function')],
        pages=PAGES,steps=STEPS,before=before or [],after=graph(app),references=references or [])
    result=subprocess.run([node(),str(ROOT/'scripts/navigation_harness.js')],input=to_json(config),
        capture_output=True,text=True,encoding='utf-8',check=True)
    return json.loads(result.stdout)


class NavigationPipelineChecks(TestCase):
    def setUp(self):
        self.fixture=transition_fixtures.MarketTransitionChecks();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.app=self.fixture.app

    def test_registered_browser_ui_matches_existing_presentations(self):
        if not node():self.skipTest('Node unavailable; no dependencies installed')
        refs=[]
        for identity in ('backtest-poll','performance-poll','comparison-poll'):
            for page,sport,expected in [('market','NFL',0),('historical','NBA',0),('backtesting','NBA',-1),('historical','RESEARCH',-1)]:
                refs.append(dict(output=identity+'.max_intervals',args=[page,sport],expected=expected))
        for sport in ('NBA','NFL','NCAAF','RESEARCH','ODDS_API','PNL'):
            for page in PAGES:
                for kind in ('team','player'):
                    refs.append(dict(output='page-title.children',args=[page,sport,kind],expected=navigate(page,sport,kind)))
                for fragment,function in [('in-game-page.style',in_game_visibility),
                    ('market-summary-page.style',summary_visibility),('filtered-results-page.style',filtered_visibility)]:
                    refs.append(dict(output=fragment,args=[page,sport],expected=function(page,sport)))
            for route in ('#research','#research-player-stats-whp','#research-player-stats-comparison','#nba-games'):
                refs.append(dict(output='research-setup.style',args=[sport,route],expected=research_visibility(sport,route)))
            for page in ('historical','market','kalshi'):
                refs.append(dict(output='historical-navigation.children',args=[page,sport,'player',{}],
                                 expected=sport_page_navigation(page,sport,'player',{})))
        for provider in ('Kalshi','Polymarket'):
            for sport in ('NBA','NFL','NCAAF'):
                identity='KX'+sport+'GAME-TEST' if provider=='Kalshi' else '123'
                for page in ('historical',provider.lower()):
                    for frequency in (0,2,5,999):
                        for selected in ('live','saved.json'):
                            args=[page,sport,f'#{sport.lower()}-{provider.lower()}-game-{identity}',frequency,selected,dict(sport=sport,id=identity)]
                            refs.append(dict(output=provider.lower()+'-page.style',args=args,
                                             expected=exchange_visibility(provider,*args)))
        result=run_client(self.app,references=refs)
        self.assertEqual(result['checks'],len(refs))

    def test_scopes_wake_only_the_active_workspace_and_revisits_activate_once(self):
        if not node():self.skipTest('Node unavailable')
        result=run_client(self.app)
        for step in result['steps']:
            for name in step['after']:
                metadata=next(item for item in graph(self.app) if item['name']==name)
                scoped=[i['id'] for i in metadata['inputs'] if i['id'].startswith('nav-')]
                self.assertTrue(not scoped or all(i in {'nav-'+step['page'],'nav-'+step['page']+'-active'} for i in scoped))
        category=result['steps'][-1]
        self.assertFalse(any(item.startswith('nav-') for item in category['updates']))
        back=next(item for item in result['steps'] if item['label']=='Back to prompt')
        self.assertIn('nav-market-active',back['updates'])
        self.assertNotIn('nav-historical',back['updates'])

    def test_stale_or_inactive_http_context_does_not_read_data(self):
        values={'workspace-page':'kalshi','sport':'NFL','market-url':'#nfl-kalshi-market',
                'nav-market':dict(v=1,page='market',sport='NFL',hash='#nfl-game-123'),
                'market-schedule':[],'market-snapshot':'saved','market-detail-context':None}
        result=post(self.app,'open_game',values,'nav-market.data')
        self.assertEqual(result,{})
        self.fixture.markets.history.assert_not_called()
        values.update({'workspace-page':'market','market-url':'#nfl-game-456'})
        self.assertEqual(post(self.app,'open_game',values,'nav-market.data'),{})
        self.fixture.markets.history.assert_not_called()

    def test_warm_http_return_preserves_the_header_without_reading_snapshots(self):
        values={'workspace-page':'market','sport':'NFL','market-url':'#nfl-game-123','market-schedule':[],
            'market-snapshot':'saved.csv','market-detail-context':dict(sport='NFL',id='123',token='same')}
        self.assertEqual(post(self.app,'open_game',values,'nav-market.data').get('response',{}),{})
        self.fixture.markets.history.assert_not_called()

    def test_warm_http_market_return_does_not_fit_or_rebuild_loaded_results(self):
        import check_mispricing as fixtures
        fixture=fixtures.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        location='#nfl-game-123';selection=fixture.saved['pulled_at']
        history=dict(context=[location,selection],view_key=['Game','PPP','WHP',None,POLICY_VERSION])
        values={'workspace-page':'market','sport':'NFL','market-url':location,'market-category':'Game',
                'market-snapshot':selection,'market-forecast-method':'PPP','player-forecast-method':'WHP',
                'market-forecast-history':history}
        reply=post(fixture.app,'show_saved_markets',values,'nav-market.data')
        self.assertEqual(reply.get('response',{}),{})
        fixture.game_services['PPP'].forecast.assert_not_called()
        fixture.player_services['WHP'].forecast.assert_not_called()
        fixture.markets.history.assert_not_called()

    def test_navigation_is_local_and_data_callbacks_have_no_global_route_inputs(self):
        for meta in self.app.callback_map.values():
            if not meta.get('callback'):continue
            name=meta['callback'].__wrapped__.__name__
            if name in {'structure_open','render_parlay','in_game_supported_page','pnl_visibility'}:continue
            self.assertFalse({'market-url','sport','workspace-page'} & {i['id'] for i in meta['inputs']},name)
        self.assertNotIn('callback',self.app.callback_map['..sport.value...workspace-page.value..'])
        for item in ('kalshi-games','polymarket-games','market-forecast-history','kalshi-rules-signature'):
            self.assertNotIn(item,{i['id'] for i in self.app.callback_map['app-shell.className']['inputs']})


if __name__=='__main__':main()
