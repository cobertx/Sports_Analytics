"""Offline checks for market links, routing, stat selection and historical charts."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import sys
import tempfile

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app, ALL_TEAMS
from sports_analytics.historical_links import history_href, history_target, target_entity, target_metric, market_metrics
from sports_analytics.historical_view import historical_route
from sports_analytics.market_navigation import remembered_market
from sports_analytics.market_view import market_sections, game_header
from sports_analytics.nba_markets import NBA_MARKETS
from sports_analytics.nfl_markets import NFL_MARKETS
from sports_analytics.metrics import relevant_metrics


def player_frame():
    return pd.DataFrame([dict(sport='NFL',entity_id=identity, entity_name=name, team=team, position='WR',
        opponent='BAL', event_date=day, game_id=str(i), season=2026, season_type='regular',
        week=i+1, matchup=f'{team} @ BAL', receiving_yards=value+i, receptions=4+i, receiving_tds=i%2)
        for i,day in enumerate(pd.date_range('2026-09-01', periods=7, freq='7D', tz='UTC'))
        for identity,name,team,value in [('j','Jerry Jeudy','CLE',50),('a','Another Receiver','DEN',100)]])


def components(component):
    if isinstance(component, (list,tuple)):
        for child in component:yield from components(child)
    elif hasattr(component, 'to_plotly_json'):
        yield component
        yield from components(getattr(component,'children',None))


class MarketHistoryChecks(TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.frame=player_frame()
        self.service=Mock(path=Path(self.directory.name)/'cache.sqlite3')
        self.service.read.side_effect=lambda key:SimpleNamespace(frame=self.frame)
        self.service.load.side_effect=lambda query,refresh=False,local_first=False:SimpleNamespace(
            key='fixture',frame=self.frame,status='cached',fetched_at='2026-10-20T12:00:00Z',notice='')
        self.backtesting=Mock(root=Path(self.directory.name)/'reports')
        self.backtesting.player_inputs.side_effect=lambda sport:self.frame
        self.markets=Mock();self.markets.history.return_value=[]
        self.college=Mock()
        self.college.resolve_school.side_effect=lambda name:{'Michigan Wolverines':'Michigan'}.get(name,name)
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        self.network.start();self.addCleanup(self.network.stop)
        self.app=create_app(service=self.service,backtesting_service=self.backtesting,ncaaf_model_data=self.college,
                            nfl_market_service=self.markets,nba_market_service=self.markets,ncaaf_market_service=self.markets)
        self.callbacks=callback_functions(self.app)

    def test_only_market_headers_link_to_history_and_game_lines_have_no_links(self):
        for sport,markets in [('NBA',NBA_MARKETS),('NFL',NFL_MARKETS),('NCAAF',NFL_MARKETS)]:
            for key,(category,label) in markets.items():
                player=key.startswith('player_')
                subject='Jerry Jeudy' if player else 'Cleveland Browns' if key=='alternate_spreads' else 'Over'
                snapshot=dict(sport=sport,home_team='Cleveland Browns',away_team='Baltimore Ravens',game_id='1',
                    commence_time='2026-10-25T17:00:00Z',rows=[dict(market_key=key,group=category,market=label,
                    subject=subject,selection='Over' if player or key=='alternate_totals' else subject,line=10.5,odds=-110)])
                rendered=market_sections(snapshot,category)
                links=[x for x in components(rendered) if getattr(x,'className',None)=='market-history-link']
                self.assertEqual(len(links),1 if player else 0,(sport,key))
                for outcome in [x for x in components(rendered) if 'market-outcome' in (getattr(x,'className','') or '').split()]:
                    self.assertFalse(any(getattr(c,'className',None)=='market-history-link' for c in components(outcome)))
                for link in links:
                    target=history_target(link.href)
                    self.assertEqual((target['sport'],target['market'],target['kind']),(sport,key,'player' if player else 'team'))
                if player:
                    self.assertEqual(links[0].children,'Jerry Jeudy')
                    self.assertIn(' - ',str(rendered[0].children[0]))
        banner=game_header(dict(start='2026-10-25T17:00:00Z',home=dict(name='Cleveland Browns',abbreviation='CLE'),
                               away=dict(name='Baltimore Ravens',abbreviation='BAL')),sport='NFL')
        self.assertEqual(len([x for x in components(banner) if getattr(x,'className',None)=='market-history-link']),2)

    def test_sidebar_returns_to_saved_game_snapshot_and_market_category(self):
        snapshot=dict(sport='NFL',game_id='123',pulled_at='2026-10-20T12:00:00Z',csv_file='older-snapshot',cost=17,
            commence_time='2026-10-25T17:00:00Z',home_team='Cleveland Browns',away_team='Baltimore Ravens',
            rows=[dict(group='Game'),dict(group='Receiving')])
        self.markets.history.return_value=[{**snapshot,'pulled_at':'2026-10-21T12:00:00Z','csv_file':'newer-snapshot'},snapshot]
        entry=dict(route='#nfl-game-123',snapshot='older-snapshot',category='Receiving',section='market-section-example')
        saved=dict(NFL=entry)
        self.assertEqual(self.callbacks['sport_page_navigation']('historical','NFL','player',saved)[-1],entry['route'])
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_prop_ids={})):
            opened=self.callbacks['open_game'](entry['route'],'NFL','market',[],None,None,None,saved)
        self.assertEqual(opened[4],'older-snapshot')
        categories=self.callbacks['snapshot_categories'](opened[4],entry['route'],'Game',saved)
        self.assertEqual(categories[1],'Receiving')
        for method in ['WHP','UO','OAM','PGE']:
            from dash import no_update
            self.assertEqual(self.callbacks['player_forecast_methods']('#historical-player',method),(no_update,no_update))
            self.assertEqual(self.callbacks['player_forecast_methods'](entry['route'],method)[1],method)
        self.assertIsNone(remembered_market(saved,'NBA'))
        self.assertIsNone(remembered_market(dict(NFL=dict(route='https://example.com')),'NFL'))
        self.assertIsNone(remembered_market(saved,'NFL','#nfl-game-456'))
        self.assertEqual(self.callbacks['sport_page_navigation']('historical','NBA','player',saved)[-1],'#nba-games')
        self.assertEqual(self.callbacks['snapshot_categories']('newer-snapshot',entry['route'],None,saved)[1],'Game')

    def test_player_header_carries_first_visible_selection_line_and_return_context(self):
        snapshot=dict(sport='NBA',game_id='123',commence_time='2026-10-25T23:00:00Z',
            rows=[dict(market_key='player_points_rebounds_assists_alternate',group='Combos',market='Alt PRA',
                subject='Player One',selection=side,line=line,odds=-110)
                for side,line in [('Under',35.5),('Over',40.5),('Over',30.5)]])
        sections=market_sections(snapshot,'Combos')
        link=next(node for node in components(sections) if getattr(node,'className',None)=='market-history-link')
        target=history_target(link.href)
        self.assertEqual((target['line'],target['compare'],target['push']),(30.5,'gt',True))
        self.assertEqual(target['origin'],'#nba-game-123')
        self.assertEqual(target['as_of'],snapshot['commence_time'])
        self.assertIn('another line',link.title)

    def test_dash_http_restores_origin_on_first_market_navigation(self):
        snapshot=dict(sport='NFL',game_id='123',pulled_at='2026-10-20T12:00:00Z',csv_file='saved-snapshot',cost=17,
            commence_time='2026-10-25T17:00:00Z',home_team='Cleveland Browns',away_team='Baltimore Ravens',
            rows=[dict(group='Game'),dict(group='Receiving')])
        self.markets.history.return_value=[snapshot]
        entry=dict(route='#nfl-game-123',snapshot='saved-snapshot',category='Receiving',section='market-section-example')
        values={'workspace-page':'historical','sport':'NFL','entity-type':'player','market-return':dict(NFL=entry),
            'market-url':entry['route'],'market-schedule':[],'market-refresh-signal':None,'market-snapshot':None,'market-detail-context':None,
            'market-category':None}
        self.assertEqual(self.callbacks['sport_page_navigation']('historical','NFL','player',dict(NFL=entry))[-1],entry['route'])
        values['workspace-page']='market'
        with self.app.server.test_client() as client:
            for name in ['open_game','snapshot_categories']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                    state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=['market-url.hash']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                data=response.get_json()['response']
                if name=='open_game':
                    values['market-snapshot']=data['market-snapshot']['value'];self.assertEqual(values['market-snapshot'],'saved-snapshot')
                else:self.assertEqual(data['market-category']['value'],'Receiving')

    def test_url_roundtrip_season_and_identity_safety(self):
        for sport,stamp,year in [('NFL','2027-01-20T18:00:00Z',2026),('NBA','2026-05-01T18:00:00Z',2025)]:
            href=history_href(sport,'player',"D'Angelo Russell",'player_points_alternate',dict(commence_time=stamp))
            target=history_target(href)
            self.assertEqual((target['name'],target['season']),("D'Angelo Russell",year))
            self.assertEqual(historical_route(href),'player')
        self.assertIsNone(history_target('#historical-player?sport=NFL&name=A&name=B'))
        self.assertIsNone(history_target('#historical-player?sport=INVALID&name=A'))
        self.assertEqual(target_entity(self.frame,dict(name='Jerry Jeudy',kind='player',sport='NFL')),'j')
        self.assertIsNone(target_entity(self.frame,dict(name='Jerry Jeud',kind='player',sport='NFL')))
        ambiguous=pd.concat([self.frame,self.frame.assign(entity_id='duplicate')])
        self.assertIsNone(target_entity(ambiguous,dict(name='Jerry Jeudy',kind='player',sport='NFL')))

    def test_player_navigation_selects_correct_stat_and_chart_despite_stale_filters(self):
        href=history_href('NFL','player','Jerry Jeudy','player_reception_yds_alternate',dict(commence_time='2026-10-25T17:00:00Z'))
        cb=self.callbacks
        self.assertEqual(cb['restore_game_route'](href),('NFL','historical'))
        self.assertEqual(cb['statistics_modes']('NFL',href,'team'),'player')
        self.assertEqual(cb['historical_market_scope'](href)[0:2],(2026,'regular'))
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='sport')):
            dataset=cb['load_data'](0,0,'NFL',2026,'player','regular','Michigan',None,'historical')[0]
        self.assertEqual(cb['select_player_teams'](dataset,'DEN',href)[1],ALL_TEAMS)
        entity=cb['select_options'](dataset,ALL_TEAMS,'a',href)[1]
        self.assertEqual(entity,'j')
        category=cb['select_categories'](dataset,entity,ALL_TEAMS,'passing','passing_yards',href)[1]
        metric=cb['select_metrics'](dataset,entity,ALL_TEAMS,category,'receptions',href)[1]
        self.assertEqual((category,metric),('receiving','receiving_yards'))
        chart,cards,records,columns=cb['analyze'](dataset,entity,metric,3,ALL_TEAMS)
        self.assertEqual(len(records),7)
        self.assertTrue(all(r['receiving_yards']<100 for r in records))
        self.assertTrue(chart.data)
        missing=history_href('NFL','player','Unknown Player','player_reception_yds_alternate')
        self.assertIsNone(cb['select_options'](dataset,ALL_TEAMS,'a',missing)[1])
        self.assertIn('No unique match',cb['historical_market_status'](dataset,None,None,missing))

    def test_exact_combination_stats_team_margins_totals_and_missing_components(self):
        nba=pd.DataFrame(dict(pts=[30,20],reb=[7,None],ast=[8,4],plus_minus=[10,-10]))
        augmented=market_metrics(nba,'NBA','player')
        self.assertEqual(augmented.points_rebounds_assists.iloc[0],45)
        self.assertTrue(pd.isna(augmented.points_rebounds_assists.iloc[1]))
        self.assertNotIn('points_rebounds_assists',nba)
        self.assertEqual(target_metric(dict(sport='NBA',kind='player',market='player_points_rebounds_assists_alternate'),augmented.columns),
                         'points_rebounds_assists')
        for sport,frame in [('NBA',nba),('NFL',pd.DataFrame(dict(pf=[30,20],pa=[20,30]))),
                            ('NCAAF',pd.DataFrame(dict(points=[30,20],game_id=['1','1'],team=['Michigan','Alabama'],opponent=['Alabama','Michigan'])))]:
            result=market_metrics(frame,sport,'team')
            self.assertEqual(result.game_total.tolist(),[50,50])
            self.assertEqual(result.scoring_margin.tolist(),[10,-10])
            for market,expected in [('alternate_spreads','scoring_margin'),('alternate_totals','game_total')]:
                self.assertEqual(target_metric(dict(kind='team',sport=sport,market=market),result.columns),expected)
        frame=pd.DataFrame(dict(team=['CLE'],entity_id=['CLE'],entity_name=['CLE']))
        self.assertEqual(target_entity(frame,dict(sport='NFL',kind='team',name='Cleveland Browns')),'CLE')
        for field in ['passing_interceptions','interceptions']:
            available=relevant_metrics(pd.DataFrame(dict(position=['QB'],**{field:[0]})),'NFL','player')
            self.assertEqual(target_metric(dict(sport='NFL',kind='player',market='player_pass_interceptions_alternate'),available),field)

    def test_college_props_read_saved_history_and_team_aliases_resolve(self):
        href=history_href('NCAAF','player','Jerry Jeudy','player_reception_yds_alternate')
        cb=self.callbacks
        self.assertEqual(cb['statistics_modes']('NCAAF',href,'team'),'player')
        dataset=cb['load_data'](0,0,'NCAAF',2026,'player','regular','Michigan',None,'historical')[0]
        self.service.load.assert_not_called()
        self.assertEqual(cb['select_options'](dataset,ALL_TEAMS,None,href)[1],'j')
        self.assertEqual(cb['select_categories'](dataset,'j',ALL_TEAMS,None,None,href)[1],'receiving')
        self.assertEqual(len(cb['analyze'](dataset,'j','receiving_yards',3,ALL_TEAMS)[2]),7)
        self.assertEqual(cb['prepare_college_selection'](dataset,'j',0,'historical'),'')
        team=history_href('NCAAF','team','Michigan Wolverines','alternate_spreads')
        self.assertEqual(cb['historical_market_scope'](team)[2],'Michigan')

    def test_dash_http_name_and_market_selection_callbacks(self):
        dataset=dict(key='fixture',sport='NFL',season=2026,kind='player',phase='regular')
        href=history_href('NFL','player','Jerry Jeudy','player_reception_yds_alternate')
        values={'dataset':dataset,'player-team':ALL_TEAMS,'entity':'a','market-url':href,
                'metric-group':'receiving','metric':'receptions'}
        with self.app.server.test_client() as client:
            self.assertEqual(client.get('/_dash-layout').status_code,200)
            for name in ['select_options','select_categories','select_metrics']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items()
                    if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                output=meta['output']
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in output],
                    inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                    state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],
                    changedPropIds=['market-url.hash']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                data=response.get_json()['response']
                if name=='select_options':
                    values['entity']=data['entity']['value'];self.assertEqual(values['entity'],'j')
                elif name=='select_categories':self.assertEqual(data['metric-group']['value'],'receiving')
                else:self.assertEqual(data['metric']['value'],'receiving_yards')


if __name__=='__main__':main()
