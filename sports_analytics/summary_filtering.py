"""Button-triggered method consensus using each provider's settlement rules."""
import math
from threading import Lock

from models.Game_Outcome.PPP.PPP import fair_american_odds
from .filter_jobs import forecast_progress
from .market_summary import calculate,prepare_forecast
from app.sections.markets.market_view import criteria_match
from app.sections.markets.mispricing import run_forecasts


def selection_key(row):return row['provider'],row['index'],row['side'] or ''


def calculate_filter(bundle,methods,player_methods,criteria,game_forecast,player_forecast,report=None):
    values={};errors=[]
    prepared=prepare_forecast(bundle)
    total=len(methods)+len(player_methods)
    forecast_progress(report,0,total)
    progress_lock=Lock()
    completed=0

    def evaluate(kind,selected):
        nonlocal completed
        results=[]
        for method in selected:
            result=calculate(bundle,method if kind=='game' else methods[0],
                method if kind=='player' else player_methods[0],game_forecast,player_forecast,
                kinds=(kind,),prepared=prepared,inline=True)
            results.append((method,result))
            with progress_lock:
                completed+=1
                forecast_progress(report,completed,total)
        return results

    # One pool level. Within each family, preserve the selected method order.
    results=run_forecasts({'game':lambda:evaluate('game',methods),'player':lambda:evaluate('player',player_methods)},
                          bundle['sport'])
    for kind,ranked in results.items():
        for method,result in ranked:
            errors.extend(result['errors'])
            values[kind,method]={selection_key(row):row for row in result['rows']}
    if report:report(85,'Applying thresholds and ranking selections')
    matches=[];evaluated=unavailable=0
    for row in prepared[0]:
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
    player_lines=[row for (kind,_),indexed in values.items() if kind=='player' for row in indexed.values()
                  if row['category']=='Player Props' and row.get('comparison')]
    return dict(matches=matches,evaluated=evaluated,unavailable=unavailable,errors=list(dict.fromkeys(errors)),
                player_lines=player_lines)
