"""Offline per-prompt assignments, consensus, persistence and Dash wire checks."""
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from models.Game_Outcome.PPP.PPP import MarketProbability
from sports_analytics import quick_methods as qm
from scripts.callback_fixtures import wire_value
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi_analysis import FILTER,prompt_targets
from sports_analytics.mispricing import FILTER_CATEGORY
import check_mispricing as fanduel
import check_kalshi_analysis as kalshi


def fields(prefix,sport,game_id,overrides):
    ids=[dict(type=prefix+'-quick-prompt-method',scope=qm.scope(prefix,sport,game_id),prompt=key) for key in overrides]
    return list(overrides.values()),ids


def invoke(app,name,values,prompt_values,prompt_ids,trigger=None):
    key,meta=next((key,meta) for key,meta in app.callback_map.items()
                  if meta.get('callback') and meta['callback'].__wrapped__.__name__==name)
    def value(item):
        if item['id'].startswith('{'):return prompt_values if item['property']=='value' else prompt_ids
        return wire_value(item,values)
    outputs=meta['output']
    result=app.server.test_client().post('/_dash-update-component',json=dict(output=key,
        outputs=([dict(id=o.component_id,property=o.component_property) for o in outputs] if isinstance(outputs,list)
                 else dict(id=outputs.component_id,property=outputs.component_property)),
        inputs=[dict(**item,value=value(item)) for item in meta['inputs']],
        state=[dict(**item,value=value(item)) for item in meta['state']],
        changedPropIds=[trigger or meta['inputs'][0]['id']+'.'+meta['inputs'][0]['property']]))
    if result.status_code!=200:raise AssertionError(result.get_data(as_text=True))
    output=result.get_json()['response']
    history=output.get('mispricing-forecast-history',{}).get('data',{})
    if name=='quick_filter_markets' and history.get('pending'):
        app._filter_jobs.tasks.wait(history['job'],10)
        values['mispricing-forecast-history']=history
        return invoke(app,name,values,prompt_values,prompt_ids,'market-quick-poll.n_intervals')
    return output


def quick_scan(f,*args):
    scan,status=f.cb['kalshi_quick_scan'](*args)
    return fanduel.finish_filter(f.app,scan),status


