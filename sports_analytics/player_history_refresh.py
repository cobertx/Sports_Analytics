"""Refresh public box scores in forecast workers, keeping historical replays offline."""
from datetime import datetime, timezone
from threading import Lock

import pandas as pd

from .data import DataSourceError, Query
from .service import DataService
from .whp_service import WHPMarketService

_guard = Lock()
_locks = {}


def refresh_live_history(snapshot, model_service, *, now=None):
    """Require fresh source caches before calculating a current player forecast.

    Only the active season is refreshed. Older/saved snapshots keep their
    original cutoff and never trigger downloads. Test/custom repositories retain
    control of their own inputs. No odds or paid data endpoints are involved.
    """
    sport = snapshot['sport']
    if sport not in {'NBA', 'NFL'}:
        return
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    start, pulled = (pd.Timestamp(snapshot[key]) for key in ('commence_time', 'pulled_at'))
    if start <= now or not 0 <= (now-pulled).total_seconds() <= 1800:
        return
    repository = getattr(model_service, 'repository', None)
    data = getattr(repository, 'data', None)
    if not isinstance(data, DataService):
        return
    season = now.year - int(now.month < (10 if sport == 'NBA' else 3))
    phase = 'postseason' if (4 <= now.month <= 6 if sport == 'NBA' else now.month <= 2) else 'regular'
    kinds = ['player'] if isinstance(model_service, WHPMarketService) else ['player', 'team']
    for kind in kinds:
        query = Query(sport, season, kind, phase)
        key = (str(data.path.resolve()), data.key(query))
        with _guard:
            lock = _locks.setdefault(key, Lock())
        with lock:
            result = data.load(query)
        if result.status == 'stale cache':
            raise DataSourceError(
                f'{sport} {kind} history could not be refreshed (last source refresh {result.fetched_at}). '
                'Current player probabilities are unavailable until recent box scores can be loaded. '
                + result.notice)
