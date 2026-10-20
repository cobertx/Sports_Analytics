"""Frozen-quote replay with proper scores, intervals and volume validation."""
import math
import numpy as np
from models.Player_Stats.Opponent_Adjusted_Matchup.backtesting import walk_forward_backtest as _replay,empirical_crps
from models.Player_Stats.Usage_Opportunity.market_comparison import compare_price


def walk_forward_backtest(service,snapshots,actuals):
    report = _replay(service,snapshots,actuals)
    scored = []
    for row in report['records']:
        if row['outcome']=='push':
            continue
        q = compare_price(None,row['odds'])['market_implied']
        if q is not None:
            y = float(row['outcome']=='win')
            scored.append(((row['probability']-y)**2,(q-y)**2,
                           -math.log(max(1e-12,q if y else 1-q))))
    report['market_benchmark'] = dict(nonpush_quotes=len(scored),
        model_brier=float(np.mean([r[0] for r in scored])) if scored else None,
        raw_market_brier=float(np.mean([r[1] for r in scored])) if scored else None,
        raw_market_log_loss=float(np.mean([r[2] for r in scored])) if scored else None,
        note='Same non-push quotes; raw odds include vig. Market odds do not identify a separate push probability.')
    return report

__all__ = ['walk_forward_backtest','empirical_crps']
