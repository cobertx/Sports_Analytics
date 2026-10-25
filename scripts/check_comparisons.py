"""Offline common-cohort, persistent replay and Dash comparison regressions."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch
from threading import Barrier,Event
import json
import sys
import tempfile
import time

import pandas as pd
from dash import no_update
from dash.exceptions import PreventUpdate

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_backtesting import team_history,saved_game_report
from scripts.check_research import player_inputs
from sports_analytics.backtesting_service import BacktestingService,game_model
from sports_analytics.comparison import Comparisons,compare_reports,methods,page_route
from sports_analytics.dashboard import create_app
from sports_analytics import research_view


def report(key='ppp',identity='a'*64):
    result=saved_game_report(identity)
    result['context']['model']=key
    result['context']['workspace']='research'
    return result


def player_report(key,identity):
    result=report(key,identity)
    result['context'].update(group='player-stats',market='player_points',entity='__all__',line=20)
    result['records']=[dict(game_id='game',label=name+' · BOS vs DET',date='2025-09-20T00:00:00Z',
        actual=actual,predicted=actual+error,baseline=actual-1,lower=actual-2,upper=actual+2,interval_level=.8,
        probability=.7,event_actual=int(actual>20),push=False) for name,actual,error in [('Player A',25,2),('Player B',10,-4)]]
    result['skipped']=[]
    return result


def components(component):
    if isinstance(component,(list,tuple)):
        for child in component:yield from components(child)
    elif hasattr(component,'to_plotly_json'):
        yield component
        yield from components(getattr(component,'children',None))


class ComparisonChecks(TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.backend=Mock(root=Path(self.temporary.name)/'reports')
        self.backend.now.return_value=pd.Timestamp('2026-01-01',tz='UTC')
        self.reports={'ppp':report(),'epa':report('epa','b'*64),'elo':report('elo','c'*64)}
        self.backend.saved.side_effect=lambda sport,group,key,run_id:self.reports.get(key) if run_id and self.reports.get(key,{}).get('run_id')==run_id else None
        self.backend.start.side_effect=lambda **kwargs:'job-'+kwargs['key']
        self.backend.progress.side_effect=lambda job_id:dict(state='completed',result=self.reports[job_id.removeprefix('job-')])
        self.backend.options.return_value=([dict(label='All teams',value='__all__')],[dict(label='Points',value='player_points')],pd.DataFrame(dict(team=['BOS'])))
        self.backend.history.return_value=[]
        self.manager=Comparisons(self.backend,watch=False)
        self.context=dict(sport='NBA',group='game-outcome')

    def app(self):
        self.application=create_app(backtesting_service=self.backend)
        self.cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__
                 for m in self.application.callback_map.values() if m.get('callback')}
        return self.application

    def test_readers_wait_for_manifest_replacement_and_receive_the_completed_update(self):
        from sports_analytics.comparison import atomic_json
        manifest=self.manager.new(self.context,dict(ppp=dict(state='running')),{},'new')
        updated=deepcopy(manifest);updated['entries']['ppp']['state']='completed'
        entered,release=Event(),Event()
        def saving(path,data):
            entered.set()
            if not release.wait(5):raise AssertionError('Manifest write was not released')
            atomic_json(path,data)
        with patch('sports_analytics.comparison.atomic_json',side_effect=saving),ThreadPoolExecutor(max_workers=2) as pool:
            writer=pool.submit(self.manager.save,updated)
            try:
                self.assertTrue(entered.wait(2))
                reader=pool.submit(self.manager.read,self.context,manifest['id'])
                with self.assertRaises(TimeoutError):reader.result(timeout=.05)
            finally:release.set()
            writer.result(timeout=3)
            self.assertEqual(reader.result(timeout=3),updated)

    def test_navigation_and_page_controls_for_both_groups_and_every_sport(self):
        self.app()
        for sport in ['NBA','NFL','NCAAF']:
            for group,count in [('game-outcome',3),('player-stats',4)]:
                href=f'#research-{group}-comparison'
                self.assertEqual(page_route('RESEARCH',href,sport),dict(sport=sport,group=group))
                links=[c for c in components(research_view.navigation(sport,href)) if getattr(c,'href',None)==href]
                self.assertEqual(len(links),1);self.assertIn('active',links[0].className)
                self.assertIn('Methodology Comparison',str(research_view.page(sport,href)))
                configured=self.cb['configure_comparison']('RESEARCH',href,sport)
                self.assertEqual(len(configured[4]),count)
                self.assertEqual(configured[2],dict(sport=sport,group=group))
                self.assertEqual(configured[0],{'display':'block'})
                self.assertEqual(configured[1],{'display':'none'})
                self.assertEqual(self.cb['restore_game_route'](href),('RESEARCH','backtesting'))
                self.assertEqual(self.cb['show_backtesting']('backtesting','RESEARCH',href,sport)[3],{'display':'none'})
        self.assertIsNone(page_route('NBA','#research-game-outcome-comparison','NBA'))
        self.assertEqual(self.cb['configure_comparison']('RESEARCH','#research-game-outcome-ppp','NBA')[0],{'display':'none'})
        with self.application.server.test_client() as client:
            self.assertEqual(client.get('/_dash-layout').status_code,200)
            self.assertEqual(client.get('/_dash-dependencies').status_code,200)

    def test_common_game_samples_recompute_metrics_for_margin_and_total_without_mutation(self):
        second=self.reports['epa'];second['records']=second['records'][1:]
        second['records'][0]['predicted_margin']+=4
        second['records'][0]['predicted_total']+=5
        before=deepcopy(self.reports)
        compared=compare_reports(self.context,{k:self.reports[k] for k in ['ppp','epa']})
        self.assertEqual(compared['common'],1)
        self.assertEqual([r['compared'] for r in compared['summary']],[1,1])
        self.assertEqual([r['evaluated'] for r in compared['summary']],[2,1])
        self.assertEqual([r['mae'] for r in compared['summary']],[1,3])
        total=compare_reports(self.context,{k:self.reports[k] for k in ['ppp','epa']},target='total',tolerance=2)
        self.assertEqual([r['mae'] for r in total['summary']],[2,3])
        self.assertEqual([r['within_tolerance'] for r in total['summary']],[1,0])
        own=compare_reports(self.context,{k:self.reports[k] for k in ['ppp','epa']},basis='all')
        self.assertEqual([r['compared'] for r in own['summary']],[2,1])
        self.assertEqual(self.reports,before)
        second['records'][0]['actual_margin']+=1
        self.assertEqual(compare_reports(self.context,{k:self.reports[k] for k in ['ppp','epa']})['conflicting_actuals'],1)

    def test_player_appearance_alignment_statistic_and_line_compatibility(self):
        context=dict(sport='NBA',group='player-stats')
        reports={'whp':player_report('whp','d'*64),'uo':player_report('uo','e'*64)}
        reports['uo']['records']=reports['uo']['records'][1:]
        result=compare_reports(context,reports)
        self.assertEqual(result['common'],1)
        self.assertEqual(result['summary'][0]['mae'],4)
        self.assertIn('Player B',result['records']['whp'][0]['label'])
        for field,value in [('market','player_assists'),('line',None)]:
            changed=deepcopy(reports);changed['uo']['context'][field]=value
            with self.assertRaises(ValueError):compare_reports(context,changed)
        duplicate=deepcopy(reports);duplicate['uo']['records']*=2
        with self.assertRaises(ValueError):compare_reports(context,duplicate)

    def test_saved_comparisons_persist_and_reject_cross_group_and_invalid_paths(self):
        manifest=self.manager.from_saved(self.context,['ppp','epa'],dict(ppp='a'*64,epa='b'*64))
        restarted=Comparisons(self.backend)
        self.assertEqual(restarted.read(self.context,manifest['id']),manifest)
        self.assertEqual(restarted.history(self.context)[0],manifest)
        self.assertEqual(set(restarted.reports(manifest)),{'ppp','epa'})
        self.assertIsNone(restarted.read(dict(sport='NFL',group='game-outcome'),manifest['id']))
        self.assertIsNone(restarted.read(self.context,'../report'))
        for keys in [[],['whp'],['ppp','ppp']]:
            with self.assertRaises(ValueError):restarted.from_saved(self.context,keys,{})
        with self.assertRaises(ValueError):restarted.from_saved(self.context,['epa'],dict(epa='a'*64))

    def test_real_default_game_replays_overlap_save_and_restore(self):
        frame=team_history().assign(season_type='regular')
        backend=BacktestingService(root=Path(self.temporary.name)/'real',now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
        backend.game_inputs=lambda *args:(frame,None)
        manager=Comparisons(backend);context=dict(sport='NCAAF',group='game-outcome')
        entered,release=Barrier(3),Event();original=backend._games
        def concurrent(*args,**kwargs):
            entered.wait(timeout=25)
            if not release.wait(timeout=25):raise RuntimeError('Workers were not released')
            return original(*args,**kwargs)
        backend._games=concurrent
        with patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
            manifest=manager.launch(context,['ppp','epa'],limit=2)
            try:entered.wait(timeout=25)
            finally:release.set()
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                manifest=manager.read(context,manifest['id'])
                if all(e['state'] not in {'queued','running'} for e in manifest['entries'].values()):break
                time.sleep(.03)
            self.assertTrue(all(e['state']=='completed' for e in manifest['entries'].values()),manifest)
            reports=manager.reports(manifest)
            self.assertEqual(set(reports),{'ppp','epa'})
            result=compare_reports(context,reports)
            self.assertEqual(result['common'],2)
            for key,report_value in reports.items():
                self.assertEqual(report_value['config'],__import__('dataclasses').asdict(game_model('NCAAF',key).config))
                self.assertTrue((backend.root/report_value['saved_directory']/'report.json').exists())
                self.assertTrue(all(pd.to_datetime(r['latest_training'],utc=True)<pd.Timestamp(r['date']) for r in report_value['records']))
            restored=Comparisons(backend).read(context,manifest['id'])
            self.assertEqual(Comparisons(backend).reports(restored),reports)

    def test_real_player_comparison_shares_stat_team_and_appearance_cohort(self):
        from models.Player_Stats.Usage_Opportunity.prediction_service import UsageOpportunityMarketService
        players,teams=player_inputs()
        backend=BacktestingService(root=Path(self.temporary.name)/'real-players',now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
        backend.player_inputs=lambda sport:players
        backend.game_inputs=lambda *args:(teams,None)
        backend.phase_map=lambda sport:{}
        backend.player_service=lambda key:UsageOpportunityMarketService(repository=SimpleNamespace(
            root=backend.root/'model_cache'/key,load=lambda *args:(players,teams)))
        manager=Comparisons(backend);context=dict(sport='NBA',group='player-stats')
        with patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
            manifest=manager.launch(context,['whp','uo'],market='player_points',team_filter='BOS',limit=2,line=20)
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                manifest=manager.refresh(context,manifest['id'])
                if all(e['state'] not in {'queued','running'} for e in manifest['entries'].values()):break
                time.sleep(.03)
            self.assertTrue(all(e['state']=='completed' for e in manifest['entries'].values()),manifest)
            reports=manager.reports(manifest);data=compare_reports(context,reports)
            self.assertEqual(data['common'],4)
            self.assertTrue(all('BOS vs DET' in row['label'] for rows in data['records'].values() for row in rows))
            self.assertEqual([row['probability_samples'] for row in data['summary']],[4,4])

    def test_failed_or_interrupted_methods_do_not_hide_successful_reports(self):
        manifest=self.manager.launch(self.context,['ppp','epa'],limit=2)
        self.backend.progress.side_effect=lambda job_id:dict(state='completed',result=self.reports['ppp']) if job_id=='job-ppp' else dict(state='failed',message='Missing EPA inputs',percent=0)
        refreshed=self.manager.refresh(self.context,manifest['id'])
        self.assertEqual(refreshed['entries']['epa']['state'],'failed')
        self.assertEqual(set(self.manager.reports(refreshed)),{'ppp'})
        running=self.manager.launch(self.context,['elo'],limit=2)
        self.backend.progress.return_value=None;self.backend.progress.side_effect=None
        interrupted=Comparisons(self.backend).refresh(self.context,running['id'])
        self.assertEqual(interrupted['entries']['elo']['state'],'interrupted')

    def test_background_persistence_retries_transient_windows_locks(self):
        manifest=self.manager.from_saved(self.context,['ppp'],dict(ppp='a'*64))
        with patch.object(self.manager,'refresh',side_effect=[PermissionError('Temporary file lock'),manifest]) as refresh, \
             patch('sports_analytics.comparison.time.sleep') as pause:
            self.manager.follow(self.context,manifest['id'])
            self.assertEqual(refresh.call_count,2);pause.assert_called_once_with(.5)
        with patch('sports_analytics.comparison.Path.read_text',side_effect=PermissionError('Temporary file lock')):
            self.assertIsNone(self.manager.read(self.context,manifest['id']))
            with self.assertRaises(PermissionError):self.manager.refresh(self.context,manifest['id'])

    def test_callbacks_preserve_page_jobs_validate_before_launch_and_skip_progress_rerenders(self):
        self.app();run=self.cb['run_comparison']
        common=[['ppp','epa'],'__all__','__all__',None,None,None,2,'exclude','Model Default',5,None]
        with patch('sports_analytics.comparison_callbacks.ctx',SimpleNamespace(triggered_id='comparison-run')):
            initial=run(1,self.context,0,0,0,*common,None,{},None,[],[])
            self.assertEqual(self.backend.start.call_count,2)
            for call in self.backend.start.call_args_list:
                self.assertEqual(call.kwargs['limit'],2);self.assertEqual(call.kwargs['workspace'],'research')
            invalid=common.copy();invalid[6]=1.5
            self.backend.start.reset_mock()
            rejected=run(2,self.context,0,0,0,*invalid,initial[0],initial[2],None,[],[])
            self.backend.start.assert_not_called();self.assertIs(rejected[0],no_update)
        with patch('sports_analytics.comparison_callbacks.ctx',SimpleNamespace(triggered_id='comparison-context')):
            restored=run(1,self.context,0,0,0,*common,None,initial[2],None,[],[])
        self.assertEqual(restored[0]['id'],initial[0]['id']);self.assertTrue(restored[3])
        shown=self.cb['show_comparison'](restored[0],self.context,'margin','common',5,None)
        self.assertFalse(shown[1]);self.assertEqual(len(shown[2]),2)
        with self.assertRaises(PreventUpdate):self.cb['show_comparison'](restored[0],self.context,'margin','common',5,None,shown[4])
        self.assertIn('Saved comparison',str(shown[0]))
        detail=self.cb['comparison_details'](restored[0],self.context,'ppp','margin')
        self.assertIn('Parameters used',str(detail[0]))
        exported=self.cb['export_comparison'](1,restored[0],self.context,'margin','common',5)
        self.assertIn('model_parameters',exported['content']);self.assertIn('comparison_id',exported['content'])

    def test_real_dash_http_submits_only_selected_methods_and_poll_restores_results(self):
        self.app()
        values={'comparison-run':1,'comparison-context':self.context,'comparison-poll':0,'comparison-history-open':0,'comparison-saved-open':0,
            'comparison-methods':['ppp','epa'],'comparison-entity':'__all__','comparison-team':'__all__','comparison-statistic':None,
            'comparison-start':None,'comparison-end':None,'comparison-limit':2,'comparison-playoffs':'exclude','comparison-lookback':'Model Default',
            'comparison-tolerance':5,'comparison-line':None,'comparison-state':None,'comparison-pages':{},'comparison-history':None}
        key,meta=next((key,meta) for key,meta in self.application.callback_map.items()
            if meta.get('callback') and meta['callback'].__wrapped__.__name__=='run_comparison')
        with self.application.server.test_client() as client:
            payload=dict(output=key,outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                state=[dict(**item,value=values.get(item['id'],[])) for item in meta['state']],changedPropIds=['comparison-run.n_clicks'])
            response=client.post('/_dash-update-component',json=payload)
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            first=response.get_json()['response']
            self.assertEqual(set(first['comparison-state']['data']['entries']),{'ppp','epa'})
            for item in payload['state']:
                if item['id']=='comparison-state':item['value']=first['comparison-state']['data']
                if item['id']=='comparison-pages':item['value']=first['comparison-pages']['data']
            payload['changedPropIds']=['comparison-poll.n_intervals']
            response=client.post('/_dash-update-component',json=payload)
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            self.assertTrue(response.get_json()['response']['comparison-poll']['disabled'])


if __name__=='__main__':main()
