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
from app.shell.navigation import (PAGES,navigate,sport_page_navigation,exchange_visibility,
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


def run_client(app,before=None,references=None,steps=None):
    config=dict(asset=(ROOT/'app/assets/js/market_transition.js').read_text(encoding='utf-8'),
        inline=app._inline_scripts,clients=[m for m in app._callback_list if m.get('clientside_function')],
        pages=PAGES,steps=STEPS if steps is None else steps,before=before or [],after=graph(app),references=references or [])
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
                refs.append(dict(output='historical-page.style',args=[page,sport],expected=navigate(page,sport)))
                for fragment,function in [('in-game-page.style',in_game_visibility),
                    ('market-summary-page.style',summary_visibility),('filtered-results-page.style',filtered_visibility)]:
                    refs.append(dict(output=fragment,args=[page,sport],expected=function(page,sport)))
            for route in ('#research','#research-player-stats-whp','#research-player-stats-comparison','#nba-games'):
                refs.append(dict(output='research-setup.style',args=[sport,route],expected=research_visibility(sport,route)))
            for page in ('historical','market','kalshi','in-game','backtesting'):
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

    def test_player_chart_requests_follow_a_matching_selection_and_forecast(self):
        if not node():self.skipTest('Node unavailable; no dependencies installed')
        route,stamp,category,method='#nfl-game-123','saved','Passing','WHP'
        request=dict(click=7,context=[route,stamp,category])
        history=dict(context=[route,stamp],view_key=[category,'PPP',method],
                     player={method:dict(fit='ready-fit')})
        output='market-prop-request.data-model-revision'
        no={'_dash_no_update':'_dash_no_update'}
        pending='[7,"WHP",false,false,null]'
        ready='[7,"WHP",true,false,"ready-fit"]'
        cases=[
            ([None,None,route,stamp,category,method,'market',None],[no,{'display':'none'}]),
            ([request,None,route,stamp,category,method,'market',None],[pending,{}]),
            ([request,history,route,stamp,category,method,'market',pending],[ready,{}]),
            ([request,history,route,stamp,category,method,'market',ready],[no,{}]),
            ([request,history,'#nfl-game-124',stamp,category,method,'market',ready],[no,{'display':'none'}]),
        ]
        checks=run_client(self.app,references=[dict(output=output,args=args,expected=expected)
                                              for args,expected in cases])
        self.assertEqual(checks['checks'],len(cases))

    def test_scopes_wake_only_the_active_workspace_and_revisits_activate_once(self):
        if not node():self.skipTest('Node unavailable')
        result=run_client(self.app)
        for step in result['steps']:
            self.assertTrue({'market_prop_probabilities','polymarket_distributions'}.isdisjoint(step['after']),
                            step['label'])
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

    def test_schedule_refresh_keeps_open_game_and_quick_filter_snapshot(self):
        stamp = '2026-11-22T23:00:00Z'
        game = dict(id='123', start=stamp, home=dict(name='Baltimore Ravens', abbreviation='BAL'),
                    away=dict(name='Cleveland Browns', abbreviation='CLE'), time_tbd=False)
        context = dict(game, sport='NFL', token='open-game')
        values = {'workspace-page':'market', 'sport':'NFL', 'market-url':'#nfl-game-123',
                  'market-schedule':[], 'market-snapshot':'saved.csv', 'market-detail-context':context}
        self.fixture.markets.history.return_value = []
        # The schedule can drop the game at kickoff while its scan still uses
        # the selected snapshot. Neither store should be cleared in that case.
        self.assertFalse(post(self.app, 'open_game', values, 'market-schedule.data').get('response'))
        self.fixture.markets.history.assert_not_called()
        values['market-schedule'] = [game]
        self.assertFalse(post(self.app, 'open_game', values, 'market-schedule.data').get('response'))

        # A successful pull on the still-open game can select a new snapshot
        # even after the game disappears from the upcoming schedule.
        self.fixture.markets.history.return_value = [dict(game=game, pulled_at=stamp, csv_file='new.csv',
                                                        home_team=game['home']['name'], away_team=game['away']['name'])]
        values.update({'market-schedule':[], 'market-refresh-signal':dict(sport='NFL', game_id='123', snapshot='new.csv')})
        pulled = post(self.app, 'open_game', values, 'market-refresh-signal.data')['response']
        self.assertEqual(pulled['market-snapshot']['value'], 'new.csv')
        self.assertNotIn('market-detail-context', pulled)

    def test_reopening_a_card_selects_a_newly_pulled_snapshot(self):
        stamp = '2026-11-22T23:00:00Z'
        game = dict(id='123', start=stamp, home=dict(name='Baltimore Ravens', abbreviation='BAL'),
                    away=dict(name='Cleveland Browns', abbreviation='CLE'), time_tbd=False)
        previous = dict(game=game, pulled_at=stamp, csv_file='old.csv',
                        home_team=game['home']['name'], away_team=game['away']['name'])
        values = {'workspace-page':'market', 'sport':'NFL', 'market-url':'#nfl-game-123',
                  'market-schedule':[game], 'market-list':{'display':'block'}, 'market-snapshot':'old.csv',
                  'market-detail-context':dict(game, sport='NFL', token='open-game')}
        # The previous detail view knows about one later snapshot and the user
        # intentionally selected the older one before returning to the list.
        latest = dict(previous, pulled_at='2026-11-22T23:01:00Z', csv_file='latest.csv')
        values['market-snapshot.options'] = [{'value':'latest.csv'}, {'value':'old.csv'}]
        self.fixture.markets.history.return_value = [latest, previous]

        def reopen():
            return post(self.app, 'open_game', values, 'nav-market.data')['response']['market-snapshot']

        self.assertNotIn('value', reopen())
        self.fixture.markets.history.return_value = [
            dict(latest, pulled_at='2026-11-22T23:02:00Z', csv_file='new.csv'), latest, previous]
        self.assertEqual(reopen()['value'], 'new.csv')

    def test_return_to_filter_preserves_tabs_only_for_the_loaded_snapshot(self):
        route, selection = '#nfl-game-123', 'saved.csv'
        self.fixture.markets.history.return_value = [dict(pulled_at='2026-11-22T23:00:00Z', csv_file=selection,
                                                        cost=17, rows=[dict(group='Game')])]
        values = {'workspace-page':'market', 'sport':'NFL', 'market-url':route,
                  'market-snapshot':selection, 'market-category':'Mispricing'}
        first = post(self.app, 'snapshot_categories', values, 'market-snapshot.value')['response']
        values['market-category-scope'] = first['market-category-scope']['data']
        self.assertEqual(values['market-category-scope'], [route, selection])
        self.fixture.markets.history.return_value = []
        self.assertFalse(post(self.app, 'snapshot_categories', values, 'nav-market.data').get('response'))
        values['market-url'] = '#nfl-game-456'
        different = post(self.app, 'snapshot_categories', values, 'nav-market.data')['response']
        self.assertEqual(different['market-category']['value'], 'Additional Information')
        self.assertIsNone(different['market-category-scope']['data'])

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
