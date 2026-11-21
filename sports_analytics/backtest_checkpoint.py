"""Durable job controls and an ordered, transactional replay journal."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import sqlite3


ACTIVE_STATES = {'running', 'pausing', 'paused'}


class BacktestPaused(Exception):
    """The worker reached a safe stopping point."""


class BacktestInterrupted(Exception):
    """The worker exited without publishing a terminal result."""


@contextmanager
def job_lock(path, *, wait=False):
    """A lock released by the OS even when the owner is killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                if wait:
                    from time import sleep
                    while True:
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            if exc.errno not in {errno.EACCES, errno.EDEADLK}:raise
                            sleep(.1)
                else:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:raise
            yield False
        else:
            # Closing the file releases the lock on both platforms.
            yield True


class BacktestJournal:
    def __init__(self, root):
        self.directory = Path(root) / 'checkpoints'
        self.path = self.directory / 'jobs.sqlite3'

    @contextmanager
    def connect(self, *, create=False):
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            with db:
                if create:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS jobs (
                            id TEXT PRIMARY KEY, signature TEXT NOT NULL,
                            parameters TEXT NOT NULL, display_label TEXT NOT NULL,
                            as_of TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
                            completed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                            run_id TEXT, reused INTEGER NOT NULL DEFAULT 0
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS active_signature ON jobs(signature)
                            WHERE state IN ('running','paused');
                        CREATE TABLE IF NOT EXISTS replays (
                            job_id TEXT PRIMARY KEY, identity TEXT NOT NULL, created_at TEXT NOT NULL
                        );
                        CREATE TABLE IF NOT EXISTS folds (
                            job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, skipped INTEGER NOT NULL,
                            payload TEXT NOT NULL, PRIMARY KEY(job_id,ordinal)
                        );
                    """)
                yield db

    def job_dir(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid backtest job ID.')
        return self.directory / job_id

    def create(self, job_id, signature, parameters, label, as_of):
        with self.connect(create=True) as db:
            db.execute("""INSERT OR IGNORE INTO jobs(id,signature,parameters,display_label,as_of,state,message)
                       VALUES (?,?,?,?,?,'running','Waiting to start')""",
                       (job_id,signature,json.dumps(parameters),label,as_of))
            return db.execute("SELECT id FROM jobs WHERE signature=? AND state IN ('running','paused')",
                              (signature,)).fetchone()['id']

    @staticmethod
    def job(row):
        result=dict(row)
        result['parameters']=json.loads(result['parameters'])
        result['reused']=bool(result['reused'])
        done,total=result['completed'],result['total']
        result['percent']=100 if result['state']=='completed' else min(99,int(100*done/total)) if total else 0
        return result

    def active(self):
        if not self.path.exists():return []
        with self.connect() as db:
            return [self.job(row) for row in db.execute(
                "SELECT * FROM jobs WHERE state IN ('running','paused') ORDER BY rowid")]

    def read(self, job_id):
        if not self.path.exists() or job_id is None:return None
        with self.connect() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            return self.job(row) if row else None

    def set_state(self, job_id, expected, state, message):
        with self.connect() as db:
            return bool(db.execute('UPDATE jobs SET state=?,message=? WHERE id=? AND state=?',
                                   (state,message,job_id,expected)).rowcount)

    def finish(self, job_id, state, message, *, run_id=None, reused=False):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,message=?,run_id=?,reused=? WHERE id=?',
                       (state,message,run_id,int(reused),job_id))
            if state=='completed':
                db.execute('DELETE FROM folds WHERE job_id=?',(job_id,))
                db.execute('DELETE FROM replays WHERE job_id=?',(job_id,))


class ReplayCheckpoint:
    def __init__(self, db, job_id):
        self.db=db
        self.job_id=job_id

    def check_running(self):
        state=self.db.execute('SELECT state FROM jobs WHERE id=?',(self.job_id,)).fetchone()['state']
        if state=='paused':raise BacktestPaused()

    def restore(self, identity, result, total):
        key=json.dumps(identity,sort_keys=True,allow_nan=False)
        with self.db:
            replay=self.db.execute('SELECT identity,created_at FROM replays WHERE job_id=?',(self.job_id,)).fetchone()
            if replay:
                if replay['identity']!=key:
                    raise ValueError('This checkpoint uses different data or model code. Start a new backtest with the current version.')
                result['created_at']=replay['created_at']
            else:
                self.db.execute('INSERT INTO replays VALUES (?,?,?)',(self.job_id,key,result['created_at']))
                self.db.execute('UPDATE jobs SET total=? WHERE id=?',(total,self.job_id))
        done=0
        for row in self.db.execute('SELECT * FROM folds WHERE job_id=? ORDER BY ordinal',(self.job_id,)):
            done+=1
            if row['ordinal']!=done or done>total:
                raise ValueError('This backtest checkpoint is incomplete or damaged.')
            result['skipped' if row['skipped'] else 'records'].append(json.loads(row['payload']))
        return done

    def record(self, number, result, previous_records, previous_skips):
        if len(result['records'])==previous_records+1 and len(result['skipped'])==previous_skips:
            skipped,payload=False,result['records'][-1]
        elif len(result['skipped'])==previous_skips+1 and len(result['records'])==previous_records:
            skipped,payload=True,result['skipped'][-1]
        else:
            raise RuntimeError('A replay step must produce one prediction or one skip.')
        with self.db:
            self.db.execute('INSERT INTO folds VALUES (?,?,?,?)',
                            (self.job_id,number,int(skipped),json.dumps(payload,allow_nan=False)))
            self.db.execute('UPDATE jobs SET completed=? WHERE id=?',(number,self.job_id))
