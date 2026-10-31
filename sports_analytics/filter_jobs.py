"""Shared bounded forecast reuse and existing background queues for quick scans."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from hashlib import sha256
import json
from threading import Lock

from .page_cache import PageTasks


def request_key(value):
    return sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


class FilterJobs:
    def __init__(self,capacity=16):
        self.tasks=PageTasks(capacity=capacity)
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
