"""Frozen quote identities and explicitly independent parlay estimates.

No sportsbook quote or correlation-adjusted SGP price is requested or inferred.
Model probabilities are conditional on non-push settlement, like the tables.
"""
from collections import Counter
from hashlib import sha256
import json
import math
from models.Game_Outcome.PPP.PPP import fair_american_odds,implied_probability


def quote_key(snapshot,row,index):
    identity = [snapshot.get('sport','NFL'),str(snapshot.get('game_id',snapshot.get('event_id',''))),
                snapshot.get('pulled_at'),index,row]
    return sha256(json.dumps(identity,sort_keys=True,allow_nan=False).encode()).hexdigest()[:32]


def selection_key(snapshot,row):
    identity = [snapshot['sport'],str(snapshot['game_id']),row['market_key'].removesuffix('_alternate'),
                row['subject'],row['selection'],row['line']]
    return sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:32]


def decimal_odds(american):
    implied = implied_probability(american)
    if implied is None:
        raise ValueError('This selection has no valid saved market odds.')
    return 1/implied


def american_from_decimal(decimal):
    if not math.isfinite(decimal) or decimal <= 1:
        return None
    return 100*(decimal-1) if decimal>=2 else -100/(decimal-1)


def build_leg(snapshot,row,index,label,estimate=None,error=None,latest=None):
    decimal_odds(row['odds'])
    method = 'WHP' if row['market_key'].startswith('player_') else 'PPP'
    if estimate is not None:
        mass = [float(getattr(estimate,k)) for k in ['win','loss','push']]
        if not all(math.isfinite(p) and 0<=p<=1 for p in mass) or not math.isclose(sum(mass),1,abs_tol=1e-8):
            raise ValueError('Model returned invalid settlement probabilities.')
        probability = estimate.probability
    else:
        mass,probability = [None]*3,None
    return dict(key=selection_key(snapshot,row),quote_key=quote_key(snapshot,row,index),
        sport=snapshot['sport'],game_id=str(snapshot['game_id']),home=snapshot['home_team'],away=snapshot['away_team'],
        game_start=snapshot['commence_time'],snapshot_time=snapshot['pulled_at'],
        label=label,market=row.get('market',row['market_key']),market_key=row['market_key'],
        subject=row['subject'],selection=row['selection'],line=row['line'],odds=float(row['odds']),
        market_probability=implied_probability(row['odds']),method=method,
        model_probability=probability,model_odds=fair_american_odds(probability),
        win=mass[0],loss=mass[1],push=mass[2],error=error if probability is None else None,latest=latest)


def clean_state(state):
    """Old/corrupt browser state cannot poison rendering or silently price a slip."""
    if not isinstance(state,dict) or state.get('version')!=1:
        return dict(version=1,legs=[],collapsed=False)
    legs,seen=[],set()
    for leg in state.get('legs',[]) if isinstance(state.get('legs'),list) else []:
        try:
            if not isinstance(leg,dict) or not all(k in leg for k in
                ['key','quote_key','sport','game_id','home','away','label','market','market_key',
                 'subject','selection','line','odds','snapshot_time','method']):
                continue
            decimal_odds(leg['odds'])
            if leg['key'] in seen:
                continue
            probability=leg.get('model_probability')
            if probability is not None and (not isinstance(probability,(int,float)) or not math.isfinite(probability) or not 0<=probability<=1):
                continue
            normalized={**leg,'market_probability':implied_probability(leg['odds']),
                        'model_odds':fair_american_odds(probability)}
            seen.add(leg['key']);legs.append(normalized)
        except (TypeError,ValueError,OverflowError):
            continue
    return dict(version=1,legs=legs,collapsed=bool(state.get('collapsed',False)))


def toggle_leg(state,leg):
    state=clean_state(state)
    existing=next((r for r in state['legs'] if r['key']==leg['key']),None)
    if existing and existing['quote_key']==leg['quote_key']:
        state['legs']=[r for r in state['legs'] if r['key']!=leg['key']]
        return state,'Selection removed.'
    if existing:
        state['legs']=[leg if r['key']==leg['key'] else r for r in state['legs']]
        message='Selection updated to the clicked saved quote.'
    else:
        state['legs'].append(leg);message='Selection added.'
    state['collapsed']=False
    return state,message


def conflicting_selections(legs):
    bounds={}
    for leg in legs:
        market=leg['market_key'].removesuffix('_alternate')
        line,side=leg.get('line'),leg['selection']
        event=(leg['sport'],leg['game_id'])
        if market=='alternate_spreads' and line is not None:
            variable=(*event,'margin')
            if leg['subject']==leg['home']:
                line,side=-line,'Over'
            elif leg['subject']==leg['away']:
                side='Under'
            else:
                continue
        elif market=='alternate_totals':
            variable=(*event,'total')
        elif market.startswith('player_'):
            variable=(*event,market,leg['subject'])
            if market=='player_anytime_td' and side in {'Yes','No'}:
                line,side=.5,'Over' if side=='Yes' else 'Under'
        else:
            continue
        if line is None or side not in {'Over','Under'}:
            continue
        lower,upper=bounds.get(variable,(-math.inf,math.inf))
        if side=='Over':lower=max(lower,math.floor(line)+1)
        else:upper=min(upper,math.ceil(line)-1)
        bounds[variable]=(lower,upper)
        if lower>upper:
            return True
    return False


def parlay_summary(legs):
    if not legs:
        return dict(legs=0)
    same_game=any(n>1 for n in Counter((r['sport'],r['game_id']) for r in legs).values())
    conflict=conflicting_selections(legs)
    market_log=sum(math.log(decimal_odds(r['odds'])) for r in legs)
    market_decimal=math.exp(market_log) if market_log<700 else None
    probabilities=[r.get('model_probability') for r in legs]
    complete=all(p is not None for p in probabilities)
    probability=math.prod(probabilities) if complete else None
    if conflict:probability=0.
    masses=all(all(r.get(k) is not None for k in ['win','loss','push']) for r in legs)
    all_win=math.prod(r['win'] for r in legs) if masses and not conflict else None
    return dict(legs=len(legs),same_game=same_game,conflict=conflict,missing_models=sum(p is None for p in probabilities),
        market_decimal=market_decimal if not conflict else None,
        market_odds=american_from_decimal(market_decimal) if market_decimal is not None and not conflict else None,
        market_probability=math.exp(-market_log) if not conflict else None,
        model_probability=probability,model_odds=fair_american_odds(probability),all_win=all_win,
        has_pushes=any((r.get('push') or 0)>1e-10 for r in legs))
