"""Translate supported binary contracts to existing, historical-only forecasts."""
import hashlib
import json
import math
import re
from models.season_weights import POLICY_VERSION

from .data import DataSourceError
from .kalshi import parse_time
from .market_view import criteria_match
from .mispricing import GAME_METHODS, PLAYER_METHODS, ALL_PROBABILITIES, consensus_rankings, run_forecasts
from .availability import revision as availability_revision
from .availability_adjustment import note as availability_note
from . import quick_methods

FILTER = 'Mispricing Filter'
NBA_STATS = {'PTS':'points','REB':'rebounds','AST':'assists','3PT':'threes','STL':'steals',
             'BLK':'blocks','PRA':'points_rebounds_assists','PR':'points_rebounds',
             'PA':'points_assists','RA':'rebounds_assists'}
FOOTBALL_STATS = {'PASSYDS':'pass_yds','PASSATT':'pass_attempts','PASSCOMP':'pass_completions',
                  'PASSTD':'pass_tds','PASSINT':'pass_interceptions','RUSHYDS':'rush_yds',
                  'RSHYDS':'rush_yds','RSHATT':'rush_attempts','RUSHATT':'rush_attempts',
                  'RECYDS':'reception_yds','REC':'receptions','RECEPTIONS':'receptions',
                  'RRYDS':'rush_yds','TD':'tds'}


def definition(row):
    fields=('ticker','event_ticker','event_title','title','yes_label','line','strike_type',
            'cap_strike','market_type','rules_primary','rules_secondary')
    return hashlib.sha256(json.dumps([row.get(key) for key in fields],sort_keys=True).encode()).hexdigest()


def identity(game, selected, method, player_method):
    return [game['sport'],game['id'],game.get('start'),game.get('matchup_title') or game.get('title'),
            game.get('teams'),bool(game.get('neutral',False)),POLICY_VERSION,availability_revision(game['sport']),selected,method,player_method]


def implied(row, side):
    if row.get('market_type') not in {None,'binary'}:return None
    ask,notional=row.get(side+'_ask'),row.get('notional')
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (ask,notional)):
        return None
    return ask/notional if notional>0 and 0<=ask<=notional else None


def model_snapshot(game, snapshot, cutoff=None):
    # Match only explicit away/home labels; a "vs" title does not establish venue.
    from .kalshi_view import matchup
    found=matchup(game)
    if not found or found[1]!=['Away','Home']:
        raise DataSourceError('The provider has not identified away/home teams for this game.')
    away,home=found[0]
    start,stamp=parse_time(game.get('start')),parse_time(cutoff or snapshot.get('fetched_at'))
    if not start or not stamp:raise DataSourceError('A timestamped game start and quote are required for forecasts.')
    # Never create a pregame estimate using history after kickoff.
    stamp=min(start,stamp)
    return dict(sport=game['sport'],game_id=game['id'],home_team=home['name'],away_team=away['name'],
                commence_time=start.isoformat(),pulled_at=stamp.isoformat(),rows=[],
                game={'neutral':bool(game.get('neutral',False))})


def threshold(row):
    line=row.get('line');kind=row.get('strike_type')
    if not isinstance(line,(int,float)) or not math.isfinite(line):return None
    if kind not in {'greater','greater_or_equal','less','less_or_equal'}:
        if kind:return None
        # Older saved snapshots did not preserve strike_type. Only explicit wording qualifies.
        label=row.get('yes_label','')+' '+row.get('title','')
        if re.search(r'\b(over|more than)\b',label,re.I):kind='greater'
        elif re.search(r'\b(at least)\b|\d\s*\+',label,re.I):kind='greater_or_equal'
        else:return None
    # Observed stats are integers. Half-integer boundaries retain equality mass
    # on its actual binary side rather than treating it as a sportsbook push.
    if kind=='greater':return math.floor(line)+.5,'Over'
    if kind=='greater_or_equal':return math.ceil(line)-.5,'Over'
    if kind=='less':return math.ceil(line)-.5,'Under'
    return math.floor(line)+.5,'Under'


