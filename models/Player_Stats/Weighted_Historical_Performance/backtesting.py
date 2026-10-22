"""PDF section 9: genuinely forward forecasts and probability diagnostics."""

import math
import numpy as np
import pandas as pd
from models.replay import replay_scope
from .core import WeightedHistoricalPerformance, eligible_history
from .errors import WHPError


@replay_scope
def walk_forward_backtest(history, config, *, nonnegative=True, count=False, line=None,
                          side="Over", evaluation_start=None):
    """Fixed-parameter replay. Dispersion for each test game uses only earlier errors.

    A user can reserve a final interval with evaluation_start. This function does
    not search parameters or use target-game minutes/attempts to forecast it.
    """
    if side not in {"Over", "Under"}:
        raise WHPError("Backtest selection must be Over or Under.")
    last = pd.to_datetime(history["available_at"], utc=True).max() + pd.Timedelta(seconds=1)
    data = eligible_history(history, last)
    model, records = WeightedHistoricalPerformance(config), []
    start = pd.Timestamp(evaluation_start) if evaluation_start else None
    for _, game in data.iterrows():
        if start is not None and game["event_date"] < start:
            continue
        try:
            forecast = model.forecast(data, game["event_date"], nonnegative=nonnegative, count=count)
        except WHPError:
            continue
        actual = float(game["value"])
        record = dict(game_id=str(game["game_id"]), event_date=game["event_date"].isoformat(),
                      actual=actual, predicted=forecast.mean, predictive_sd=math.sqrt(forecast.variance))
        if line is not None:
            price = forecast.market(line, side)
            record.update(line=float(line), probability=price.probability, push=actual == line,
                          outcome=int(actual > line if side == "Over" else actual < line))
        records.append(record)
    if not records:
        raise WHPError("No out-of-sample forecasts available; provide more chronological history.")
    errors = np.array([r["predicted"] - r["actual"] for r in records])
    metrics = dict(games=len(records), mae=float(np.abs(errors).mean()),
                   rmse=float(np.sqrt(np.mean(errors ** 2))), bias=float(errors.mean()))
    settled = [r for r in records if "probability" in r and not r["push"] and r["probability"] is not None]
    if settled:
        p = np.array([r["probability"] for r in settled])
        y = np.array([r["outcome"] for r in settled])
        clipped = np.clip(p, 1e-12, 1-1e-12)
        metrics.update(settled_games=len(settled), brier=float(np.mean((p-y)**2)),
                       log_loss=float(-np.mean(y*np.log(clipped)+(1-y)*np.log1p(-clipped))),
                       reliability=[dict(lower=i/10, upper=(i+1)/10,
                            n=int(((p >= i/10) & (p < (i+1)/10 if i < 9 else p <= 1)).sum()),
                            predicted=float(p[mask].mean()) if mask.any() else None,
                            observed=float(y[mask].mean()) if mask.any() else None)
                           for i in range(10)
                           for mask in [(p >= i/10) & (p < (i+1)/10 if i < 9 else p <= 1)]])
    return dict(metrics=metrics, records=records,
                note="Fixed research parameters. Reserve a final interval; do not tune on the reported evaluation games.")
