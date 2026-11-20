"""Shared bounded forecast reuse and existing background queues for quick scans."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from hashlib import sha256
import json
from threading import Lock
from time import monotonic

from .page_cache import PageTasks


FORECASTS_FINISHED = 80
WAITING = {'percent':0, 'message':'Waiting for a calculation worker'}


def request_key(value):
    return sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


class QuickScanTasks(PageTasks):
    """Track completed stages alongside the existing bounded quick scan queue."""

    def __init__(self, capacity=16):
        super().__init__(capacity=capacity)
        self.progress_records = OrderedDict()

    def scan(self, key, work, *, refresh=False, force=False):
        with self.lock:
            if self.queue.pending(key):
                return self.peek(key), True, None
            started = monotonic()
            progress = dict(WAITING, started=started)

            def report(percent, message):
                with self.lock:
                    if percent >= progress['percent']:
                        progress.update(percent=percent,message=message)
                    if percent == FORECASTS_FINISHED:
                        progress['forecast_checkpoint'] = message

            result, pending, error = self.read(key, lambda:work(report), refresh=refresh, force=force)
            if pending:
                self.progress_records[key] = progress
                self.progress_records.move_to_end(key)
                while len(self.progress_records)>128:
                    old = next(k for k in self.progress_records if not self.queue.pending(k))
                    self.progress_records.pop(old)
            return result, pending, error

    def progress(self, key):
        with self.lock:
            progress = dict(self.progress_records.get(key, WAITING))
        started = progress.pop('started', None)
        if started is not None:
            progress['elapsed'] = int(monotonic() - started)
        return progress


def forecast_progress(report, completed, total):
    if report and total:
        report(15 + (FORECASTS_FINISHED - 15)*completed//total, f'{completed} of {total} forecast methods finished')


class FilterJobs:
    def __init__(self,capacity=16):
        self.tasks=QuickScanTasks(capacity=capacity)
        self.lock=Lock();self.forecasts=OrderedDict();self.pending={};self.capacity=capacity

    def forecast(self,key,calculate):
        with self.lock:
            if key in self.forecasts:
                self.forecasts.move_to_end(key);value=self.forecasts[key];future=None;owner=False
            else:
                value=None;future=self.pending.get(key);owner=future is None
                if owner:future=Future();self.pending[key]=future
        if future is None:return deepcopy(value)
        if not owner:return deepcopy(future.result())
        try:
            value=calculate()
            # Missing/failed estimates must remain retryable on the next explicit scan.
            failed=isinstance(value,tuple) and len(value)>1 and bool(value[1])
            retained=deepcopy(value) if not failed else None
            with self.lock:
                if not failed:
                    self.forecasts[key]=retained
                    while len(self.forecasts)>self.capacity:self.forecasts.popitem(last=False)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc);raise
        finally:
            with self.lock:self.pending.pop(key,None)
