"""Offline restart and per-run controls with actual durable replay records."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
from hashlib import sha256
import json
import subprocess
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.dashboard import create_app
from sports_analytics.backtest_checkpoint import BacktestJournal,ReplayCheckpoint
from sports_analytics.backtest_process import BacktestProcesses
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.data import Query
from sports_analytics.service import DataService
from scripts.check_backtest_concurrency import wait_for
from scripts.check_backtesting import team_history


WORKER = """
from pathlib import Path
import sys,time
from sports_analytics.backtest_checkpoint import ReplayCheckpoint
from sports_analytics.backtest_worker import main
folder=Path(sys.argv[1])
record=ReplayCheckpoint.record
def gated(self,ordinal,*args):
    record(self,ordinal,*args)
    with (folder/'ordinals.txt').open('a') as trace:trace.write(str(ordinal)+'\\n')
    if ordinal==3:
        (folder/'checkpoint-ready').touch()
        while not (folder/'release').exists():time.sleep(.05)
ReplayCheckpoint.record=gated
raise SystemExit(main())
"""

CONTROLLER = """
import subprocess,sys,time
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.service import DataService
folder=Path(sys.argv[1])
original=subprocess.Popen
with patch('sports_analytics.backtest_process.subprocess.Popen',
           side_effect=lambda command,**kw: original([sys.executable,'-u','-c',sys.argv[2],str(folder)],**kw)):
    backend=BacktestingService(folder/'reports',DataService(folder/'source.sqlite3'),
        now=lambda:pd.Timestamp('2026-02-01T00:00:00Z'),isolated=True)
    backend.start(sport='NBA',group='player-stats',key='whp',entity='player',market='player_points',limit=5)
    while backend.active_jobs():time.sleep(.05)
