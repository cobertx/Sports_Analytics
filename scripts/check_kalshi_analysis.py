"""Binary settlement, quote/model comparison, filter scope and Dash callback checks."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash._utils import to_json
from dash.exceptions import PreventUpdate
from models.Game_Outcome.PPP.PPP import MarketProbability,PPPForecast
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi import contract
from sports_analytics.kalshi_analysis import (FILTER,adapter,calculate,decorate,identity,implied,
                                             model_snapshot,rank,threshold)
from sports_analytics import kalshi_view as view
from sports_analytics.exchange_render import render
from check_exchange_updates import apply_patch,normalized
import check_kalshi as fixtures
from check_kalshi import raw_market,components,NOW,GAME


def row(family='TOTAL',index=0,**changes):
    event='KXNFL'+family+'-26OCT25PITNO'
    raw=raw_market(event,ticker=event+f'-{index}',market_type='binary',strike_type='greater',
                   floor_strike=40+index+.5,title=f'Over {40+index+.5} points',yes_sub_title=f'Over {40+index+.5} points',
                   no_sub_title=f'Over {40+index+.5} points',rules_secondary='',**changes)
    return contract(raw,dict(event_ticker=event,title='Pittsburgh vs New Orleans: Total Points'),'NFL')


def game():return dict(sport='NFL',id=GAME,title='Pittsburgh at New Orleans',start='2026-10-25T13:30:00Z',events=[GAME,'KXNFLTOTAL-26OCT25PITNO'])
def snapshot():return dict(sport='NFL',game_id=GAME,category='Game Lines',fetched_at=NOW.isoformat(),rows=[row(index=i) for i in range(8)],notices=[],saved=None)


class KalshiAnalysisChecks(TestCase):
    def setUp(self):
        self.game=game();self.snapshot=snapshot()
        self.gf=PPPForecast('NO','PIT',30,20,12,10,diagnostics={'last_game_date':'2026-10-18'})
        self.game_engine=Mock(return_value=(self.gf,None))
        self.pf=SimpleNamespace(selection=Mock(return_value=MarketProbability(.8,.2,0)),detail=lambda r:'Saved player history')
        self.player_engine=Mock(return_value=(self.pf,None,dict(latest_date='2026-10-20')))

    def calculate(self,snapshot=None,previous=None,method='PPP',player_method='WHP'):
        return calculate(self.game,snapshot or self.snapshot,'live',method,player_method,self.game_engine,self.player_engine,previous)

    def test_implied_uses_each_ask_payout_and_never_substitutes_bids_or_last_trade(self):
        r=dict(self.snapshot['rows'][0],yes_ask=.6,no_ask=.45,yes_bid=.5,last_price=.55,notional=2)
        self.assertEqual(implied(r,'yes'),.3);self.assertEqual(implied(r,'no'),.225)
        self.assertIsNone(implied(dict(r,yes_ask=None),'yes'))
        self.assertEqual(implied(dict(r,yes_ask=0),'yes'),0)
        for change in [dict(yes_ask=3),dict(notional=0),dict(yes_ask=float('nan')),dict(yes_ask=True)]:
            self.assertIsNone(implied(dict(r,**change),'yes'))

    def test_integer_boundaries_retain_equality_probability_instead_of_refunding_pushes(self):
        r=self.snapshot['rows'][0]
        for kind,expected in [('greater',(40.5,'Over')),('greater_or_equal',(39.5,'Over')),
                               ('less',(39.5,'Under')),('less_or_equal',(40.5,'Under'))]:
            self.assertEqual(threshold(dict(r,line=40,strike_type=kind)),expected)
        saved=dict(r,line=40,strike_type='greater')
        models=self.calculate(dict(self.snapshot,rows=[saved]))
        probability=models['predictions'][saved['ticker']]['probability']
        self.assertEqual(probability,self.gf.total(40.5,'Over').win)
        self.assertNotEqual(probability,self.gf.total(40,'Over').probability)

    def test_supported_exact_team_player_targets_and_unsupported_contracts(self):
        total=self.snapshot['rows'][0]
        spread=dict(total,event_ticker='KXNFLSPREAD-26OCT25PITNO',yes_label='Pittsburgh wins by over 3.5 points',line=3.5)
        target,invert=adapter(spread,'NFL',['Pittsburgh Steelers','New Orleans Saints'])
        self.assertEqual((target['subject'],target['line'],invert),('Pittsburgh Steelers',-3.5,False))
        player=dict(total,event_ticker='KXNFLPASSYDS-26OCT25PITNO',event_title='Test Quarterback: Passing Yards',category='Passing Props',line=200,strike_type='greater_or_equal')
        target,_=adapter(player,'NFL',[])
        self.assertEqual((target['market_key'],target['subject'],target['line']),('player_pass_yds_alternate','Test Quarterback',199.5))
        for change in [dict(market_type='scalar'),dict(cap_strike=250),dict(strike_type='between'),
                       dict(event_ticker='KXNFL1HTOTAL-TEST'),dict(rules_secondary='Ties settle at $0.50.'),
                       dict(rules_primary='Protected market: refund if inactive')]:
            self.assertIsNone(adapter(dict(total,**change),'NFL',[]))
        self.assertIsNone(adapter(dict(player,event_title='Unknown market'),'NFL',[]))
        legacy=dict(total,strike_type=None)
        self.assertEqual(threshold(legacy),(40.5,'Over'))

    def test_forecasts_run_once_and_price_polls_and_criteria_reuse_predictions(self):
        model=self.calculate()
        self.game_engine.assert_called_once();self.player_engine.assert_not_called()
        revised=deepcopy(self.snapshot);revised['rows'][0]['yes_ask']=.8
        revised['fetched_at']=(NOW+timedelta(seconds=5)).isoformat()
        self.assertEqual(self.calculate(revised,model),model)
        self.game_engine.assert_called_once()
        shown=decorate(revised,self.game,'live','PPP','WHP',model,dict(probability=.5,edge=.05))
        first=shown['rows'][0]
        self.assertEqual(first['yes_edge'],first['yes_probability']-.8)
        self.assertEqual(first['no_probability'],1-first['yes_probability'])
        # New lines use the original historical cutoff, even after kickoff.
        revised['rows'].append(dict(revised['rows'][0],ticker='KXNFLTOTAL-NEW',line=45))
        revised['fetched_at']='2026-10-26T00:00:00Z'
        next_model=self.calculate(revised,model)
        self.assertEqual(next_model['cutoff'],model['cutoff'])
        self.assertEqual(self.game_engine.call_args.args[0]['pulled_at'],model['cutoff'])

    def test_model_context_rule_changes_missing_history_and_invalid_home_away_are_isolated(self):
        model=self.calculate()
        shown=decorate(self.snapshot,self.game,'other.json','PPP','WHP',model)
        self.assertTrue(all(r['yes_probability'] is None for r in shown['rows']))
        revised=deepcopy(self.snapshot);revised['rows'][0]['rules_primary']='Other settlement'
        shown=decorate(revised,self.game,'live','PPP','WHP',model)
        self.assertIsNone(shown['rows'][0]['yes_probability'])
        self.game_engine.return_value=(None,'No saved team history')
        failed=self.calculate(method='EPA')
        self.assertIn('EPA: No saved team history',failed['errors'])
        self.assertTrue(all(r['probability'] is None for r in failed['predictions'].values()))
        ambiguous=dict(self.game,title='Pittsburgh vs New Orleans')
        with self.assertRaises(DataSourceError):model_snapshot(ambiguous,self.snapshot)
        cutoff=model_snapshot(self.game,dict(self.snapshot,fetched_at='2026-10-26T00:00:00Z'))['pulled_at']
        self.assertEqual(cutoff,'2026-10-25T13:30:00+00:00')

    def test_rank_sides_strict_thresholds_missing_inactive_and_duplicate_quotes(self):
        model=self.calculate()
        shown=decorate(self.snapshot,self.game,'live','PPP','WHP',model,dict(probability=0,edge=-1))
        ranked=rank(shown,dict(probability=0,edge=-1))
        self.assertEqual(len(ranked['matches']),16)
        self.assertEqual([i['edge'] for i in ranked['matches']],sorted([i['edge'] for i in ranked['matches']],reverse=True))
        p=shown['rows'][0]['yes_probability']
        exact=rank(dict(shown,rows=[shown['rows'][0]]),dict(probability=p,edge=-1))
        self.assertFalse(any(i['side']=='yes' for i in exact['matches']))
        shown['rows']+=[shown['rows'][0]]
        self.assertEqual(len(rank(shown,dict(probability=0,edge=-1))['matches']),16)
        shown['rows'][0]['status']='closed'
        self.assertEqual(rank(shown,dict(probability=0,edge=-1))['unavailable'],2)

    def test_model_and_criteria_changes_patch_rows_without_replacing_accordions(self):
        models=self.calculate()
        first=decorate(self.snapshot,self.game,'live','PPP','WHP',models)
        before=render(first,table_open=True,rules_open=True)
        revised=deepcopy(models)
        revised['predictions'][first['rows'][-1]['ticker']]['probability']=.999
        after=decorate(self.snapshot,self.game,'live','PPP','WHP',revised,dict(probability=.75,edge=.05))
        update=render(after,before[-1],table_open=True,rules_open=True)
        self.assertEqual(apply_patch(before[3],update[3]),normalized(view.market_sections(after)))
        self.assertEqual(apply_patch(before[0],update[0]),normalized(view.table(after)[0]))
        self.assertTrue(all('open' not in op['location'] for op in normalized(update[3])['operations']))
        self.assertIs(update[1],no_update);self.assertIs(update[2],no_update)

    def test_player_forecast_and_game_methods_use_same_historical_snapshot_and_binary_sides(self):
        player=dict(self.snapshot['rows'][0],ticker='KXNFLPASSYDS-PLAYER',event_ticker='KXNFLPASSYDS-26OCT25PITNO',
                    event_title='Test Quarterback: Passing Yards',category='Passing Props',line=200,strike_type='greater_or_equal')
        selected=dict(self.snapshot,rows=[*self.snapshot['rows'],player])
        for method in ['PPP','EPA','ELO']:
            for player_method in ['WHP','UO','OAM','PGE']:
                models=self.calculate(selected,method=method,player_method=player_method)
                self.assertEqual(models['predictions'][player['ticker']]['probability'],.8)
                self.assertEqual(self.game_engine.call_args.args[1],method)
                self.assertEqual(self.player_engine.call_args.args[1],player_method)
                self.assertEqual(self.game_engine.call_args.args[0]['pulled_at'],self.player_engine.call_args.args[0]['pulled_at'])
                target=self.pf.selection.call_args.args[0]
                self.assertEqual((target['selection'],target['line']),('Over',199.5))


class KalshiFilterCallbacks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Mock(path=Path(self.tmp.name)/'data.sqlite3')
        self.exchange=Mock();self.game=game();self.snapshot=snapshot()
        self.exchange.all_quotes.return_value=self.snapshot
        self.engines={m:Mock() for m in ['PPP','EPA','ELO']}
        self.forecast=PPPForecast('NO','PIT',30,20,12,10,diagnostics={'last_game_date':'2026-10-18'})
        for engine in self.engines.values():engine.forecast.return_value=self.forecast
        self.players={m:Mock() for m in ['WHP','UO','OAM','PGE']}
        for engine in self.players.values():engine.forecast.return_value=SimpleNamespace(forecasts={},selection=lambda r:None)
        self.no_network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external HTTP'))
        self.no_network.start();self.addCleanup(self.no_network.stop)
        self.app=create_app(service=self.source,kalshi_service=self.exchange,ncaaf_model_data=Mock(),ncaaf_forecast_services=self.engines,
            ppp_service=self.engines['PPP'],epa_service=self.engines['EPA'],elo_service=self.engines['ELO'],
            nba_ppp_service=self.engines['PPP'],nba_epa_service=self.engines['EPA'],nba_elo_service=self.engines['ELO'],
            whp_service=self.players['WHP'],usage_opportunity_service=self.players['UO'],
            opponent_adjusted_service=self.players['OAM'],pace_game_environment_service=self.players['PGE'])
        self.cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in self.app.callback_map.values() if m.get('callback')}

    def scan(self,selected='live'):
        with patch('sports_analytics.kalshi_analysis_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-apply-criteria')):
            return self.cb['kalshi_scan'](1,0,0,-100,self.game,FILTER,selected,'kalshi')

    def test_controls_and_parallel_filter_tab_all_sports_and_paused_polling(self):
        ids=[getattr(c,'id',None) for c in components(self.app.layout)]
        for name in ['kalshi-game-method','kalshi-player-method','kalshi-probability-threshold','kalshi-edge-threshold','kalshi-quick-filter',
                     'kalshi-quick-game-methods','kalshi-quick-player-methods','kalshi-quick-probability','kalshi-quick-edge']:
            self.assertEqual(ids.count(name),1)
        for sport in ['NBA','NFL','NCAAF']:
            g=dict(self.game,sport=sport,events=['KX'+sport+'GAME-TEST'])
            self.assertEqual(view.categories(g),['Game Lines',FILTER])
        visible=self.cb['kalshi_filter_visibility'](FILTER)
        self.assertEqual(visible[0],{'display':'block'});self.assertEqual(visible[1:],({'display':'none'},)*4)
        visibility=self.cb['kalshi_visibility']('kalshi','NFL',view.href('NFL',GAME),5,'live',self.game,FILTER)
        self.assertTrue(visibility[-2])
        with self.assertRaises(PreventUpdate):self.cb['kalshi_quotes']('kalshi','NFL',self.game,FILTER,'live',1,0,5)

    def test_live_scan_ranking_method_changes_and_polling_do_not_refit(self):
        scan,criteria,_=self.scan()
        self.exchange.all_quotes.assert_called_once_with(self.game,refresh=False,save=False)
        models=self.cb['kalshi_models'](None,scan,self.game,'EPA','WHP','live','kalshi',FILTER,None)
        self.engines['EPA'].forecast.assert_called_once()
        output=self.cb['kalshi_filter_results'](scan,criteria,self.game,'EPA','WHP','live',FILTER,'kalshi',models)
        bodies=[c for c in components(output) if c.__class__.__name__=='Tbody']
        self.assertEqual([len(b.children) for b in bodies],[3,13])
        self.assertIn('Top 3 by edge',str(output));self.assertIn('Remaining qualifying markets',str(output))
        self.assertIs(self.cb['kalshi_models'](None,scan,self.game,'EPA','WHP','live','kalshi',FILTER,models),no_update)
        self.engines['EPA'].forecast.assert_called_once()
        changed=self.cb['kalshi_models'](None,scan,self.game,'ELO','WHP','live','kalshi',FILTER,models)
        self.assertEqual(changed['context'][-2],'ELO');self.engines['ELO'].forecast.assert_called_once()
        self.assertIn('Click Apply Criteria',str(self.cb['kalshi_filter_results'](scan,criteria,dict(self.game,id='another'),'EPA','WHP','live',FILTER,'kalshi',models)))

    def test_archive_scan_never_fetches_live_prices_and_bad_criteria_preserve_results(self):
        self.exchange.history.side_effect=lambda g,c:['archive.json'] if c=='Game Lines' else []
        self.exchange.saved.return_value=self.snapshot
        scan,criteria,status=self.scan('archive.json')
        self.exchange.all_quotes.assert_not_called();self.exchange.saved.assert_called_once_with(self.game,'Game Lines','archive.json')
        self.assertIn('saved category only',status)
        invalid=self.cb['kalshi_scan'](1,0,None,5,self.game,FILTER,'live','kalshi')
        self.assertIs(invalid[0],no_update);self.assertIs(invalid[1],no_update)
        with self.assertRaises(PreventUpdate):self.cb['kalshi_scan'](1,0,75,5,self.game,'Game Lines','live','kalshi')

    def test_manual_pull_from_filter_archive_switches_live_and_saves_without_duplicate_fetch(self):
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-pull')):
            selected=self.cb['kalshi_quotes']('kalshi','NFL',self.game,FILTER,'archive.json',0,1,5)
        self.assertEqual(selected[-1],'live');self.assertTrue(all(value is no_update for value in selected[:3]))
        self.exchange.quotes.assert_not_called()
        with patch('sports_analytics.kalshi_analysis_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-pull')):
            scan,_,_=self.cb['kalshi_scan'](0,1,75,5,self.game,FILTER,'archive.json','kalshi')
        self.exchange.all_quotes.assert_called_once_with(self.game,refresh=True,save=True)
        self.assertEqual(scan['selection'],'live')

    def test_each_sport_dispatches_selected_existing_game_service(self):
        for sport,title in [('NBA','Atlanta Hawks at Charlotte Hornets'),('NFL',self.game['title']),
                             ('NCAAF','Michigan Wolverines at Ohio State Buckeyes')]:
            for engine in self.engines.values():engine.forecast.reset_mock()
            g=dict(self.game,sport=sport,id='KX'+sport+'GAME-TEST',title=title)
            s=deepcopy(self.snapshot);s.update(sport=sport,game_id=g['id'])
            for r in s['rows']:r.update(ticker=r['ticker'].replace('KXNFL','KX'+sport),event_ticker=r['event_ticker'].replace('KXNFL','KX'+sport))
            model=self.cb['kalshi_models'](s,None,g,'EPA','WHP','live','kalshi','Game Lines',None)
            self.engines['EPA'].forecast.assert_called_once()
            self.engines['PPP'].forecast.assert_not_called();self.engines['ELO'].forecast.assert_not_called()
            self.assertTrue(model['predictions'])

    def test_http_models_and_comparison_outputs_serialize_without_dependency_reordering(self):
        values={'kalshi-quotes':self.snapshot,'kalshi-filter-quotes':None,'kalshi-context':self.game,
                'kalshi-game-method':'PPP','kalshi-player-method':'WHP','kalshi-snapshot':'live','workspace-page':'kalshi',
                'kalshi-category':'Game Lines','kalshi-models':None,'kalshi-applied-criteria':None,
                'kalshi-detail-table':False,'kalshi-rules-panel':False,'kalshi-rules-signature':None}
        values['market-url']=view.href('NFL',GAME)
        for name in ['kalshi_models','kalshi_table']:
            key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
            outputs=meta['output']
            output=([dict(id=o.component_id,property=o.component_property) for o in outputs] if isinstance(outputs,list)
                    else dict(id=outputs.component_id,property=outputs.component_property))
            with self.app.server.test_client() as client:
                response=client.post('/_dash-update-component',json=dict(output=key,outputs=output,
                    inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                    state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=['kalshi-quotes.data']))
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            data=response.get_json()['response']
            if name=='kalshi_models':values['kalshi-models']=data['kalshi-models']['data']
            else:
                text=to_json(data)
                self.assertIn('Calculated probability',text);self.assertIn('market-line-comparison',text)
                self.assertIn('51%',text.replace('51.00%','51%'))


    def quick(self,methods=None,players=None,probability=0,edge=-100,selected='live'):
        return self.cb['kalshi_quick_scan'](1,probability,edge,methods or ['PPP','EPA','ELO'],players or ['WHP','UO','OAM','PGE'],
            self.game,FILTER,selected,'kalshi',view.href('NFL',GAME))

    def test_quick_consensus_fits_each_method_once_and_keeps_apply_state_independent(self):
        player=dict(self.snapshot['rows'][0],ticker='KXNFLPASSYDS-PLAYER',event_ticker='KXNFLPASSYDS-26OCT25PITNO',
                    event_title='Test Quarterback: Passing Yards',category='Passing Props',line=200,strike_type='greater_or_equal')
        self.snapshot['rows'].append(player)
        for engine in self.players.values():
            engine.forecast.return_value=SimpleNamespace(forecasts={},selection=lambda r:MarketProbability(.9,.1,0))
        scan,_=self.quick()
        for engine in [*self.engines.values(),*self.players.values()]:engine.forecast.assert_called_once()
        self.assertEqual(len(scan['ranked']['matches']),18)
        output=self.cb['kalshi_quick_results'](scan,self.game,['PPP','EPA','ELO'],['WHP','UO','OAM','PGE'],0,-100,'live',FILTER,'kalshi')
        self.assertIn('Lowest probability',str(output));self.assertIn('OAM: 90.00% /',str(output))
        self.assertIn('Top 3 by edge',str(output));self.assertIn('Remaining qualifying markets',str(output))
        meta=next(m for m in self.app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__=='kalshi_quick_scan')
        self.assertEqual({o.component_id for o in meta['output']},{'kalshi-quick-scan','kalshi-quick-status'})
        self.assertNotIn('kalshi-probability-threshold',{i['id'] for i in meta['state']})
        self.exchange.all_quotes.assert_called_once_with(self.game,refresh=False,save=False)

    def test_quick_disagreement_and_failed_method_exclude_markets_and_context_changes_hide_results(self):
        self.engines['EPA'].forecast.side_effect=DataSourceError('Missing EPA history')
        scan,status=self.quick()
        self.assertEqual(scan['ranked']['matches'],[])
        self.assertEqual(scan['ranked']['unavailable'],16)
        self.assertIn('EPA: Missing EPA history',scan['errors'])
        self.engines['EPA'].forecast.side_effect=None
        lower=PPPForecast('NO','PIT',20,10,12,10,diagnostics={})
        self.engines['EPA'].forecast.return_value=lower
        scan,_=self.quick(probability=50)
        # Opposite forecasts disagree on every side of these totals.
        self.assertFalse(scan['ranked']['matches'])
        for g,methods,selected,p in [(self.game,['PPP'],'live',50),(self.game,['PPP','EPA','ELO'],'archive.json',50),
                                     (dict(self.game,id='different'),['PPP','EPA','ELO'],'live',50),(self.game,['PPP','EPA','ELO'],'live',60)]:
            output=self.cb['kalshi_quick_results'](scan,g,methods,['WHP','UO','OAM','PGE'],p,-100,selected,FILTER,'kalshi')
            self.assertIn('Click Quick Filtering',str(output))

    def test_quick_archive_is_immutable_and_invalid_inputs_do_not_fetch(self):
        for methods,players in [([],['WHP']),(['PPP'],[]),(['INVALID'],['WHP'])]:
            scan,status=self.cb['kalshi_quick_scan'](1,75,5,methods,players,self.game,FILTER,'live','kalshi',view.href('NFL',GAME))
            self.assertIs(scan,no_update);self.assertIn('Select at least one supported',status)
        self.exchange.all_quotes.assert_not_called()
        self.exchange.history.side_effect=lambda g,c:['archive.json'] if c=='Game Lines' else []
        self.exchange.saved.return_value=self.snapshot
        scan,status=self.quick(selected='archive.json')
        self.exchange.all_quotes.assert_not_called();self.exchange.saved.assert_called_once_with(self.game,'Game Lines','archive.json')
        self.assertIn('saved category only',status)
        self.assertEqual(scan['context'][-3],'archive.json')

    def test_http_quick_scan_and_results_keep_independent_outputs(self):
        values={'kalshi-quick-filter':1,'kalshi-quick-probability':0,'kalshi-quick-edge':-100,
            'kalshi-quick-game-methods':['PPP','EPA'],'kalshi-quick-player-methods':['WHP','PGE'],
            'kalshi-context':self.game,'kalshi-category':FILTER,'kalshi-snapshot':'live','workspace-page':'kalshi',
            'market-url':view.href('NFL',GAME)}
        with self.app.server.test_client() as client:
            for name in ['kalshi_quick_scan','kalshi_quick_results']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                for item in [*meta['inputs'],*meta['state']]:
                    if item['id'].startswith('{'):values.setdefault(item['id'],[])
                outputs=meta['output']
                outputs=([dict(id=o.component_id,property=o.component_property) for o in outputs] if isinstance(outputs,list)
                         else dict(id=outputs.component_id,property=outputs.component_property))
                response=client.post('/_dash-update-component',json=dict(output=key,outputs=outputs,
                    inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                    state=[dict(**item,value=values[item['id']]) for item in meta['state']],
                    changedPropIds=['kalshi-quick-filter.n_clicks'] if name=='kalshi_quick_scan' else ['kalshi-quick-scan.data']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                data=response.get_json()['response']
                self.assertNotIn('kalshi-applied-criteria',data);self.assertNotIn('kalshi-models',data)
                if name=='kalshi_quick_scan':values['kalshi-quick-scan']=data['kalshi-quick-scan']['data']
                else:self.assertIn('Lowest edge (pp)',str(data));self.assertIn('Top 3 by edge',str(data))

    def test_all_category_fetch_retains_exact_links_cache_and_snapshot_saving(self):
        f=fixtures.KalshiChecks();f.setUp();self.addCleanup(f.doCleanups)
        result=f.service.all_quotes(f.game,save=True)
        self.assertEqual(result['category'],'All Markets')
        self.assertEqual({r['event_ticker'] for r in result['rows']},set(f.game['events']))
        self.assertTrue(f.service.history(f.game,'Game Lines'));self.assertTrue(f.service.history(f.game,'Passing Props'))
        calls=f.client.json.call_count;f.service.all_quotes(f.game)
        self.assertEqual(calls,f.client.json.call_count)


if __name__=='__main__':main()
