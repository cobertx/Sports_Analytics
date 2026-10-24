"""Offline ranking, method dispatch and Dash integration checks for Mispricing Filter."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import sys
import tempfile
from threading import Barrier

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from models.Game_Outcome.PPP.PPP import MarketProbability
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.mispricing import FILTER_CATEGORY,criteria_values,filter_context,rank_markets,results


def snapshot(sport='NFL'):
    rows=[]
    for i in range(7):
        rows.append(dict(group='Receiving' if i%2 else 'Passing',market='Alt player yards',
            market_key='player_reception_yds_alternate' if i%2 else 'player_pass_yds_alternate',
            subject=f'Player {i}',selection='Over',line=float(i)+.5,odds=100))
    rows.append(dict(group='Game',market='Alternate spreads',market_key='alternate_spreads',
                     subject='Baltimore Ravens',selection='Baltimore Ravens',line=-3.5,odds=100))
    rows.append(dict(group='Game',market='Alternate total',market_key='alternate_totals',
                     subject='Over',selection='Over',line=45.5,odds=100))
    return dict(sport=sport,game_id='123',home_team='Baltimore Ravens',away_team='Cleveland Browns',
                pulled_at='2026-10-21T12:00:00Z',commence_time='2026-10-25T17:00:00Z',rows=rows,cost=17)


class GameForecast:
    diagnostics={'last_game_date':'2026-10-18'}
    def spread(self,team,line):return MarketProbability(.90,.10,0)
    def total(self,line,side):return MarketProbability(.80,.20,0)


class PlayerForecast:
    forecasts={'a':SimpleNamespace(diagnostics={'last_game_date':'2026-10-19'})}
    def selection(self,row):
        probability=.60+int(row['subject'].split()[-1])*.05
        return MarketProbability(probability*.9,(1-probability)*.9,.1)


def components(component):
    if isinstance(component,(list,tuple)):
        for child in component:yield from components(child)
    elif hasattr(component,'to_plotly_json'):
        yield component
        yield from components(getattr(component,'children',None))


class MispricingChecks(TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.source=Mock(path=Path(self.directory.name)/'cache.sqlite3')
        self.saved=snapshot()
        self.markets=Mock();self.markets.history.side_effect=lambda game:[self.saved]
        self.game_services={name:Mock() for name in ['PPP','EPA','ELO']}
        self.player_services={name:Mock() for name in ['WHP','UO','OAM','PGE']}
        for service in self.game_services.values():service.forecast.return_value=GameForecast()
        for service in self.player_services.values():service.forecast.return_value=PlayerForecast()
        self.college=Mock()
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        self.network.start();self.addCleanup(self.network.stop)
        self.app=create_app(service=self.source,nfl_market_service=self.markets,nba_market_service=self.markets,
            ncaaf_market_service=self.markets,ncaaf_model_data=self.college,ncaaf_forecast_services=self.game_services,
            ppp_service=self.game_services['PPP'],epa_service=self.game_services['EPA'],elo_service=self.game_services['ELO'],
            nba_ppp_service=self.game_services['PPP'],nba_epa_service=self.game_services['EPA'],nba_elo_service=self.game_services['ELO'],
            whp_service=self.player_services['WHP'],usage_opportunity_service=self.player_services['UO'],
            opponent_adjusted_service=self.player_services['OAM'],pace_game_environment_service=self.player_services['PGE'])
        self.cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__
                 for m in self.app.callback_map.values() if m.get('callback')}

    def test_controls_are_only_in_filter_panel_and_category_is_parallel_for_every_sport(self):
        panel=next(c for c in components(self.app.layout) if getattr(c,'id',None)=='mispricing-filter-panel')
        ids=[getattr(c,'id',None) for c in components(panel)]
        for control in ['market-probability-threshold','market-edge-threshold','market-apply-criteria',
                        'market-forecast-method','player-forecast-method','market-quick-filter']:
            self.assertEqual(ids.count(control),1)
            self.assertEqual(sum(getattr(c,'id',None)==control for c in components(self.app.layout)),1)
        for sport in ['NBA','NFL','NCAAF']:
            self.saved['sport']=sport
            tabs,value,_=self.cb['snapshot_categories'](self.saved['pulled_at'],f'#{sport.lower()}-game-123',FILTER_CATEGORY)
            self.assertEqual(value,FILTER_CATEGORY)
            self.assertEqual(tabs[-1].label,'Mispricing Filter')
            self.assertIn('Game',[tab.value for tab in tabs])
        self.assertEqual(self.cb['mispricing_visibility'](FILTER_CATEGORY),({'display':'block'},{'display':'none'}))
        self.assertEqual(self.cb['mispricing_visibility']('Game'),({'display':'none'},{'display':'block'}))
        self.cb['show_saved_markets'](FILTER_CATEGORY,self.saved['pulled_at'],'#nfl-game-123',None,'PPP','WHP',None)
        for service in [*self.game_services.values(),*self.player_services.values()]:service.forecast.assert_not_called()

    def test_ranking_uses_strict_thresholds_push_adjustment_and_deduplicates_quotes(self):
        saved=snapshot();saved['rows'].append(dict(saved['rows'][-1]))
        ranked=rank_markets(saved,GameForecast(),PlayerForecast(),criteria_values(75,5),'EPA','PGE')
        matches=ranked['matches']
        self.assertEqual(len(matches),5) # Players 4, 5, 6 plus spread and total; player 3 is exactly 75%.
        self.assertEqual([round(m['edge'],2) for m in matches],[.40,.40,.35,.30,.30])
        self.assertTrue(all(m['probability']>.75 and m['edge']>.05 for m in matches))
        self.assertEqual([m['method'] for m in matches].count('PGE'),3)
        self.assertEqual([m['method'] for m in matches].count('EPA'),2)
        rendered=results(saved,ranked,criteria_values(75,5))
        bodies=[c for c in components(rendered) if c.__class__.__name__=='Tbody']
        self.assertEqual([len(body.children) for body in bodies],[3,2])
        self.assertEqual(bodies[1].children[0].children[0].children,4)
        self.assertFalse(any(getattr(c,'className',None)=='market-history-link' for c in components(rendered)))
        exact=rank_markets(snapshot(),GameForecast(),PlayerForecast(),criteria_values(0,40))
        self.assertFalse(exact['matches'])

    def test_invalid_thresholds_missing_models_bad_quotes_and_empty_results(self):
        for probability,edge in [(True,5),(float('nan'),5),(101,5),(75,None),(75,float('inf')),(75,-101)]:
            with self.assertRaises(ValueError):criteria_values(probability,edge)
        saved=snapshot();saved['rows'][0]['odds']=None;saved['rows'][1]['odds']=0
        ranked=rank_markets(saved,GameForecast(),None,criteria_values(99,0))
        self.assertFalse(ranked['matches']);self.assertEqual(ranked['unavailable'],7)
        self.assertEqual(ranked['evaluated'],2)
        rendered=results(saved,ranked,criteria_values(99,0),['Player history unavailable'])
        self.assertIn('No qualifying markets.',str(rendered));self.assertIn('Player history unavailable',str(rendered))
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-quick-filter')):
            response=self.cb['apply_market_criteria'](0,None,5,FILTER_CATEGORY,1,'#nfl-game-123','saved','PPP','WHP')
        self.assertIs(response[0],no_update);self.assertIs(response[3],no_update)

    def test_quick_filter_runs_each_selected_engine_once_for_all_categories(self):
        for sport in ['NBA','NFL','NCAAF']:
            for player_method in ['WHP','UO','OAM','PGE']:
                for service in [*self.game_services.values(),*self.player_services.values()]:service.reset_mock()
                location=f'#{sport.lower()}-game-123';selection=self.saved['pulled_at']
                with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-quick-filter')):
                    criteria,status,category,request=self.cb['apply_market_criteria'](0,75,5,FILTER_CATEGORY,1,location,selection,'EPA',player_method)
                rendered,history=self.cb['quick_filter_markets'](request,category,location,selection,'EPA',player_method,criteria,None)
                self.assertIn('Top 3 by edge',str(rendered));self.assertIn('Remaining qualifying markets',str(rendered))
                self.assertEqual(history['game']['EPA']['latest_date'],'2026-10-18')
                self.assertEqual(history['player'][player_method]['latest_date'],'2026-10-19')
                self.game_services['EPA'].forecast.assert_called_once()
                self.player_services[player_method].forecast.assert_called_once()
                for name,service in self.game_services.items():
                    if name!='EPA':service.forecast.assert_not_called()
                for name,service in self.player_services.items():
                    if name!=player_method:service.forecast.assert_not_called()
                received=self.player_services[player_method].forecast.call_args.args[0]
                self.assertEqual(len(received['rows']),7 if player_method=='WHP' else 9)
                self.assertEqual(received['sport'],sport)
        self.markets.pull.assert_not_called()

    def test_changed_context_drops_stale_rankings_and_partial_failures_keep_other_markets(self):
        criteria=criteria_values(75,5);location='#nfl-game-123';selection=self.saved['pulled_at']
        request=filter_context(location,selection,'EPA','WHP',criteria)
        for category,loc,selected,method,player,applied in [
            ('Game',location,selection,'EPA','WHP',criteria),
            (FILTER_CATEGORY,'#nfl-game-456',selection,'EPA','WHP',criteria),
            (FILTER_CATEGORY,location,'another snapshot','EPA','WHP',criteria),
            (FILTER_CATEGORY,location,selection,'PPP','WHP',criteria),
            (FILTER_CATEGORY,location,selection,'EPA','UO',criteria),
            (FILTER_CATEGORY,location,selection,'EPA','WHP',criteria_values(80,5))]:
            rendered,history=self.cb['quick_filter_markets'](request,category,loc,selected,method,player,applied,None)
            self.assertIn('Click Quick Filtering',str(rendered));self.assertIs(history,no_update)
        self.game_services['EPA'].forecast.assert_not_called();self.player_services['WHP'].forecast.assert_not_called()
        self.player_services['WHP'].forecast.side_effect=DataSourceError('No saved player history.')
        rendered,history=self.cb['quick_filter_markets'](request,FILTER_CATEGORY,location,selection,'EPA','WHP',criteria,None)
        self.assertIn('2 qualifying selections',str(rendered));self.assertIn('7 unavailable quotes excluded',str(rendered))
        self.assertIn('Player estimates are unavailable',str(rendered))

    def test_real_dash_http_button_applies_and_renders_all_market_results(self):
        values={'market-apply-criteria':0,'market-probability-threshold':75,'market-edge-threshold':5,
            'market-category':FILTER_CATEGORY,'market-quick-filter':1,'market-url':'#nfl-game-123',
            'market-snapshot':self.saved['pulled_at'],'market-forecast-method':'EPA','player-forecast-method':'PGE',
            'market-forecast-history':None}
        with self.app.server.test_client() as client:
            for name in ['apply_market_criteria','quick_filter_markets']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                    state=[dict(**item,value=values[item['id']]) for item in meta['state']],
                    changedPropIds=['market-quick-filter.n_clicks'] if name=='apply_market_criteria' else ['mispricing-request.data']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                data=response.get_json()['response']
                if name=='apply_market_criteria':
                    values['market-applied-criteria']=data['market-applied-criteria']['data']
                    values['mispricing-request']=data['mispricing-request']['data']
                else:
                    self.assertIn('Top 3 by edge',str(data));self.assertIn('Remaining qualifying markets',str(data))
                    self.assertIn('EPA',str(data));self.assertIn('PGE',str(data))

    def test_quick_filter_computes_independent_game_and_player_forecasts_concurrently(self):
        barrier=Barrier(2,timeout=5)
        def game(snapshot):
            barrier.wait()
            return GameForecast()
        def player(snapshot):
            barrier.wait()
            return PlayerForecast()
        self.game_services['PPP'].forecast.side_effect=game
        self.player_services['WHP'].forecast.side_effect=player
        criteria=criteria_values(75,5)
        for sport in ['NBA','NFL']:
            self.saved['sport']=sport
            location=f'#{sport.lower()}-game-123';selection=self.saved['pulled_at']
            request=filter_context(location,selection,'PPP','WHP',criteria)
            rendered,_=self.cb['quick_filter_markets'](request,FILTER_CATEGORY,location,selection,
                                                     'PPP','WHP',criteria,None)
            self.assertIn('5 qualifying selections',str(rendered))


if __name__=='__main__':main()
