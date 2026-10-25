"""Exact market-to-stat prompts and descriptive historical threshold outcomes."""
import math
import re
from html import escape

import pandas as pd
from dash import html

from .historical_links import history_href,target_metric
from .display_time import EASTERN
from models.Player_Stats.Weighted_Historical_Performance.markets import stat_spec

INVERSE={'gt':'le','ge':'lt','lt':'ge','le':'gt','eq':'ne','ne':'eq'}
POLY_STATS={'points':'points','rebounds':'rebounds','assists':'assists','threes':'threes','blocks':'blocks',
    'steals':'steals','turnovers':'turnovers','points_rebounds_assists':'points_rebounds_assists',
    'points_rebounds':'points_rebounds','points_assists':'points_assists','rebounds_assists':'rebounds_assists',
    'passing_yards':'pass_yds','passing_tds':'pass_tds','passing_attempts':'pass_attempts',
    'passing_completions':'pass_completions','passing_interceptions':'pass_interceptions',
    'rushing_yards':'rush_yds','rushing_attempts':'rush_attempts','receiving_yards':'reception_yds',
    'receptions':'receptions','touchdowns':'tds'}


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def fanduel_target(snapshot,row):
    key=row['market_key'];sport=snapshot.get('sport','NFL');player=key.startswith('player_')
    name=row['subject'] if player or key in {'alternate_spreads','spreads','h2h','moneyline'} else snapshot.get('home_team')
    if not name:return None
    target=dict(sport=sport,kind='player' if player else 'team',name=name,market=key)
    line=row.get('line');selection=row.get('selection')
    if key in {'alternate_spreads','spreads'} and finite(line):target.update(line=-line,compare='gt',push=True)
    elif key in {'h2h','moneyline'}:target.update(market='alternate_spreads',line=0,compare='gt',push=False)
    elif key=='player_anytime_td':target.update(line=1,compare='lt' if selection=='No' else 'ge',push=False)
    elif key in {'player_1st_td','player_last_td'}:
        target['note']='Scorer-order history is unavailable; touchdown counts are a reference only. No prompt hit rate is calculated.'
    elif finite(line) and selection in {'Over','Under'} and (key in {'totals','alternate_totals'} or stat_spec(sport,key)):
        target.update(line=line,compare='gt' if selection=='Over' else 'lt',push=True)
    else:return None
    target['prompt']=str(row.get('market',''))+' · '+name+' · '+str(selection)+(f' {line:g}' if finite(line) else '')
    target['origin']=f'#{sport.lower()}-game-'+str(snapshot.get('game_id',''))
    return target


def _teams(snapshot):
    from .kalshi_view import matchup
    game=snapshot.get('game') or {}
    found=matchup(game) if game.get('sport') else None
    return [t['name'] for t in found[0]] if found else []


def _team(name,sport,teams):
    from .kalshi_view import team_identity
    resolved=team_identity(name.strip(),sport)['name']
    return resolved if resolved in teams else None


def exchange_target(snapshot,row,provider,side='yes'):
    sport=snapshot.get('sport');teams=_teams(snapshot)
    if not sport or not teams:return None
    target=dict(sport=sport,kind='team',name=teams[0],market='alternate_totals',push=False)
    if provider=='Kalshi':
        from .kalshi_analysis import NBA_STATS,FOOTBALL_STATS,threshold
        family=row['event_ticker'].split('-',1)[0].removeprefix('KX'+sport)
        label=row.get('yes_label','');title=row.get('title','')
        kind=row.get('strike_type');line=row.get('line')
        compare={'greater':'gt','greater_or_equal':'ge','less':'lt','less_or_equal':'le'}.get(kind)
        if family=='GAME':
            name=_team(label,sport,teams)
            if not name:return None
            target.update(name=name,market='alternate_spreads',line=0,compare='gt')
        else:
            if row.get('market_type') not in {None,'binary'} or row.get('cap_strike') is not None:return None
            if not finite(line):return None
            if not compare:
                parsed=threshold(row)
                if not parsed:return None
                line,selection=parsed;compare='gt' if selection=='Over' else 'lt'
            target.update(line=line,compare=compare)
            if family=='SPREAD':
                name=_team(re.split(r'\s+wins?\s+by\s+',label,flags=re.I)[0],sport,teams)
                if not name:return None
                target.update(name=name,market='alternate_spreads')
            elif family=='TEAMTOTAL':
                name=_team(re.split(r'\s+(?:over|under|scores?)\b',label,flags=re.I)[0],sport,teams)
                if not name:return None
                target.update(name=name,market='team_total')
            elif family!='TOTAL':
                stat=(NBA_STATS if sport=='NBA' else FOOTBALL_STATS).get(family)
                match=re.fullmatch(r'([^:]+):\s*(.+)',row.get('event_title',''))
                if not stat or not match:return None
                target.update(kind='player',name=match[1].strip(),market='player_'+stat)
        if side=='no':target['compare']=INVERSE[target['compare']]
    else:
        kind=row.get('sports_market_type') or row.get('section','').lower().replace(' ','_')
        label=row.get(side+'_label','');line=row.get('line');title=row.get('title','')
        if kind=='moneyline':
            name=_team(label,sport,teams)
            if not name:return None
            target.update(name=name,market='alternate_spreads',line=0,compare='gt')
        elif kind=='spreads':
            name=_team(label,sport,teams)
            # Explicit signed team handicaps establish which side owns the line.
            match=re.fullmatch(r'(.+?)\s*\(([+-]\d+(?:\.\d+)?)\)|(.+?)\s+([+-]\d+(?:\.\d+)?)',title)
            if not name or not match:return None
            primary=_team(match[1] or match[3],sport,teams);handicap=float(match[2] or match[4])
            if not primary:return None
            target.update(name=name,market='alternate_spreads',line=-handicap if name==primary else handicap,compare='gt',push=True)
        else:
            selection=label if label in {'Over','Under'} else None
            if not finite(line):
                match=re.search(r'\b(?:Over|Under|O/U)\s*([+-]?\d+(?:\.\d+)?)\b',title,re.I)
                line=float(match[1]) if match else None
            if not selection or not finite(line):return None
            target.update(line=line,compare='gt' if selection=='Over' else 'lt',push=True)
            if kind=='team_totals':
                name=_team(title.split(':',1)[0],sport,teams)
                if not name:return None
                target.update(name=name,market='team_total')
            elif kind!='totals':
                stat=POLY_STATS.get(kind);name=title.split(':',1)[0].strip() if ':' in title else None
                if not stat or not name:return None
                target.update(kind='player',name=name,market='player_'+stat)
    if target['kind']=='player' and not stat_spec(sport,target['market']):return None
    target['prompt']=row['title']+' · '+(side.title() if provider=='Kalshi' else row[side+'_label'])
    target['origin']=f'#{sport.lower()}-{provider.lower()}-game-'+str(snapshot['game_id'])
    return target


