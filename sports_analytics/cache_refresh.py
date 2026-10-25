"""Bounded, deduplicated refreshes while the UI reads timestamped local data."""
from collections import OrderedDict
from queue import Queue
from threading import Lock,Thread
from time import monotonic


class RefreshQueue:
    def __init__(self,workers=2,retry_seconds=30):
        self.lock=Lock();self.jobs=OrderedDict();self.retry_seconds=retry_seconds
        self.workers=workers;self.tasks=Queue(maxsize=32);self.threads=[];self.closed=False

    def request(self,key,work,*,force=False):
        with self.lock:
            prior=self.jobs.get(key)
            if prior and (prior['pending'] or (not force and monotonic()<prior['retry_at'])):return dict(prior)
            if self.closed:return dict(pending=False,error='Automatic updates stopped.',retry_at=0)
            if sum(job['pending'] for job in self.jobs.values())>=32:
                return dict(pending=False,error='Updates are busy; retry shortly.',retry_at=0)
            job=dict(pending=True,error=None,retry_at=0)
            self.jobs[key]=job;self.jobs.move_to_end(key)
            while len(self.jobs)>128:
                old=next((k for k,v in self.jobs.items() if not v['pending']),None)
                if old is None:break
                self.jobs.pop(old)
            if not self.threads:
                for _ in range(self.workers):
                    thread=Thread(target=self._run,name='cache-refresh',daemon=True)
                    self.threads.append(thread);thread.start()
            self.tasks.put_nowait((job,work))
        return dict(job)

    def _run(self):
        while True:
            task=self.tasks.get()
            if task is None:
                self.tasks.task_done();return
            job,work=task
            error=None
            try:work()
            except Exception as exc:error=str(exc) or 'Refresh unavailable.'
            with self.lock:job.update(pending=False,error=error,retry_at=monotonic()+self.retry_seconds if error else 0)
            self.tasks.task_done()

    def pending(self,key):
        return self.state(key)['pending']

    def state(self,key):
        with self.lock:return dict(self.jobs.get(key,dict(pending=False,error=None,retry_at=0)))

    def shutdown(self,wait=True):
        with self.lock:
            if self.closed:return
            self.closed=True
        if wait:self.tasks.join()
        for _ in self.threads:self.tasks.put(None)
        if wait:
            for thread in self.threads:thread.join()
