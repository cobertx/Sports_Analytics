"""Button-triggered method consensus using each provider's settlement rules."""
import math

from models.Game_Outcome.PPP.PPP import fair_american_odds
from .market_summary import calculate,entries
from .market_view import criteria_match


def selection_key(row):return row['provider'],row['index'],row['side'] or ''


def calculate_filter(bundle,methods,player_methods,criteria,game_forecast,player_forecast):
    values={};errors=[]
    for kind,selected in [('game',methods),('player',player_methods)]:
        for method in selected:
            result=calculate(bundle,method if kind=='game' else methods[0],
                method if kind=='player' else player_methods[0],game_forecast,player_forecast,kinds=(kind,))
            errors.extend(result['errors'])
            values[kind,method]={selection_key(row):row for row in result['rows']}
    matches=[];evaluated=unavailable=0
    for row in entries(bundle):
        kind='player' if row['category']=='Player Props' else 'game'
        selected=player_methods if kind=='player' else methods
        estimates=[values[kind,method].get(selection_key(row)) for method in selected]
        if row['target'] is None or any(not value or any(not isinstance(value.get(key),(int,float)) or
                not math.isfinite(value[key]) for key in ('probability','implied','edge')) for value in estimates):
            unavailable+=1;continue
        evaluated+=1
        if not all(criteria_match(value['probability'],value['edge'],criteria) for value in estimates):continue
        probability=min(value['probability'] for value in estimates)
        matches.append({**row,'probability':probability,'model_odds':fair_american_odds(probability),
            'edge':min(value['edge'] for value in estimates),'method':' / '.join(selected),
            'estimates':[dict(method=method,probability=value['probability'],model_odds=value['model_odds'],edge=value['edge'])
                         for method,value in zip(selected,estimates)]})
    matches.sort(key=lambda row:(-row['edge'],-row['probability'],selection_key(row)))
    return dict(matches=matches,evaluated=evaluated,unavailable=unavailable,errors=list(dict.fromkeys(errors)))
