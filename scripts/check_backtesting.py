"""Offline replay equivalence and isolation regressions; no saved data required."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
import sys
import json
import tempfile
import time
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.replay import calibration_tail, cached_calculation, replay_cache, fingerprint
from sports_analytics.backtesting_service import BacktestingService, estimation_history, game_model, eligible_pairs
from models.Player_Stats.Weighted_Historical_Performance.core import WHPConfig, WeightedHistoricalPerformance, eligible_history
from models.Player_Stats.Usage_Opportunity.historical_data import UsageHistoryRepository
from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
from models.Player_Stats.Pace_Game_Environment.pace_model import PaceModel


def full_calibration(history, grouping, maximum, evaluate):
    errors = [row for _, held in history.groupby(grouping, sort=True) for row in evaluate(held)]
    return sorted(errors, key=lambda row: (row['date'], row['game_id']))[-maximum:]


def team_history():
    rng, rows = np.random.default_rng(761), []
    for day in range(22):
        date = pd.Timestamp('2025-08-01', tz='UTC')+pd.Timedelta(days=3*day)
        for number, (home, away) in enumerate([('A', 'B'), ('C', 'D')]):
            scores = rng.integers(15, 45, 2)
            if scores[0] == scores[1]: scores[0] += 1
            for i, (team, opponent) in enumerate([(home, away), (away, home)]):
                rows.append(dict(game_id=f'{day:02d}-{number}', event_date=date,
                    available_at=date+pd.Timedelta(hours=8), team=team, opponent=opponent,
                    points=int(scores[i]), points_allowed=int(scores[1-i]), possessions=100.,
                    offensive_points=int(scores[i]), offensive_points_allowed=int(scores[1-i]),
                    non_offensive_points=0., offensive_plays=65., defensive_plays=65.,
                    offensive_drives=11., defensive_drives=11., season=2025, week=day+1,
                    neutral=0, is_home=int(i == 0), off_ppa=float(rng.normal(.1, .2)), def_ppa=.1,
                    off_epa_plays=65., def_epa_plays=65.,
                    **{f'{side}_{kind}_epa_{stat}': (35. if kind == 'pass' else 30.) if stat == 'plays'
                       else float(rng.normal(2, 5)) for side in ('off', 'def')
                       for kind in ('pass', 'rush') for stat in ('plays', 'sum')}))
    return pd.DataFrame(rows)


def saved_game_report(run_id='a'*64,created='2025-10-01T12:00:00Z'):
    context=dict(sport='NBA',group='game-outcome',model='ppp',entity='__all__',entity_label='All teams',
        market=None,start=None,end=None,limit=10,tolerance=5,line=None,lookback=20,include_playoffs=False,interval_level=.8)
    records=[dict(game_id=str(i),date=f'2025-09-2{i}T00:00:00Z',label='A @ B',actual_margin=5+i,
        predicted_margin=4+i,actual_total=200+i,predicted_total=198+i,margin_sd=10,total_sd=20,
        baseline_margin=3,baseline_total=201,probability=.6,event_actual=1,push=False,latest_training='2025-09-19') for i in range(2)]
    return dict(context=context,run_id=run_id,created_at=created,records=records,config={'example':1},
        attempted=3,skipped=[dict(game_id='skip',date='2025-09-19T00:00:00Z',reason='Insufficient history')])


class ReplayChecks(TestCase):
    def test_prediction_timeline_omits_offseason_gaps_and_retains_dates_and_intervals(self):
        from copy import deepcopy
        from sports_analytics.backtesting_results import render
        from sports_analytics.display_time import format_eastern
        dates=['2025-04-10T00:00:00Z','2025-04-12T00:00:00Z',
               '2025-10-21T00:00:00Z','2025-10-21T02:00:00Z']
        for sport in ('NBA','NFL','NCAAF'):
            for group,target in [('game-outcome','margin'),('game-outcome','total'),('player-stats','margin')]:
                with self.subTest(sport=sport,group=group,target=target):
                    source=saved_game_report()
                    source['context'].update(sport=sport,group=group,market='player_points',limit=4)
                    source['records']=[dict(source['records'][i%2],game_id=str(i),date=stamp,
                        actual=10.,predicted=9.,baseline=8.,lower=8.,upper=10.,interval_level=.8,
                        margin_sd=.01 if i==2 else 10.,total_sd=.01 if i==2 else 20.) for i,stamp in enumerate(dates)]
                    source['attempted']=4;original=deepcopy(source)
                    content=render(source,target)
                    grid=next(c for c in content.children if getattr(c,'className','')=='backtest-chart-grid')
                    timeline=next(c.children[0].figure for c in grid.children
                                  if c.children[0].figure.layout.title.text=='Predicted vs. actual over time')
                    self.assertEqual(timeline.layout.xaxis.type,'linear')
                    self.assertEqual(list(timeline.layout.xaxis.tickvals),[1,2,3,4])
                    self.assertEqual(list(timeline.data[2].x),[1,2,3,4])
                    self.assertTrue(all(list(trace.x)==[1,2,3,4] for trace in timeline.data[:3]))
                    expected=[format_eastern(stamp) for stamp in dates]
                    self.assertEqual([row[0] for row in timeline.data[2].customdata],expected)
                    self.assertEqual(sorted(x for trace in timeline.data[3:] for x in trace.x),[1,2,3,4])
                    for trace in timeline.data[3:]:
                        for x,custom in zip(trace.x,trace.customdata):
                            self.assertEqual(custom[0],expected[x-1])
                            self.assertEqual(custom[1],timeline.data[0].y[x-1])
                            self.assertEqual(custom[2],timeline.data[1].y[x-1])
                    self.assertEqual(source,original)

    def test_team_game_limit_counts_home_and_away_games_before_replaying_every_model(self):
        frame=team_history().assign(season_type='regular')
        # A alternates home/away; unrelated C/D games fill half the league sample.
        flip=frame.game_id.str.endswith('-0') & frame.game_id.str[:2].astype(int).mod(2).eq(1)
        frame.loc[flip,'is_home']=1-frame.loc[flip,'is_home']
        frame=frame.sample(frac=1,random_state=13)
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
            service.game_inputs=lambda sport,key:(frame.copy(),None)
            def evaluate(result,model,history,dataset,games,progress):
                pd.testing.assert_frame_equal(history,frame,check_dtype=False)  # Full league training inputs remain intact.
                for pair in games:
                    row=dict(saved_game_report()['records'][0])
                    row.update(game_id=str(pair.iloc[0].game_id),date=pair.event_date.min().isoformat(),
                        actual=5.,predicted=4.,baseline=3.,lower=-10.,upper=20.,interval_level=.8)
                    result['records'].append(row)
            with patch.object(service,'_games',side_effect=evaluate),patch(
                    'sports_analytics.backtesting_service.implementation_signature',return_value='scope-test'):
                for sport in ('NBA','NFL','NCAAF'):
                    for key in ('ppp','epa','elo'):
                        with self.subTest(sport=sport,key=key):
                            for scope in (dict(entity='A'),dict(entity='__all__',team_filter='A')):
                                result=service.run(sport,'game-outcome',key,limit=3,**scope)
                                self.assertEqual([r['game_id'] for r in result['records']],['19-0','20-0','21-0'])
                                self.assertEqual(result['attempted'],3)
                                self.assertEqual(result['context']['entity'],'A')
                            self.assertEqual(service.available_games(sport,'game-outcome',key,'__all__',team_filter='A'),22)
                league=service.run('NBA','game-outcome','ppp',limit=3)
                self.assertEqual([r['game_id'] for r in league['records']],['20-1','21-0','21-1'])
                fewer=service.run('NBA','game-outcome','ppp',entity='A',limit=100)
                self.assertEqual(fewer['attempted'],22)

    def test_team_sample_respects_dates_and_complete_available_pairs(self):
        frame=team_history().assign(season_type='regular')
        lower=pd.Timestamp('2025-09-20',tz='UTC');upper=pd.Timestamp('2025-10-02',tz='UTC')
        now=pd.Timestamp('2026-01-01',tz='UTC')
        # An incomplete latest pair and a result not yet available cannot consume the cap.
        frame=frame[~(frame.game_id.eq('20-0')&frame.team.eq('B'))].copy()
        frame.loc[frame.game_id.eq('19-0')&frame.team.eq('B'),'available_at']=now
        games=eligible_pairs(frame,'A',lower,upper,now,limit=2)
        self.assertEqual([g.iloc[0].game_id for g in games],['17-0','18-0'])
        self.assertTrue(all(len(g)==2 for g in games))

    def test_player_team_scope_and_single_player_limits_use_latest_appearances(self):
        frame=team_history().rename(columns={'team':'entity_name'}).assign(season_type='regular')
        frame['entity_id']=frame.entity_name;frame['team']=frame.entity_name.map(dict(A='TARGET',B='TARGET',C='OTHER',D='OTHER'))
        frame['pts']=frame.points
        # The target team's final games precede the newest unrelated league games.
        frame=frame[~frame.entity_name.isin(['A','B'])|frame.game_id.str[:2].astype(int).le(18)]
        frame=frame.sample(frac=1,random_state=7)
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
            service.player_inputs=lambda sport:frame.copy()
            def evaluate(result,sport,key,history,selected,games,spec,market,line,progress):
                self.assertEqual(set(history.entity_id),{'A','B','C','D'})
                for g in games:
                    result['records'].append(dict(game_id=g.game_id,date=g.event_date.isoformat(),label=g.entity_name,
                        actual=float(g.value),predicted=10.,baseline=10.,lower=0.,upper=100.,interval_level=.8))
            with patch.object(service,'_players',side_effect=evaluate),patch(
                    'sports_analytics.backtesting_service.implementation_signature',return_value='scope-test'),patch(
                    'sports_analytics.backtesting_service.source_revisions',return_value=[]):
                for key in ('whp','uo','oam','pge'):
                    with self.subTest(key=key):
                        result=service.run('NBA','player-stats',key,'__all__','player_points',team_filter='TARGET',limit=3)
                        self.assertEqual([r['game_id'] for r in result['records']],['16-0']*2+['17-0']*2+['18-0']*2)
                        self.assertEqual({r['label'] for r in result['records']},{'A','B'})
                        own=service.run('NBA','player-stats',key,'A','player_points',limit=3)
                        self.assertEqual([r['game_id'] for r in own['records']],['16-0','17-0','18-0'])

    def test_game_count_note_explains_team_scope_and_insufficient_saved_games(self):
        from sports_analytics.dashboard import create_app
        service=BacktestingService()
        app=create_app(backtesting_service=service)
        callback=next(m['callback'].__wrapped__ for m in app.callback_map.values()
                      if m.get('callback') and m['callback'].__wrapped__.__name__=='backtest_available')
        with patch.object(service,'available_games',return_value=86):
            context=dict(sport='NBA',group='game-outcome',model='ppp')
            note=callback(context,'GSW',None,None,None,'exclude')
            self.assertIn('86 saved games available for GSW',note)
            self.assertIn('latest games within this scope',note)
            self.assertIn('download automatically',note)
            # Editing the cap must not reread/rank every saved player appearance.
            metadata=next(m for m in app.callback_map.values() if m.get('callback') and
                          m['callback'].__wrapped__.__name__=='backtest_available')
            self.assertNotIn('backtest-limit',[item['id'] for item in metadata['inputs']])

    def test_error_distribution_statistics_follow_saved_report_targets(self):
        from sports_analytics.backtesting_results import render,error_distribution,figure,error_summary
        source=saved_game_report()
        frozen=json.dumps(source,sort_keys=True)
        for target,expected in [('margin',-1),('total',-2)]:
            content=render(source,target)
            grid=next(c for c in content.children if getattr(c,'className','')=='backtest-chart-grid')
            charts=[c.children[0].figure for c in grid.children]
            distribution=next(chart for chart in charts if chart.layout.title.text=='Forecast error distribution')
            stats=distribution.layout.meta['error_distribution']
            self.assertEqual(stats['mean'],expected)
            self.assertEqual(stats['median'],expected)
            self.assertEqual(stats['sample_sd'],0)
            self.assertEqual(stats['mae'],abs(expected))
            self.assertEqual(stats['samples'],2)
            self.assertEqual(len(distribution.layout.annotations),6)
            self.assertTrue(all(a.y>1 and a.yref=='paper' for a in distribution.layout.annotations))
            self.assertIn('Sample SD',str(distribution.layout.annotations))
            self.assertIn('2-percentage-point bins',str(content))
        self.assertEqual(json.dumps(source,sort_keys=True),frozen)
        single=figure('Single');error_distribution(single,[2])
        self.assertIn('N/A',str(single.layout.annotations))
        self.assertEqual(sum(single.data[0].y),1)
        empty=figure('Empty');error_distribution(empty,[])
        self.assertIn('No evaluated',empty.layout.annotations[0].text)
        with self.assertRaises(ValueError):error_summary([0,float('nan')])

    def test_history_lists_legacy_and_archived_reports_once_and_refreshes_metadata(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory)
            root=Path(directory)/'nba/game-outcome/ppp';root.mkdir(parents=True)
            old=saved_game_report();old.pop('run_id')
            old_path=root/('a'*64+'.json');old_path.write_text(json.dumps(dict(result=old)),encoding='utf-8')
            new=saved_game_report('b'*64,'2025-10-02T12:00:00Z')
            service.ensure_exports(new)
            cache=root/('b'*64+'.json');cache.write_text(json.dumps(dict(result=new)),encoding='utf-8')
            wrong=deepcopy(new);wrong['context']['sport']='NFL'
            (root/('c'*64+'.json')).write_text(json.dumps(dict(result=wrong)),encoding='utf-8')
            (root/('d'*64+'.json')).write_text('broken',encoding='utf-8')
            (root/'last_run.json').write_text(json.dumps(dict(report=cache.name)),encoding='utf-8')
            history=service.history('NBA','game-outcome','ppp')
            self.assertEqual([r['run_id'] for r in history],['b'*64,'a'*64])
            self.assertEqual((history[0]['evaluated'],history[0]['skipped'],history[0]['lookback']),(2,1,20))
            history[0]['entity_label']='mutated'
            self.assertEqual(service.history('NBA','game-outcome','ppp')[0]['entity_label'],'All teams')
            self.assertEqual(service.history('NFL','game-outcome','ppp'),[])
            self.assertEqual(service.history('NBA','player-stats','whp'),[])
            self.assertEqual(service.history('NBA','../escape','ppp'),[])
            old['created_at']='2025-10-03T12:00:00Z'
            old_path.write_text(json.dumps(dict(result=old)),encoding='utf-8')
            self.assertEqual(service.history('NBA','game-outcome','ppp')[0]['run_id'],'a'*64)
            cache.unlink()
            self.assertEqual(service.saved('NBA','game-outcome','ppp','b'*64),new)

    def test_legacy_json_restores_exact_results_and_recovers_csv_exports(self):
        from sports_analytics.backtesting_service import evaluated_records
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory)
            root=Path(directory)/'nba/game-outcome/ppp';root.mkdir(parents=True)
            legacy=saved_game_report();legacy.pop('run_id')
            original=json.dumps(dict(result=legacy));path=root/('a'*64+'.json')
            path.write_text(original,encoding='utf-8')
            with patch.object(service,'run',side_effect=AssertionError('History must not rerun a model')):
                loaded=service.saved('NBA','game-outcome','ppp','a'*64)
                self.assertEqual(loaded['records'],legacy['records'])
                folder=service.ensure_exports(loaded)
                self.assertEqual(path.read_text(encoding='utf-8'),original)
                csvs=sorted(folder.glob('*.csv'));self.assertEqual(len(csvs),2)
                for target in ['margin','total']:
                    csv=next(p for p in csvs if '_'+target+'_' in p.name)
                    values=pd.read_csv(csv)
                    expected=evaluated_records(loaded,target)
                    np.testing.assert_array_equal(values.predicted,[r['predicted'] for r in expected])
                    np.testing.assert_array_equal(values.actual,[r['actual'] for r in expected])
                before=[p.stat().st_mtime_ns for p in csvs]
                service.ensure_exports(loaded)
                self.assertEqual([p.stat().st_mtime_ns for p in csvs],before)
                path.unlink()
                self.assertEqual(service.saved('NBA','game-outcome','ppp','a'*64),loaded)
                self.assertEqual(service.latest('NBA','game-outcome','ppp'),loaded)

    def test_previous_simulations_open_via_dash_and_remain_selected_after_navigation(self):
        from dash import no_update
        from sports_analytics.dashboard import create_app
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory)
            result=saved_game_report();service.ensure_exports(result)
            service._jobs['running']=dict(state='running',percent=25,message='Evaluating',display_label='All teams',completed=1,total=4,
                parameters=dict(sport='NBA',group='game-outcome',key='ppp'))
            app=create_app(backtesting_service=service)
            callbacks={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__
                       for m in app.callback_map.values() if m.get('callback')}
            context={k:result['context'][k] for k in ['sport','group','model']}
            with patch.object(service,'history',side_effect=AssertionError('Closed history should be lazy')):
                self.assertEqual(callbacks['previous_simulations'](context,False,0,None)[0],[])
            options,value,message,location=callbacks['previous_simulations'](context,True,0,None)
            self.assertEqual(options[0]['value'],result['run_id']);self.assertIsNone(value)
            self.assertIn('2 evaluated',options[0]['label']);self.assertIn('nba',location)
            self.assertTrue(callbacks['enable_previous_simulation'](None))
            self.assertFalse(callbacks['enable_previous_simulation'](result['run_id']))
            self.assertEqual(callbacks['previous_simulations'](dict(sport='NFL',group='game-outcome',model='ppp'),True,0,result['run_id'])[0],[])
            pages={'NBA/game-outcome/ppp':dict(job_id='running')}
            controls=['A',None,None,None,10,5,None,'exclude',None]
            run_key=next(key for key,m in app.callback_map.items()
                         if m.get('callback') and m['callback'].__wrapped__.__name__=='run_backtest')
            metadata=app.callback_map[run_key]
            input_values={'backtest-run':1,'backtest-context':context,'backtest-poll':0,'backtest-history-open':1}
            state_values={item['id']:value for item,value in zip(metadata['state'],[*controls,'running',[],pages,result['run_id']])}
            payload=dict(output=run_key,
                outputs=[dict(id=o.component_id,property=o.component_property) for o in metadata['output']],
                inputs=[dict(**item,value=input_values[item['id']]) for item in metadata['inputs']],
                state=[dict(**item,value=state_values.get(item['id'],[])) for item in metadata['state']],
                changedPropIds=['backtest-history-open.n_clicks'])
            with patch.object(service,'start',side_effect=AssertionError('Reopening must not start a job')):
                with app.server.test_client() as client:
                    response=client.post('/_dash-update-component',json=payload)
                    self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                    opened=response.get_json()['response']
            self.assertEqual(opened['backtest-result']['data'],result)
            remembered=opened['backtest-pages']['data']
            self.assertEqual(remembered['NBA/game-outcome/ppp'],dict(run_id=result['run_id']))
            self.assertTrue(opened['backtest-poll']['disabled'])
            self.assertEqual(service.progress('running')['state'],'running')
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='backtest-context')):
                restored=callbacks['run_backtest'](1,context,0,*controls,None,[],remembered)
            self.assertEqual(restored[0],result)
            rendered,disabled=callbacks['show_backtest_results'](result,context,'total')
            self.assertFalse(disabled)
            download=callbacks['export_backtest'](1,result,context,'total')
            self.assertIn('_total_',download['filename'])
            self.assertIn(result['run_id'][:8],download['filename'])
            self.assertIn('predicted_total',download['content'])
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='backtest-history-open')):
                invalid=callbacks['run_backtest'](1,context,0,*controls,None,[],remembered,1,'../report')
            self.assertIs(invalid[0],no_update)

    def test_navigation_restores_each_pages_job_and_background_results(self):
        from dash import no_update
        from sports_analytics.dashboard import create_app
        with tempfile.TemporaryDirectory() as directory:
            service = BacktestingService(root=directory)
            app = create_app(backtesting_service=service)
            callback = next(m['callback'].__wrapped__ for m in app.callback_map.values()
                            if m.get('callback') and m['callback'].__wrapped__.__name__ == 'run_backtest')
            pages = {}
            controls = ['A', None, None, None, 10, 5, None, 'exclude', None]
            def visit(trigger, context, visible_job=None):
                nonlocal pages
                with patch('sports_analytics.dashboard.ctx', SimpleNamespace(triggered_id=trigger)):
                    output = callback(1, context, 1, *controls, visible_job, [], pages)
                if output[8] is not no_update: pages = output[8]
                return output
            contexts = [dict(sport=sport, group=group, model=model) for sport, group, model in
                [('NBA', 'game-outcome', 'ppp'), ('NBA', 'game-outcome', 'epa'),
                 ('NBA', 'player-stats', 'whp'), ('NFL', 'game-outcome', 'ppp')]]
            results = []
            for index, context in enumerate(contexts):
                job_id = f'job-{index}'
                self.assertIsNone(visit('backtest-context', context)[0])
                with patch.object(service, 'start', return_value=job_id):
                    launched = visit('backtest-run', context)
                self.assertIs(launched[0], no_update)
                service._jobs[job_id] = dict(state='running', percent=25, message='Evaluating', reused=False,
                    parameters=dict(sport=context['sport'], group=context['group'], key=context['model']))
                result = dict(context=context, run_id=str(index)*64, records=[dict(game_id=str(index))], skipped=[])
                results.append(result)
            # Re-enter the first page while another page owns the visible job.
            restored = visit('backtest-context', contexts[0], 'job-3')
            self.assertEqual(restored[2], 'job-0')
            self.assertFalse(restored[3]); self.assertEqual(restored[4], 25)
            for index, result in enumerate(results):
                service._jobs[f'job-{index}'].update(state='completed', percent=100, result=result)
            # All of these jobs finished while their individual page polling
            # was inactive; returning must still collect the correct result.
            for index, context in enumerate(contexts):
                restored = visit('backtest-context', context, 'job-0')
                self.assertEqual(restored[0], results[index])
                self.assertEqual(restored[2], f'job-{index}')
                self.assertTrue(restored[3])
                self.assertEqual(restored[4], 100)
            hidden = visit('backtest-context', None)
            self.assertIs(hidden[8], no_update)
            self.assertEqual(visit('backtest-context', contexts[0])[0], results[0])
            self.assertEqual(len(pages), 4)
            self.assertTrue(all(set(entry) <= {'job_id', 'run_id'} for entry in pages.values()))

    def test_page_report_survives_rerun_failure_pruning_and_restart(self):
        from dash import no_update
        from sports_analytics.dashboard import create_app
        with tempfile.TemporaryDirectory() as directory:
            context = dict(sport='NBA', group='game-outcome', model='ppp')
            result = dict(context=context, run_id='a'*64, records=[dict(game_id='saved')], skipped=[])
            destination = Path(directory)/'nba/game-outcome/ppp'/('a'*64+'.json')
            destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps(dict(result=result)), encoding='utf-8')
            pages = {'NBA/game-outcome/ppp': dict(run_id=result['run_id'], job_id='pruned')}
            controls = ['A', None, None, None, 10, 5, None, 'exclude', None]
            for restart in [False, True]:
                service = BacktestingService(root=directory)
                app = create_app(backtesting_service=service)
                callback = next(m['callback'].__wrapped__ for m in app.callback_map.values()
                                if m.get('callback') and m['callback'].__wrapped__.__name__ == 'run_backtest')
                def invoke(trigger):
                    nonlocal pages
                    with patch('sports_analytics.dashboard.ctx', SimpleNamespace(triggered_id=trigger)):
                        output = callback(1, context, 1, *controls, 'other-page-job', [], pages)
                    if output[8] is not no_update: pages = output[8]
                    return output
                with patch.object(service, 'latest', side_effect=AssertionError('Must restore the exact remembered report')):
                    self.assertEqual(invoke('backtest-context')[0], result)
                    if restart: continue
                    with patch.object(service, 'start', return_value='retry'):
                        self.assertIs(invoke('backtest-run')[0], no_update)
                    service._jobs['retry'] = dict(state='failed', percent=30, message='Missing input',
                        parameters=dict(sport='NBA', group='game-outcome', key='ppp'))
                    self.assertIs(invoke('backtest-poll')[0], no_update)
                    self.assertEqual(invoke('backtest-context')[0], result)
                    service._jobs.clear()
                    self.assertEqual(invoke('backtest-context')[0], result)
            from sports_analytics.backtesting_results import layout
            store = next(child for child in layout().children if getattr(child, 'id', None) == 'backtest-pages')
            self.assertEqual(store.storage_type, 'session')

    def test_saved_report_lookup_rejects_invalid_ids_and_wrong_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            service = BacktestingService(root=directory)
            for run_id in [None, '../report', 'a'*63, 'a'*64+'.json', 'G'*64]:
                self.assertIsNone(service.saved('NBA', 'game-outcome', 'ppp', run_id))
            destination = Path(directory)/'nba/game-outcome/ppp'/('b'*64+'.json')
            destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps(dict(result=dict(context=dict(sport='NFL', group='game-outcome', model='ppp')))), encoding='utf-8')
            self.assertIsNone(service.saved('NBA', 'game-outcome', 'ppp', 'b'*64))
            destination.write_text('{invalid json', encoding='utf-8')
            self.assertIsNone(service.saved('NBA', 'game-outcome', 'ppp', 'b'*64))

    def test_real_backtests_overlap_and_save_independent_reports(self):
        from models.Game_Outcome.PPP.PPP import PPPConfig, PPPModel
        frame = team_history().assign(season_type='regular')
        config = PPPConfig(minimum_training_games=6, minimum_team_games=1,
                           minimum_calibration_games=4, maximum_calibration_games=10)
        entered, release = Barrier(3), Event()
        with tempfile.TemporaryDirectory() as directory:
            service = BacktestingService(root=directory, now=lambda: pd.Timestamp('2026-01-01', tz='UTC'))
            service.game_inputs = lambda *args: (frame, None)
            original = service._games
            def overlapping(*args, **kwargs):
                entered.wait(timeout=10)
                if not release.wait(timeout=10): raise RuntimeError('Test did not release workers')
                return original(*args, **kwargs)
            service._games = overlapping
            with patch('sports_analytics.backtesting_service.game_model', side_effect=lambda *args: PPPModel(config)), \
                    patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')):
                params = dict(sport='NFL', group='game-outcome', key='ppp')
                first = service.start(**params, limit=2)
                second = service.start(**params, limit=3)
                try:
                    # Both real run() calls must reach model evaluation before
                    # either is allowed to finish; a whole-run lock fails here.
                    entered.wait(timeout=10)
                    self.assertEqual({job['id'] for job in service.active_jobs()}, {first, second})
                    self.assertEqual(service.start(**params, limit=2), first)
                    failed = service.start(**params, entity='missing', limit=1)
                    deadline = time.monotonic()+5
                    while service.progress(failed)['state'] == 'running' and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(service.progress(failed)['state'], 'failed')
                    self.assertEqual({job['id'] for job in service.active_jobs()}, {first, second})
                finally:
                    release.set()
                deadline = time.monotonic()+15
                while service.active_jobs() and time.monotonic() < deadline: time.sleep(.01)
                self.assertEqual(service.active_jobs(), [])
                reference = BacktestingService(root=Path(directory)/'reference', now=service.now)
                reference.game_inputs = service.game_inputs
                results = []
                for job_id, limit in [(first, 2), (second, 3)]:
                    job = service.progress(job_id)
                    self.assertEqual(job['state'], 'completed', job['message'])
                    result = job['result']; results.append(result)
                    self.assertEqual(len(result['records']), limit)
                    report = Path(directory)/result['saved_directory']/'report.json'
                    self.assertEqual(json.loads(report.read_text(encoding='utf-8')), result)
                    self.assertEqual(service.run(**params, limit=limit), result)
                    sequential = reference.run(**params, limit=limit)
                    for field in ['records', 'skipped', 'metrics', 'config']:
                        self.assertEqual(result[field], sequential[field])
                self.assertNotEqual(results[0]['run_id'], results[1]['run_id'])

    def test_run_button_allows_another_job_while_polling_the_current_job(self):
        from sports_analytics.dashboard import create_app
        with tempfile.TemporaryDirectory() as directory:
            service = BacktestingService(root=directory)
            app = create_app(backtesting_service=service)
            callback = next(m['callback'].__wrapped__ for m in app.callback_map.values()
                            if m.get('callback') and m['callback'].__wrapped__.__name__ == 'run_backtest')
            context = dict(sport='NBA', group='game-outcome', model='ppp')
            controls = ['A', None, None, None, 10, 5, None, 'exclude', None]
            with patch.object(service, 'start', side_effect=['first', 'second']) as start, \
                    patch('sports_analytics.dashboard.ctx', SimpleNamespace(triggered_id='backtest-run')):
                first = callback(1, context, 0, *controls, None, [])
                self.assertFalse(first[6]); self.assertEqual(first[2], 'first')
                second = callback(2, context, 0, *controls, first[2], [])
                self.assertFalse(second[6]); self.assertEqual(second[2], 'second')
                self.assertEqual(start.call_count, 2)
            job = dict(state='running', percent=25, message='Evaluating',
                       parameters=dict(sport='NBA', group='game-outcome', key='ppp'))
            with patch.object(service, 'progress', return_value=job), \
                    patch('sports_analytics.dashboard.ctx', SimpleNamespace(triggered_id='backtest-poll')):
                polled = callback(2, context, 1, *controls, 'second', [])
                self.assertFalse(polled[6]); self.assertFalse(polled[3])
                self.assertEqual(polled[4], 25)
            with app.server.test_client() as client:
                with client.get('/assets/backtesting_monitor.css') as response:
                    self.assertIn('left: 0;', response.get_data(as_text=True))

    def test_object_array_keys_use_contents(self):
        first = np.array(['30.0', None], dtype=object)
        self.assertEqual(fingerprint(first), fingerprint(first.copy()))
        revised = first.copy(); revised[0] = '31.0'
        self.assertNotEqual(fingerprint(first), fingerprint(revised))

    def test_tail_matches_full_with_overlapping_and_empty_folds(self):
        frame = pd.DataFrame({'game_id': [str(i) for i in range(20)],
            'event_date': pd.date_range('2025-01-01', periods=20, tz='UTC'), 'fold': [i//2 for i in range(20)]})
        frame.loc[0, 'event_date'] = pd.Timestamp('2025-01-19T12:00:00Z')
        def evaluate(held):
            return [dict(game_id=r.game_id, date=r.event_date.isoformat())
                    for r in held.itertuples() if int(r.game_id) % 4 != 0]
        counted = Mock(side_effect=evaluate)
        self.assertEqual(calibration_tail(frame, 'fold', 5, counted), full_calibration(frame, 'fold', 5, evaluate))
        self.assertLess(counted.call_count, frame.fold.nunique())

    def test_cache_tracks_revisions_order_cutoffs_config_and_owns_results(self):
        model = SimpleNamespace(config=WHPConfig())
        frame = pd.DataFrame({'value': [1, 2], 'available_at': pd.date_range('2025-01-01', periods=2, tz='UTC')})
        calculate = Mock(return_value={'values': np.array([1., 2.])})
        def get(rows=frame, cutoff=1):
            return cached_calculation(model, 'test', (rows, cutoff), calculate)
        with replay_cache() as cache:
            first = get(); first['values'][0] = 900
            self.assertEqual(get()['values'][0], 1)
            calculate.return_value = {'values': np.array([1., 2.])}
            get(frame.iloc[::-1])
            revised = frame.copy(); revised.loc[0, 'value'] = 5
            get(revised); get(cutoff=2)
            delayed = frame.copy(); delayed.loc[0, 'available_at'] += pd.Timedelta(nanoseconds=1000)
            get(delayed)
            model.config = replace(model.config, window=15)
            get()
            self.assertEqual((cache.hits, calculate.call_count), (1, 6))
        with replay_cache(): get()
        self.assertEqual(calculate.call_count, 7)

    def test_concurrent_scopes_and_exception_cleanup(self):
        barrier, model = Barrier(2), SimpleNamespace(config=WHPConfig())
        def work(value):
            with replay_cache() as cache:
                barrier.wait()
                return cached_calculation(model, 'same', (), lambda: value), cache.hits
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(work, [10, 20])), [(10, 0), (20, 0)])
        with self.assertRaises(RuntimeError):
            with replay_cache(): raise RuntimeError('stop')
        with replay_cache() as cache:
            cached_calculation(model, 'same', (), lambda: 30)
            self.assertEqual(cache.hits, 0)

    def test_league_scalar_calibrations_do_not_thrash_the_fit_cache(self):
        model=SimpleNamespace(config=WHPConfig())
        calculate=Mock(return_value=1.5)
        with replay_cache() as cache:
            for _ in range(2):
                for player in range(30):
                    for fold in range(40):
                        cached_calculation(model,'calibration_point',(player,fold),calculate)
            self.assertEqual(calculate.call_count,1200)
            self.assertEqual(cache.hits,1200)
            self.assertEqual(len(cache.calculations),0)

    def test_game_models_match_full_calibration_with_rolling_windows(self):
        frame = team_history()
        from models.Game_Outcome.EPA.NBA_EPA import FEATURES
        epv_frame = frame.assign(additional_points=0., offensive_possessions=100., defensive_possessions=100.)
        for name in FEATURES:
            for prefix in ['start_', 'def_start_']:
                epv_frame[prefix+name] = float(name == 'intercept')
        ids = frame.game_id.drop_duplicates().tolist()
        xx = np.repeat((100*np.eye(len(FEATURES)))[None, :, :], len(ids), axis=0)
        xy = np.zeros((len(ids), len(FEATURES))); xy[:, 0] = 100
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')):
            for sport, key, module in [
                ('NFL', 'ppp', 'models.Game_Outcome.PPP.PPP'), ('NFL', 'epa', 'models.Game_Outcome.PPP.PPP'),
                ('NBA', 'ppp', 'models.Game_Outcome.PPP.NBA_PPP'), ('NCAAF', 'ppp', 'models.Game_Outcome.PPP.PPP'),
                ('NBA', 'epa', 'models.Game_Outcome.EPA.NBA_EPA'),
                ('NCAAF', 'epa', 'models.Game_Outcome.PPP.PPP'), ('NFL', 'elo', None),
                ('NBA', 'elo', None), ('NCAAF', 'elo', None)]:
                model = game_model(sport, key)
                model.config = replace(model.config, minimum_training_games=6, minimum_team_games=1,
                    minimum_calibration_games=4, maximum_calibration_games=10)
                inputs = []
                for lookback in [None, 12]:
                    for day in [20, 21]:
                        cutoff = pd.Timestamp('2025-08-01', tz='UTC')+pd.Timedelta(days=3*day)
                        source = epv_frame if (sport, key) == ('NBA', 'epa') else frame
                        history = estimation_history(source, cutoff, lookback, paired=True)
                        if (sport, key) == ('NBA', 'epa'):
                            history = SimpleNamespace(games=history, game_ids=ids, state_xx=xx, state_xy=xy, quality={})
                        inputs.extend((history, h, a, cutoff) for h, a in [('A', 'B'), ('C', 'D')])
                if module:
                    with patch(module+'.calibration_tail', side_effect=full_calibration):
                        expected = [asdict(model.predict(*args)) for args in inputs]
                else: expected = [asdict(model.predict(*args)) for args in inputs]
                with replay_cache() as cache:
                    actual = [asdict(model.predict(*args)) for args in inputs]
                self.assertEqual(actual, expected, (sport, key))
                self.assertGreater(cache.hits, 0, (sport, key))

    def test_whp_matches_pandas_reference_with_delayed_results_and_date_units(self):
        dates = pd.date_range('2025-01-01', periods=70, tz='UTC')
        frame = pd.DataFrame({'game_id': [str(i) for i in range(70)], 'event_date': dates,
            'available_at': dates+pd.Timedelta(hours=8), 'value': [10+i%9 for i in range(70)],
            'opportunity': [25+i%6 for i in range(70)]})
        frame.loc[frame.index % 7 == 0, 'available_at'] += pd.Timedelta(days=5)
        for method in ['calendar', 'exponential', 'linear', 'uniform']:
            model = WeightedHistoricalPerformance(WHPConfig(weighting=method, beta=.7))
            with replay_cache():
                for unit in ['us', 'ns']:
                    source = frame.copy()
                    for column in ['event_date', 'available_at']: source[column] = source[column].dt.as_unit(unit)
                    for length in [60, 65, 70]:
                        cutoff = dates[length-1]+pd.Timedelta(days=1)
                        history, records = eligible_history(source, cutoff), []
                        for _, game in history.tail(model.config.maximum_calibration).iterrows():
                            past = history[history.available_at.lt(game.event_date) & history.event_date.lt(game.event_date)]
                            if len(past) >= model.config.minimum_games:
                                point, _, _ = model._point(past, game.event_date)
                                records.append(dict(game_id=str(game.game_id), event_date=game.event_date.isoformat(),
                                    actual=float(game.value), predicted=point['adjusted_mean']))
                        actual = model.forecast(source, cutoff)
                        self.assertEqual(actual.diagnostics['calibration_records'], records)
                        errors = np.array([r['actual']-r['predicted'] for r in records])
                        self.assertEqual(actual.variance, max(.25, float(np.mean(errors**2))))

    def test_seasonal_sources_load_once_and_return_independent_copies(self):
        players = SimpleNamespace(data=Mock(), load=Mock(return_value=pd.DataFrame({'value': [1, 2]})))
        players.data.cached.return_value = SimpleNamespace(frame=pd.DataFrame({'value': [3, 4]}))
        repository = UsageHistoryRepository(player_repository=players)
        with replay_cache():
            first, _ = repository.load('NBA', '2025-12-01T00:00:00Z', ['A', 'B'])
            first.loc[0, 'value'] = 100
            second, _ = repository.load('NBA', '2026-01-01T00:00:00Z', ['A', 'C'])
            self.assertEqual(second.value.tolist(), [1, 2])
            # Three NBA source seasons, each with regular and postseason rows;
            # a second matchup in the same season reuses those six local reads.
            self.assertEqual((players.load.call_count, players.data.cached.call_count), (1, 6))
            self.assertEqual([(call.args[0].season, call.args[0].season_type)
                              for call in players.data.key.call_args_list],
                             [(year, phase) for year in (2023, 2024, 2025)
                              for phase in ('regular', 'postseason')])
            repository.load('NBA', '2026-10-01T00:00:00Z', ['A', 'C'])
            self.assertEqual(players.load.call_count, 2)

    def test_pace_batches_match_independent_refits(self):
        frame = team_history()
        rows = pd.DataFrame(dict(game_id=frame.game_id, team=frame.team, opponent=frame.opponent,
            event_date=frame.event_date, available_at=frame.available_at,
            volume=frame.points+60., allowed_volume=frame.points_allowed+60., extra=0.,
            margin=frame.points-frame.points_allowed))
        config = replace(PaceConfig(), minimum_league_games=4)
        prefixes = [rows[rows.event_date.lt(date)] for date in sorted(rows.event_date.unique())[-3:]]
        expected = [PaceModel(config).fit(prefix) for prefix in prefixes]
        with replay_cache() as cache: actual = [PaceModel(config).fit(prefix) for prefix in prefixes]
        self.assertGreater(cache.hits, 0)
        for before, after in zip(expected, actual):
            self.assertEqual((before.residuals, before.training), (after.residuals, after.training))
            np.testing.assert_array_equal(before.coefficients, after.coefficients)


if __name__ == '__main__': main()
