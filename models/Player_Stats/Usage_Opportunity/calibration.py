"""Out-of-sample diagnostics only; this module does not claim a fitted calibrator."""
import numpy as np
from .errors import UsageOpportunityError


def diagnostics(records):
    priced = [r for r in records if r.get('probability') is not None]
    unique = {tuple(r[k] for k in ('game_id', 'subject', 'market')): r for r in records}
    if not unique:
        raise UsageOpportunityError('No historical predictions could be evaluated.')
    errors = np.array([r['predicted']-r['actual'] for r in unique.values()])
    result = dict(player_game_targets=len(unique), mae=float(np.abs(errors).mean()),
                  rmse=float(np.sqrt(np.mean(errors**2))), bias=float(errors.mean()),
                  interval_90_coverage=float(np.mean([r['lower'] <= r['actual'] <= r['upper'] for r in unique.values()])))
    settled = [r for r in priced if r['outcome'] != 'push']
    if settled:
        p = np.array([r['probability'] for r in settled])
        y = np.array([r['outcome'] == 'win' for r in settled], int)
        result.update(settled_quotes=len(settled), brier=float(np.mean((p-y)**2)),
            reliability=[dict(lower=i/10, upper=(i+1)/10, n=int(mask.sum()),
                              predicted=float(p[mask].mean()) if mask.any() else None,
                              observed=float(y[mask].mean()) if mask.any() else None)
                         for i in range(10)
                         for mask in [(p >= i/10) & ((p < (i+1)/10) if i < 9 else (p <= 1))]])
    if priced:
        result.update(quotes=len(priced), pushes=sum(r['outcome'] == 'push' for r in priced),
            three_outcome_brier=float(np.mean([sum((r[k]-int(r['outcome']==k))**2 for k in ['win','loss','push']) for r in priced])))
    return result
