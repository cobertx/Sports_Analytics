"""Offline process isolation, simultaneous player replays and live filtering."""
from dataclasses import asdict
from contextlib import contextmanager
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sports_analytics.backtest_process import BacktestProcesses
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.data import Query
from sports_analytics.service import DataService
from sports_analytics.dashboard import create_app
from sports_analytics.mispricing import FILTER_CATEGORY, criteria_values, filter_context
from sports_analytics.whp_service import WHPMarketService


# Hold two real OS workers at a deterministic gate before executing the normal
# worker entry point. Busy Python work here would monopolize a shared interpreter.
GATED_WORKER = '''
import json, os, pathlib, sys, time
directory = pathlib.Path(sys.argv[1])
print(json.dumps(dict(type='progress', done=0, total=0, message='Test gate')), flush=True)
(directory / (str(os.getpid()) + '.ready')).write_text('ready')
deadline = time.monotonic() + 60
while not (directory / 'release').exists():
    if time.monotonic() > deadline: raise RuntimeError('Test gate timed out')
import requests
requests.sessions.Session.request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No HTTP'))
from sports_analytics.backtest_worker import main
raise SystemExit(main())
'''


def wait_for(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for worker progress')
        time.sleep(.02)


@contextmanager
def simulated_player_inputs():
    """Supply the same offline boxes to the actual UO/OAM/PGE engines."""
    from scripts.check_research import player_inputs
    players, teams = player_inputs()
    original = BacktestingService.player_service

    def player_service(self, key):
        service = original(self, key)
        service.repository.load = lambda *args: (players.copy(), teams.copy())
        return service

    with patch.object(BacktestingService, 'player_inputs', lambda *args: players.copy()), \
            patch.object(BacktestingService, 'game_inputs', lambda *args: (teams.copy(), None)), \
            patch.object(BacktestingService, 'phase_map', lambda *args: {}), \
            patch.object(BacktestingService, 'player_service', player_service), \
            patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')):
        yield


class ConcurrencyChecks(TestCase):
    def test_dashboard_enables_cpu_isolation(self):
        with patch('sports_analytics.dashboard.BacktestingService', wraps=BacktestingService) as backend:
            create_app()
        self.assertTrue(backend.call_args.kwargs['isolated'])

    def test_player_workers_overlap_with_quick_filter_and_preserve_exact_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dates = pd.date_range('2025-10-01', periods=40, freq='2D', tz='UTC')
            frame = pd.DataFrame(dict(game_id=[f'fixture-{i}' for i in range(40)],
                event_date=dates, available_at=dates + pd.Timedelta(days=1),
                season=2025, season_type='regular', entity_type='player',
                entity_id='concurrency-fixture', entity_name='Concurrency Player',
                team='BOS', opponent='DET', pts=[12+i%13 for i in range(40)], min=30.))
            data = DataService(root/'sports.sqlite3')
            with patch('sports_analytics.service.fetch', return_value=frame):
                data.load(Query('NBA', 2025, 'player'))
            service = BacktestingService(root=root/'reports', data=data, isolated=True,
                now=lambda: pd.Timestamp('2026-02-01', tz='UTC'))
            service._processes = BacktestProcesses(workers=2)
            params = dict(sport='NBA', group='player-stats', key='whp',
                entity='concurrency-fixture', market='player_points', limit=4)
            other = dict(params, config_overrides={'half_life': 3})
            popen = subprocess.Popen
            children = []

            def gated(command, **kwargs):
                self.assertEqual(command[-1], 'sports_analytics.backtest_worker')
                for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
                    self.assertEqual(kwargs['env'][name], '1')
                child = popen([sys.executable, '-u', '-c', GATED_WORKER, str(root)], **kwargs)
                children.append(child)
                return child

            snapshot = dict(sport='NBA', game_id='123', home_team='Boston Celtics',
                away_team='Detroit Pistons', pulled_at='2026-01-30T12:00:00Z',
                commence_time='2026-01-31T12:00:00Z', rows=[dict(group='Points',
                    market='Points', market_key='player_points', subject='Concurrency Player',
                    selection='Over', line=15.5, odds=100)])
            live = WHPMarketService(SimpleNamespace(root=root/'live', load=lambda *args: frame.copy()))
            markets = Mock()
            markets.history.return_value = [snapshot]
            application = create_app(service=data, backtesting_service=service,
                nba_market_service=markets, whp_service=live)
            callback = next(m['callback'].__wrapped__ for m in application.callback_map.values()
                if m.get('callback') and m['callback'].__wrapped__.__name__ == 'quick_filter_markets')
            request = filter_context('#nba-game-123', snapshot['pulled_at'], ['PPP'], ['WHP'], criteria_values(0, -100))
            first_live = asdict(live.forecast(snapshot))
            with patch('sports_analytics.backtest_process.subprocess.Popen', side_effect=gated), \
                    patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')):
                first = service.start(**params)
                second = service.start(**other)
                try:
                    wait_for(lambda: len(list(root.glob('*.ready'))) == 2)
                    self.assertEqual(len({child.pid for child in children}), 2)
                    self.assertNotIn(os.getpid(), {child.pid for child in children})
                    self.assertTrue(all(child.poll() is None for child in children))
                    self.assertEqual(service.start(**params), first)
                    failed = service.start(**dict(params, key='invalid'))
                    wait_for(lambda: 'Waiting' in service.progress(failed)['message'])
                    self.assertEqual(len(children), 2)  # bounded worker capacity
                    # Exercise the actual FanDuel callback while both workers
                    # are busy. Its dispatch is also shared by Kalshi/Polymarket.
                    rendered, history = callback(request, FILTER_CATEGORY, '#nba-game-123',
                        snapshot['pulled_at'], ['PPP'], ['WHP'], 0, -100, None)
                    self.assertIn('Top 3 by edge', str(rendered))
                    self.assertIn('WHP', history['player'])
                    self.assertEqual(asdict(live.forecast(snapshot)), first_live)
                    self.assertTrue(all(service.progress(job)['state'] == 'running' for job in (first, second)))
                finally:
                    (root/'release').touch()
                    wait_for(lambda: not service.active_jobs())
            self.assertEqual(service.progress(failed)['state'], 'failed')
            self.assertIn('available player model', service.progress(failed)['message'])
            reference = BacktestingService(root=root/'reference', data=data, now=service.now)
            for job, parameters in ((first, params), (second, other)):
                state = service.progress(job)
                self.assertEqual(state['state'], 'completed', state['message'])
                result = state['result']
                sequential = reference.run(**parameters)
                for field in ('records', 'skipped', 'metrics', 'config', 'run_id'):
                    self.assertEqual(result[field], sequential[field])
                self.assertEqual(service.saved('NBA', 'player-stats', 'whp', result['run_id']), result)
                report = root/'reports'/result['saved_directory']/'report.json'
                self.assertEqual(json.loads(report.read_text(encoding='utf-8')), result)
            self.assertNotEqual(service.progress(first)['result']['run_id'], service.progress(second)['result']['run_id'])
            self.assertTrue(all(child.poll() is not None for child in children))
            # Also use the unmodified production command, and reuse the report
            # through the child/parent protocol without recalculating it.
            reused = service.start(**params)
            wait_for(lambda: not service.active_jobs())
            self.assertEqual(service.progress(reused)['state'], 'completed')
            self.assertTrue(service.progress(reused)['reused'])
            self.assertEqual(service.progress(reused)['result'], service.progress(first)['result'])

    def test_uo_oam_pge_process_results_match_sequential_with_unchanged_seeds(self):
        fixture_worker = '''
from scripts.check_backtest_concurrency import simulated_player_inputs
from sports_analytics.backtest_worker import main
with simulated_player_inputs():
    raise SystemExit(main())
'''
        with tempfile.TemporaryDirectory() as directory, simulated_player_inputs():
            root = Path(directory)
            backend = BacktestingService(root=root/'reports', isolated=True,
                data=DataService(root/'sports.sqlite3'), now=lambda: pd.Timestamp('2026-01-01', tz='UTC'))
            backend._processes = BacktestProcesses(workers=3)
            original = subprocess.Popen
            children = []

            def launch(command, **kwargs):
                child = original([sys.executable, '-u', '-c', fixture_worker], **kwargs)
                children.append(child)
                return child

            parameters = {}
            for key in ('uo', 'oam', 'pge'):
                baseline = dict(simulations=1000, seed=17, role_window=8, team_window=12,
                    efficiency_window=12, minimum_games=3)
                overrides = baseline if key == 'uo' else dict(baseline=baseline,
                    league_window=16, minimum_league_games=4)
                if key == 'oam':
                    overrides.update(defense_window=8, minimum_defense_games=2)
                parameters[key] = dict(sport='NBA', group='player-stats', key=key,
                    entity='Player A', market='player_points', limit=1, config_overrides=overrides)
            with patch('sports_analytics.backtest_process.subprocess.Popen', side_effect=launch):
                jobs = {key: backend.start(**params) for key, params in parameters.items()}
                wait_for(lambda: not backend.active_jobs(), seconds=90)
            self.assertEqual(len({child.pid for child in children}), 3)
            reference = BacktestingService(root=root/'reference', data=backend.data, now=backend.now)
            for key, job in jobs.items():
                state = backend.progress(job)
                self.assertEqual(state['state'], 'completed', (key, state['message']))
                sequential = reference.run(**parameters[key])
                for field in ('records', 'skipped', 'metrics', 'config', 'run_id'):
                    self.assertEqual(state['result'][field], sequential[field], (key, field))
                self.assertTrue(list((root/'reports/model_cache'/key/state['result']['run_id']).rglob('*.json')))
            self.assertTrue(all(child.poll() is not None for child in children))


if __name__ == '__main__':
    main()
