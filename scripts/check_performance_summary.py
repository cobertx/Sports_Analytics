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
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.backtesting_service import BacktestingService,PLAYER_COHORTS,PREDICTIVE_Z
from app.sections.backtesting.backtesting_view import GROUPS,available_models,model_config,navigation,page,PERFORMANCE_ROUTE,summary_route
from app.dashboard import create_app
from sports_analytics.data import DataSourceError
from app.sections.backtesting.performance_summary import PerformanceSummaries,settings,render,range_accuracy,comparison_charts
from check_backtesting import saved_game_report
from check_kalshi import components

NBA_GAME_KEYS = tuple(model.key for model in available_models('NBA','game-outcome'))


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
        if context['group']=='player-stats':report['context']['player_cohort']=PLAYER_COHORTS[context['sport']]
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
        callbacks=callback_functions(app)
        return app,callbacks

    def post_callback(self,app,name,values,changed):
        key,meta=next((key,meta) for key,meta in app.callback_map.items() if meta.get('callback') and meta['callback'].__wrapped__.__name__==name)
        with app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                inputs=[dict(**i,value=wire_value(i,values)) for i in meta['inputs']],
                state=[dict(**i,value=wire_value(i,values)) for i in meta['state']],changedPropIds=[changed]))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        payload=response.get_json()['response']
        for identity,props in payload.items():
            if identity in values and len(props)==1:values[identity]=next(iter(props.values()))
        return payload

    def test_elo_coverage_matches_opened_results_for_full_and_common_samples(self):
        for key in ('ppp','epa','elo'):
            report=self.seed(key)
            count=500 if key=='elo' else 409
            prototype=report['records'][0]
            report['records']=[dict(prototype,game_id=str(i),date=(pd.Timestamp('2025-01-01',tz='UTC')+pd.Timedelta(hours=i)).isoformat(),
                actual_total=210,predicted_total=210 if i<335 or 409<=i<489 else 310,total_sd=1,
                actual_margin=10,predicted_margin=10,margin_sd=1) for i in range(count)]
            report['attempted']=count;report['skipped']=[]
            path=self.backend.root/'nba/game-outcome'/key/(report['run_id']+'.json')
            path.write_text(json.dumps(dict(result=report)),encoding='utf-8')
        manifest=self.manager.latest(self.context,'total','__all__')
        before={str(p):p.read_bytes() for p in self.backend.root.rglob('*.json')}
        app,callbacks=self.app()
        basis_control=next(c for c in components(app.layout) if getattr(c,'id',None)=='performance-basis')
        self.assertEqual(basis_control.value,'all')
        with patch.object(self.backend,'start',side_effect=AssertionError('Do not rerun saved backtests')):
            for basis,rate,count,hits in [('all','83.0%',500,415),('common','81.9%',409,335)]:
                content=render(self.manager,manifest,'total',7,basis)
                summary=next(row for row in performance_rows(content) if 'ELO' in row['Model'])
                self.assertEqual(summary['Accuracy (80% range)'],rate)
                self.assertEqual(summary['80% range hits / samples'],f'{hits} / {count}')
                href=next(c.href for c in components(content) if getattr(c,'children',None)=='Open results' and '-elo-' in c.href)
                route=summary_route(href)
                self.assertEqual(route['target'],'total');self.assertEqual(route['basis'],basis)
                if basis=='common':self.assertEqual(set(route['reports']),{'ppp','epa','elo'})
                self.assertEqual(callbacks['restore_game_route'](href),('NBA','backtesting'))
                self.assertEqual(callbacks['backtest_summary_target'](href),'total')
                detail,settings,status,disabled,_,_=callbacks['backtest_summary_result']('backtesting','NBA',href,'total')
                self.assertFalse(disabled)
                card=next(c for c in components(detail) if getattr(c,'className',None)=='kpi' and c.children[0].children=='80% INTERVAL COVERAGE')
                self.assertEqual(card.children[1].children,rate)
                aside=next(c for c in components(detail) if getattr(c,'className',None)=='backtest-coverage')
                self.assertEqual(aside.children[0].children,rate)
                predictions=next(c for c in detail.children if getattr(c,'className',None)=='panel backtest-table')
                self.assertEqual(len(predictions.children[1].data),count)
                breakdown=next(c for c in detail.children if getattr(c,'className',None)=='panel backtest-breakdown')
                team_rows=breakdown.children[2].children[1].data
                self.assertEqual({r['name']:r['range_samples'] for r in team_rows},
                                 {'A':f'{hits} / {count}','B':f'{hits} / {count}'})
                with patch('app.sections.backtesting.backtesting_summary.ctx',SimpleNamespace(triggered_id='backtest-summary-export')):
                    download=callbacks['backtest_summary_export'](1,'backtesting','NBA',href,'total')
                from io import StringIO
                self.assertEqual(len(pd.read_csv(StringIO(download['content']))),count)
                self.assertEqual(download['filename'].startswith('common_'),basis=='common')
                if basis=='common':
                    self.assertIn('409 of 500',status)
                    full=next(c.href for c in components(settings) if getattr(c,'children',None)=='Open the complete saved backtest')
                    self.assertEqual(summary_route(full)['basis'],'all')
                    saved_reports=self.manager.reports(manifest)
                    with patch.object(self.backend,'saved',side_effect=lambda sport,group,key,run:None if key=='epa' else saved_reports.get(key)):
                        # Missing cohort inputs must never silently change the result to full-sample coverage.
                        missing=callbacks['backtest_summary_result']('backtesting','NBA',href,'total')
                        self.assertTrue(missing[3]);self.assertIn('unavailable',missing[2])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.backend.root.rglob('*.json')})

    def test_linked_coverage_uses_identical_valid_ranges_for_player_results(self):
        context=dict(sport='NBA',group='player-stats');request=settings(context,'player_points','__all__',10)
        report=self.seed('whp',request,context)
        base=report['records'][0]
        report['records']=[dict(base,game_id=str(i),actual=actual,lower=lower,upper=upper)
                           for i,(actual,lower,upper) in enumerate([(10,10,20),(20,10,20),(21,10,20),(15,None,20)])]
        path=self.backend.root/'nba/player-stats/whp'/(report['run_id']+'.json')
        path.write_text(json.dumps(dict(result=report)),encoding='utf-8')
        manifest=self.manager.latest(context,'player_points','__all__')
        content=render(self.manager,manifest,'player_points',5)
        summary=performance_rows(content)[0]
        self.assertEqual(summary['Accuracy (80% range)'],'66.7%')
        self.assertEqual(summary['80% range hits / samples'],'2 / 3')
        href=next(c.href for c in components(content) if getattr(c,'children',None)=='Open results')
        _,callbacks=self.app()
        detail=callbacks['backtest_summary_result']('backtesting','NBA',href,'margin')[0]
        aside=next(c for c in components(detail) if getattr(c,'className',None)=='backtest-coverage')
        self.assertEqual(aside.children[0].children,'66.7%')
        self.assertEqual(aside.children[2].children,'2 of 3 actuals with valid ranges')

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
        self.assertIn('Reference line 20.5',str(render(self.manager,manifest,'player_points',5)))

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
            self.assertIn('Rolling 80% interval coverage',str(payload))
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

    def test_comparison_charts_use_the_same_cohort_for_coverage_forecast_calibration_and_interval_widths(self):
        reports={key:self.seed(key) for key in ['ppp','epa']}
        for key,report in reports.items():
            for index,row in enumerate(report['records']):
                row.update(predicted_margin=row['actual_margin']+(10*index if key=='ppp' else 0),margin_sd=1)
        manager=SimpleNamespace(reports=lambda _:reports)
        manifest=dict(context=self.context,settings=self.request,entries={key:dict(state='completed') for key in reports})
        content=render(manager,manifest,'margin',5,'common')
        charts=[c.figure for c in components(content) if c.__class__.__name__=='Graph']
        self.assertEqual(len(charts),4)
        self.assertEqual(sum('performance-chart-wide' in (getattr(c,'className','') or '') for c in components(content)),2)
        self.assertEqual(list(charts[0].data[0].y),[.5,1])
        self.assertEqual(list(charts[0].data[0].marker.color),['#ffa3af','#57dbc4'])
        self.assertEqual(charts[0].layout.shapes[0].y0,.8)
        self.assertEqual(list(charts[1].data[0].y),[1,.5])
        self.assertEqual(list(charts[1].data[0].x),[1,2])
        self.assertEqual(list(charts[1].data[0].text),['2025-09-19','2025-09-20'])
        self.assertEqual(charts[1].data[0].customdata[-1],[1,2,2])
        self.assertEqual(charts[1].layout.shapes[0].y0,.8)
        self.assertEqual(charts[1].layout.yaxis.tickformat,'.0%')
        self.assertEqual(list(charts[2].data[0].x),[5,16])
        self.assertEqual(list(charts[2].data[0].y),[5,6])
        self.assertEqual(sum(charts[2].data[0].customdata),2)
        self.assertAlmostEqual(charts[3].data[0].median[0],2*PREDICTIVE_Z)
        self.assertIn('same shared observations',str(content))
        full=render(manager,manifest,'margin',5,'all')
        self.assertIn('Each model uses its own evaluated observations',str(full))
        reports['epa']['records'][0]['game_id']='another-game'
        reports['epa']['records']=reports['epa']['records'][:1]
        empty=render(manager,manifest,'margin',5,'common')
        self.assertIn('No shared evaluated observations to plot',str(empty))
        self.assertFalse(any(c.__class__.__name__=='Graph' for c in components(empty)))

    def test_accuracy_trend_uses_a_longer_window_and_excludes_invalid_or_other_level_ranges(self):
        rows=[dict(actual=10,predicted=10,lower=10,upper=20,interval_level=.8,date=day,label='Player')
              for day in pd.date_range('2026-01-01',periods=104,tz='UTC')]
        rows[0]['interval_level']=.9
        rows[2]['lower']=None
        rows[3]['upper']=float('nan')
        rows[4].update(lower=20,upper=10)
        rows[6]['actual']=21
        rows[10]['actual']=20
        metric=dict(method='WHP',label='Player points',compared=12,**range_accuracy(rows))
        content=comparison_charts(dict(records={'whp':rows}),{'whp':metric},'all','NBA')
        chart=[c.figure for c in components(content) if c.__class__.__name__=='Graph'][1]
        self.assertIsNone(chart.data[0].y[0])
        self.assertEqual(chart.data[0].y[1],1)
        self.assertAlmostEqual(chart.data[0].y[10],6/7)
        self.assertAlmostEqual(chart.data[0].y[11],7/8)
        self.assertEqual(chart.data[0].customdata[-1],[98,99,100])
        self.assertEqual(chart.data[0].text[-1],'2026-04-13')
        self.assertFalse(chart.data[0].connectgaps)
        for row in rows:row['interval_level']=.9
        empty=comparison_charts(dict(records={'whp':rows}),{'whp':{**metric,**range_accuracy(rows)}},'all','NBA')
        chart=[c.figure for c in components(empty) if c.__class__.__name__=='Graph'][1]
        self.assertFalse(chart.data)
        self.assertTrue(any('No valid 80% intervals' in note.text for note in chart.layout.annotations))

    def test_coverage_trend_places_consecutive_seasons_side_by_side(self):
        first_dates=['2020-09-30']*150+['2020-10-11','2020-12-22','2020-12-23','2020-12-23']
        second_dates=['2020-10-11','2020-12-22']
        records={key:[dict(actual=10,predicted=10,lower=5,upper=15,interval_level=.8,date=day+'T20:00:00Z')
                      for day in dates] for key,dates in [('ppp',first_dates),('epa',second_dates)]}
        metrics={key:dict(method=key.upper(),label=key,compared=len(rows),**range_accuracy(rows))
                 for key,rows in records.items()}
        content=comparison_charts(dict(records=records),metrics,'all','NBA')
        chart=[c.figure for c in components(content) if c.__class__.__name__=='Graph'][1]
        self.assertEqual(list(chart.data[0].x),[1,2,3,4])
        self.assertEqual(list(chart.data[1].x),[2,3])
        self.assertEqual(list(chart.layout.xaxis.ticktext),['2020-09-30','2020-10-11','2020-12-22','2020-12-23'])
        self.assertEqual(list(chart.data[1].text),second_dates)
        dividers=[shape for shape in chart.layout.shapes if shape.xref=='x']
        self.assertEqual([(line.x0,line.x1) for line in dividers],[(2.5,2.5)])

    def test_matching_completed_reports_load_without_starting_jobs_and_reuse_the_manifest(self):
        for key in NBA_GAME_KEYS:self.seed(key)
        with patch.object(self.backend,'start',side_effect=AssertionError('Do not refit saved results')):
            manifest=self.manager.ensure(self.context,self.request,'__all__')
            self.assertTrue(all(e['state']=='completed' and e['reused'] for e in manifest['entries'].values()))
            self.assertEqual(len(self.manager.reports(manifest)),len(NBA_GAME_KEYS))
            self.assertEqual(self.manager.ensure(self.context,self.request,'__all__')['id'],manifest['id'])
        restarted=PerformanceSummaries(BacktestingService(root=self.backend.root),watch=False)
        self.assertEqual(len(restarted.reports(restarted.read(self.context,manifest['id']))),len(NBA_GAME_KEYS))

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
                self.assertEqual({j['parameters']['key'] for j in jobs},set(NBA_GAME_KEYS)-{'ppp'})
                self.assertTrue(all('Performance Summary' in j['display_label'] for j in jobs))
                joined=self.manager.ensure(self.context,self.request,'__all__')
                self.assertEqual(joined['id'],manifest['id']);self.assertEqual(len(self.backend.active_jobs()),len(NBA_GAME_KEYS)-1)
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
        self.assertEqual(len(self.manager.reports(completed)),len(NBA_GAME_KEYS))

    def test_load_reconciles_saved_and_missing_methods_while_another_is_running(self):
        previous=self.manager.new(self.context,{
            'ppp':dict(state='failed',message='Previous fit failed'),
            'epa':dict(state='running',job_id='active-epa'),
            'elo':dict(state='queued'),
        },self.request,'performance')
        saved=self.seed('ppp')
        self.seed('elo',{**self.request,'limit':11})
        app,_=self.app()
        values={'performance-load':1,'performance-context':self.context,'performance-poll':0,'performance-market':'margin',
                'performance-team':'__all__','performance-limit':10,'performance-start':None,'performance-end':None,
                'performance-playoffs':'exclude','performance-line':None,'performance-state':None,
                'performance-pages':{'NBA/game-outcome':previous['id']},'performance-tolerance':5,'performance-rendered':None,
                'performance-basis':'all'}
        def progress(job):
            return dict(state='running',percent=25,message='Evaluating') if job else None
        with patch.object(self.backend,'progress',side_effect=progress),patch.object(
                self.backend,'start',side_effect=lambda **p:f"new-{p['key']}") as start:
            result=self.post_callback(app,'performance_load',values,'performance-load.n_clicks')
            manifest=values['performance-state']
            self.assertEqual(manifest['id'],previous['id'])
            self.assertEqual(manifest['entries']['ppp'],dict(state='completed',run_id=saved['run_id'],reused=True))
            self.assertEqual(manifest['entries']['epa']['job_id'],'active-epa')
            self.assertEqual({call.kwargs['key'] for call in start.call_args_list},set(NBA_GAME_KEYS)-{'ppp','epa'})
            self.assertIn('1 loaded from saved runs',result['performance-status']['children'])
            table=self.post_callback(app,'performance_results',values,'performance-state.data')
            self.assertIn(saved['run_id'],str(table))
            self.assertIn('Running',str(table))
            self.assertIn('100.0%',str(table))
            self.post_callback(app,'performance_load',values,'performance-load.n_clicks')
            self.assertEqual(start.call_count,len(NBA_GAME_KEYS)-2)

    def test_load_joins_an_independent_backtest_with_the_same_inputs(self):
        self.seed('ppp')
        epa=next(model for model in available_models('NBA','game-outcome') if model.key=='epa')
        parameters=dict(**self.context,key='epa',entity='__all__',limit=10,lookback='Model Default',
                        market='winner',config_overrides=model_config(epa,'NBA'))
        running=[
            dict(id='matching-epa',state='running',percent=35,message='Evaluating',parameters=parameters),
            dict(id='different-elo',state='running',percent=20,message='Evaluating',parameters=dict(parameters,key='elo',limit=11)),
        ]
        with patch.object(self.backend,'active_jobs',return_value=running),patch.object(
                self.backend,'start',side_effect=lambda **p:f"new-{p['key']}") as start:
            manifest=self.manager.ensure(self.context,self.request,'__all__')
        self.assertTrue(manifest['entries']['ppp']['reused'])
        self.assertEqual(manifest['entries']['epa']['job_id'],'matching-epa')
        self.assertEqual(manifest['entries']['epa']['percent'],35)
        self.assertEqual({call.kwargs['key'] for call in start.call_args_list},set(NBA_GAME_KEYS)-{'ppp','epa'})

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
        reports={key:self.seed(key) for key in NBA_GAME_KEYS}
        changed=reports['epa'];changed['records']=changed['records'][1:]
        path=self.backend.root/'nba/game-outcome/epa'/(changed['run_id']+'.json')
        path.write_text(json.dumps(dict(result=changed)),encoding='utf-8')
        manifest=self.manager.ensure(self.context,self.request,'__all__')
        content=render(self.manager,manifest,'total',5,'common')
        self.assertIn('Shared sample (1)',str(content));self.assertIn('MAE',str(content))
        self.assertNotIn('Winner accuracy',str(content));self.assertNotIn('Brier score',str(next(c for c in components(content) if c.__class__.__name__=='Thead')))
        rows=performance_rows(content)
        self.assertTrue(all(row['Compared']=='1' for row in rows))
        self.assertTrue(all(row['MAE']=='2.000' for row in rows))
        self.assertEqual(list(rows[0])[2],'Accuracy (80% range)')
        self.assertTrue(all(row['Accuracy (80% range)']=='100.0%' for row in rows))
        margin=render(self.manager,manifest,'margin',5,'common')
        self.assertIn('Winner accuracy',str(margin));self.assertIn('Open results',str(margin));self.assertIn('100.0%',str(margin))
        full=render(self.manager,manifest,'margin',5,'all')
        self.assertEqual([row['Compared'] for row in performance_rows(full)],
                         ['1' if key=='epa' else '2' for key in NBA_GAME_KEYS])
        self.assertIn('Per-model samples',str(full))

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
            common=performance_rows(render(manager,manifest,'margin',5,'common'))
            self.assertEqual([row['Accuracy (80% range)'] for row in common[:2]],['0.0%','0.0%'])
            self.assertEqual([row['80% range hits / samples'] for row in common[:2]],['0 / 1','0 / 1'])
            self.assertEqual(common[0]['Winner accuracy'],'100.0%')
            total=performance_rows(render(manager,manifest,'total',5,'common'))
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
        self.assertIn('Rerun legacy 90% simulations for 80% coverage.',str(content))

    def test_player_team_scope_runs_all_supported_methods_and_unsupported_methods_stay_explicit(self):
        context=dict(sport='NFL',group='player-stats')
        request=settings(context,'player_pass_yds','PIT',10,line=200.5)
        self.seed('whp',request,context)
        with patch.object(self.backend,'start',return_value='pending') as start:
            manifest=self.manager.ensure(context,request,'PIT')
        self.assertEqual({call.kwargs['key'] for call in start.call_args_list},{'uo','oam','pge'})
        self.assertTrue(all(call.kwargs['entity']=='__all__' and call.kwargs['team_filter']=='PIT' for call in start.call_args_list))
        self.assertIn('Over accuracy',str(render(self.manager,manifest,'player_pass_yds',5)))
        with patch('app.sections.backtesting.performance_summary.market_spec',return_value=None),patch.object(self.backend,'start',return_value='pending') as start:
            other=self.manager.ensure(context,settings(context,'player_tds','__all__',10),'__all__')
        self.assertEqual(start.call_count,1)
        self.assertEqual(sum(e['state']=='unsupported' for e in other['entries'].values()),3)

    def test_routes_saved_team_options_and_hidden_pages_for_every_sport(self):
        app,callbacks=self.app()
        self.assertEqual(callbacks['restore_game_route'](PERFORMANCE_ROUTE),(no_update,'backtesting'))
        for sport in ['NBA','NFL','NCAAF']:
            self.assertEqual(page(sport,PERFORMANCE_ROUTE),[])
            link=next(c for c in navigation(sport) if getattr(c,'href',None)==PERFORMANCE_ROUTE)
            self.assertEqual(link.children,'Performance Summary')
            self.assertEqual(callbacks['show_backtesting']('backtesting',sport,PERFORMANCE_ROUTE)[1],{'display':'none'})
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
        for key in NBA_GAME_KEYS:self.seed(key)
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
                    inputs=[dict(**i,value=wire_value(i,values)) for i in meta['inputs']],state=[dict(**i,value=wire_value(i,values)) for i in meta['state']],changedPropIds=[changed]))
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
        report=self.seed('ppp')
        with patch.object(self.backend,'start',return_value='job-pending'):
            manifest=self.manager.ensure(self.context,self.request,'__all__')
        active_rows=performance_rows(render(self.manager,manifest,'margin',5))
        self.assertEqual({row['Status'] for row in active_rows},{'Completed','Running'})
        completed=next(row for row in active_rows if row['Status']=='Completed')
        link,stamp=completed['Saved simulation']
        self.assertEqual((link.children,stamp.dateTime),('Open results',report['created_at']))
        self.assertEqual(summary_route(link.href)['run_id'],report['run_id'])
        with patch.object(self.backend,'progress',return_value=dict(state='failed',message='No saved model inputs')):
            failed=self.manager.refresh(self.context,manifest['id'])
        content=render(self.manager,failed,'margin',5)
        issues=next(c for c in components(content) if getattr(c,'className',None)=='backtest-status performance-run-issues')
        self.assertIn('No saved model inputs',str(issues))
        failed_rows=[row for row in performance_rows(content) if row['Status']!='Completed']
        self.assertTrue(all(row['Status']=='' and row['Saved simulation'] is None for row in failed_rows))
        self.assertEqual(failed['entries']['ppp']['state'],'completed')
        with patch.object(self.backend,'start',return_value='new-job') as start:
            retried=self.manager.ensure(self.context,self.request,'__all__')
        self.assertNotEqual(retried['id'],failed['id'])
        self.assertEqual({call.kwargs['key'] for call in start.call_args_list},set(NBA_GAME_KEYS)-{'ppp'})
        restarted=PerformanceSummaries(BacktestingService(root=self.backend.root),watch=False)
        interrupted=restarted.refresh(self.context,retried['id'])
        self.assertEqual(interrupted['entries']['epa']['state'],'interrupted')
        self.assertEqual(len(restarted.reports(interrupted)),1)


if __name__=='__main__':
    main()
