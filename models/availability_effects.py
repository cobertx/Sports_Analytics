"""Regularized, pre-cutoff with/without associations layered over a baseline.

These are observational comparisons, not causal player valuations. Unknown
participation is excluded. No news parsing, downloads or model refits live here.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .season_weights import calendar_weights

VERSION = 'availability-effects-1'


@dataclass
class Effect:
    delta: np.ndarray
    covariance: np.ndarray
    baseline: np.ndarray
    evidence: dict


def estimate(frame, players, targets, cutoff, sport, *, minimum=3):
    """Fit joint absence indicators with opponent, venue, season and trend controls.

    Predictions are changes from recent observed availability, not from an
    imaginary fully healthy team. Require both states and the proposed joint
    absence pattern; never extrapolate an unseen combination of absences.
    """
    required = {'event_date', 'available_at', 'opponent', *targets, *players}
    if not required <= set(frame) or not players:
        return None
    cutoff = pd.Timestamp(cutoff)
    if pd.isna(cutoff) or cutoff.tzinfo is None:
        raise ValueError('Availability effects require a timezone-aware cutoff.')
    data = frame.copy()
    for key in ('event_date', 'available_at'):
        data[key] = pd.to_datetime(data[key], utc=True, errors='coerce')
    data = data[data.event_date.lt(cutoff) & data.available_at.lt(cutoff)
                & data.available_at.ge(data.event_date)]
    numeric = [*targets, *players]
    data[numeric] = data[numeric].apply(pd.to_numeric, errors='coerce')
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=[*numeric, 'opponent'])
    data = data.sort_values(['event_date', 'game_id'])
    if len(data) < max(12, 2*minimum) or not data[players].isin([0, 1]).all().all():
        return None
    w = np.asarray(calendar_weights(data, cutoff, 120 if sport == 'NBA' else 180, sport),float)
    w = w/w.sum()
    absence = data[players].to_numpy(float)
    support = []
    for j, player in enumerate(players):
        groups = []
        for state in (0, 1):
            weights = w[absence[:, j] == state]
            neff = float(weights.sum()**2 / (weights @ weights)) if len(weights) else 0.
            if len(weights) < minimum or neff < minimum-1:
                return None
            groups.append(dict(games=len(weights), effective_n=neff))
        support.append(dict(player=player, playing=groups[0], not_playing=groups[1]))
    if np.all(absence == 1, axis=1).sum() < minimum:
        return None
    # Absences already established throughout the latest role window should
    # not receive the same full penalty on top of the original forecast.
    window = 5 if sport == 'NBA' else 3
    recent = data.tail(window)
    rw = calendar_weights(recent, cutoff, 30 if sport == 'NBA' else 60, sport)
    baseline_absence = np.average(recent[players], axis=0, weights=rw)
    change = 1-baseline_absence
    # Nuisance variables reduce opponent/calendar confounding without claiming
    # to resolve unmeasured lineup, coaching or selection effects.
    opponents = pd.get_dummies(data.opponent.astype(str), dtype=float)
    seasons = pd.get_dummies(data.get('season', data.event_date.dt.year).astype(str), dtype=float)
    age = (data.event_date-cutoff).dt.total_seconds().to_numpy()/86400/180
    home = pd.to_numeric(data.get('is_home', pd.Series(0., index=data.index)), errors='coerce').fillna(0).to_numpy()
    x = np.column_stack([np.ones(len(data)), home, age, opponents, seasons, absence])
    penalty = np.full(x.shape[1], 20.)
    penalty[0] = 0.
    penalty[-len(players):] = 10.
    # Scale to effective observations, preserving current-season priorities.
    fit_w = w/(w @ w)
    normal = x.T @ (fit_w[:, None]*x) + np.diag(penalty)
    inverse = np.linalg.solve(normal, np.eye(len(normal)))
    y = data[targets].to_numpy(float)
    coefficients = inverse @ (x.T @ (fit_w[:, None]*y))
    contrast = np.zeros(x.shape[1]); contrast[-len(players):] = change
    delta = contrast @ coefficients
    residuals = y-x @ coefficients
    residual_covariance = (residuals.T*w) @ residuals
    uncertainty = max(0., float(contrast @ inverse @ contrast))
    covariance = residual_covariance*uncertainty
    baseline = np.average(recent[targets], axis=0, weights=rw)
    for j, item in enumerate(support):
        item['baseline_absence'] = float(baseline_absence[j])
        item['coefficient'] = coefficients[-len(players)+j].tolist()
        # Descriptive scoring/workload differences are retained for review.
        for state, label in ((0, 'playing'), (1, 'not_playing')):
            mask = absence[:, j] == state
            item[label]['target_means'] = np.average(y[mask], axis=0, weights=w[mask]).tolist()
            if 'won' in data:
                item[label]['win_rate'] = float(np.average(data.loc[mask, 'won'], weights=w[mask]))
    return Effect(np.asarray(delta), covariance, np.asarray(baseline),
                  dict(players=support, targets=targets, games=len(data),
                       joint_absence_games=int(np.all(absence == 1, axis=1).sum()),
                       method='Season/recency-weighted joint ridge association; opponent, home, season and calendar controls',
                       cutoff=cutoff.isoformat()))
