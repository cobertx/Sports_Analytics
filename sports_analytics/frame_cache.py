"""Byte-bounded decoded frames with per-payload single-flight coordination."""
from collections import OrderedDict, namedtuple
from concurrent.futures import Future
from threading import Lock


CacheInfo = namedtuple('CacheInfo', 'hits misses maxsize currsize bytes maxbytes')


class FrameCache:
    def __init__(self, decode, maxsize=16, maxbytes=128*1024*1024):
        self.decode=decode;self.maxsize=maxsize;self.maxbytes=maxbytes
        self.lock=Lock();self.values=OrderedDict();self.pending={}
        self.bytes=self.hits=self.misses=self.generation=0

    def __call__(self,payload):
        with self.lock:
            if payload in self.values:
                self.hits+=1;self.values.move_to_end(payload)
                return self.values[payload][0]
            future=self.pending.get(payload)
            owner=future is None
            if owner:
                future=Future();self.pending[payload]=future;self.misses+=1
            else:self.hits+=1
            generation=self.generation
        if not owner:return future.result()
        try:
            frame=self.decode(payload)
            weight=len(payload)+int(frame.memory_usage(index=True,deep=True).sum())
            with self.lock:
                if generation==self.generation and weight<=self.maxbytes:
                    self.values[payload]=(frame,weight);self.bytes+=weight
                    while len(self.values)>self.maxsize or self.bytes>self.maxbytes:
                        _,(_,size)=self.values.popitem(last=False);self.bytes-=size
            future.set_result(frame)
            return frame
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                if self.pending.get(payload) is future:self.pending.pop(payload)

    def cache_clear(self):
        with self.lock:
            self.generation+=1;self.values.clear();self.bytes=self.hits=self.misses=0

    def cache_info(self):
        with self.lock:
            return CacheInfo(self.hits,self.misses,self.maxsize,len(self.values),self.bytes,self.maxbytes)
