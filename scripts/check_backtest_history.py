"""Offline NBA sample expansion, warm-up, source reuse and held-out EPA targets."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import patch
import json
import sys
import tempfile

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.backtest_history import NBAReplayHistory
from sports_analytics.backtesting_service import BacktestingService,eligible_pairs,evaluation_bounds,estimation_history
from sports_analytics.data import DataSourceError,Query
from sports_analytics.service import DataService
from sports_analytics.performance_summary import PerformanceSummaries,settings
from sports_analytics.backtesting_view import available_models,model_config
from check_backtesting import saved_game_report


def boxes(season,count=82):
    rows=[]
    for day in range(count):
        stamp=pd.Timestamp(f'{season}-10-01',tz='UTC')+pd.Timedelta(days=day)
        for index,(away,home) in enumerate([('GSW','LAL'),('CLE','ATL')]):
            for team,opponent,points in [(away,home,100),(home,away,110)]:
                rows.append(dict(game_id=f'002{season%100:02d}{1000*index+day+1:05d}',team=team,opponent=opponent,
                    matchup=f'{away} @ {home}',event_date=stamp.isoformat(),pts=points,fga=85,fta=20,oreb=10,tov=12))
    return pd.DataFrame(rows)


def evaluate(result,model,frame,dataset,games,progress):
    for pair in games:
        h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
        row=dict(saved_game_report()['records'][0])
        row.update(game_id=str(h.game_id),date=h.event_date.isoformat(),actual_margin=float(h.points-a.points),
                   actual_total=float(h.points+a.points),actual=float(h.points-a.points),predicted=4.,baseline=3.,
                   lower=-10.,upper=20.,interval_level=.8)
        result['records'].append(row)


class HistoryChecks(TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        # Production input discovery and signatures use the same isolated project root.
        signature=self.root/'sports_analytics/ncaaf_model_data.py';signature.parent.mkdir();signature.write_text('')
        root=patch('sports_analytics.backtesting_service.ROOT',self.root);root.start();self.addCleanup(root.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No live data requests in checks'))
        network.start();self.addCleanup(network.stop)
        self.source=patch('sports_analytics.service.fetch',side_effect=lambda query:boxes(query.season,2 if query.season==2026 else 82))
        self.fetch=self.source.start();self.addCleanup(self.source.stop)
        self.data=DataService(self.root/'data/cache/sports.sqlite3')
        self.backend=BacktestingService(root=self.root/'data/backtesting',data=self.data,
            now=lambda:pd.Timestamp('2026-10-30',tz='UTC'))
        self.history=NBAReplayHistory(self.backend)
        self.config=SimpleNamespace(minimum_training_games=30,maximum_calibration_games=30,minimum_team_games=2)
        self.lower,self.upper=evaluation_bounds(None,None,self.backend.now())
        self.history.box_season(2026,['regular']);self.fetch.reset_mock()

    def test_expands_to_400_team_games_and_earlier_warmup_then_reuses_local_files(self):
        self.data.load(Query('NBA',2024,'team','regular'));self.fetch.reset_mock()
        progress=[]
        self.history.ensure('elo','GSW',400,self.lower,self.upper,False,self.config,
                            lambda done,total,message:progress.append((done,total,message)))
        self.assertEqual({call.args[0].season for call in self.fetch.call_args_list},{2025,2023,2022,2021,2020})
        self.assertTrue(all(call.args[0].season_type=='regular' for call in self.fetch.call_args_list))
        frame,_=self.backend.game_inputs('NBA','elo')
        games=eligible_pairs(frame,'GSW',self.lower,self.upper,self.backend.now(),limit=400)
        self.assertEqual(len(games),400)
        self.assertTrue(all('GSW' in set(pair.team) and len(pair)==2 for pair in games))
        self.assertEqual(games[-1].event_date.min(),pd.Timestamp('2026-10-02',tz='UTC'))
        prior=estimation_history(frame,games[0].event_date.min(),paired=True)
        self.assertGreaterEqual(prior.game_id.nunique(),60)
        self.assertTrue(any('earlier training/calibration' in message for _,_,message in progress))
        self.fetch.reset_mock()
        self.history.ensure('elo','GSW',400,self.lower,self.upper,False,self.config)
        self.fetch.assert_not_called()

    def test_real_run_path_retains_full_training_and_saves_exact_400_targets(self):
        captured=[]
        def replay(result,model,frame,dataset,games,progress):
            captured.extend(games)
            self.assertGreater(frame.game_id.nunique(),400)
            self.assertLess(frame.event_date.min(),games[0].event_date.min())
            evaluate(result,model,frame,dataset,games,progress)
        config=dict(minimum_training_games=30,maximum_calibration_games=30,minimum_calibration_games=10,minimum_team_games=2)
        with patch.object(self.backend,'_games',side_effect=replay):
            result=self.backend.run('NBA','game-outcome','elo','GSW',limit=400,prepare_history=True,config_overrides=config)
        self.assertEqual((len(result['records']),result['attempted'],len(result['skipped'])),(400,400,0))
        self.assertEqual(result['context']['limit'],400)
        with patch.object(self.backend,'_games',side_effect=AssertionError('Reuse completed results')):
            loaded=self.backend.run('NBA','game-outcome','elo','GSW',limit=400,prepare_history=True,config_overrides=config)
        self.assertEqual(result,loaded)

    def test_download_failure_never_reports_short_sample_as_requested_400(self):
        self.fetch.side_effect=DataSourceError('Historical source unavailable')
        with self.assertRaisesRegex(DataSourceError,'requested 400 GSW games'):
            self.backend.run('NBA','game-outcome','elo','GSW',limit=400,prepare_history=True)
        self.assertFalse(list(self.backend.root.rglob('*.json')))

    def test_date_bounds_are_not_widened_to_manufacture_400_games(self):
        with self.assertRaisesRegex(DataSourceError,'Only 2 of the requested 400'):
            self.history.ensure('elo','GSW',400,pd.Timestamp('2026-10-01',tz='UTC'),self.upper,False,self.config)
        self.fetch.assert_not_called()

    def test_two_independent_source_loaders_share_one_preparation(self):
        entered=Event();release=Event()
        def fetch(query):
            entered.set()
            if not release.wait(3):raise AssertionError('Source was not released')
            return boxes(query.season)
        self.fetch.side_effect=fetch
        other=NBAReplayHistory(BacktestingService(data=DataService(self.data.path)))
        with ThreadPoolExecutor(max_workers=2) as workers:
            first=workers.submit(self.history.box_season,2025,['regular'])
            self.assertTrue(entered.wait(2))
            second=workers.submit(other.box_season,2025,['regular'])
            release.set();first.result(timeout=5);second.result(timeout=5)
        self.fetch.assert_called_once()
        frame,_=self.backend.game_inputs('NBA','elo')
        self.assertFalse(frame.duplicated(['game_id','team']).any())

    def test_current_season_updates_while_completed_seasons_keep_the_local_cache(self):
        self.fetch.side_effect=lambda query:boxes(query.season,3)
        with patch.object(self.data,'fresh',return_value=False):
            self.history.box_season(2026,['regular'],refresh_current=True)
        self.fetch.assert_called_once()
        frame,_=self.backend.game_inputs('NBA','elo')
        self.assertEqual(frame[frame.team.eq('GSW')].game_id.nunique(),3)

    def test_epa_evaluates_box_score_targets_without_using_their_possession_states(self):
        self.history.box_season(2025,['regular'])
        native,_=self.backend.game_inputs('NBA','ppp')
        training=native[native.event_date.dt.year.eq(2025)].copy()
        dataset=SimpleNamespace(games=training)
        def inputs(sport,key):return (training.copy(),dataset) if key=='epa' else (native.copy(),None)
        def replay(result,model,frame,states,games,progress):
            self.assertEqual(set(frame.game_id),set(training.game_id))
            self.assertTrue(all(not set(pair.game_id)&set(training.game_id) for pair in games))
            self.assertIs(states,dataset)
            evaluate(result,model,frame,states,games,progress)
        with patch.object(self.backend,'game_inputs',side_effect=inputs),patch.object(self.backend,'_games',side_effect=replay):
            result=self.backend.run('NBA','game-outcome','epa','GSW',limit=2)
            self.assertEqual(len(result['records']),2)
            self.assertEqual(self.backend.available_games('NBA','game-outcome','epa','GSW'),84)
            native['points']+=1;native['points_allowed']+=1
            revised=self.backend.run('NBA','game-outcome','epa','GSW',limit=2)
            self.assertNotEqual(result['run_id'],revised['run_id'])
            self.assertNotEqual(result['records'][-1]['actual_total'],revised['records'][-1]['actual_total'])

    def test_worker_path_enables_history_preparation_without_changing_offline_run_default(self):
        finished=Event();parameters=[]
        report=saved_game_report()
        def run(**values):parameters.append(values);finished.set();return report
        with patch.object(self.backend,'run',side_effect=run):
            self.backend.start(sport='NBA',group='game-outcome',key='elo',entity='GSW',limit=400)
            self.assertTrue(finished.wait(2))
        self.assertTrue(parameters[0]['prepare_history'])

    def test_performance_summary_does_not_reuse_old_short_team_reports(self):
        manager=PerformanceSummaries(self.backend,watch=False)
        context=dict(sport='NBA',group='game-outcome');request=settings(context,'margin','GSW',400)
        report=saved_game_report();report['context'].update(**request,entity_label='GSW',model='elo')
        model=next(m for m in available_models('NBA','game-outcome') if m.key=='elo')
        report['config']=model_config(model,'NBA')
        self.backend.ensure_exports(report)
        self.assertIsNone(manager.matching(context,model,request))
        with patch.object(self.backend,'start',return_value='waiting') as start:
            manifest=manager.ensure(context,request,'GSW')
        self.assertEqual(manifest['entries']['elo']['state'],'running')
        self.assertEqual(start.call_count,3)


if __name__=='__main__':main()
