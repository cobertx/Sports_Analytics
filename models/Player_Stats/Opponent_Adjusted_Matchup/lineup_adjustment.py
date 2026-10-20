"""Optional user-supplied pregame scenarios relative to the fitted lineup baseline."""
import numpy as np
import pandas as pd


def scenario_draws(scenarios, offense, cutoff, simulations, rng):
    relevant=[s for s in scenarios if s['offense']==offense]
    if not relevant:
        return {}
    probabilities=np.array([float(s['probability']) for s in relevant])
    if not np.isfinite(probabilities).all() or (probabilities<0).any() or not np.isclose(probabilities.sum(),1):
        raise ValueError('Lineup scenario probabilities must be nonnegative and sum to one per offense.')
    for scenario in relevant:
        timestamp=pd.Timestamp(scenario['available_at'])
        if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp>=pd.Timestamp(cutoff):
            raise ValueError('Lineup scenarios need explicit availability strictly before the forecast cutoff.')
        if not all(np.isfinite(v) and v>0 for v in scenario['factors'].values()):
            raise ValueError('Lineup component factors must be finite and positive.')
    selected=rng.choice(len(relevant),size=simulations,p=probabilities)
    components=set().union(*(s['factors'] for s in relevant))
    return {c:np.array([s['factors'].get(c,1.) for s in relevant])[selected] for c in components}
