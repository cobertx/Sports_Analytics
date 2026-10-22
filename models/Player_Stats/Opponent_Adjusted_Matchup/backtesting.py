"""Chronological frozen-quote replay; no target-game opportunities enter fitting."""
import math
import numpy as np
import pandas as pd
from models.replay import replay_scope
from models.Player_Stats.Usage_Opportunity.calibration import diagnostics as base_diagnostics
from models.Player_Stats.Usage_Opportunity.markets import prop_key


def quantile(forecast,q):
    if hasattr(forecast,'quantile'):
        return forecast.quantile(q)
    # WHP's predictive distribution, not its historical weighted sample.
    sigma=math.sqrt(forecast.variance)
    lower=math.floor(forecast.mean-12*sigma)-1
    upper=math.ceil(forecast.mean+12*sigma)+1
    if getattr(forecast,'nonnegative',True):lower=-1
    while upper-lower>1:
        middle=(lower+upper)//2
        if forecast.market(middle+.5,'Under').win>=q:upper=middle
        else:lower=middle
    return upper


def empirical_crps(forecast,actual):
    if not hasattr(forecast,'values') or not hasattr(forecast,'probabilities'):
        return None
    x,p=np.asarray(forecast.values,float),np.asarray(forecast.probabilities,float)
    before_p=np.cumsum(p)-p
    before_x=np.cumsum(p*x)-p*x
    return float(p@np.abs(x-actual)-np.sum(p*(x*before_p-before_x)))


@replay_scope
def walk_forward_backtest(service,snapshots,actuals):
    required={'game_id','subject','market','value'}
    if not required<=set(actuals):raise ValueError('Actuals need game_id,subject,market,value.')
    actuals=actuals.copy();actuals['game_id']=actuals.game_id.astype(str)
    actuals['market']=actuals.market.str.removesuffix('_alternate')
    actuals['value']=pd.to_numeric(actuals.value,errors='raise')
    if actuals.duplicated(['game_id','subject','market']).any():raise ValueError('Actual outcomes must have unique identities.')
    lookup=actuals.set_index(['game_id','subject','market']).value.to_dict()
    records,unavailable=[],[]
    for snapshot in sorted(snapshots,key=lambda s:pd.Timestamp(s['pulled_at'])):
        pulled,start=pd.Timestamp(snapshot['pulled_at']),pd.Timestamp(snapshot['commence_time'])
        if pd.isna(pulled) or pd.isna(start) or pulled.tzinfo is None or start.tzinfo is None or pulled>=start:
            raise ValueError('Historical replay requires genuinely pregame timestamped market quotes.')
        try:predictions=service.forecast(snapshot)
        except (ValueError,RuntimeError,OSError) as exc:
            unavailable.append(dict(game_id=str(snapshot['game_id']),reason=str(exc)));continue
        for row in snapshot['rows']:
            key=(str(snapshot['game_id']),*prop_key(row))
            actual=lookup.get(key)
            forecast=predictions.forecasts.get(prop_key(row))
            price=predictions.selection(row)
            if actual is None or forecast is None or price is None:continue
            if not math.isfinite(actual) or actual%1:raise ValueError('Observed player outcomes must be finite integer statistics.')
            # WHP can model anytime TD in a comparison even though OAM/UO
            # correctly leave that event unavailable. Settle its Yes/No event
            # against TD count > 0, not against a missing numeric quote line.
            if row['market_key']=='player_anytime_td' and row['selection'] in {'Yes','No'}:
                line,side=.5,'Over' if row['selection']=='Yes' else 'Under'
            elif row['line'] is not None and row['selection'] in {'Over','Under'}:
                line,side=row['line'],row['selection']
            else:continue
            outcome='push' if actual==line else 'win' if (actual>line)==(side=='Over') else 'loss'
            mass={'win':price.win,'loss':price.loss,'push':price.push}[outcome]
            records.append(dict(game_id=key[0],subject=key[1],market=key[2],actual=float(actual),
                snapshot_timestamp=snapshot['pulled_at'],
                predicted=forecast.mean,lower=quantile(forecast,.05),upper=quantile(forecast,.95),
                line=row['line'],side=row['selection'],odds=row['odds'],outcome=outcome,
                probability=price.probability,win=price.win,loss=price.loss,push=price.push,
                log_loss=-math.log(max(1e-12,mass)),crps=empirical_crps(forecast,actual),
                opponent_multiplier=forecast.diagnostics.get('opponent_multiplier'),
                component_projection=forecast.diagnostics.get('opportunity_projection',{})))
    metrics=base_diagnostics(records)
    unique={tuple(r[k] for k in ['game_id','subject','market']):r for r in records}
    scored=[r['crps'] for r in unique.values() if r['crps'] is not None]
    metrics.update(three_outcome_log_loss=float(np.mean([r['log_loss'] for r in records])),
                   empirical_crps=float(np.mean(scored)) if scored else None)
    buckets=[]
    for lower,upper in [(0.,.9),(.9,1.),(1.,1.1),(1.1,float('inf'))]:
        selected=[r for r in unique.values() if r['opponent_multiplier'] is not None and lower<=r['opponent_multiplier']<upper]
        buckets.append(dict(lower=lower,upper=upper if math.isfinite(upper) else None,n=len(selected),
                            mae=float(np.mean([abs(r['predicted']-r['actual']) for r in selected])) if selected else None))
    return dict(metrics=metrics,multiplier_buckets=buckets,records=records,unavailable=unavailable,
                note='Fixed research settings; evaluate an untouched chronological interval. Historical publication times/revisions are not fully archived. No trained probability calibrator is applied.')
