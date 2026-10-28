"""Cross-provider game matching and one shared, historical-only forecast."""
import math
import re

from models.Game_Outcome.PPP.PPP import fair_american_odds,implied_probability
from models.season_weights import POLICY_VERSION
from .kalshi import parse_time
from .kalshi_view import matchup,team_identity
from .kalshi_analysis import adapter as kalshi_target,implied as exchange_implied
from .market_view import MARKET_NAMES,_market_probability,selection_text
from .mispricing import run_forecasts
from .prompt_history import exchange_target

PROVIDERS=('FanDuel','Kalshi','Polymarket')


def route(value):
    match=re.fullmatch(r'#(nba|nfl|ncaaf)-market-summary',value or '')
    return match[1].upper() if match else None


def matching_game(game,sport,catalog):
    """Require both teams, a nearby start, and exactly one matching event."""
    wanted={team_identity(game[side]['name'],sport)['name'].casefold() for side in ('away','home')}
    start=parse_time(game.get('start'))
    if len(wanted)!=2 or not start:return None
    found=[]
    for candidate in (catalog or {}).get('games',[]):
        if candidate.get('sport')!=sport:continue
        teams=matchup(candidate);stamp=parse_time(candidate.get('start'))
        if not teams or not stamp or abs((stamp-start).total_seconds())>90*60:continue
        if {team['name'].casefold() for team in teams[0]}==wanted:found.append(candidate)
    return found[0] if len(found)==1 else None


def polymarket_target(snapshot,row):
    kind=row.get('sports_market_type','')
    rules=' '.join(str(row.get(key) or '') for key in ('title','rules_primary','rules_secondary'))
    # Only full-game binary outcomes with unambiguous settlement boundaries.
    if (re.search(r'\b(protected|refund|regulation.only|first half|second half|quarter)\b|50\s*(?:cents|/\s*50)|\$0\.50',rules,re.I)
            or kind in {'team_totals','first_td','last_td'}):return None
    target=exchange_target(snapshot,row,'Polymarket','yes')
    if not target or target.get('note'):return None
    line=target.get('line')
    if not isinstance(line,(int,float)) or not math.isfinite(line):return None
    if kind!='moneyline' and (line*2%2!=1):return None
    if target['market']=='team_total':return None
    spread=target['market']=='alternate_spreads'
    player=target['kind']=='player'
    key=target['market']+('_alternate' if player else '')
    side='Over' if target['compare'] in {'gt','ge'} else 'Under'
    return dict(market_key=key,subject=target['name'] if player or spread else side,
                selection=target['name'] if spread else side,line=-line if spread else line,
                group='Game' if not player else row['category'],market=row['title'],odds=None),False


def comparison(target,invert,side,provider,sport,teams):
    """Identify the same integer-valued outcome, including its push treatment."""
    if not target:return None
    key=target['market_key'].removesuffix('_alternate')
    spread=key=='alternate_spreads';player=key.startswith('player_')
    if not spread and target['selection'] not in {'Over','Under'}:return None
    line=target.get('line')
    if not isinstance(line,(int,float)) or not math.isfinite(line):return None
    subject=team_identity(target['subject'],sport)['name'] if spread else target['subject'] if player else ''
    boundary=-line if spread else line
    compare='gt' if spread or target['selection']=='Over' else 'lt'
    push=provider=='FanDuel' and float(line).is_integer()
    if invert != (side=='no'):compare={'gt':'le','lt':'ge'}[compare]
    if spread and compare in {'lt','le'}:
        other=[team for team in teams if team!=subject]
        if len(other)!=1:return None
        subject=other[0];boundary=-boundary
        compare={'lt':'gt','le':'ge'}[compare]
    # A half-point boundary gives identical winning outcomes for integer stats.
    # Integer sportsbook lines retain their separate refunded equality outcome.
    if not push:
        boundary=(math.floor(boundary)+.5 if compare in {'gt','le'} else math.ceil(boundary)-.5)
        compare='gt' if compare in {'gt','ge'} else 'lt'
    category='Player Props' if player else 'Game Lines'
    market=MARKET_NAMES.get(key+'_alternate',MARKET_NAMES.get(key,(key.replace('_',' ').title(),)))[0]
    group=subject+' - '+market if player else 'Spread / Winner' if spread else 'Game Total'
    label=f'{subject} {-boundary:+g}' if spread else f'{"Over" if compare=="gt" else "Under"} {boundary:g}'
    return dict(key=[category,key,' '.join(subject.casefold().split()),boundary,compare,push],
                category=category,group=group,label=label,line=-boundary if spread else boundary,
                note=f'Push at {line:g}' if push else '')


