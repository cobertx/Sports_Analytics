"""Replay supplied contemporaneous market snapshots against final outcomes."""
import math
import pandas as pd
from .calibration import diagnostics
from .errors import UsageOpportunityError
from .markets import prop_key


def walk_forward_backtest(service, snapshots, actuals):
    """Never fabricate historical prices or use held-out realized opportunities.

    actuals: game_id, subject, market (canonical key), value. The same saved-history
    cutoff logic is used as the dashboard. Parameters are fixed, not optimized.
    """
    required = {'game_id', 'subject', 'market', 'value'}
    if not required <= set(actuals):
        raise UsageOpportunityError('Actuals need game_id, subject, market and value.')
    actuals = actuals.copy()
    actuals['game_id'] = actuals.game_id.astype(str)
    actuals['market'] = actuals.market.str.removesuffix('_alternate')
    if actuals.duplicated(['game_id', 'subject', 'market']).any():
        raise UsageOpportunityError('Actuals must have unique player/game/market identities.')
    outcomes = actuals.set_index(['game_id', 'subject', 'market']).value.to_dict()
    records, unavailable = [], []
    for snapshot in sorted(snapshots, key=lambda s: s['pulled_at']):
        pulled, start = pd.Timestamp(snapshot['pulled_at']), pd.Timestamp(snapshot['commence_time'])
        if pulled.tzinfo is None or start.tzinfo is None or pulled >= start:
            raise UsageOpportunityError('Backtesting requires contemporaneous pregame timestamps and quotes.')
        try:
            forecast = service.forecast(snapshot)
        except (ValueError, RuntimeError, OSError) as exc:
            unavailable.append(dict(game_id=str(snapshot['game_id']), reason=str(exc)))
            continue
        for row in snapshot['rows']:
            key = (str(snapshot['game_id']), *prop_key(row))
            actual = outcomes.get(key)
            estimate = forecast.selection(row)
            fitted = forecast.forecasts.get(prop_key(row))
            if actual is None or estimate is None or fitted is None:
                continue
            if not math.isfinite(actual) or actual % 1:
                raise UsageOpportunityError('Actual player outcomes must be finite integer statistics.')
            outcome = 'push' if actual == row['line'] else 'win' if (actual > row['line']) == (row['selection']=='Over') else 'loss'
            records.append(dict(game_id=key[0], subject=key[1], market=key[2], actual=actual,
                predicted=fitted.mean, lower=fitted.quantile(.05), upper=fitted.quantile(.95), line=row['line'],
                side=row['selection'], odds=row['odds'], outcome=outcome, probability=estimate.probability,
                win=estimate.win, loss=estimate.loss, push=estimate.push,
                component_projection=fitted.diagnostics['opportunity_projection']))
    return dict(metrics=diagnostics(records), records=records, unavailable=unavailable,
                note='Fixed research defaults. Reserve an untouched evaluation interval. Revisions/availability proxies limit point-in-time fidelity; no fitted probability calibrator is applied.')
