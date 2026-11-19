"""One refresh policy for active-season analysis inputs, independent of odds."""
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
import os
from time import sleep

ACTIVE_REFRESH_SECONDS = 300


def active_season(sport, now=None):
    now = now or datetime.now(timezone.utc)
    return now.year - int(now.month < (10 if sport == 'NBA' else 3))


def active_phases(sport, now=None):
    now = now or datetime.now(timezone.utc)
    playoffs = 4 <= now.month <= 6 if sport == 'NBA' else now.month <= 2
    return ('regular', 'postseason') if playoffs else ('regular',)


def source_fresh(path, sport, season, *, fetched_at=None, now=None):
    """Completed seasons are immutable unless explicitly refreshed by the user."""
    path = Path(path)
    if not path.is_file():
        return False
    now = now or datetime.now(timezone.utc)
    if season < active_season(sport, now):
        return True
    try:
        stamp = (datetime.fromisoformat(fetched_at.replace('Z', '+00:00')) if fetched_at else
                 datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        return 0 <= (now-stamp).total_seconds() < ACTIVE_REFRESH_SECONDS
    except (OSError, ValueError, TypeError):
        return False


def retain_first_availability(frame, previous, keys):
    """Rechecking the same completed result must not move its first-seen time."""
    import pandas as pd
    if (previous is None or 'available_at' not in frame or 'available_at' not in previous or
            not set(keys)<=set(previous) or not set(keys)<=set(frame)):return frame
    old=previous.drop_duplicates(keys).set_index(keys).available_at
    index=(pd.MultiIndex.from_frame(frame[keys]) if len(keys)>1 else pd.Index(frame[keys[0]]))
    known=pd.Series(pd.to_datetime(old.reindex(index),utc=True,errors='coerce',format='mixed').to_numpy(),index=frame.index)
    current=pd.to_datetime(frame.available_at,utc=True,errors='coerce',format='mixed')
    frame['available_at']=current.where(known.isna()|current.le(known),known)
    return frame


@contextmanager
def update_lease(path, *, wait=False, stop=None):
    """One refresh cycle across the dashboard and unattended Windows worker."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError as exc:
                # Lock contention is expected. Filesystem failures must still surface.
                windows_lock = os.name=='nt' and (exc.errno in {13,36} or getattr(exc,'winerror',None) in {32,33})
                if not isinstance(exc, BlockingIOError) and not windows_lock:
                    raise
                if not wait or (stop is not None and stop.wait(.25)):
                    yield False
                    return
                if stop is None:sleep(.25)
        try:yield True
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)
