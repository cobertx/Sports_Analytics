"""Bounded, run-local reuse of deterministic historical calculations.

Keys include the complete ordered input contents and model configuration, not
just dates or row counts. Rolling windows and revised observations therefore
cannot accidentally reuse a fit from a different information set. ContextVar
keeps concurrent jobs separate; nothing survives the replay that created it.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import fields, is_dataclass
from functools import wraps
from hashlib import sha256

import numpy as np
import pandas as pd

_active = ContextVar('model_replay_cache', default=None)


def fingerprint(value):
    """Content key retaining column types, order, index and subsecond times."""
    if isinstance(value, pd.DataFrame):
        schema = (tuple(value.columns), tuple(map(repr, value.dtypes)),
                  repr(value.index.dtype), tuple(value.index.names))
        return ('frame', schema, sha256(pd.util.hash_pandas_object(value, index=True).to_numpy().tobytes()).digest())
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            # Object-array bytes contain memory addresses, not value contents.
            return ('array', value.dtype.str, value.shape, fingerprint(value.tolist()))
        return ('array', value.dtype.str, value.shape, sha256(value.tobytes()).digest())
    if is_dataclass(value):
        return (type(value), tuple((f.name, fingerprint(getattr(value, f.name))) for f in fields(value)))
    if isinstance(value, dict):
        return tuple((key, fingerprint(item)) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return tuple(fingerprint(item) for item in value)
    return value


class ReplayCache:
    def __init__(self):
        self.calculations = OrderedDict()
        self.sources = OrderedDict()
        self.instances = OrderedDict()
        self.scalars = OrderedDict()
        self.hits = self.misses = 0

    def compute(self, key, calculate, *, source=False):
        for cache in ([self.sources] if source else [self.scalars,self.calculations]):
            if key in cache:
                self.hits += 1
                cache.move_to_end(key)
                return deepcopy(cache[key])
        self.misses += 1
        result = calculate()
        # A league replay can visit hundreds of players * 40 WHP folds before
        # revisiting one player. Small scalar errors need their own working set;
        # they must not evict (or be evicted by) the larger fitted model objects.
        if source:cache,maximum=self.sources,4
        elif isinstance(result,(float,int,bool,np.number)):cache,maximum=self.scalars,32768
        else:cache,maximum=self.calculations,512
        # Callers may modify fitted arrays, frames or diagnostics. Keep an
        # independent copy so a later cache hit always sees the original result.
        cache[key] = deepcopy(result)
        if len(cache) > maximum:
            cache.popitem(last=False)
        return result


@contextmanager
def replay_cache():
    current = _active.get()
    if current is not None:
        yield current
        return
    cache = ReplayCache()
    token = _active.set(cache)
    try:
        yield cache
    finally:
        _active.reset(token)


def replay_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with replay_cache():
            return function(*args, **kwargs)
    return wrapped


def replay_instance(model_type, config):
    """Keep incremental fit state inside one replay, never across live jobs."""
    cache = _active.get()
    if cache is None:
        return model_type(config)
    key = (model_type, fingerprint(config))
    if key not in cache.instances:
        cache.instances[key] = model_type(config)
        if len(cache.instances) > 8:
            cache.instances.popitem(last=False)
    cache.instances.move_to_end(key)
    return cache.instances[key]


def cached_calculation(model, operation, inputs, calculate):
    cache = _active.get()
    if cache is None:
        return calculate()
    if callable(inputs):
        inputs = inputs()
    # Keep keys compact even for pace folds with long lists of prior records.
    # fingerprint has already reduced frames/arrays to their content digests;
    # repr of the remaining primitive tuples retains full values and types.
    input_key = sha256(repr(fingerprint(inputs)).encode()).digest()
    key = (type(model), fingerprint(model.config), operation, input_key)
    return cache.compute(key, calculate)


def replay_memoized(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        return cached_calculation(self, function.__qualname__, (args, kwargs),
                                  lambda: function(self, *args, **kwargs))
    return wrapped


def replay_repository(function):
    """Load the same saved seasonal sources once, then filter at each cutoff.

    Repository load methods select source seasons, not pregame eligibility.
    The forecast services still perform their strict availability filtering.
    """
    @wraps(function)
    def wrapped(self, sport, start, teams=()):
        cache = _active.get()
        if cache is None:
            return function(self, sport, start, teams)
        from models.Player_Stats.Weighted_Historical_Performance.historical_data import seasons_for
        if sport == 'NCAAF':
            from sports_analytics.ncaaf_model_data import seasons_for_start
            years = seasons_for_start(start)
        else:
            years = seasons_for(sport, start)
        # NBA/NFL repositories read league-wide seasons regardless of matchup.
        cohort = tuple(teams) if sport == 'NCAAF' else ()
        key = (self, function.__qualname__, sport, tuple(years), cohort)
        return cache.compute(key, lambda: function(self, sport, start, teams), source=True)
    return wrapped


def calibration_tail(history, grouping, maximum, evaluate):
    """Compute exactly the newest N valid errors without fitting unused folds.

    Sort by each fold's latest possible record, not its week label. A postponed
    game can overlap another week. Stop only when no unvisited fold could change
    the retained tail; failed/insufficient folds do not count toward the limit.
    """
    def record_key(row):
        return row['date'], row['game_id']
    folds = []
    for _, held in history.groupby(grouping, sort=True):
        if held.empty:
            continue
        upper = max(zip(held.event_date.map(pd.Timestamp.isoformat), held.game_id))
        folds.append((upper, held))
    folds.sort(key=lambda item: item[0], reverse=True)
    errors = []
    for upper, held in folds:
        if len(errors) == maximum and upper <= record_key(errors[0]):
            break
        errors.extend(evaluate(held))
        errors = sorted(errors, key=record_key)[-maximum:]
    return errors
