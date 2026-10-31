"""Cross-provider scope, probability semantics, navigation and Dash integration."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from models.Game_Outcome.PPP.PPP import MarketProbability
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.market_summary import calculate,entries,matching_game,polymarket_target,route
from sports_analytics.market_summary_view import sections,comparison_rows
from sports_analytics.mispricing import GAME_METHODS,PLAYER_METHODS
from check_kalshi import components

START='2026-10-25T13:30:00Z'
STAMP='2026-10-24T12:00:00Z'
KID='KXNFLGAME-26OCT25PITNO'


def game():
    return dict(id='123',start=START,away=dict(name='Pittsburgh Steelers',abbreviation='PIT'),
                home=dict(name='New Orleans Saints',abbreviation='NO'),neutral=False)


def exchange(provider='Kalshi'):
    return dict(sport='NFL',id=KID if provider=='Kalshi' else '100',start=START,
                title='Pittsburgh at New Orleans',events=[KID],categories=['Game Lines','Player Props'])


def binary(player=False,provider='Kalshi'):
    row=dict(ticker='player' if player else 'total',event_ticker='KXNFLPASSYDS-TEST' if player else 'KXNFLTOTAL-TEST',
        event_title='Test Quarterback: Passing Yards' if player else 'Game total',
        title='Test Quarterback: Passing Yards Over 200.5' if player else 'Over 47.5 points',
        category='Player Props' if player else 'Game Lines',yes_label='Over',no_label='Under',
        yes_ask=.5,no_ask=.4,notional=1,line=200 if player else 47,strike_type='greater',cap_strike=None,
        market_type='binary',rules_primary='Official full-game result, including overtime.',rules_secondary='',status='active')
    if provider=='Polymarket':row.update(line=200.5 if player else 47.5,sports_market_type='passing_yards' if player else 'totals',status='Open')
    return row


def bundle():
    fd=dict(sport='NFL',game_id='123',pulled_at='2026-10-23T12:00:00Z',commence_time=START,
        home_team='New Orleans Saints',away_team='Pittsburgh Steelers',rows=[
            dict(group='Game',market='Spread',market_key='alternate_spreads',subject='New Orleans Saints',selection='New Orleans Saints',line=-3.5,odds=100),
            dict(group='Passing',market='Passing yards',market_key='player_pass_yds_alternate',subject='Test Quarterback',selection='Over',line=200,odds=100)])
    records={'FanDuel':dict(game=game(),snapshot=fd)}
    for provider in ['Kalshi','Polymarket']:
        g=exchange(provider)
        records[provider]=dict(game=g,snapshot=dict(sport='NFL',game_id=g['id'],fetched_at=STAMP,
            metadata_fetched_at=STAMP,rows=[binary(False,provider),binary(True,provider)],notices=[]))
    return dict(id='bundle-1',sport='NFL',game=game(),providers=records)


class GameForecast:
    home_team='NO';away_team='PIT';diagnostics={'last_game_date':'2026-10-18'}
    def spread(self,team,line):
        if team not in {'NO','PIT'}:raise ValueError('Unknown team')
        return MarketProbability(.6,.3,.1)
    def total(self,line,side):return MarketProbability(.7,.2,.1)


class PlayerForecast:
    forecasts={}
    def selection(self,row):return MarketProbability(.65,.25,.1)


class SummaryAnalysisChecks(TestCase):
    def test_equivalent_totals_align_in_source_columns_for_each_sport(self):
        for sport,away,home in [('NFL','Pittsburgh Steelers','New Orleans Saints'),
                                ('NBA','Miami Heat','Charlotte Hornets'),('NCAAF','Texas Longhorns','Oklahoma Sooners')]:
            source=bundle();source['sport']=sport
            source['game']['away']['name']=away;source['game']['home']['name']=home
            fd=source['providers']['FanDuel']['snapshot']
            fd.update(sport=sport,away_team=away,home_team=home,rows=[dict(group='Game',market='Total',market_key='alternate_totals',
                subject=side,selection=side,line=47.5,odds=100) for side in ['Over','Under']])
            for provider in ['Kalshi','Polymarket']:
                record=source['providers'][provider];record['game'].update(sport=sport,title=away+' at '+home)
                record['snapshot'].update(sport=sport,rows=[binary(provider=provider)])
                record['snapshot']['rows'][0]['event_ticker']='KX'+sport+'TOTAL-TEST'
            groups=comparison_rows(entries(source),'Game Lines')
            markets=groups['Game Lines','Game Total']
            self.assertEqual(len(markets),2)
            self.assertEqual([m['outcome']['label'] for m in markets],['Over 47.5','Under 47.5'])
            self.assertTrue(all(set(m['quotes'])=={'FanDuel','Kalshi','Polymarket'} for m in markets))
            rendered=sections(source,entries(source),'Game Lines')
            table=next(c for c in components(rendered) if c.__class__.__name__=='Table')
            header=next(c for c in components(table) if c.__class__.__name__=='Thead')
            self.assertEqual(len(header.children.children),4)
            self.assertIn('FanDuel',str(header));self.assertIn('Kalshi',str(header));self.assertIn('Polymarket',str(header))
            body=next(c for c in components(table) if c.__class__.__name__=='Tbody')
            self.assertEqual(len(body.children),2)
            self.assertTrue(all(len(row.children)==4 for row in body.children))
            self.assertIn('summary-best',str(rendered));self.assertIn('Historical Data',str(rendered))

    def test_push_lines_and_unsupported_contracts_are_never_merged(self):
        source=bundle();fd=source['providers']['FanDuel']['snapshot']
        fd['rows']=[dict(group='Game',market='Total',market_key='alternate_totals',subject='Over',selection='Over',line=47,odds=100)]
        source['providers']['Kalshi']['snapshot']['rows']=[binary()]
        source['providers']['Polymarket']['snapshot']['rows']=[{**binary(provider='Polymarket'),'rules_primary':'First half result only'}]
        groups=comparison_rows(entries(source),'All Markets')
        totals=groups['Game Lines','Game Total']
        self.assertEqual(len(totals),3)
        push=next(item for item in totals if item['outcome']['note'])
        self.assertEqual(set(push['quotes']),{'FanDuel'});self.assertEqual(push['outcome']['label'],'Over 47')
        self.assertTrue(any(key[0]=='Other Markets' for key in groups))
        self.assertIn('summary-missing',str(sections(source,entries(source),'All Markets')))

    def test_spread_complements_align_with_the_opponents_handicap(self):
        source=bundle();source['providers'].pop('Polymarket')
        source['providers']['FanDuel']['snapshot']['rows']=[dict(group='Game',market='Spread',market_key='alternate_spreads',
            subject=name,selection=name,line=line,odds=100) for name,line in [('New Orleans Saints',-3.5),('Pittsburgh Steelers',3.5)]]
        source['providers']['Kalshi']['snapshot']['rows']=[{**binary(),'event_ticker':'KXNFLSPREAD-TEST','line':3,
            'yes_label':'New Orleans Saints wins by over 3','title':'New Orleans Saints wins by over 3 points'}]
        groups=comparison_rows(entries(source),'Game Lines')
        spread=groups['Game Lines','Spread / Winner']
        self.assertEqual(len(spread),2)
        self.assertEqual({item['outcome']['label'] for item in spread},{'New Orleans Saints -3.5','Pittsburgh Steelers +3.5'})
        self.assertTrue(all(set(item['quotes'])=={'FanDuel','Kalshi'} for item in spread))

    def test_player_stats_and_duplicate_contracts_keep_every_forecast_cell(self):
        source=bundle();source['providers'].pop('Polymarket')
        source['providers']['FanDuel']['snapshot']['rows']=[dict(group='Passing',market='Passing yards',market_key='player_pass_yds_alternate',
            subject='Test Quarterback',selection=side,line=200.5,odds=100) for side in ['Over','Under']]
        source['providers']['Kalshi']['snapshot']['rows']=[binary(True),{**binary(True),'ticker':'another-offering'},
            {**binary(True),'ticker':'other-player','event_title':'Another Quarterback: Passing Yards'}]
        groups=comparison_rows(entries(source),'Player Props')
        player=groups['Player Props','Test Quarterback - Alt Passing Yds']
        self.assertEqual(len(player),4)
        self.assertEqual(sum(len(item['quotes']) for item in player),6)
        self.assertIn(('Player Props','Another Quarterback - Alt Passing Yds'),groups)
        rendered=sections(source,entries(source),'Player Props')
        identities=[c.id for c in components(rendered) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='summary-model-cell']
        self.assertEqual(len(identities),24)
        self.assertEqual(len({str(identity) for identity in identities}),24)

    def test_polymarket_team_handicaps_and_equality_rules_are_preserved(self):
        snapshot={**bundle()['providers']['Polymarket']['snapshot'],'game':exchange('Polymarket')}
        row={**binary(provider='Polymarket'),'sports_market_type':'spreads','title':'New Orleans Saints (-3.5)',
             'line':-3.5,'yes_label':'New Orleans Saints','no_label':'Pittsburgh Steelers'}
        target,invert=polymarket_target(snapshot,row)
        self.assertEqual((target['subject'],target['line'],invert),('New Orleans Saints',-3.5,False))
        other=polymarket_target(snapshot,{**row,'yes_label':'Pittsburgh Steelers'})[0]
        self.assertEqual((other['subject'],other['line']),('Pittsburgh Steelers',3.5))
        winner=polymarket_target(snapshot,{**row,'sports_market_type':'moneyline','title':'Pittsburgh at New Orleans','line':None})[0]
        self.assertEqual((winner['subject'],winner['line']),('New Orleans Saints',0))
        self.assertIsNone(polymarket_target(snapshot,{**row,'title':'New Orleans Saints (-3)','line':-3}))
        for rules in ['Refund if tied','First half result only','Ties settle at $0.50']:
            self.assertIsNone(polymarket_target(snapshot,{**row,'rules_primary':rules}))

    def test_college_team_models_can_use_verified_home_away_identity_without_aliases(self):
        source=bundle();source['sport']='NCAAF';source['game']['home']['name']='Texas Longhorns'
        source['game']['away']['name']='Oklahoma Sooners'
        source['providers']={'FanDuel':source['providers']['FanDuel']}
        source['providers']['FanDuel']['snapshot']['rows']=source['providers']['FanDuel']['snapshot']['rows'][:1]
        source['providers']['FanDuel']['snapshot']['rows'][0]['subject']='Texas Longhorns'
        forecast=Mock(home_team='Texas',away_team='Oklahoma',diagnostics={})
        forecast.spread.return_value=MarketProbability(.6,.4,0)
        models=calculate(source,'PPP','WHP',lambda *args:(forecast,None),Mock())
        forecast.spread.assert_called_once_with('Texas',-3.5)
        self.assertEqual(models['rows'][0]['probability'],.6)

    def test_game_matching_requires_exact_teams_start_sport_and_uniqueness(self):
        g=game();candidate=exchange()
        self.assertEqual(matching_game(g,'NFL',dict(games=[candidate])),candidate)
        for changes in [dict(title='Pittsburgh at Cleveland'),dict(start='2026-10-26T13:30:00Z'),dict(start=None),dict(sport='NBA')]:
            self.assertIsNone(matching_game(g,'NFL',dict(games=[dict(candidate,**changes)])))
        self.assertIsNone(matching_game(g,'NFL',dict(games=[candidate,dict(candidate,id=KID+'2')])))
        self.assertEqual(matching_game(g,'NFL',dict(games=[dict(candidate,start='2026-10-25T09:30:00-04:00')])),
                         dict(candidate,start='2026-10-25T09:30:00-04:00'))

    def test_all_providers_use_one_game_and_player_fit_with_a_common_safe_cutoff(self):
        source=bundle();before=deepcopy(source)
        game_model=Mock(return_value=(GameForecast(),None));player_model=Mock(return_value=(PlayerForecast(),None,{}))
        models=calculate(source,'EPA','OAM',game_model,player_model)
        game_model.assert_called_once();player_model.assert_called_once()
        self.assertEqual(game_model.call_args.args[1],'EPA');self.assertEqual(player_model.call_args.args[1],'OAM')
        self.assertEqual(game_model.call_args.args[0],player_model.call_args.args[0])
        self.assertEqual(models['cutoff'],'2026-10-23T12:00:00+00:00')
        self.assertEqual(len(models['rows']),10);self.assertEqual(source,before)
        fd,player=models['rows'][:2]
        self.assertAlmostEqual(fd['probability'],.6/.9);self.assertAlmostEqual(fd['model_odds'],-200)
        self.assertAlmostEqual(player['probability'],.65/.9)
        k_yes,k_no=models['rows'][2:4]
        self.assertEqual(k_yes['probability'],.7);self.assertAlmostEqual(k_no['probability'],.3)
        self.assertEqual(k_yes['implied'],.5);self.assertEqual(k_no['implied'],.4)
        p_yes,p_no=models['rows'][6:8]
        self.assertEqual(p_yes['probability'],.7);self.assertAlmostEqual(p_no['probability'],.3)
        self.assertEqual({r['method'] for r in models['rows']},{'EPA','OAM'})

    def test_unsupported_rules_missing_prices_and_missing_models_stay_visible(self):
        source=bundle()
        source['providers']['Polymarket']['snapshot']['rows'][1]['line']=200
        source['providers']['Kalshi']['snapshot']['rows'][0]['yes_ask']=None
        models=calculate(source,'PPP','WHP',lambda *args:(None,'No team history'),lambda *args:(PlayerForecast(),None,{}))
        self.assertEqual(len(models['rows']),10)
        self.assertIsNone(models['rows'][2]['implied']);self.assertIsNone(models['rows'][2]['probability'])
        self.assertIsNone(models['rows'][-1]['probability']);self.assertIsNotNone(models['rows'][1]['probability'])
        self.assertIn('PPP: No team history',models['errors'])
        rendered=sections(source,models['rows'],'All Markets')
        self.assertIn('FanDuel',str(rendered));self.assertIn('Kalshi',str(rendered));self.assertIn('Polymarket',str(rendered))
        self.assertIn('Historical Data',str(rendered));self.assertIn('Model fair odds',str(rendered))

    def test_zero_and_one_probabilities_and_quote_endpoints_are_preserved(self):
        class Certain(PlayerForecast):
            def selection(self,row):return MarketProbability(0,1,0)
        source=bundle();source['providers']['Kalshi']['snapshot']['rows'][1]['yes_ask']=0
        models=calculate(source,'PPP','WHP',lambda *args:(GameForecast(),None),lambda *args:(Certain(),None,{}))
        player=[r for r in models['rows'] if r['provider']=='Kalshi' and r['index']==1]
        self.assertEqual([r['probability'] for r in player],[0,1]);self.assertEqual(player[0]['implied'],0)
        self.assertIn('+∞',str(sections(source,models['rows'],'Player Props')))


class SummaryCallbackChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Mock(path=Path(self.tmp.name)/'cache.sqlite3')
        self.schedule=Mock();self.schedule.load.return_value=SimpleNamespace(games=[game()],failed_days=[],stale_days=[])
        self.fd=Mock();self.kalshi=Mock();self.poly=Mock();self.data=bundle()
        self.fd.latest.return_value={'123':self.data['providers']['FanDuel']['snapshot']}
        self.fd.pull.return_value=self.data['providers']['FanDuel']['snapshot']
        for name,service in [('Kalshi',self.kalshi),('Polymarket',self.poly)]:
            service.games.return_value=dict(sport='NFL',games=[exchange(name)])
            service.all_quotes.return_value=self.data['providers'][name]['snapshot']
        self.engines={key:Mock() for key in GAME_METHODS};self.players={key:Mock() for key in PLAYER_METHODS}
        for engine in self.engines.values():engine.forecast.return_value=GameForecast()
        for engine in self.players.values():engine.forecast.return_value=PlayerForecast()
        self.app=create_app(service=self.source,schedule_service=self.schedule,nfl_market_service=self.fd,
            kalshi_service=self.kalshi,polymarket_service=self.poly,ppp_service=self.engines['PPP'],epa_service=self.engines['EPA'],
            elo_service=self.engines['ELO'],whp_service=self.players['WHP'],usage_opportunity_service=self.players['UO'],
            opponent_adjusted_service=self.players['OAM'],pace_game_environment_service=self.players['PGE'])
        self.cb=callback_functions(self.app)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests'))
        network.start();self.addCleanup(network.stop)
        self.catalog=dict(sport='NFL',days=7,games=[game()],Kalshi=dict(games=[exchange()]),Polymarket=dict(games=[exchange('Polymarket')]))

    def quotes(self,pull=False,previous=None):
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull' if pull else 'summary-game')):
            return self.cb['summary_quotes']('123',self.catalog,1 if pull else 0,'market-summary','NFL',previous)

    def test_market_data_contains_all_four_subpages_and_summary_routes_for_all_sports(self):
        nav=next(c for c in components(self.app.layout) if getattr(c,'id',None)=='market-data-navigation')
        links={getattr(c,'id',None):getattr(c,'children',None) for c in components(nav)}
        self.assertEqual([links[k] for k in ['market-page-link','kalshi-page-link','polymarket-page-link','market-summary-page-link']],
                         ['FanDuel','Kalshi','Polymarket','Market Summary'])
        for sport in ['NBA','NFL','NCAAF']:
            href=f'#{sport.lower()}-market-summary'
            self.assertEqual(route(href),sport)
            self.assertEqual(self.cb['restore_game_route'](href),(sport,'market-summary'))
            self.assertEqual(self.cb['navigate']('market-summary',sport)[0],{'display':'none'})
            visible=self.cb['summary_visibility']('market-summary',sport)
            self.assertEqual(visible[0],{'display':'grid'});self.assertEqual(visible[3],href)
        self.assertEqual(self.cb['summary_visibility']('historical','RESEARCH')[1],{'display':'none'})

    def test_initial_summary_never_spends_fanduel_credits_and_manual_pull_refreshes_all(self):
        quoted,status=self.quotes()
        self.fd.pull.assert_not_called();self.fd.latest.assert_called_once_with(['123'])
        self.kalshi.all_quotes.assert_called_once_with(exchange(),refresh=False,save=False)
        self.poly.all_quotes.assert_called_once_with(exchange('Polymarket'),refresh=False,save=False)
        self.assertEqual(sum(len(p['snapshot']['rows']) for p in quoted['providers'].values()),6)
        self.quotes(True,quoted)
        self.fd.pull.assert_called_once();self.assertIn('summary:NFL:123:',self.fd.pull.call_args.args[1])
        self.assertEqual(self.kalshi.all_quotes.call_args.kwargs,dict(refresh=True,save=True))
        self.assertEqual(self.poly.all_quotes.call_args.kwargs,dict(refresh=True,save=True))

    def test_provider_failures_and_wrong_games_do_not_hide_other_quotes(self):
        self.kalshi.all_quotes.side_effect=DataSourceError('Provider offline')
        self.poly.all_quotes.return_value=dict(self.poly.all_quotes.return_value,game_id='another-game')
        quotes,status=self.quotes()
        self.assertIsNotNone(quotes['providers']['FanDuel']['snapshot'])
        self.assertIsNone(quotes['providers']['Kalshi']['snapshot']);self.assertIsNone(quotes['providers']['Polymarket']['snapshot'])
        self.assertIn('Provider offline',str(status));self.assertIn('another game',str(status))

    def test_pending_cache_poll_publishes_fresh_quotes_without_spending_fanduel_credits(self):
        self.kalshi.all_quotes.return_value['updating']=True
        quoted,_=self.quotes();self.assertTrue(quoted['updating'])
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            unchanged,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',deepcopy(quoted),1)
            self.assertIs(unchanged,no_update)
            self.kalshi.all_quotes.return_value={**self.kalshi.all_quotes.return_value,'updating':False,'fetched_at':'2026-10-24T12:05:00Z'}
            updated,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',quoted,2)
            self.assertFalse(updated['updating']);self.assertNotEqual(updated['id'],quoted['id'])
            with self.assertRaises(PreventUpdate):
                self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',updated,3)
            with self.assertRaises(PreventUpdate):
                self.cb['summary_catalog']('market-summary','NFL',7,0,{**self.catalog,'updating':False},3)
        self.fd.pull.assert_not_called()

    def test_pending_catalog_poll_bypasses_recent_check_and_stops_after_refresh(self):
        self.schedule.load.return_value.updating=True
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            old,_=self.cb['summary_catalog']('market-summary','NFL',7,0,None)
        self.assertTrue(old['updating']);self.schedule.load.return_value.updating=False
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            fresh,_=self.cb['summary_catalog']('market-summary','NFL',7,0,old,1)
            self.assertFalse(fresh['updating'])
            with self.assertRaises(PreventUpdate):self.cb['summary_catalog']('market-summary','NFL',7,0,fresh,2)

    def test_returning_to_summary_keeps_quotes_models_and_rendered_accordions(self):
        quoted,_=self.quotes()
        with self.assertRaises(PreventUpdate):self.quotes(previous=quoted)
        self.fd.latest.assert_called_once()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'EPA','OAM',0,'market-summary','NFL','123',None)
        self.engines['EPA'].forecast.assert_called_once();self.players['OAM'].forecast.assert_called_once()
        shown,signature=self.cb['summary_sections'](quoted,models,'All Markets','EPA','OAM','123','market-summary','NFL')
        self.assertIn('Calculated probability',str(shown))
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            with self.assertRaises(PreventUpdate):self.cb['summary_models'](quoted,'EPA','OAM',0,'market-summary','NFL','123',models)
        with self.assertRaises(PreventUpdate):self.cb['summary_sections'](quoted,models,'All Markets','EPA','OAM','123','market-summary','NFL',signature)
        with self.assertRaises(PreventUpdate):self.cb['summary_quotes']('123',self.catalog,1,'market','NFL',None)

    def test_catalogs_load_in_parallel_and_game_cards_select_and_return_to_list(self):
        barrier=Barrier(3,timeout=5)
        def scheduled(*args,**kwargs):barrier.wait();return SimpleNamespace(games=[game()],failed_days=[],stale_days=[])
        def k(*args,**kwargs):barrier.wait();return dict(games=[exchange()])
        def p(*args,**kwargs):barrier.wait();return dict(games=[exchange('Polymarket')])
        self.schedule.load.side_effect=scheduled;self.kalshi.games.side_effect=k;self.poly.games.side_effect=p
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            catalog,status=self.cb['summary_catalog']('market-summary','NFL',7,0,None)
        self.assertEqual(len(catalog['games']),1)
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id={'type':'summary-game-choice','game':'123'})):
            options,selected=self.cb['summary_choose'](catalog,[1],0,None)
        self.assertEqual(selected,'123');self.assertEqual(len(options),1)
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-back')):
            self.assertIsNone(self.cb['summary_choose'](catalog,[],1,'123')[1])

    def test_real_dash_http_quote_model_and_table_callbacks_serialize(self):
        values={'summary-game':'123','summary-catalog':self.catalog,'summary-pull':0,'workspace-page':'market-summary','sport':'NFL',
                'summary-quotes':None,'summary-models':None,'summary-game-method':'PPP','summary-player-method':'WHP',
                'summary-recalculate':0,'summary-category':'All Markets','summary-rendered':None,'summary-cache-poll':0}
        with self.app.server.test_client() as client:
            for name in ['summary_quotes','summary_models','summary_sections']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                    state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=['summary-game.value']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                result=response.get_json()['response']
                if name=='summary_quotes':values['summary-quotes']=result['summary-quotes']['data']
                elif name=='summary_models':values['summary-models']=result['summary-models']['data']
                else:self.assertIn('Model fair odds',str(result));self.assertIn('Polymarket',str(result))
        self.fd.pull.assert_not_called()

    def test_quote_view_is_independent_of_forecasts_and_model_completion_only_updates_cells(self):
        raw_meta=next(meta for meta in self.app.callback_map.values() if meta.get('callback') and
                      meta['callback'].__wrapped__.__name__=='summary_sections')
        self.assertNotIn('summary-models',{item['id'] for item in raw_meta['inputs']})
        quoted,_=self.quotes()
        raw,_=self.cb['summary_sections'](quoted,None,'All Markets','PPP','WHP','123','market-summary','NFL')
        identities=[c.id for c in components(raw) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='summary-model-cell']
        self.assertEqual(len(identities),30);self.assertIn('Market odds',str(raw))
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',None)
        key,meta=next((key,meta) for key,meta in self.app.callback_map.items() if meta.get('callback') and
                      meta['callback'].__wrapped__.__name__=='summary_model_cells')
        pattern=next(item['id'] for item in meta['inputs'] if item['id'].startswith('{'))
        values={'summary-models':models,'summary-game-method':'PPP','summary-player-method':'WHP',pattern:identities,
                'workspace-page':'market-summary','sport':'NFL','summary-quotes':quoted,'summary-game':'123'}
        with self.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[[dict(id=identity,property=output.component_property) for identity in identities] for output in meta['output']],
                inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=['summary-models.data']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        data=response.get_json()['response']
        self.assertEqual(len(data),30);self.assertIn('70.00%',str(data));self.assertNotIn('summary-market-sections',data)
        outdated=self.cb['summary_model_cells'](models,'EPA','OAM',identities,'market-summary','NFL',quoted,'123')
        self.assertNotIn('70.00%',str(outdated));self.fd.pull.assert_not_called()


if __name__=='__main__':main()