"""


class BacktestResumeChecks(TestCase):
    def setUp(self):
        temporary=TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.data=DataService(self.root/'source.sqlite3')
        dates=pd.date_range('2025-10-01',periods=25,freq='2D',tz='UTC')
        self.frame=pd.DataFrame(dict(game_id=[f'g{i}' for i in range(25)],
            event_date=dates,available_at=dates+pd.Timedelta(days=1),season=2025,
            season_type='regular',entity_type='player',entity_id='player',
            entity_name='Test Player',team='BOS',opponent='NYK',position='PG',
            pts=[10+i%12 for i in range(25)],min=34.))
        self.now=lambda:pd.Timestamp('2026-02-01T00:00:00Z')
        self.params=dict(sport='NBA',group='player-stats',key='whp',entity='player',market='player_points',limit=5)
        self.load(self.frame)

    def load(self,frame):
        with patch('sports_analytics.service.fetch',return_value=frame):
            self.data.load(Query('NBA',2025,'player'),refresh=True)

    def test_hard_shutdown_restarts_at_next_fold_with_original_inputs(self):
        reference=BacktestingService(self.root/'reference',self.data,now=self.now).run(**self.params)
        journal=BacktestJournal(self.root/'reports')
        coordinator=subprocess.Popen([sys.executable,'-u','-c',CONTROLLER,str(self.root),WORKER],
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            wait_for(lambda:(self.root/'checkpoint-ready').exists(),seconds=60)
            saved,=journal.active()
            self.assertEqual((saved['completed'],saved['percent'],saved['state']),(3,60,'running'))
            with journal.connect() as db:
                identity=db.execute('SELECT identity FROM replays WHERE job_id=?',(saved['id'],)).fetchone()['identity']
            original_id=sha256(identity.encode()).hexdigest()
        finally:
            coordinator.terminate()
            coordinator.wait(timeout=15)
            (self.root/'release').touch()
        self.load(self.frame.assign(pts=self.frame.pts+60))
        launch=subprocess.Popen
        with patch('sports_analytics.backtest_process.subprocess.Popen',
                   side_effect=lambda command,**kw:launch([sys.executable,'-u','-c',WORKER,str(self.root)],**kw)):
            recovered=BacktestingService(self.root/'reports',self.data,
                now=lambda:pd.Timestamp('2026-04-01T00:00:00Z'),isolated=True)
            wait_for(lambda:not recovered.active_jobs())
        state=recovered.progress(saved['id'])
        self.assertEqual(state['state'],'completed',state['message'])
        self.assertEqual((self.root/'ordinals.txt').read_text().splitlines(),['1','2','3','4','5'])
        for key in ('records','skipped','metrics','config'):
            self.assertEqual(reference[key],state['result'][key])
        self.assertEqual(state['result']['run_id'],original_id)
        self.assertEqual(recovered.progress(saved['id'])['parameters'],self.params)
        wait_for(lambda:not (journal.job_dir(saved['id'])/'inputs').exists(),seconds=5)

    def test_sidebar_pause_survives_restart_and_other_job_finishes(self):
        backend=BacktestingService(self.root/'reports',self.data,now=self.now)
        held=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        record=ReplayCheckpoint.record
        def gate(checkpoint,ordinal,result,*counts):
            record(checkpoint,ordinal,result,*counts)
            if ordinal==3 and result['config']['half_life']!=3:
                held.set()
                if not release.wait(30):raise RuntimeError('Test gate timed out')

        app=create_app(backtesting_service=backend)
        output='backtest-monitor-jobs.data'
        meta=app.callback_map[output]
        def action(job,verb):
            button=dict(type='backtest-control',job=job,action=verb)
            identifier=json.dumps(button,sort_keys=True,separators=(',',':'))
            values={'backtest-monitor-timer':0,'backtest-job':job,'backtest-monitor-jobs':None}
            def wire(spec):
                if spec['id'].startswith('{'):
                    return [dict(id=identifier,property=spec['property'],value=1 if spec['property']=='n_clicks' else button)]
                return dict(spec,value=values[spec['id']])
            response=app.server.test_client().post('/_dash-update-component',json=dict(
                output=output,outputs=dict(id='backtest-monitor-jobs',property='data'),
                inputs=[wire(spec) for spec in meta['inputs']],state=[wire(spec) for spec in meta['state']],
                changedPropIds=[identifier+'.n_clicks']))
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            return response.get_json()['response']['backtest-monitor-jobs']['data']

        with patch.object(ReplayCheckpoint,'record',gate):
            paused_id=backend.start(**self.params)
            self.assertTrue(held.wait(30))
            other_id=backend.start(**self.params,config_overrides={'half_life':3})
            jobs=action(paused_id,'pause')
            self.assertEqual(next(job['state'] for job in jobs if job['id']==paused_id),'pausing')
            release.set()
            wait_for(lambda:backend.progress(paused_id)['state']=='paused')
            wait_for(lambda:backend.progress(other_id)['state'] not in {'running','pausing'})
        self.assertEqual(backend.progress(other_id)['state'],'completed')
        reopened=BacktestingService(backend.root,self.data,now=self.now)
        self.assertEqual(reopened.progress(paused_id)['percent'],60)
        self.assertEqual(reopened.start(**self.params),paused_id)
        self.assertEqual(reopened.progress(paused_id)['state'],'paused')
        self.assertEqual(action(paused_id,'resume')[0]['state'],'running')
        wait_for(lambda:backend.progress(paused_id)['state'] not in {'running','pausing','paused'})
        self.assertEqual(backend.progress(paused_id)['state'],'completed')

    def test_game_replay_keeps_skipped_folds_and_resumes_in_original_order(self):
        from types import SimpleNamespace
        from models.Game_Outcome.PPP.PPP import PPPConfig,PPPForecast
        frame=team_history().assign(season_type='regular')
        params=dict(sport='NFL',group='game-outcome',key='ppp',limit=5)
        attempted=[]
        def predict(_,home,away,cutoff,neutral):
            attempted.append((home,cutoff))
            if home=='A':raise ValueError('Fixture has insufficient history')
            return PPPForecast(home,away,24,17,14,13)
        model=SimpleNamespace(config=PPPConfig(),predict=predict)
        backend=BacktestingService(self.root/'games',self.data,now=self.now)
        held=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        record=ReplayCheckpoint.record
        def gate(checkpoint,ordinal,result,*counts):
            record(checkpoint,ordinal,result,*counts)
            if ordinal==3:
                held.set()
                if not release.wait(30):raise RuntimeError('Test gate timed out')
        with patch('sports_analytics.backtesting_service.game_model',return_value=model), \
                patch.object(BacktestingService,'game_inputs',return_value=(frame,None)):
            reference=BacktestingService(self.root/'game-reference',self.data,now=self.now).run(**params)
            expected=attempted.copy()
            attempted.clear()
            with patch.object(ReplayCheckpoint,'record',gate):
                job=backend.start(**params)
                self.assertTrue(held.wait(30))
                self.assertTrue(backend.pause(job))
                release.set()
                wait_for(lambda:backend.progress(job)['state']=='paused')
            self.assertEqual(attempted,expected[:3])
            reopened=BacktestingService(backend.root,self.data,now=self.now)
            self.assertTrue(reopened.resume(job))
            wait_for(lambda:not reopened.active_jobs())
        state=reopened.progress(job)
        self.assertEqual(state['state'],'completed',state['message'])
        self.assertEqual(attempted,expected)
        for key in ('records','skipped','metrics'):
            self.assertEqual(state['result'][key],reference[key])

    def test_waiting_run_can_pause_before_acquiring_a_worker(self):
        backend=BacktestingService(self.root/'reports',self.data,now=self.now,isolated=True)
        backend._processes=BacktestProcesses(workers=1)
        slots=backend._processes.slots
        slots.acquire()
        try:
            with patch('sports_analytics.backtest_process.subprocess.Popen') as launch:
                job=backend.start(**self.params)
                wait_for(lambda:backend.progress(job)['message']=='Waiting for a backtest worker')
                self.assertTrue(backend.pause(job))
                wait_for(lambda:backend.progress(job)['state']=='paused')
                self.assertEqual(backend.progress(job)['percent'],0)
                launch.assert_not_called()
        finally:
            slots.release()


if __name__=='__main__':main()
