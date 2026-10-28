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
from sports_analytics.performance_summary import PerformanceSummaries,settings,render,range_accuracy
from check_backtesting import saved_game_report
from check_kalshi import components


def performance_rows(content):
    header=next(c for c in components(content) if c.__class__.__name__=='Thead')
    labels=[c.children for c in header.children.children]
    body=next(c for c in components(content) if c.__class__.__name__=='Tbody')
    return [dict(zip(labels,[cell.children for cell in row.children])) for row in body.children]


class PerformanceSummaryChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.backend=BacktestingService(root=temporary.name,now=lambda:pd.Timestamp('2026-10-24',tz='UTC'))
        self.manager=PerformanceSummaries(self.backend,watch=False)
        self.context=dict(sport='NBA',group='game-outcome')
        self.request=settings(self.context,'margin','__all__',10)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Performance summaries must not pull APIs'))
        network.start();self.addCleanup(network.stop)

    def seed(self,key,request=None,context=None,identity=None,config=None,created=None):
        context=context or self.context;request=request or self.request
        identity=identity or sha256(json.dumps([context,key,request,config],sort_keys=True).encode()).hexdigest()
        report=saved_game_report(identity)
        if created:report['created_at']=created
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

    def post_callback(self,app,name,values,changed):
        key,meta=next((key,meta) for key,meta in app.callback_map.items() if meta.get('callback') and meta['callback'].__wrapped__.__name__==name)
        with app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                inputs=[dict(**i,value=values[i['id']]) for i in meta['inputs']],
                state=[dict(**i,value=values[i['id']]) for i in meta['state']],changedPropIds=[changed]))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        payload=response.get_json()['response']
        for identity,props in payload.items():
            if identity in values and len(props)==1:values[identity]=next(iter(props.values()))
        return payload

    def test_latest_history_uses_newest_nonempty_saved_report_per_model_and_retains_original_settings(self):
        for sport in ['NBA','NFL','NCAAF']:
            context=dict(sport=sport,group='game-outcome')
            self.seed('ppp',context=context,identity='a'*64)
            request={**self.request,'limit':20,'lookback':30,'include_playoffs':True}
            latest=self.seed('ppp',request,context,identity='b'*64,config={'custom':True},created='2026-10-24T12:00:00Z')
            self.seed('epa',context=context,identity='c'*64)
            self.seed('epa',{**self.request,'entity':'BOS'},context,identity='d'*64,created='2026-10-25T12:00:00Z')
            empty=self.seed('ppp',context=context,identity='e'*64,created='2026-10-26T12:00:00Z')
            empty['records']=[]
            path=self.backend.root/sport.lower()/'game-outcome/ppp'/(empty['run_id']+'.json')
            path.write_text(json.dumps(dict(result=empty)),encoding='utf-8')
            with patch.object(self.backend,'start',side_effect=AssertionError('Browsing history must not run models')):
                manifest=self.manager.latest(context,'margin','__all__')
            self.assertEqual(manifest['source'],'latest-saved')
            self.assertEqual(manifest['settings'],request)
            self.assertEqual(manifest['entries']['ppp']['run_id'],latest['run_id'])
            self.assertEqual(manifest['entries']['epa']['run_id'],'c'*64)
            self.assertEqual(manifest['entries']['elo']['state'],'not saved')
            self.assertEqual(self.manager.latest(context,'total','__all__')['id'],manifest['id'])
            self.assertIsNone(self.manager.latest(context,'margin','NYK'))
            self.assertFalse(self.manager.root.exists(),'Reading individual history should not write comparison manifests')

    def test_latest_player_history_keeps_stat_team_and_reference_line_comparable(self):
        context=dict(sport='NBA',group='player-stats')
        request=settings(context,'player_points','BOS',10,line=20.5)
        self.seed('whp',request,context,identity='a'*64,created='2026-10-26T12:00:00Z')
        self.seed('uo',request,context,identity='b'*64)
        self.seed('uo',{**request,'line':25.5},context,identity='c'*64,created='2026-10-25T12:00:00Z')
        self.seed('oam',{**request,'line':25.5},context,identity='d'*64)
        self.seed('pge',{**request,'market':'player_assists'},context,identity='e'*64)
        manifest=self.manager.latest(context,'player_points','BOS')
        self.assertEqual(manifest['settings']['line'],20.5)
        self.assertEqual(manifest['entries']['uo']['run_id'],'b'*64)
        self.assertEqual(manifest['entries']['oam']['state'],'not saved')
        self.assertEqual(manifest['entries']['pge']['state'],'not saved')
        self.assertIsNone(self.manager.latest(context,'player_points','__all__'))
        self.assertIn('Shared reference line: 20.5',str(render(self.manager,manifest,'player_points',5)))

    def test_entering_and_changing_category_auto_loads_history_and_restores_controls_without_running(self):
        for key in ['ppp','epa','elo']:self.seed(key)
        self.seed('ppp',{**self.request,'entity':'BOS','limit':20},created='2026-10-26T12:00:00Z')
        app,_=self.app()
        values={'performance-load':0,'performance-context':self.context,'performance-poll':0,'performance-market':'margin',
                'performance-team':'__all__','performance-limit':100,'performance-start':None,'performance-end':None,
                'performance-playoffs':'exclude','performance-line':None,'performance-state':None,'performance-pages':{},
                'performance-tolerance':5,'performance-rendered':None,'performance-basis':'common'}
        with patch.object(self.backend,'start',side_effect=AssertionError('Auto loading must never run a model')):
            payload=self.post_callback(app,'performance_load',values,'performance-context.data')
            self.assertIn('Latest saved backtests',payload['performance-status']['children'])
            self.assertEqual(values['performance-limit'],10)
            self.assertEqual(values['performance-state']['source'],'latest-saved')
            payload=self.post_callback(app,'performance_results',values,'performance-state.data')
            self.assertIn('Recent forecast error by model',str(payload))
            values['performance-team']='BOS'
            self.post_callback(app,'performance_load',values,'performance-team.value')
            self.assertEqual(values['performance-limit'],20)
            self.assertEqual(values['performance-state']['settings']['entity'],'BOS')
            values['performance-market']='total'
            self.post_callback(app,'performance_load',values,'performance-market.value')
            payload=self.post_callback(app,'performance_results',values,'performance-state.data')
            self.assertNotIn('Winner accuracy',str(payload))
            values['performance-context']=dict(sport='NBA',group='player-stats');values['performance-market']='player_points'
            payload=self.post_callback(app,'performance_load',values,'performance-context.data')
            self.assertIsNone(values['performance-state'])
            self.assertIn('No saved backtests',payload['performance-status']['children'])

    def test_revisiting_latest_history_finds_new_results_and_running_simulations_keep_polling(self):
        self.seed('ppp')
        app,_=self.app()
        values={'performance-load':0,'performance-context':self.context,'performance-poll':0,'performance-market':'margin',
                'performance-team':'__all__','performance-limit':100,'performance-start':None,'performance-end':None,
                'performance-playoffs':'exclude','performance-line':None,'performance-state':None,'performance-pages':{}}
        self.post_callback(app,'performance_load',values,'performance-context.data')
        before=values['performance-state']['id']
        self.seed('ppp',identity='f'*64,created='2026-10-26T12:00:00Z')
        self.post_callback(app,'performance_load',values,'performance-context.data')
        self.assertNotEqual(values['performance-state']['id'],before)
        with patch.object(self.backend,'start',return_value='pending'):
            pending=self.manager.ensure(self.context,self.request,'__all__')
        values['performance-pages']={'NBA/game-outcome':pending['id']}
        with patch.object(self.backend,'progress',return_value=dict(state='running',percent=25,message='Evaluating')):
            self.post_callback(app,'performance_load',values,'performance-context.data')
        self.assertEqual(values['performance-state']['id'],pending['id'])
        self.assertTrue(any(e['state']=='running' for e in values['performance-state']['entries'].values()))

    def test_comparison_charts_plot_coverage_error_and_chronological_rolling_error_from_the_same_sample(self):
        reports={key:self.seed(key) for key in ['ppp','epa']}
        for report in reports.values():
            for index,row in enumerate(report['records']):
                row.update(predicted_margin=row['actual_margin']+10*index,margin_sd=1)
        manager=SimpleNamespace(reports=lambda _:reports)
        manifest=dict(context=self.context,settings=self.request,entries={key:dict(state='completed') for key in reports})
        content=render(manager,manifest,'margin',5)
        charts=[c.figure for c in components(content) if c.__class__.__name__=='Graph']
        self.assertEqual(len(charts),3)
        self.assertEqual(list(charts[0].data[0].y),[.5,.5])
        self.assertEqual(charts[0].layout.shapes[0].y0,.8)
        self.assertEqual(list(charts[1].data[0].y),[5,5])
        self.assertAlmostEqual(charts[1].data[1].y[0],50**.5)
        self.assertEqual(list(charts[1].data[2].y),[2.5,2.5])
        self.assertEqual(list(charts[2].data[0].y),[0,5])
        self.assertIn('same shared observations',str(content))
        full=render(manager,manifest,'margin',5,'all')
        self.assertIn('equal positions can represent different games',str(full))
        reports['epa']['records'][0]['game_id']='another-game'
        reports['epa']['records']=reports['epa']['records'][:1]
        empty=render(manager,manifest,'margin',5)
        self.assertIn('No shared evaluated observations to plot',str(empty))
        self.assertFalse(any(c.__class__.__name__=='Graph' for c in components(empty)))

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
        rows=performance_rows(content)
        self.assertTrue(all(row['Compared']=='1' for row in rows))
        self.assertTrue(all(row['MAE']=='2.000' for row in rows))
        self.assertEqual(list(rows[0])[2],'Accuracy (80% range)')
        self.assertTrue(all(row['Accuracy (80% range)']=='100.0%' for row in rows))
        margin=render(self.manager,manifest,'margin',5)
        self.assertIn('Winner accuracy',str(margin));self.assertIn('Open results',str(margin));self.assertIn('100.0%',str(margin))
        full=render(self.manager,manifest,'margin',5,'all')
        self.assertEqual([row['Compared'] for row in performance_rows(full)],['2','1','2'])
        self.assertIn('Each model uses its own evaluated sample',str(full))

    def test_range_accuracy_includes_endpoints_and_excludes_invalid_or_other_level_ranges(self):
        row=dict(actual=10,lower=10,upper=20,interval_level=.8)
        records=[row,{**row,'actual':20},{**row,'actual':21},
                 {**row,'interval_level':.9},{**row,'interval_level':None},
                 {**row,'lower':None},{**row,'upper':float('inf')},
                 {**row,'actual':float('nan')},{**row,'lower':30}]
        result=range_accuracy(records)
        self.assertAlmostEqual(result['range_accuracy'],2/3)
        self.assertEqual(result['range_samples'],'2 / 3')
        self.assertEqual(range_accuracy([]),dict(range_accuracy=None,range_samples='0 / 0'))

    def test_accuracy_uses_selected_target_and_common_or_all_sample_for_every_sport(self):
        for sport in ['NBA','NFL','NCAAF']:
            context=dict(sport=sport,group='game-outcome')
            reports={key:self.seed(key,context=context) for key in ['ppp','epa']}
            for report in reports.values():
                first,last=report['records']
                first.update(predicted_margin=first['actual_margin'],margin_sd=0,
                             predicted_total=first['actual_total']+100,total_sd=1)
                last.update(predicted_margin=last['actual_margin']+100,margin_sd=1,
                            predicted_total=last['actual_total'],total_sd=0)
            reports['epa']['records']=reports['epa']['records'][1:]
            manager=SimpleNamespace(reports=lambda _:reports)
            manifest=dict(context=context,settings=self.request,
                          entries={key:dict(state='completed') for key in reports})
            common=performance_rows(render(manager,manifest,'margin',5))
            self.assertEqual([row['Accuracy (80% range)'] for row in common[:2]],['0.0%','0.0%'])
            self.assertEqual([row['80% range hits / samples'] for row in common[:2]],['0 / 1','0 / 1'])
            self.assertEqual(common[0]['Winner accuracy'],'100.0%')
            total=performance_rows(render(manager,manifest,'total',5))
            self.assertEqual([row['Accuracy (80% range)'] for row in total[:2]],['100.0%','100.0%'])
            full=performance_rows(render(manager,manifest,'winner',5,'all'))
            self.assertEqual([row['Accuracy (80% range)'] for row in full[:2]],['50.0%','0.0%'])

    def test_player_accuracy_needs_no_reference_line_and_does_not_relabel_legacy_ranges(self):
        context=dict(sport='NBA',group='player-stats')
        request=settings(context,'player_points','__all__',10)
        report=self.seed('whp',request,context)
        report['records'][0].update(actual=10,lower=10,upper=20)
        report['records'][1].update(actual=22,lower=10,upper=20)
        manager=SimpleNamespace(reports=lambda _:{'whp':report})
        manifest=dict(context=context,settings=request,entries={'whp':dict(state='completed')})
        row=performance_rows(render(manager,manifest,'player_points',5))[0]
        self.assertEqual(row['Accuracy (80% range)'],'50.0%')
        self.assertEqual(row['80% range hits / samples'],'1 / 2')
        self.assertEqual(row['Probability samples'],'0')
        for record in report['records']:record['interval_level']=.9
        content=render(manager,manifest,'player_points',5)
        row=performance_rows(content)[0]
        self.assertEqual(row['Accuracy (80% range)'],'—')
        self.assertEqual(row['80% range hits / samples'],'0 / 0')
        self.assertIn('must be rerun',str(content))

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
