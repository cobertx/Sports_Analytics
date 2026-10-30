"""Per-prompt method assignments shared by sportsbook and exchange scans."""
import json
from collections import Counter

from dash import dcc,html

from .market_view import MARKET_NAMES

INHERIT='__default__'
GAME_METHODS=('PPP','EPA','ELO')
PLAYER_METHODS=('WHP','UO','OAM','PGE')


def scope(prefix,sport,game_id):
    return json.dumps([prefix,sport,str(game_id)],separators=(',',':'))


def prompt_key(row):
    key=row['market_key'].removesuffix('_alternate')
    if key=='alternate_spreads':key='winner' if row.get('line')==0 else 'spread'
    elif key=='alternate_totals':key='total'
    subject=row.get('subject','') if key.startswith('player_') or key in {'winner','spread'} else ''
    return json.dumps([key,subject],ensure_ascii=False,separators=(',',':'))


def catalog(rows):
    prompts={}
    for row in rows:
        key=prompt_key(row)
        if key in prompts:continue
        kind,subject=json.loads(key)
        label=({'winner':'Game winner','spread':'Spread','total':'Total points'}.get(kind)
               or MARKET_NAMES.get(row['market_key'],MARKET_NAMES.get(kind+'_alternate',(row.get('market') or kind,)))[0])
        prompts[key]=dict(label=(subject+' — ' if subject else '')+label,player=kind.startswith('player_'))
    return dict(sorted(prompts.items(),key=lambda item:(item[1]['player'],item[1]['label'].casefold())))


def controls(prefix,rows,current_scope):
    prompts=catalog(rows)
    if not prompts:return html.P('No saved prompts are available for this game.',className='saved-market-empty')
    return html.Div([html.Div([
        html.Label(prompt['label']),
        dcc.Dropdown(id=dict(type=prefix+'-quick-prompt-method',scope=current_scope,prompt=key),
            options=[dict(label='Use default methods',value=INHERIT),
                     *[dict(label=method,value=method) for method in (PLAYER_METHODS if prompt['player'] else GAME_METHODS)]],
            value=[INHERIT],multi=True,clearable=True,placeholder='Skipped — choose methods to include',
            persistence=True,persistence_type='session'),
    ],className='field quick-prompt-method') for key,prompt in prompts.items()],className='quick-prompt-grid')


def choices(values,identities,current_scope):
    if values is None and identities is None:return {}
    if not isinstance(values,list) or not isinstance(identities,list) or len(values)!=len(identities):
        raise ValueError('Wait for the prompt method choices to finish loading.')
    result={}
    for value,identity in zip(values,identities):
        if identity.get('scope')!=current_scope:
            raise ValueError('Prompt choices belong to another game. Wait for this game to finish loading.')
        key=identity['prompt']
        try:kind,subject=json.loads(key)
        except (ValueError,TypeError):raise ValueError('Choose a valid game or player prompt.') from None
        if not isinstance(kind,str) or not isinstance(subject,str):raise ValueError('Choose a valid game or player prompt.')
        supported=PLAYER_METHODS if kind.startswith('player_') else GAME_METHODS
        if not isinstance(value,list) or any(method not in (*supported,INHERIT) for method in value):
            raise ValueError('Choose supported methods for each game or player prompt.')
        methods=[method for method in supported if method in value]
        if methods or INHERIT not in value:result[key]=methods  # Empty explicitly skips the prompt.
    return dict(sorted(result.items()))


def plan(rows,game_methods,player_methods,overrides=None):
    return {key:list((overrides or {}).get(key,player_methods if entry['player'] else game_methods))
            for key,entry in catalog(rows).items()}


def consensus(rankings,assignments,rows,criteria,selection_key,combine,*,prompt=prompt_key,sides=1):
    """Reuse each fitted method; intersect only the methods assigned to each prompt."""
    counts=Counter(prompt(row) for row in rows)
    indexed={method:{} for method in rankings}
    for method,ranked in rankings.items():
        for item in ranked['matches']:indexed[method].setdefault(prompt(item['row']),[]).append(item)
    groups=[];excluded=0
    for key,methods in assignments.items():
        total=counts[key]*sides
        if not methods:excluded+=total;continue
        group=[]
        for method in methods:
            matches=indexed.get(method,{}).get(key,[])
            group.append((method,dict(matches=matches,evaluated=len(matches),unavailable=max(0,total-len(matches)))))
        groups.append(group)
    return {**combine(groups,criteria,selection_key),'excluded':excluded}
