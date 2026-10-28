"""Chronological actual/expected defense ratios with neutralized offense priors."""
from collections import defaultdict, deque
from functools import lru_cache
import heapq
import math
import numpy as np
import pandas as pd
from models.season_weights import apply_season_weights, season_ids


@lru_cache(maxsize=256)
def recency(n, half_life):
    w = np.exp2(-np.arange(n-1,-1,-1,dtype=float)/half_life)
    return w/w.sum()


def record_weights(records, half_life, config):
    return apply_season_weights(recency(len(records), half_life), pd.DataFrame(records),
                                config.baseline.sport, config.baseline.prior_season_weight)


def shrunk_factor(actual, expected, weights=None, prior_strength=8):
    a,e = np.asarray(actual,float),np.asarray(expected,float)
    w = np.ones(len(a)) if weights is None else np.asarray(weights,float)
    if (not len(a) or a.shape != e.shape or a.shape != w.shape
            or not all(np.isfinite(x).all() for x in (a,e,w)) or (e <= 0).any()
            or (w < 0).any() or w.sum() <= 0 or not math.isfinite(prior_strength) or prior_strength <= 0):
        raise ValueError('Defense factors need aligned finite allowed/expected production and positive exposure weights.')
    w=w/w.sum()
    raw=float(w@a)/float(w@e)
    effective=1/float(w@w)
    alpha=effective/(effective+prior_strength)
    return dict(raw=raw,effective_n=effective,alpha=alpha,mean=alpha*raw+1-alpha)


def _prior_rate(rows, window, half_life, config):
    selected=list(rows)[-window:]
    if not selected:
        return None,0.
    w=record_weights(selected,half_life,config)
    rate=float(w@np.array([r['neutral_actual'] for r in selected]))/float(w@np.array([r['exposure'] for r in selected]))
    return rate,1/float(w@w)


class ScheduleAdjustedDefense:
    """No coefficient or normalization factor is fit using later game results."""
    def __init__(self, config):
        self.config=config
        self.records=[]
        self.historical={}

    def fit(self, features):
        cfg=self.config
        self.records,self.historical=[],{}
        features = features.assign(season=season_ids(features, cfg.baseline.sport))
        for component, frame in features.groupby('component',sort=True):
            league=deque(maxlen=cfg.league_window)
            offense=defaultdict(lambda:deque(maxlen=cfg.baseline.efficiency_window))
            defense=defaultdict(lambda:deque(maxlen=cfg.defense_window))
            pending=[]
            for seq,row in enumerate(frame.itertuples(index=False)):
                # Availability, not merely start order, controls admission. Equal
                # timestamps are excluded, including concurrent slate results.
                while pending and pending[0][0] < row.event_date.value:
                    _,_,finished=heapq.heappop(pending)
                    league.append(finished); offense[finished['offense']].append(finished)
                    if finished['expected'] > 0:
                        defense[finished['defense']].append(finished)
                old=list(defense[row.defense])
                prior=cfg.position_prior if component.count(':')>1 else cfg.defense_prior
                factor=(shrunk_factor([r['actual'] for r in old],[r['expected'] for r in old],
                                     record_weights(old,cfg.defense_half_life,cfg),prior)['mean']
                        if len(old)>=cfg.minimum_defense_games else 1.)
                factor=float(np.clip(factor,cfg.factor_floor,cfg.factor_ceiling))
                self.historical[(component,str(row.game_id),row.offense)]=factor
                league_rate,_=_prior_rate(league,cfg.league_window,cfg.baseline.efficiency_half_life,cfg)
                rate,n=_prior_rate(offense[row.offense],cfg.baseline.efficiency_window,cfg.baseline.efficiency_half_life,cfg)
                expected=0.
                if len(league)>=cfg.minimum_league_games and league_rate is not None and league_rate>0:
                    alpha=n/(n+cfg.offense_prior)
                    neutral=alpha*rate+(1-alpha)*league_rate if rate is not None else league_rate
                    expected=max(0.,neutral*row.exposure)
                record=dict(component=component,game_id=str(row.game_id),offense=row.offense,defense=row.defense,
                            event_date=row.event_date.isoformat(),available_at=row.available_at.isoformat(),season=int(row.season),
                            actual=float(row.actual),exposure=float(row.exposure),expected=float(expected),
                            neutral_actual=float(row.actual)/factor,historical_factor=factor)
                self.records.append(record)
                heapq.heappush(pending,(row.available_at.value,seq,record))
        return self

    def profile(self, defense, component):
        cfg=self.config
        records=[r for r in self.records if r['defense']==defense and r['component']==component and r['expected']>0]
        records=sorted(records,key=lambda r:(r['event_date'],r['game_id']))[-cfg.defense_window:]
        if len(records)<cfg.minimum_defense_games:
            return None
        prior=cfg.position_prior if component.count(':')>1 else cfg.defense_prior
        fit=shrunk_factor([r['actual'] for r in records],[r['expected'] for r in records],
                          record_weights(records,cfg.defense_half_life,cfg),prior)
        return {**fit,'component':component,'defense':defense,'records':records,
                'mean':float(np.clip(fit['mean'],cfg.factor_floor,cfg.factor_ceiling))}