def adapter(row, sport, teams):
    if row.get('market_type') not in {None,'binary'} or row.get('cap_strike') is not None:
        return None
    rules=row.get('rules_primary','')+' '+row.get('rules_secondary','')
    if re.search(r'\b(ties?|draws?)\b.{0,70}(\$0?\.50?|50\s*(cents|¢)|fair price|refund)|\b(protected|refund|regulation.only)\b',rules,re.I):
        return None
    family=row['event_ticker'].split('-',1)[0].removeprefix('KX'+sport)
    label=row.get('yes_label','');title=row.get('title','')
    base=dict(group='Game',market=title,odds=None)
    if family in {'GAME','SPREAD'}:
        from .kalshi_view import team_identity
        name=label if family=='GAME' else re.split(r'\s+wins?\s+by\s+',label,flags=re.I)[0]
        team=team_identity(name,sport)['name']
        if team not in teams:return None
        if family=='GAME':
            if not re.search(r'\bwins?\b',title,re.I):return None
            return dict(base,market_key='alternate_spreads',subject=team,selection=team,line=0),False
        strike=threshold(row)
        if not strike:return None
        line,side=strike
        return dict(base,market_key='alternate_spreads',subject=team,selection=team,line=-line),side=='Under'
    strike=threshold(row)
    if not strike:return None
    line,side=strike
    if family=='TOTAL':return dict(base,market_key='alternate_totals',subject=side,selection=side,line=line),False
    stat=(NBA_STATS if sport=='NBA' else FOOTBALL_STATS).get(family)
    if not stat:return None
    # Provider event titles identify the player; never infer names from ticker initials.
    event=row.get('event_title','')
    match=re.fullmatch(r'([^:]+):\s*(.+)',event)
    if not match:return None
    subject=match[1].strip()
    if len(subject.split())<2 or re.search(r'\b(vs|at|@)\b',subject,re.I):return None
    # Exclude conditional/period targets which the full-game player models do not cover.
    if re.search(r'\b(first|last|quarter|half|combined|each|double.double|triple.double)\b',event+' '+title,re.I):return None
    return dict(base,group=row['category'],market_key='player_'+stat+'_alternate',subject=subject,selection=side,line=line),False


def calculate(game, snapshot, selected, method, player_method, game_forecast, player_forecast, previous=None):
    context=identity(game,selected,method,player_method)
    old=previous if previous and previous.get('context')==context else {}
    records=dict(old.get('predictions',{}))
    pending=[row for row in snapshot['rows'] if records.get(row['ticker'],{}).get('definition')!=definition(row)]
    if not pending:return old
    errors=[]
    try:source=model_snapshot(game,snapshot,old.get('cutoff'))
    except DataSourceError as exc:
        return dict(context=context,predictions={},errors=[str(exc)],cutoff=None)
    mapped={row['ticker']:adapter(row,game['sport'],[source['home_team'],source['away_team']]) for row in snapshot['rows']}
    source['rows']=[item[0] for item in mapped.values() if item]
    game_rows=[row for row in source['rows'] if not row['market_key'].startswith('player_')]
    player_rows=[row for row in source['rows'] if row['market_key'].startswith('player_')]
    gf=pf=None;history=dict(old.get('history',{}))
    if game_rows:
        gf,error=game_forecast({**source,'rows':game_rows},method)
        if error:errors.append(f'{method}: {error}')
        history['game']=gf.diagnostics.get('last_game_date') if gf else None
    if player_rows:
        pf,error,record=player_forecast({**source,'rows':player_rows},player_method)
        if error:errors.append(f'{player_method}: {error}')
        history['player']=record.get('latest_date')
    message=availability_note(gf,pf)
    if message:errors.append(message)
    for row in pending:
        item=mapped[row['ticker']];probability=None
        detail='This contract is not supported by the selected full-game models.'
        model=method
        if item:
            target,invert=item;player=target['market_key'].startswith('player_');model=player_method if player else method
            try:
                if player:
                    estimate=pf.selection(target) if pf else None
                    detail=pf.detail(target) if pf and hasattr(pf,'detail') else 'Player history or this forecast target is unavailable.'
                else:
                    estimate=(gf.total(target['line'],target['selection']) if target['market_key']=='alternate_totals' else
                              gf.spread(gf.diagnostics.get('team_aliases',{}).get(target['subject']) or
                                        (gf.home_team if target['subject']==source['home_team'] else gf.away_team),target['line'])) if gf else None
                    detail=f'{method} full-game forecast; historical cutoff {source["pulled_at"]}.'
                if estimate is not None:probability=estimate.loss if invert else estimate.win
                if probability is not None and not (math.isfinite(probability) and 0<=probability<=1):probability=None
            except (ValueError,TypeError,OverflowError) as exc:detail=str(exc)
        records[row['ticker']]=dict(definition=definition(row),probability=probability,method=model,detail=detail)
    return dict(context=identity(game,selected,method,player_method),predictions=records,errors=list(dict.fromkeys([*old.get('errors',[]),*errors])),
                history=history,cutoff=source['pulled_at'])


