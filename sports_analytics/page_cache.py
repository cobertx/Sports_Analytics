"""Bounded page work and timestamped exchange views outside navigation callbacks."""
from collections import OrderedDict
from datetime import datetime,timezone
from hashlib import sha256
import json
from os import walk
from pathlib import Path
from threading import Condition,RLock

from .cache_refresh import RefreshQueue
from .data import DataSourceError
from .kalshi import parse_time
from .nfl_model_data import atomic_text


def forecast_revision(service,sport):
    """Cheap source revisions; generated forecasts/audits are not inputs."""
    files=set();seen=set()
    def inspect(source):
        if id(source) in seen:return
        seen.add(id(source));fields=getattr(source,'__dict__',{})
        for key in ('path','schedule_path'):
            value=fields.get(key)
            if isinstance(value,(str,Path)):
                path=Path(value);files.update([path,Path(str(path)+'-wal')])
        for key in ('root','directory','college_directory'):
            value=fields.get(key)
            if not isinstance(value,(str,Path)):continue
            root=Path(value)
            files.update(root.glob('*.csv'));files.update(root.glob('*.manifest.json'))
            files.add(root/'imports'/f'{sport.lower()}.csv')
            files.update((root/'ncaaf').glob('*.json'))
            if sport=='NCAAF' and key!='root':
                for folder,children,names in walk(root):
                    children[:]=[name for name in children if name not in {'forecasts','evaluations','calibration','model_cache'}]
                    files.update(Path(folder)/name for name in names if Path(name).suffix in {'.csv','.json'})
        for key in ('data','repository','players'):
            child=fields.get(key)
            if child is not None:inspect(child)
    inspect(service)
    revision=[]
    for path in sorted(files):
        try:
            stat=path.stat()
            revision.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino))
        except OSError:pass
    return revision


class PageTasks:
    def __init__(self, capacity=4):
        self.queue=RefreshQueue(workers=2)
        self.lock=RLock();self.values=OrderedDict();self.generations={};self.capacity=capacity
        self.ready=Condition(self.lock)
        self.publish_locks={}

    def peek(self,key):
        with self.lock:return self.values.get(key)

    def _put(self,key,value):
        self.values[key]=value;self.values.move_to_end(key)
        while len(self.values)>self.capacity:self.values.popitem(last=False)
        self.ready.notify_all()

    def wait(self,key,timeout):
        with self.ready:
            self.ready.wait_for(lambda:key in self.values,timeout)
            return self.values.get(key)

    def put(self,key,value,publish=None):
        with self.lock:
            # A manual pull takes precedence over an earlier background read.
            self.generations[key]=self.generations.get(key,0)+1
            generation=self.generations[key]
            self._put(key,value)
        self._publish(key,value,generation,publish)

    def _publish(self,key,value,generation,publish):
        if publish is None:return
        with self.lock:guard=self.publish_locks.setdefault(key,RLock())
        # File writes never hold the memory-cache lock used by navigation.
        with guard:
            with self.lock:current=self.generations.get(key)==generation
            if current:publish(value)

    def read(self,key,work,*,refresh=False,publish=None,force=False):
        with self.lock:
            state=self.queue.state(key)
            if not state['pending'] and (refresh or key not in self.values):
                generation=self.generations.get(key,0)+1
                self.generations[key]=generation
                def update():
                    value=work()
                    with self.lock:
                        if self.generations.get(key)==generation:
                            self._put(key,value)
                    self._publish(key,value,generation,publish)
                state=self.queue.request(key,update,force=force)
            value=self.values.get(key)
            return value,state['pending'],state['error']


class QuotePages:
    def __init__(self,service,directory=None,now=None):
        self.service=service
        self.directory=Path(directory or service.directory/'page_views')
        self.now=now or (lambda:datetime.now(timezone.utc))
        self.tasks=PageTasks(capacity=8)

    @staticmethod
    def key(game,category):
        return sha256(json.dumps([1,game,category],sort_keys=True).encode()).hexdigest()

    def publish(self,key,record):
        try:atomic_text(self.directory/(key+'.json'),json.dumps(record,allow_nan=False))
        except OSError:
            record['snapshot'].setdefault('notices',[]).append('The local page snapshot could not be saved.')

    def remember(self,game,category,snapshot):
        key=self.key(game,category)
        self.tasks.put(key,dict(snapshot=snapshot,checked_at=self.now().isoformat()),lambda r:self.publish(key,r))

    def read(self,game,category):
        key=self.key(game,category);saved=self.tasks.peek(key)
        if saved is None:
            try:
                saved=json.loads((self.directory/(key+'.json')).read_text(encoding='utf-8'))
                snapshot=saved['snapshot']
                if (snapshot['sport'],snapshot['game_id'],snapshot['category'])!=(game['sport'],game['id'],category):
                    saved=None
                if saved is not None:self.tasks.put(key,saved)
            except (OSError,ValueError,KeyError,TypeError):saved=None
        checked=parse_time(saved.get('checked_at')) if saved else None
        refresh=not checked or not 0 <= (self.now()-checked).total_seconds()<2 or bool(saved['snapshot'].get('updating'))
        def load():
            snapshot=(self.service.all_quotes(game) if category=='All Markets' else self.service.quotes(game,category))
            if (snapshot['sport'],snapshot['game_id'],snapshot['category'])!=(game['sport'],game['id'],category):
                raise DataSourceError('The provider returned quotes for another game or category.')
            return dict(snapshot=snapshot,checked_at=self.now().isoformat())
        record,pending,error=self.tasks.read(key,load,refresh=refresh,publish=lambda r:self.publish(key,r))
        if record is None and pending:
            # An already-cached raw response often assembles in a few ms. Allow
            # that fast result through without waiting for a slow API request.
            record=self.tasks.wait(key,.025)
            state=self.tasks.queue.state(key);pending,error=state['pending'],state['error']
        record=record or saved
        if not record and error:raise DataSourceError(error)
        snapshot=dict(record['snapshot']) if record else dict(sport=game['sport'],game_id=game['id'],category=category,
            rows=[],fetched_at=None,notices=[])
        snapshot['notices']=list(snapshot.get('notices',[]))
        snapshot['rows']=[dict(row) for row in snapshot['rows']]
        if pending:
            snapshot['notices'].append('Loading current quotes in the background; any displayed prices retain their original timestamp.')
        if error:snapshot['notices'].append('Showing saved quotes. '+error)
        snapshot['updating']=pending or (bool(snapshot.get('updating')) and not error)
        snapshot['page_pending']=snapshot['updating']
        return snapshot