def entries(bundle):
    game=bundle['game'];sport=bundle['sport'];result=[]
    teams=[team_identity(game[side]['name'],sport)['name'] for side in ('home','away')]
    for provider in PROVIDERS:
        record=bundle.get('providers',{}).get(provider) or {}
        snapshot=record.get('snapshot')
        if not snapshot:continue
        context={**snapshot,'game':record.get('game')}
        for index,row in enumerate(snapshot['rows']):
            if provider=='FanDuel':
                target=dict(row);invert=False;sides=[None]
                if target['market_key'] not in {'alternate_spreads','alternate_totals'} and not target['market_key'].startswith('player_'):target=None
            else:
                adapted=(kalshi_target(row,sport,teams) if provider=='Kalshi' else polymarket_target(context,row))
                target,invert=adapted if adapted else (None,False);sides=['yes','no']
            key=target['market_key'] if target else ''
            player=key.startswith('player_')
            if target:
                market=MARKET_NAMES.get(key,(key.replace('alternate_','').replace('_',' ').title(),))[0]
                group=(target['subject']+' - '+market if player else
                       target['subject']+' - Spread / Winner' if key=='alternate_spreads' else 'Game Total')
                category='Player Props' if player else 'Game Lines'
            else:
                group=row.get('event_title') or row.get('market') or row.get('title') or 'Other markets'
                category='Other Markets'
            for side in sides:
                try:
                    market_implied=(implied_probability(row['odds']) if side is None and row.get('odds') is not None else
                                    exchange_implied(row,side) if side else None)
                except (TypeError,ValueError,OverflowError):market_implied=None
                result.append(dict(provider=provider,index=index,side=side,target=target,invert=invert,
                    comparison=comparison(target,invert,side,provider,sport,teams),
                    category=category,group=group,line=target.get('line') if target else row.get('line'),selection=selection_text(row) if side is None else
                    row['title']+' - '+row[side+'_label'],implied=market_implied,
                    odds=(row.get('odds') if side is None else fair_american_odds(market_implied)) if market_implied is not None else None,
                    ask=row.get(side+'_ask') if side else None,status=row.get('status','Saved quote'),
                    probability=None,model_odds=None,edge=None,method=None,
                    detail='No compatible full-game model for this contract or its settlement rules.' if target is None else 'Calculating the selected method.'))
    return result


def current_models(models,bundle,method,player_method):
    return bool(models and models.get('weighting_policy')==POLICY_VERSION and
                [models.get('bundle_id'),models.get('method'),models.get('player_method')]==[bundle['id'],method,player_method])


def calculate(bundle,method,player_method,game_forecast,player_forecast):
    rows=entries(bundle);game=bundle['game'];sport=bundle['sport']
    stamps=[parse_time(record['snapshot'].get('pulled_at') or record['snapshot'].get('fetched_at') or
                       record['snapshot'].get('metadata_fetched_at')) for record in bundle['providers'].values() if record.get('snapshot')]
    start=parse_time(game.get('start'));stamps=[stamp for stamp in stamps if stamp]
    cutoff=min([start,*stamps]) if start and stamps else None
    unique={}
    for row in rows:
        target=row['target']
        if target:unique.setdefault(tuple(target.get(key) for key in ('market_key','subject','selection','line')),target)
    if not cutoff:
        return dict(bundle_id=bundle['id'],method=method,player_method=player_method,rows=clean(rows),cutoff=None,weighting_policy=POLICY_VERSION,
                    errors=['Timestamped quotes and a game start are required for calculated probabilities.'])
    source=dict(sport=sport,game_id=str(game['id']),home_team=team_identity(game['home']['name'],sport)['name'],
                away_team=team_identity(game['away']['name'],sport)['name'],commence_time=start.isoformat(),
                pulled_at=cutoff.isoformat(),game={'neutral':bool(game.get('neutral',False))},rows=list(unique.values()))
    tasks={}
    if any(not target['market_key'].startswith('player_') for target in unique.values()):
        tasks['game']=lambda:game_forecast(source,method)
    if any(target['market_key'].startswith('player_') for target in unique.values()):
        tasks['player']=lambda:player_forecast(source,player_method)
    forecasts=run_forecasts(tasks,sport) if tasks else {}
    for row in rows:
        target=row['target']
        if not target:continue
        player=target['market_key'].startswith('player_');row['method']=player_method if player else method
        forecast=forecasts.get('player' if player else 'game',(None,None))[0]
        try:
            if forecast and target['market_key']=='alternate_spreads':
                team=forecast.diagnostics.get('team_aliases',{}).get(target['subject']) or (
                    forecast.home_team if target['subject']==source['home_team'] else
                    forecast.away_team if target['subject']==source['away_team'] else None)
                estimate=forecast.spread(team,target['line']) if team else None
            else:estimate=_market_probability(target,forecast)
            probability=(estimate.probability if row['provider']=='FanDuel' else
                         estimate.loss if row['invert'] else estimate.win) if estimate else None
            if row['side']=='no' and probability is not None:probability=1-probability
            if probability is not None and (not math.isfinite(probability) or not 0<=probability<=1):probability=None
        except (TypeError,ValueError,OverflowError):probability=None
        row.update(probability=probability,model_odds=fair_american_odds(probability),
                   edge=probability-row['implied'] if probability is not None and row['implied'] is not None else None,
                   detail='Forecast as of '+cutoff.isoformat() if probability is not None else 'History or this forecast target is unavailable.')
    return dict(bundle_id=bundle['id'],method=method,player_method=player_method,rows=clean(rows),cutoff=cutoff.isoformat(),weighting_policy=POLICY_VERSION,
                errors=[f'{method if kind=="game" else player_method}: {record[1]}' for kind,record in forecasts.items() if record[1]])


def clean(rows):
    return [{key:value for key,value in row.items() if key not in {'target','invert'}} for row in rows]
