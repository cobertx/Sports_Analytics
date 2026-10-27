"""Small, provider-independent context for a market's shared activity panel."""
import json
import re
from dash import html


def reference(snapshot, row, provider='FanDuel', side='yes'):
    """Related outcome identity, without asserting identical refund rules."""
    from .prompt_history import fanduel_target, exchange_target
    from .market_summary import comparison
    from .kalshi_view import team_identity, matchup
    rules = ' '.join(str(row.get(key) or '') for key in ('title','rules_primary','rules_secondary'))
    if re.search(r'\b(?:first|second)\s+half\b|\b[1-4](?:st|nd|rd|th)\s+quarter\b|\bregulation[ -]only\b', rules, re.I):
        return None
    target = fanduel_target(snapshot, row) if provider == 'FanDuel' else exchange_target(snapshot, row, provider, side)
    if not target or target.get('note') or target.get('compare') not in {'gt','ge','lt','le'}:
        return None
    sport = snapshot['sport'];spread = target['market'] == 'alternate_spreads'
    line = target['line'] + (-1 if target['compare']=='ge' else 1 if target['compare']=='le' else 0)
    teams = ([team_identity(snapshot[key], sport)['name'] for key in ('home_team','away_team')] if provider == 'FanDuel'
             else [team['name'] for team in matchup(snapshot['game'])[0]])
    if spread and target['compare'] in {'lt','le'}:
        name = team_identity(target['name'], sport)['name']
        other = next((team for team in teams if team != name), None)
        if not other:
            return None
        target = {**target, 'name':other};line = -line
    adapted = dict(market_key=target['market'].removesuffix('_alternate'), subject=target['name'],
        selection=target['name'] if spread else 'Over' if target['compare'] in {'gt','ge'} else 'Under',
        line=-line if spread else line)
    # comparison treats game spread keys specially, including winner markets.
    if spread:
        adapted['market_key'] = 'alternate_spreads'
    result = comparison(adapted, False, 'yes', 'Kalshi', sport, teams)
    if result and target['market'] == 'team_total':
        result['key'][2] = team_identity(target['name'], sport)['name'].casefold()
    return result['key'] if result else None


def context(snapshot, row, provider='FanDuel', side='yes'):
    # Imports are deferred because market matching also uses the quote views.
    from .market_summary import entries
    from .kalshi_view import matchup
    from .kalshi import parse_time
    if provider == 'FanDuel':
        game = dict(id=str(snapshot.get('game_id', '')), start=snapshot.get('commence_time'),
            home=dict(name=snapshot.get('home_team')), away=dict(name=snapshot.get('away_team')))
    else:
        original = snapshot.get('game') or {}
        teams = matchup(original) if original.get('sport') else None
        if not teams:
            return None
        game = dict(id=str(snapshot.get('game_id', '')), start=original.get('start'),
                    away=dict(name=teams[0][0]['name']), home=dict(name=teams[0][1]['name']))
    sport = snapshot.get('sport')
    if sport not in {'NBA', 'NFL', 'NCAAF'} or not parse_time(game['start']) or not all(game[s]['name'] for s in ('away', 'home')):
        return None
    bundle = dict(sport=sport, game=game, providers={provider:dict(snapshot={**snapshot, 'rows':[row]}, game=snapshot.get('game'))})
    rows = entries(bundle)
    chosen = next((item for item in rows if item['side'] == (None if provider == 'FanDuel' else side)), None)
    return dict(sport=sport, game=game, focus=chosen['comparison']['key'] if chosen and chosen['comparison'] else None,
        reference=reference(snapshot,row,provider,side),
        label=(chosen['group'] + ' · ' + chosen['selection'])[:300] if chosen else str(row.get('title', 'Market'))[:300],
        category=row.get('category') or ('Player Props' if row.get('market_key', '').startswith('player_') else 'Game Lines'),
        provider=provider, ticker=str(row.get('ticker', '')))


def button(snapshot, row, provider='FanDuel', side='yes', *, surface='quotes'):
    try:
        target = context(snapshot, row, provider, side)
    except (KeyError, TypeError, ValueError):
        target = None
    if not target:
        return html.Button('Market Structure', disabled=True, className='market-structure-button',
            title='The teams or scheduled start for this game have not been identified.')
    return html.Button('Market Structure', id=dict(type='market-structure-open',
        context=json.dumps(target, sort_keys=True, separators=(',', ':')), surface=surface), n_clicks=0,
        className='market-structure-button', title='Compare trading activity and liquidity across all market sources')