def button(snapshot,row,provider='FanDuel',side='yes'):
    target=fanduel_target(snapshot,row) if provider=='FanDuel' else exchange_target(snapshot,row,provider,side)
    if not target:
        return html.Button('Historical Data',className='market-history-button',disabled=True,
                           title='An exact historical statistic for this prompt is not connected.')
    parameters={key:target[key] for key in ('line','compare','prompt','origin','note') if key in target}
    if 'line' in target:parameters['push']='1' if target['push'] else '0'
    stamp=snapshot.get('commence_time') or (snapshot.get('game') or {}).get('start')
    if stamp:parameters['as_of']=stamp
    return html.A('Historical Data',href=history_href(target['sport'],target['kind'],target['name'],target['market'],snapshot,prompt=parameters),
                  className='market-history-button market-history-link',title='Show recorded history, this threshold and historical hits',
                  **{'aria-label':'Historical Data — '+target['prompt']})


def prior_history(frame,target):
    if not target or not target.get('as_of'):return frame
    cutoff=pd.Timestamp(target['as_of'])
    dates=pd.to_datetime(frame['event_date'],utc=True,errors='coerce')
    # NBA boxes often carry a date at midnight UTC, not the actual tipoff.
    # Exclude the linked game date so a reopened archive cannot count its result.
    days=dates.dt.tz_convert(EASTERN).dt.date.where(dates.ne(dates.dt.normalize()),dates.dt.date)
    mask=dates.notna() & days.lt(cutoff.tz_convert(EASTERN).date())
    if 'available_at' in frame:mask &= pd.to_datetime(frame['available_at'],utc=True,errors='coerce').lt(cutoff)
    return frame[mask].copy()


def outcomes(history,metric,target):
    if not target or 'line' not in target or target_metric(target,[metric],history)!=metric:return None
    values=pd.to_numeric(history[metric],errors='coerce');line=target['line'];comparison=target['compare']
    hit={'gt':values.gt,'ge':values.ge,'lt':values.lt,'le':values.le,'eq':values.eq,'ne':values.ne}[comparison](line)
    equal=(values-line).abs().le(1e-9)
    if comparison in {'gt','lt','ne'}:hit &= ~equal
    if comparison in {'ge','le','eq'}:hit |= equal
    result=pd.Series('Miss',index=history.index,dtype=object)
    result[hit]='Hit'
    if target.get('push'):result[equal]='Push'
    result[values.isna() | ~values.map(lambda value:math.isfinite(value))]=None
    return result


def annotate(figure,history,metric,target,result):
    colors={'Hit':'#57dbc4','Miss':'#e77c85','Push':'#a6b2c5'}
    figure.data[0].marker.color=[colors.get(value,'#586577') for value in result]
    hits=int(result.eq('Hit').sum());count=int(result.notna().sum());pushes=int(result.eq('Push').sum())
    symbols={'gt':'>','ge':'≥','lt':'<','le':'≤','eq':'=','ne':'≠'}
    label=f"Prompt {symbols[target['compare']]} {target['line']:g}"
    figure.add_hline(y=target['line'],line_dash='dash',line_color='#f6c667',annotation_text=label,annotation_position='top right')
    bounds=[0,target['line'],*[value for value in history[metric] if finite(value)]]
    low,high=min(bounds),max(bounds);padding=max(1,(high-low)*.08)
    figure.update_yaxes(range=[low-padding,high+padding])
    figure.add_annotation(x=0,y=1.15,xref='paper',yref='paper',showarrow=False,xanchor='left',
        text=f"{hits}/{count} historical hits ({100*hits/count:.1f}%) · {int(result.eq('Miss').sum())} misses · {pushes} pushes" if count else 'No recorded prompt outcomes')
    figure.update_layout(margin={'t':80},title={'text':escape(target.get('prompt','Market prompt')),'font':{'size':13}})
    return hits,count,pushes
