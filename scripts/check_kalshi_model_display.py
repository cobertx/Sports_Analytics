"""End-to-end forecast polling and visible Kalshi probability/odds/edge rows."""
import json
from threading import Event
from types import SimpleNamespace
from pathlib import Path
import sys
from unittest import TestCase,main
from unittest.mock import patch
from dash._utils import to_json

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from models.Game_Outcome.PPP.PPP import PPPForecast,MarketProbability
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi_analysis import calculate,decorate,model_matches
from sports_analytics.kalshi_view import odds_text
from sports_analytics.kalshi_labels import selection
from sports_analytics.lazy_markets import markets
from scripts.callback_fixtures import wire_value
from check_dashboard_efficiency import post
import check_kalshi_analysis as fixtures


def row_ids(value):
    if isinstance(value,list):
        return [identity for child in value for identity in row_ids(child)]
    if isinstance(value,dict):
        identity=value.get('props',{}).get('id')
        found=[identity] if isinstance(identity,dict) and identity.get('type')=='kalshi-comparison-row' else []
        return found+[identity for child in value.values() for identity in row_ids(child)]
    return []


class ModelDisplayChecks(TestCase):
    def setUp(self):
        self.f=fixtures.KalshiFilterCallbacks();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.app._page_tasks.queue.shutdown)
        self.revision=[1]
        source=patch('sports_analytics.kalshi_analysis.availability_revision',side_effect=lambda sport:list(self.revision))
        source.start();self.addCleanup(source.stop)
        self.values={'kalshi-quotes':self.f.snapshot,'kalshi-filter-quotes':None,'kalshi-context':self.f.game,
            'kalshi-game-method':'PPP','kalshi-player-method':'WHP','kalshi-snapshot':'live',
            'workspace-page':'kalshi','kalshi-category':'Game Lines','kalshi-models':None,
            'market-url':fixtures.view.href('NFL',self.f.game['id']),'kalshi-model-poll':0,'kalshi-model-retry':0,
            'kalshi-detail-table':False,'kalshi-rules-panel':False,'kalshi-rules-signature':None}

    def models(self,trigger='kalshi-model-poll.n_intervals'):
        self.values['kalshi-model-poll']+=1
        response=post(self.f.app,'kalshi_models',self.values,trigger)
        data=response.get('response',{}).get('kalshi-models',{}).get('data')
        if data is not None:self.values['kalshi-models']=data
        return self.values['kalshi-models']

    def complete(self):
        self.models();self.f.app._page_tasks.queue.tasks.join()
        result=self.models();self.assertFalse(result['pending'])
        return result

    def quote_rows(self,models=None,*,expanded=False):
        values={**self.values,'kalshi-models':models if models is not None else self.values['kalshi-models']}
        data=post(self.f.app,'kalshi_table',values,'kalshi-quotes.data')['response']
        self.values['kalshi-rules-signature']=data['kalshi-rules-signature']['data']
        sections=data['kalshi-market-sections']['children']
        def load(value):
            if isinstance(value,dict):
                identity=value.get('props',{}).get('id')
                if isinstance(identity,dict) and identity.get('type')=='lazy-market-body':
                    value['props']['children']=json.loads(to_json(markets.read(identity,0)[0]))
                for child in value.values():load(child)
            elif isinstance(value,list):
                for child in value:load(child)
        if expanded:load(sections)
        return row_ids(sections)

    def overlay(self,identities):
        key,meta=next((k,m) for k,m in self.f.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='kalshi_model_rows')
        values=dict(self.values)
        pattern=next(item['id'] for item in meta['inputs'] if item['id'].startswith('{'))
        values[pattern]=identities
        with self.f.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[[dict(id=identity,property=o.component_property) for identity in identities] for o in meta['output']],
                inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=['kalshi-rules-signature.data']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        return response.get_json()['response']

    def test_all_game_methods_reach_numeric_probability_fair_odds_and_edge_over_http(self):
        for i,method in enumerate(('PPP','EPA','ELO')):
            self.values['kalshi-game-method']=method
            self.values['kalshi-rules-signature']=None
            forecast=PPPForecast('NO','PIT',30+i*3,20,12,10,diagnostics={})
            self.f.engines[method].forecast.return_value=forecast
            self.complete()
            identities=self.quote_rows()
            rows=self.overlay(identities)
            source={row['ticker']:row for row in self.f.snapshot['rows']}
            for key,value in rows.items():
                identity=json.loads(key);row=source[identity['ticker']]
                yes=forecast.total(row['line'],'Over').win
                probability=yes if identity['side']=='yes' else 1-yes
                children=value['children']
                self.assertEqual(children[3]['props']['children'],f'{100*probability:.2f}%')
                self.assertEqual(children[4]['props']['children'],odds_text(probability))
                self.assertEqual(children[5]['props']['children'],f"{100*(probability-row[identity['side']+'_ask']):+.2f} pp")
            self.f.engines[method].forecast.assert_called_once()

    def test_pending_job_finishes_even_when_availability_changes_on_every_poll(self):
        entered,release=Event(),Event();self.addCleanup(release.set)
        original=self.f.engines['PPP'].forecast.return_value
        def slow(*args):
            entered.set()
            if not release.wait(10):raise AssertionError('Forecast was not released')
            return original
        self.f.engines['PPP'].forecast.side_effect=slow
        first=self.models();self.assertTrue(entered.wait(2));self.assertTrue(first['pending'])
        for revision in (2,3,4):
            self.revision=[revision];pending=self.models()
            self.assertEqual(pending['job'],first['job']);self.assertTrue(pending['pending'])
        release.set();self.f.app._page_tasks.queue.tasks.join()
        ready=self.models();self.assertFalse(ready['pending']);self.assertEqual(ready['job'],first['job'])
        self.assertTrue(model_matches(ready,self.f.game,'live','PPP','WHP'))
        self.assertTrue(all(row['yes_probability'] is not None for row in decorate(self.f.snapshot,self.f.game,'live','PPP','WHP',ready)['rows']))
        status=self.f.cb['kalshi_model_status'](ready,self.f.game,'PPP','WHP','live')
        self.assertNotIn('waiting',str(status));self.f.engines['PPP'].forecast.assert_called_once()

    def test_winner_spread_and_total_rows_all_display_both_contract_sides(self):
        base=self.f.snapshot['rows'][0]
        winner=dict(base,ticker='KXNFLGAME-TEST-PIT',event_ticker='KXNFLGAME-TEST',
            title='Pittsburgh wins',yes_label='Pittsburgh',line=None,strike_type='structured')
        spread=dict(base,ticker='KXNFLSPREAD-TEST-PIT4',event_ticker='KXNFLSPREAD-TEST',
            title='Pittsburgh wins by over 3.5 points',yes_label='Pittsburgh wins by over 3.5 points',line=3.5)
        self.f.snapshot['rows']=[winner,spread,base]
        self.complete();identities=self.quote_rows(expanded=True);self.assertEqual(len(identities),6)
        for value in self.overlay(identities).values():
            self.assertNotEqual(value['children'][3]['props']['children'],'—')
            self.assertNotEqual(value['children'][4]['props']['children'],'—')
            self.assertNotEqual(value['children'][5]['props']['children'],'—')

    def test_quote_render_after_forecast_completion_cannot_leave_rows_blank(self):
        ready=self.complete()
        pending=dict(ready,pending=True,predictions={})
        identities=self.quote_rows(pending)
        meta=next(m for m in self.f.app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__=='kalshi_model_rows')
        self.assertIn({'id':'kalshi-rules-signature','property':'data'},meta['inputs'])
        self.assertTrue(identities)
        for value in self.overlay(identities).values():
            self.assertNotEqual(value['children'][3]['props']['children'],'—')
            self.assertNotEqual(value['children'][4]['props']['children'],'—')
            self.assertNotEqual(value['children'][5]['props']['children'],'—')

    def test_refresh_keeps_completed_values_visible_but_method_change_never_reuses_them(self):
        ready=self.complete();self.revision=[2]
        entered,release=Event(),Event();self.addCleanup(release.set)
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise AssertionError('Forecast was not released')
            return calculate(*args,**kwargs)
        with patch('sports_analytics.kalshi_analysis_callbacks.calculate',side_effect=slow):
            pending=self.models();self.assertTrue(entered.wait(2))
            self.assertTrue(pending['pending']);self.assertTrue(pending['keep_visible'])
            self.assertEqual(pending['predictions'],ready['predictions'])
            self.values['kalshi-game-method']='EPA'
            other=self.models();self.assertTrue(other['pending']);self.assertFalse(other['keep_visible'])
            shown=decorate(self.f.snapshot,self.f.game,'live','EPA','WHP',ready)
            self.assertTrue(all(row['yes_probability'] is None for row in shown['rows']))
            release.set();self.f.app._page_tasks.queue.tasks.join()
        self.assertEqual(self.models()['status'],'complete')

    def test_missing_history_is_terminal_and_explained_instead_of_waiting_forever(self):
        self.f.engines['PPP'].forecast.side_effect=DataSourceError('No saved team history fixture')
        ready=self.complete();self.assertEqual(ready['status'],'error')
        self.assertFalse(ready['pending'])
        status=self.f.cb['kalshi_model_status'](ready,self.f.game,'PPP','WHP','live')
        self.assertIn('No saved team history fixture',str(status));self.assertNotIn('waiting',str(status))

    def test_applied_criteria_can_render_a_completed_forecast_after_a_source_revision(self):
        ready=self.complete();self.revision=[2]
        scan=dict(snapshot=self.f.snapshot,selection='live')
        result=self.f.cb['kalshi_filter_results'](scan,dict(probability=0,edge=-1),self.f.game,
            'PPP','WHP','live',fixtures.FILTER,'kalshi',ready)
        self.assertIn('Top 3 by edge',str(result));self.assertNotIn('Calculating the selected',str(result))

    def test_actual_player_contract_title_is_supported_for_every_player_method(self):
        game=dict(self.f.game,sport='NBA',id='KXNBAGAME-PLAYER',title='Boston at Toronto')
        row=dict(self.f.snapshot['rows'][0],ticker='KXNBAPTS-PLAYER',event_ticker='KXNBAPTS-GAME',
            category='Player Props',event_title='Boston vs Toronto: Points',title='Player One: 20+ points',
            yes_label='Player One: 20+',line=19.5)
        protected=dict(row,ticker='KXNBAPTSPROT-PLAYER',rules_secondary='This contract is protected.')
        snapshot=dict(self.f.snapshot,sport='NBA',game_id=game['id'],category='Player Props',rows=[row,protected])
        self.values.update({'kalshi-context':game,'kalshi-quotes':snapshot,'kalshi-category':'Player Props',
            'market-url':fixtures.view.href('NBA',game['id'])})
        for method in ('WHP','UO','OAM','PGE'):
            self.f.players[method].forecast.return_value=SimpleNamespace(forecasts={},selection=lambda row:MarketProbability(.8,.2,0))
            self.values['kalshi-player-method']=method
            ready=self.complete()
            self.assertEqual(ready['predictions'][row['ticker']]['probability'],.8)
            self.assertIsNone(ready['predictions'][protected['ticker']]['probability'])
            shown=decorate(snapshot,game,'live','PPP',method,ready)
            self.assertEqual(shown['rows'][0]['yes_probability'],.8)
            self.assertAlmostEqual(shown['rows'][0]['no_probability'],.2)
            self.assertEqual(selection(row,'yes',snapshot)[0],'Player One Over 19.5')
            self.f.players[method].forecast.assert_called_once()


if __name__=='__main__':main()
