"""Integer predictive mass from the same fitted forecast used to price a prop."""
from hashlib import sha256
import json
import math

import numpy as np
from dash import ClientsideFunction, Input, MATCH, Output, State, dcc, html

from models.Player_Stats.Usage_Opportunity.markets import prop_key


def distribution(forecast, lines=()):
    """Keep exact empirical support; analytic tails are shown without rescaling."""
    mean,sd=float(forecast.mean),math.sqrt(float(forecast.variance))
    if not math.isfinite(mean) or not math.isfinite(sd):
        raise ValueError('A finite predictive distribution is required.')
    if hasattr(forecast,'values') and hasattr(forecast,'probabilities'):
        values,weights=forecast.values,forecast.probabilities
    elif getattr(forecast,'family',None)=='empirical':
        values,weights=forecast.samples,forecast.weights
    else:values=weights=None
    if values is not None:
        values,weights=np.asarray(values,float),np.asarray(weights,float)
        if (not len(values) or values.shape!=weights.shape or not np.isfinite(values).all()
                or not np.equal(values%1,0).all() or not np.isfinite(weights).all()
                or (weights<0).any() or not np.isclose(weights.sum(),1)):
            raise ValueError('The forecast does not contain valid integer probability mass.')
        low,high=min(0,int(values.min())),int(values.max())
    else:
        low,high=math.floor(mean-6*sd)-1,math.ceil(mean+6*sd)+1
        if getattr(forecast,'nonnegative',False) or getattr(forecast,'family',None)=='count':low=0
    # Keep quoted thresholds visible even when they lie just beyond the fitted
    # support. Bound unusual lines so a malformed quote cannot allocate a huge plot.
    for line in lines:
        if isinstance(line,(int,float)) and math.isfinite(line) and low-100<=line<=high+100:
            low=min(low,math.floor(line));high=max(high,math.ceil(line))
    if high-low>10000:
        raise ValueError('This forecast spans too many individual values to chart.')
    x=np.arange(low,high+1,dtype=int)
    if values is not None:
        mass=np.zeros(len(x))
        np.add.at(mass,values.astype(int)-low,weights)
        cumulative=np.minimum(1.,np.cumsum(mass))
        below=0.;above=0.
    else:
        def cdf(value):
            price=forecast.market(int(value),'Under')
            return min(1.,max(0.,price.win+price.push))
        cumulative=np.array([cdf(value) for value in x])
        below=cdf(low-1)
        mass=np.maximum(0.,np.diff(np.r_[below,cumulative]))
        above=max(0.,1-float(cumulative[-1]))
    return dict(x=x.tolist(),mass=mass.tolist(),cumulative=cumulative.tolist(),mean=mean,sd=sd,
                below=below,above=above)


def panel(rows,forecast,method,section,stat):
    if forecast is None:return None
    fitted=getattr(forecast,'forecasts',{}).get(prop_key(rows[0]))
    if fitted is None:return None
    try:payload=distribution(fitted,[row.get('line') for row in rows])
    except (ValueError,TypeError,OverflowError) as exc:
        return html.P(f'{method} distribution unavailable: {exc}',className='chart-note')
    payload.update(method=method,player=rows[0]['subject'],stat=stat)
    identity=sha256((section+json.dumps(payload,sort_keys=True,allow_nan=False)).encode()).hexdigest()
    return html.Section([
        dcc.Store(id=dict(type='player-distribution-data',key=identity),data=payload),
        html.Button('',id=dict(type='player-distribution-trigger',key=identity),n_clicks=0,
                    className='player-distribution-trigger',style={'display':'none'},tabIndex=-1,**{'aria-hidden':'true'}),
        html.Div(id=dict(type='player-distribution-body',key=identity),className='player-distribution-body',
                 children=html.P(f'Open this market to see the {method} predictive distribution.',className='chart-note')),
    ],className='player-distribution',**{'aria-label':f"{rows[0]['subject']} {stat} {method} predictive distribution"})


def register(app):
    app.clientside_callback(ClientsideFunction(namespace='player_distribution',function_name='render'),
        Output(dict(type='player-distribution-body',key=MATCH),'children'),
        Input(dict(type='player-distribution-trigger',key=MATCH),'n_clicks'),
        State(dict(type='player-distribution-data',key=MATCH),'data'),prevent_initial_call=True)
