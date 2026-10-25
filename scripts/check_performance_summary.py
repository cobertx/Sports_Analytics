"""Local performance reports, missing-run orchestration, sample accuracy and Dash wiring."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import patch

import pandas as pd
from dash import no_update
from dash.exceptions import PreventUpdate

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.backtesting_view import GROUPS,available_models,model_config,navigation,page,PERFORMANCE_ROUTE
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.performance_summary import PerformanceSummaries,settings,render
from check_backtesting import saved_game_report
from check_kalshi import components


class PerformanceSummaryChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.backend=BacktestingService(root=temporary.name,now=lambda:pd.Timestamp('2026-10-24',tz='UTC'))
        self.manager=PerformanceSummaries(self.backend,watch=False)
        self.context=dict(sport='NBA',group='game-outcome')
        self.request=settings(self.context,'margin','__all__',10)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Performance summaries must not pull APIs'))
        network.start();self.addCleanup(network.stop)

    def seed(self,key,request=None,context=None,identity=None,config=None):
        context=context or self.context;request=request or self.request
        identity=identity or sha256(json.dumps([context,key,request,config],sort_keys=True).encode()).hexdigest()
        report=saved_game_report(identity)
        report['context'].update(**context,model=key,**request)
        model=next(m for m in available_models(context['sport'],context['group']) if m.key==key)
        report['config']=model_config(model,context['sport']) if config is None else config
        if context['group']=='player-stats':
            for row in report['records']:
                row.update(actual=22,predicted=21,baseline=20,lower=10,upper=30,interval_level=.8,
                           probability=.7 if request['line'] is not None else None,
                           event_actual=1 if request['line'] is not None else None,label='Test Player · BOS vs DET')
        directory=self.backend.root/context['sport'].lower()/context['group']/key
        directory.mkdir(parents=True,exist_ok=True)
        (directory/(identity+'.json')).write_text(json.dumps(dict(result=report)),encoding='utf-8')
        return report

    def app(self):
        app=create_app(backtesting_service=self.backend)
        callbacks={meta['callback'].__wrapped__.__name__:meta['callback'].__wrapped__ for meta in app.callback_map.values() if meta.get('callback')}
        return app,callbacks

    def test_matching_completed_reports_load_without_starting_jobs_and_reuse_the_manifest(self):
        for key in ['ppp','epa','elo']:self.seed(key)
        with patch.object(self.backend,'start',side_effect=AssertionError('Do not refit saved results')):
            manifest=self.manager.ensure(self.context,self.request,'__all__')
            self.assertTrue(all(e['state']=='completed' and e['reused'] for e in manifest['entries'].values()))
            self.assertEqual(len(self.manager.reports(manifest)),3)
            self.assertEqual(self.manager.ensure(self.context,self.request,'__all__')['id'],manifest['id'])
        restarted=PerformanceSummaries(BacktestingService(root=self.backend.root),watch=False)
        self.assertEqual(len(restarted.reports(restarted.read(self.context,manifest['id']))),3)

    def test_only_missing_models_run_in_the_shared_progress_monitor_and_repeat_clicks_join(self):
        self.seed('ppp');entered=Event();release=Event()
        def run(**parameters):
            progress=parameters.pop('progress');progress(1,10,'Evaluating saved history');entered.set()
            if not release.wait(5):raise AssertionError('Release test simulations')
            return self.seed(parameters['key'])
        with patch.object(self.backend,'run',side_effect=run):
            try:
                manifest=self.manager.ensure(self.context,self.request,'__all__')
                self.assertTrue(entered.wait(2))
                self.assertEqual(manifest['entries']['ppp']['state'],'completed')
                jobs=self.backend.active_jobs()
                self.assertEqual({j['parameters']['key'] for j in jobs},{'epa','elo'})
                self.assertTrue(all('Performance Summary' in j['display_label'] for j in jobs))
                joined=self.manager.ensure(self.context,self.request,'__all__')
                self.assertEqual(joined['id'],manifest['id']);self.assertEqual(len(self.backend.active_jobs()),2)
            finally:release.set()
            for entry in manifest['entries'].values():
                job=entry.get('job_id')
                if job:
                    for _ in range(100):
                        if self.backend.progress(job)['state']!='running':break
                        Event().wait(.01)
                    self.assertEqual(self.backend.progress(job)['state'],'completed')
        completed=self.manager.refresh(self.context,manifest['id'])
        self.assertTrue(all(e['state']=='completed' for e in completed['entries'].values()))
        self.assertEqual(len(self.manager.reports(completed)),3)

    def test_scope_defaults_and_reference_lines_must_match_before_reuse(self):
        original=self.seed('ppp')
        model=available_models('NBA','game-outcome')[0]
        self.assertEqual(self.manager.matching(self.context,model,self.request)['run_id'],original['run_id'])
        for changes in [dict(entity='BOS'),dict(limit=11),dict(start='2025-01-01'),dict(include_playoffs=True),dict(lookback=20)]:
            self.assertIsNone(self.manager.matching(self.context,model,{**self.request,**changes}))
        changed=deepcopy(original['config']);changed[next(iter(changed))]='different'
        self.seed('ppp',identity=original['run_id'],config=changed)
        self.assertIsNone(self.manager.matching(self.context,model,self.request))
        player=dict(sport='NFL',group='player-stats')
        request=settings(player,'player_pass_yds','PIT',10,line=200.5)
        self.seed('whp',request,player)
        model=available_models('NFL','player-stats')[0]
        self.assertIsNotNone(self.manager.matching(player,model,request))
        for changes in [dict(team_filter=None),dict(line=201.5),dict(market='player_rush_yds'),dict(entity='player-1')]:
            self.assertIsNone(self.manager.matching(player,model,{**request,**changes}))

    def test_total_metrics_do_not_mislabel_winner_probabilities_and_common_sample_is_exact(self):
        reports={key:self.seed(key) for key in ['ppp','epa','elo']}
        changed=reports['epa'];changed['records']=changed['records'][1:]
        path=self.backend.root/'nba/game-outcome/epa'/(changed['run_id']+'.json')
        path.write_text(json.dumps(dict(result=changed)),encoding='utf-8')
        manifest=self.manager.ensure(self.context,self.request,'__all__')
        content=render(self.manager,manifest,'total',5)
        self.assertIn('1 identical evaluated',str(content));self.assertIn('MAE',str(content))
        self.assertNotIn('Winner accuracy',str(content));self.assertNotIn('Brier score',str(next(c for c in components(content) if c.__class__.__name__=='Thead')))
        table=next(c for c in components(content) if c.__class__.__name__=='Tbody')
        self.assertTrue(all(row.children[2].children=='1' for row in table.children))
        self.assertTrue(all(row.children[5].children=='2.000' for row in table.children))
        margin=render(self.manager,manifest,'margin',5)
        self.assertIn('Winner accuracy',str(margin));self.assertIn('Open results',str(margin));self.assertIn('100.0%',str(margin))
        full=render(self.manager,manifest,'margin',5,'all')
        table=next(c for c in components(full) if c.__class__.__name__=='Tbody')
        self.assertEqual([row.children[2].children for row in table.children],['2','1','2'])
        self.assertIn('Each model uses its own evaluated sample',str(full))

    def test_player_team_scope_runs_all_supported_methods_and_unsupported_methods_stay_explicit(self):
        context=dict(sport='NFL',group='player-stats')
        request=settings(context,'player_pass_yds','PIT',10,line=200.5)
        self.seed('whp',request,context)
        with patch.object(self.backend,'start',return_value='pending') as start:
            manifest=self.manager.ensure(context,request,'PIT')
        self.assertEqual({call.kwargs['key'] for call in start.call_args_list},{'uo','oam','pge'})
        self.assertTrue(all(call.kwargs['entity']=='__all__' and call.kwargs['team_filter']=='PIT' for call in start.call_args_list))
        self.assertIn('Over accuracy',str(render(self.manager,manifest,'player_pass_yds',5)))
        with patch('sports_analytics.performance_summary.market_spec',return_value=None),patch.object(self.backend,'start',return_value='pending') as start:
            other=self.manager.ensure(context,settings(context,'player_tds','__all__',10),'__all__')
        self.assertEqual(start.call_count,1)
        self.assertEqual(sum(e['state']=='unsupported' for e in other['entries'].values()),3)

    def test_routes_saved_team_options_and_hidden_pages_for_every_sport(self):
        app,callbacks=self.app()
        self.assertEqual(callbacks['restore_game_route'](PERFORMANCE_ROUTE),(no_update,'backtesting'))
        for sport in ['NBA','NFL','NCAAF']:
            self.assertEqual(page(sport,PERFORMANCE_ROUTE),[])
            link=next(c for c in navigation(sport,PERFORMANCE_ROUTE) if getattr(c,'href',None)==PERFORMANCE_ROUTE)
            self.assertIn('active',link.className)
            self.assertEqual(callbacks['show_backtesting']('backtesting',sport,PERFORMANCE_ROUTE)[3],{'display':'none'})
            for group in GROUPS:
                context=dict(sport=sport,group=group)
                self.assertEqual(callbacks['performance_context']('backtesting',sport,PERFORMANCE_ROUTE,group)[1],context)
        self.assertIsNone(callbacks['performance_context']('market','NBA',PERFORMANCE_ROUTE,'game-outcome')[1])
        self.seed('ppp',{**self.request,'entity':'BOS'})
        with patch.object(self.backend,'options',side_effect=DataSourceError('No prepared inputs')):
            options=callbacks['performance_options'](self.context,None,None)
        self.assertIn('BOS',{o['value'] for o in options[2]})
        ids=[c.id for c in components(app.layout) if isinstance(getattr(c,'id',None),str)]
        self.assertEqual(len(ids),len(set(ids)))
        with self.assertRaises(PreventUpdate):callbacks['performance_results'](None,None,None,None,None,None,None,None,None,5,None)

    def test_dash_http_load_results_and_changed_filters_do_not_show_wrong_scope(self):
        for key in ['ppp','epa','elo']:self.seed(key)
        app,callbacks=self.app()
        values={'performance-load':1,'performance-context':self.context,'performance-poll':0,'performance-market':'margin',
                'performance-team':'__all__','performance-limit':10,'performance-start':None,'performance-end':None,
                'performance-playoffs':'exclude','performance-line':None,'performance-state':None,'performance-pages':{},
                'performance-tolerance':5,'performance-rendered':None,'performance-basis':'common'}
        with patch.object(self.backend,'start',side_effect=AssertionError('Saved reports should open directly')),app.server.test_client() as client:
            for name,changed in [('performance_load','performance-load.n_clicks'),('performance_results','performance-state.data')]:
                key,meta=next((key,meta) for key,meta in app.callback_map.items() if meta.get('callback') and meta['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**i,value=values[i['id']]) for i in meta['inputs']],state=[dict(**i,value=values[i['id']]) for i in meta['state']],changedPropIds=[changed]))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                payload=response.get_json()['response']
                if name=='performance_load':values['performance-state']=payload['performance-state']['data']
                else:self.assertIn('Model accuracy',str(payload));self.assertIn('Winner accuracy',str(payload))
        wrong=callbacks['performance_results'](values['performance-state'],self.context,'margin','BOS',10,None,None,'exclude',None,5,None)
        self.assertIn('Load performance',str(wrong));self.assertNotIn('Winner accuracy',str(wrong))

    def test_invalid_filters_are_rejected_without_starting_work(self):
        for changes in [dict(limit=0),dict(limit=1.5),dict(line=float('nan')),dict(team=None),dict(market='other')]:
            inputs=dict(context=self.context,market='margin',team='__all__',limit=10);inputs.update(changes)
            with self.assertRaises(ValueError):settings(**inputs)
        with self.assertRaises(ValueError):self.manager.ensure(self.context,{**self.request,'start':'2026-12-01','end':'2026-01-01'},'__all__')

    def test_failed_and_interrupted_methods_preserve_completed_results_and_can_be_retried(self):
        self.seed('ppp')
        with patch.object(self.backend,'start',return_value='job-pending'):
            manifest=self.manager.ensure(self.context,self.request,'__all__')
        with patch.object(self.backend,'progress',return_value=dict(state='failed',message='No saved model inputs')):
            failed=self.manager.refresh(self.context,manifest['id'])
        content=render(self.manager,failed,'margin',5)
        self.assertIn('No saved model inputs',str(content));self.assertIn('Open results',str(content))
        self.assertEqual(failed['entries']['ppp']['state'],'completed')
        with patch.object(self.backend,'start',return_value='new-job') as start:
            retried=self.manager.ensure(self.context,self.request,'__all__')
        self.assertNotEqual(retried['id'],failed['id']);self.assertEqual(start.call_count,2)
        restarted=PerformanceSummaries(BacktestingService(root=self.backend.root),watch=False)
        interrupted=restarted.refresh(self.context,retried['id'])
        self.assertEqual(interrupted['entries']['epa']['state'],'interrupted')
        self.assertEqual(len(restarted.reports(interrupted)),1)


if __name__=='__main__':
    main()