class PromptMethodChecks(TestCase):
    def fixture(self,cls):
        result=cls();result.setUp();self.addCleanup(result.doCleanups)
        return result

    def test_headers_share_lines_but_separate_players_statistics_and_game_types(self):
        points=dict(market_key='player_points_alternate',market='Points',subject='Player A',line=20.5)
        rows=[points,dict(points,line=25.5),dict(points,market_key='player_points'),
              dict(points,subject='Player B'),dict(points,market_key='player_assists_alternate'),
              dict(points,market_key='alternate_spreads',subject='Boston Celtics',line=0),
              dict(points,market_key='alternate_spreads',subject='Boston Celtics',line=-3.5),
              dict(points,market_key='alternate_totals',subject='Over'),
              dict(points,market_key='alternate_totals',subject='Under')]
        self.assertEqual(len(qm.catalog(rows)),6)
        self.assertIn('Player A — Alt Assists',[p['label'] for p in qm.catalog(rows).values()])
        for sport in ('NBA','NFL','NCAAF'):
            rendered=qm.controls('market',rows,qm.scope('market',sport,'123'))
            dropdowns=[c for c in fanduel.components(rendered) if c.__class__.__name__=='Dropdown']
            self.assertEqual(len(dropdowns),6)
            self.assertTrue(all(c.persistence and c.persistence_type=='session' for c in dropdowns))
            self.assertEqual(len({c.id['prompt'] for c in dropdowns}),6)

    def test_choices_inherit_override_skip_and_validate_family_and_game(self):
        row=dict(market_key='player_points_alternate',subject='Player A')
        key=qm.prompt_key(row);scope=qm.scope('market','NBA','123')
        values,ids=fields('market','NBA','123',{key:[qm.INHERIT]})
        self.assertEqual(qm.choices(values,ids,scope),{})
        self.assertEqual(qm.choices([[qm.INHERIT,'UO','WHP','UO']],ids,scope),{key:['WHP','UO']})
        self.assertEqual(qm.choices([[]],ids,scope),{key:[]})
        for value in (['PPP'],['unknown'],None):
            with self.assertRaises(ValueError):qm.choices([value],ids,scope)
        with self.assertRaisesRegex(ValueError,'another game'):
            qm.choices([['WHP']],ids,qm.scope('market','NBA','other'))
        with self.assertRaises(ValueError):qm.choices([],ids,scope)

    def test_fanduel_mixed_assignments_http_save_consensus_and_changed_choices(self):
        f=self.fixture(fanduel.MispricingChecks)
        f.saved['rows'].append(dict(f.saved['rows'][0],line=20.5))
        overrides={key:[] for key in qm.catalog(f.saved['rows'])}
        overrides.update({qm.prompt_key(f.saved['rows'][0]):['UO','PGE'],qm.prompt_key(f.saved['rows'][1]):['OAM'],
                          qm.prompt_key(f.saved['rows'][7]):['EPA'],qm.prompt_key(f.saved['rows'][8]):['ELO']})
        for method,p in [('UO',.9),('PGE',.85),('OAM',.95)]:
            f.player_services[method].forecast.return_value=SimpleNamespace(forecasts={},selection=lambda r,p=p:MarketProbability(p,1-p,0))
        pv,pi=fields('market','NFL','123',overrides)
        values={'market-quick-filter':1,'market-quick-probability':75,'market-quick-edge':5,
                'market-quick-game-methods':['PPP'],'market-quick-player-methods':['WHP'],
                'market-url':'#nfl-game-123','market-snapshot':f.saved['pulled_at'],
                'market-category':FILTER_CATEGORY,'mispricing-forecast-history':None,'workspace-page':'market'}
        response=invoke(f.app,'request_quick_filter',values,pv,pi)
        request=response['mispricing-request']['data'];self.assertEqual(request['prompt_methods'],overrides)
        values['mispricing-request']=request
        output=invoke(f.app,'quick_filter_markets',values,pv,pi)
        self.assertIn('5 qualifying selections',str(output));self.assertIn('UO: 90.00%',str(output))
        self.assertIn('PGE: 85.00%',str(output));self.assertIn('OAM: 95.00%',str(output))
        self.assertIn('skipped by prompt selection',str(output))
        for method in ('EPA','ELO'):f.game_services[method].forecast.assert_called_once()
        for method in ('UO','OAM','PGE'):f.player_services[method].forecast.assert_called_once()
        f.game_services['PPP'].forecast.assert_not_called();f.player_services['WHP'].forecast.assert_not_called()
        with sqlite3.connect(f.source.path.with_name('filtered_results.sqlite3')) as connection:
            saved=json.loads(connection.execute('SELECT payload FROM quick_runs').fetchone()[0])
        self.assertEqual(saved['request']['prompt_methods'],overrides)
        player0=[r for r in saved['matches'] if r['selection'].startswith('Player 0')]
        self.assertEqual(len(player0),2)
        self.assertTrue(all(r['probability']==.85 for r in player0))
        # Mounting the editor after a completed scan must not start the models again.
        mounted=invoke(f.app,'quick_filter_markets',values,pv,pi,json.dumps(pi[0],sort_keys=True,separators=(',',':'))+'.value')
        self.assertIn('Click Quick Filtering',str(mounted))
        for engine in [f.game_services['EPA'],f.game_services['ELO'],f.player_services['UO'],f.player_services['OAM'],f.player_services['PGE']]:
            engine.forecast.assert_called_once()
        pv[0]=[] if pv[0] else ['WHP']
        output=f.cb['quick_filter_markets'](request,FILTER_CATEGORY,values['market-url'],values['market-snapshot'],
            ['PPP'],['WHP'],75,5,None,None,pv,pi)
        self.assertIn('Click Quick Filtering',str(output[0]));self.assertIs(output[1],no_update)

    def test_failed_prompt_method_does_not_exclude_unrelated_prompts(self):
        f=self.fixture(fanduel.MispricingChecks)
        overrides={qm.prompt_key(f.saved['rows'][0]):['UO','OAM']}
        f.player_services['OAM'].forecast.side_effect=DataSourceError('OAM missing history')
        pv,pi=fields('market','NFL','123',overrides)
        loc='#nfl-game-123';selection=f.saved['pulled_at']
        request,_=f.cb['request_quick_filter'](1,75,5,['PPP'],['WHP'],loc,selection,pv,pi)
        rendered,_=f.cb['quick_filter_markets'](request,FILTER_CATEGORY,loc,selection,['PPP'],['WHP'],75,5,None,None,pv,pi)
        self.assertIn('5 qualifying selections',str(rendered))  # Unrelated WHP player props and PPP lines remain.
        self.assertIn('OAM missing history',str(rendered))
        before=sum(s.forecast.call_count for s in [*f.game_services.values(),*f.player_services.values()])
        stale=[dict(pi[0],scope=qm.scope('market','NFL','other'))]
        rejected,status=f.cb['request_quick_filter'](2,75,5,['PPP'],['WHP'],loc,selection,pv,stale)
        self.assertIs(rejected,no_update);self.assertIn('another game',status)
        self.assertEqual(before,sum(s.forecast.call_count for s in [*f.game_services.values(),*f.player_services.values()]))

    def test_same_players_different_statistics_have_independent_consensus(self):
        f=self.fixture(fanduel.MispricingChecks)
        row=dict(f.saved['rows'][0],group='Points',market='Points',market_key='player_points_alternate',subject='Player 0')
        f.saved.update(sport='NBA',rows=[row,dict(row,market='Assists',market_key='player_assists_alternate'),
            dict(row,market='Points + Assists',market_key='player_points_assists_alternate'),dict(row,subject='Player 1')])
        overrides={qm.prompt_key(f.saved['rows'][0]):['UO','PGE'],qm.prompt_key(f.saved['rows'][1]):['OAM'],
                   qm.prompt_key(f.saved['rows'][2]):['WHP'],qm.prompt_key(f.saved['rows'][3]):['UO']}
        for method,p in [('UO',.9),('PGE',.7),('OAM',.9),('WHP',.9)]:
            f.player_services[method].forecast.return_value=SimpleNamespace(forecasts={},selection=lambda r,p=p:MarketProbability(p,1-p,0))
        pv,pi=fields('market','NBA','123',overrides);loc='#nba-game-123';selection=f.saved['pulled_at']
        request,_=f.cb['request_quick_filter'](1,75,5,['PPP'],['WHP'],loc,selection,pv,pi)
        rendered,_=f.cb['quick_filter_markets'](request,FILTER_CATEGORY,loc,selection,['PPP'],['WHP'],75,5,None,None,pv,pi)
        self.assertIn('3 qualifying selections',str(rendered))  # Only Player 0 points fail PGE consensus.
        with sqlite3.connect(f.source.path.with_name('filtered_results.sqlite3')) as connection:
            saved=json.loads(connection.execute('SELECT payload FROM quick_runs').fetchone()[0])
        self.assertEqual({r['method'] for r in saved['matches']},{'UO','WHP','OAM'})
        for engine in f.player_services.values():engine.forecast.assert_called_once()
        for engine in f.game_services.values():engine.forecast.assert_not_called()

    def test_clearing_all_prompts_skips_fits_and_saves_an_empty_scan(self):
        f=self.fixture(fanduel.MispricingChecks)
        pv,pi=fields('market','NFL','123',{key:[] for key in qm.catalog(f.saved['rows'])})
        loc='#nfl-game-123';selection=f.saved['pulled_at']
        request,_=f.cb['request_quick_filter'](1,75,5,['PPP'],['WHP'],loc,selection,pv,pi)
        rendered,_=f.cb['quick_filter_markets'](request,FILTER_CATEGORY,loc,selection,['PPP'],['WHP'],75,5,None,None,pv,pi)
        self.assertIn('0 qualifying selections',str(rendered));self.assertIn('9 quotes skipped',str(rendered))
        for engine in [*f.game_services.values(),*f.player_services.values()]:engine.forecast.assert_not_called()

    def test_kalshi_prompt_overrides_use_binary_sides_and_only_needed_methods(self):
        f=self.fixture(kalshi.KalshiFilterCallbacks)
        for family,name in [('PASSYDS','Test Quarterback'),('RECYDS','Test Receiver')]:
            f.snapshot['rows'].append(dict(f.snapshot['rows'][0],ticker='KXNFL'+family+'-PLAYER',
                event_ticker='KXNFL'+family+'-26OCT25PITNO',event_title=name+': Yards',line=20,
                category='Passing Props' if family=='PASSYDS' else 'Receiving Props',strike_type='greater_or_equal'))
        targets=prompt_targets(f.game,f.snapshot)
        assignments={key:['EPA'] for key in qm.catalog(targets.values())}
        assignments[qm.prompt_key(targets[f.snapshot['rows'][-2]['ticker']])]=['PGE']
        assignments[qm.prompt_key(targets[f.snapshot['rows'][-1]['ticker']])]=['UO','OAM']
        for engine in f.players.values():engine.forecast.return_value=SimpleNamespace(forecasts={},selection=lambda row:MarketProbability(.9,.1,0))
        pv,pi=fields('kalshi','NFL',f.game['id'],assignments)
        scan,_=quick_scan(f,1,0,-100,['PPP'],['WHP'],f.game,FILTER,'live','kalshi',
            kalshi.view.href('NFL',f.game['id']),pv,pi)
        self.assertEqual(len(scan['ranked']['matches']),20)
        for m in ('UO','OAM','PGE'):f.players[m].forecast.assert_called_once()
        f.engines['EPA'].forecast.assert_called_once();f.engines['PPP'].forecast.assert_not_called()
        f.engines['ELO'].forecast.assert_not_called();f.players['WHP'].forecast.assert_not_called()
        self.assertEqual(scan['prompt_methods'],assignments)
        output=f.cb['kalshi_quick_results'](scan,f.game,['PPP'],['WHP'],0,-100,'live',FILTER,'kalshi',pv,pi)
        self.assertIn('Top 3 by edge',str(output));self.assertIn('OAM: 90.00%',str(output))
        changed=[list(v) for v in pv];changed[0]=[]
        self.assertIn('Click Quick Filtering',str(f.cb['kalshi_quick_results'](
            scan,f.game,['PPP'],['WHP'],0,-100,'live',FILTER,'kalshi',changed,pi)))
        f.players['OAM'].forecast.side_effect=DataSourceError('Unavailable OAM')
        f.app._filter_jobs.forecasts.clear()
        failed,_=quick_scan(f,2,0,-100,['PPP'],['WHP'],f.game,FILTER,'live','kalshi',
            kalshi.view.href('NFL',f.game['id']),pv,pi)
        self.assertEqual(len(failed['ranked']['matches']),18)

    def test_closed_prompt_editor_does_not_load_or_erase_current_choices(self):
        f=self.fixture(fanduel.MispricingChecks)
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-quick-prompts-panel')):
            self.assertIs(f.cb['quick_prompt_methods'](False,'#nfl-game-123','saved',FILTER_CATEGORY,'market'),no_update)
        f.markets.history.assert_not_called()
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-quick-prompts-panel')):
            rendered=f.cb['quick_prompt_methods'](True,'#nfl-game-123',f.saved['pulled_at'],FILTER_CATEGORY,'market')
        ids=[c.id for c in fanduel.components(rendered) if c.__class__.__name__=='Dropdown']
        self.assertTrue(ids);self.assertTrue(all(i['scope']==qm.scope('market','NFL','123') for i in ids))


if __name__=='__main__':main()
