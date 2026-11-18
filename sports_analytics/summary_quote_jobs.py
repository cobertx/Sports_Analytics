"""Keep filesystem and provider waits outside the initial quote callback."""
from time import time
from collections import OrderedDict
from threading import RLock
from uuid import uuid4

from .page_cache import PageTasks


class SummaryQuoteJobs:
    def __init__(self,timeout=20,now=time):
        self.tasks=PageTasks(capacity=48,workers=3)
        self.timeout=timeout;self.now=now
        self.bundles=OrderedDict();self.lock=RLock()

    def wire(self,bundle):
        """The browser needs scope, counts and progress, not every raw contract."""
        reference=uuid4().hex
        with self.lock:
            self.bundles[reference]=bundle
            while len(self.bundles)>16:self.bundles.popitem(last=False)
        records={}
        fields={'sport','game_id','category','pulled_at','fetched_at','metadata_fetched_at','notices','updating','page_pending'}
        for provider,record in bundle['providers'].items():
            snapshot=record.get('snapshot')
            records[provider]=dict(game={'id':record['game']['id']} if record.get('game') else None,error=record.get('error'),
                snapshot={**{k:v for k,v in snapshot.items() if k in fields},'row_count':len(snapshot['rows'])} if snapshot else None)
        return dict(id=bundle['id'],quote_ref=reference,sport=bundle['sport'],game=bundle['game'],
                    providers=records,updating=bundle['updating'])

    def resolve(self,bundle):
        if not bundle or not bundle.get('quote_ref'):return bundle
        with self.lock:
            stored=self.bundles.get(bundle['quote_ref'])
            if stored and (stored['id'],stored['sport'],str(stored['game']['id']))==(
                    bundle.get('id'),bundle.get('sport'),str((bundle.get('game') or {}).get('id'))):
                self.bundles.move_to_end(bundle['quote_ref']);return stored
        return None

    def read(self,key,work,*,started_at,repeat=False):
        cached=self.tasks.peek(key)
        repeat=bool(repeat and cached and (cached[0] or {}).get('updating'))
        value,pending,error=self.tasks.read(key,work,refresh=repeat)
        if value is None and pending:
            # Let memory-only reads through, with a strict per-source budget.
            value=self.tasks.wait(key,.005)
            state=self.tasks.queue.state(key);pending,error=state['pending'],state['error']
        if self.now()-started_at>=self.timeout and (pending or value and (value[0] or {}).get('updating')):
            return value,False,'Loading took too long. Use the source pull button to retry; other sources remain available.'
        return value,pending,error