def decorate(snapshot, game, selected, method, player_method, models=None, criteria=None):
    current=models if models and models.get('context')==identity(game,selected,method,player_method) else {}
    rows=[]
    for original in snapshot['rows']:
        row=dict(original);record=current.get('predictions',{}).get(row['ticker'],{})
        probability=record.get('probability') if record.get('definition')==definition(row) else None
        row.update(model_method=record.get('method',method),model_detail=record.get('detail','Calculating or unavailable for this contract.'))
        for side,p in [('yes',probability),('no',1-probability if probability is not None else None)]:
            market=implied(row,side);edge=p-market if p is not None and market is not None else None
            row.update({side+'_implied':market,side+'_probability':p,side+'_edge':edge,
                        side+'_match':row.get('status') in {'open','active'} and criteria_match(p,edge,criteria)})
        rows.append(row)
    return {**snapshot,'rows':rows}


def rank(snapshot, criteria):
    matches=[];unavailable=evaluated=0
    seen=set()
    for row in snapshot['rows']:
        for side in ('yes','no'):
            key=(row['ticker'],side)
            if key in seen:continue
            seen.add(key)
            p,market,edge=(row.get(side+'_'+field) for field in ('probability','implied','edge'))
            if p is None or market is None or row.get('status') not in {'active','open'}:
                unavailable+=1;continue
            evaluated+=1
            if criteria_match(p,edge,criteria):matches.append(dict(row=row,side=side,probability=p,implied=market,edge=edge,method=row['model_method']))
    matches.sort(key=lambda item:(-item['edge'],-item['probability'],item['row']['ticker'],item['side']))
    return dict(matches=matches,evaluated=evaluated,unavailable=unavailable)


def prompt_targets(game,snapshot):
    source=model_snapshot(game,snapshot)
    targets={}
    for row in snapshot['rows']:
        target=adapter(row,game['sport'],[source['home_team'],source['away_team']])
        targets[row['ticker']]=target[0] if target else dict(market_key='unsupported',market='Unsupported contracts')
    return targets


def quick_calculate(game, snapshot, selected, methods, player_methods, criteria, game_forecast, player_forecast,prompt_methods=None):
    """Fit each selected method once, with the same quotes and historical cutoff."""
    try:
        targets=prompt_targets(game,snapshot)
    except DataSourceError as exc:
        return dict(matches=[],evaluated=0,unavailable=2*len(snapshot['rows']),consensus=True), [str(exc)]
    groups={False:[],True:[]}
    for row in snapshot['rows']:
        groups[targets[row['ticker']]['market_key'].startswith('player_')].append(row)
    assignments=quick_methods.plan(targets.values(),methods,player_methods,prompt_methods)
    needed={method for choices in assignments.values() for method in choices}
    def evaluate(method, player):
        subset={**snapshot,'rows':groups[player]}
        models=calculate(game,subset,selected,methods[0] if player else method,
                         method if player else player_methods[0],game_forecast,player_forecast)
        displayed=decorate(subset,game,selected,methods[0] if player else method,
                           method if player else player_methods[0],models)
        return rank(displayed,ALL_PROBABILITIES),models.get('errors',[])
    tasks={method:lambda method=method:evaluate(method,False) for method in GAME_METHODS if method in needed}
    tasks.update({method:lambda method=method:evaluate(method,True) for method in PLAYER_METHODS if method in needed})
    forecasts=run_forecasts(tasks,game['sport'])
    ranked=quick_methods.consensus({method:record[0] for method,record in forecasts.items()},assignments,
        snapshot['rows'],criteria,lambda item:(item['row']['ticker'],item['side']),consensus_rankings,
        prompt=lambda row:quick_methods.prompt_key(targets[row['ticker']]),sides=2)
    errors=list(dict.fromkeys(error for _, messages in forecasts.values() for error in messages))
    return ranked,errors
