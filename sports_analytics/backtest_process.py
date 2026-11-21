"""CPU-isolated dashboard replays, without importing or starting the web app."""
import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore, Lock
from .backtest_checkpoint import BacktestInterrupted, BacktestJournal, BacktestPaused


_children = set()
_children_lock = Lock()
_stopping = False


def _stop_children():
    global _stopping
    with _children_lock:
        _stopping = True
        for child in _children:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass


atexit.register(_stop_children)


def shutting_down():
    return _stopping


class BacktestProcesses:
    def __init__(self, workers=None):
        # Keep capacity for HTTP requests and live-market forecasts. At least
        # two replays can advance together, even on a small machine.
        self.capacity = workers or min(4, max(2, (os.cpu_count() or 2) - 1))
        self.slots = BoundedSemaphore(self.capacity)

    @contextmanager
    def _slot(self, root, job_id):
        journal=BacktestJournal(root) if job_id else None
        def check():
            if shutting_down():raise BacktestInterrupted('The backtest server is shutting down.')
            if journal and journal.read(job_id)['state']=='paused':raise BacktestPaused()
        while not self.slots.acquire(timeout=.25):
            check()
        try:
            check()
            yield
        finally:
            self.slots.release()

    def run(self, root, data, now, parameters, progress, *, job_id=None):
        progress(0, 0, 'Waiting for a backtest worker')
        with self._slot(root,job_id):
            progress(0, 0, 'Starting backtest worker')
            request = dict(root=str(Path(root).resolve()), cache=str(data.path.resolve()),
                           now=now.isoformat(), parameters=parameters, job_id=job_id)
            env = os.environ.copy()
            # Limit only the child, before NumPy/BLAS is imported. Concurrent
            # fits must not each launch a machine-sized native thread pool.
            for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                         'BLIS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                env[name] = '1'
            with tempfile.TemporaryFile() as errors:
                with _children_lock:
                    if _stopping:
                        raise BacktestInterrupted('The backtest server is shutting down.')
                    child = subprocess.Popen([sys.executable, '-u', '-m', 'sports_analytics.backtest_worker'],
                        cwd=Path(__file__).resolve().parents[1], env=env,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                        text=True, encoding='utf-8',
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    _children.add(child)
                try:
                    child.stdin.write(json.dumps(request) + '\n')
                    child.stdin.close()
                    result = None
                    error = None
                    for line in child.stdout:
                        message = json.loads(line)
                        if message['type'] == 'progress':
                            progress(message['done'], message['total'], message['message'])
                        elif message['type'] == 'result':
                            result = message['result']
                        elif message['type'] == 'error':
                            error = message['message']
                        elif message['type'] == 'paused':
                            raise BacktestPaused()
                    code = child.wait()
                    if code or result is None:
                        errors.seek(0, 2)
                        errors.seek(max(0, errors.tell() - 2000))
                        detail = errors.read().decode('utf-8', errors='replace').strip()
                        if error:raise RuntimeError(error)
                        raise BacktestInterrupted(detail or f'Backtest worker exited ({code}).')
                    return result
                finally:
                    if child.poll() is None:
                        child.terminate()
                    child.wait()
                    child.stdout.close()
                    if not child.stdin.closed:
                        child.stdin.close()
                    with _children_lock:
                        _children.discard(child)
