"""Season discounts shared by live forecasts and chronological replays."""
import numpy as np
import pandas as pd


PRIOR_SEASON_WEIGHT = .2
POLICY_VERSION = 'current-season-priority-1'


def season_ids(frame, sport):
    """Use source season-start years, with the application's date fallback."""
    if 'season' in frame:
        supplied = pd.to_numeric(frame['season'], errors='coerce')
        if supplied.notna().all():
            return supplied
    dates = pd.to_datetime(frame['event_date'], utc=True)
    inferred = dates.dt.year - dates.dt.month.lt(10 if sport == 'NBA' else 3).astype(int)
    if 'season' in frame:
        return supplied.fillna(inferred)
    return inferred


def season_factors(frame, sport, as_of=None, discount=PRIOR_SEASON_WEIGHT):
    """Multiply existing recency weights by discount ** seasons_old.

    Normalized estimators can omit as_of: shifting the reference season
    multiplies every weight equally and leaves their relative mass unchanged.
    Only already-eligible observations may be passed by the caller.
    """
    if not len(frame):
        return np.empty(0)
    seasons = season_ids(frame, sport).to_numpy(dtype=float)
    if as_of is None:
        current = seasons.max()
    else:
        cutoff = pd.Timestamp(as_of)
        current = cutoff.year - int(cutoff.month < (10 if sport == 'NBA' else 3))
    return np.power(discount, np.maximum(0., current - seasons))


def calendar_weights(frame, as_of, half_life, sport, discount=PRIOR_SEASON_WEIGHT):
    age = (pd.Timestamp(as_of) - frame['event_date']).dt.total_seconds() / 86400
    return np.exp2(-age / half_life) * season_factors(frame, sport, as_of, discount)


def apply_season_weights(weights, frame, sport, discount=PRIOR_SEASON_WEIGHT):
    result = np.asarray(weights, dtype=float) * season_factors(frame, sport, discount=discount)
    return result / result.sum()
