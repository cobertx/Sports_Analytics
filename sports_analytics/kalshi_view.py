"""Kalshi game cards and exchange quotes, kept separate from sportsbook prices."""
import re
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from dash import dash_table, dcc, html
from dash.dash_table.Format import Format, Scheme, Symbol

from .kalshi import CATEGORIES, DOCS, KalshiService, category, ticker
from .display_time import format_eastern
from .market_view import logo_badge, NBA_LOGO_FILES, NFL_LOGO_FILES, NCAAF_LOGO_FILES
from .nba_markets import NBA_TEAM_CODES
from .nfl_markets import NFL_TEAM_CODES
from .mispricing import quick_controls
from .kalshi_analysis import FILTER, implied
from .market_view import probability_text
from models.Game_Outcome.PPP.PPP import fair_american_odds
from .prompt_history import button as history_button
from .market_structure_link import button as structure_button
from .loading import indicator

MULTIWORD_MASCOTS = {'Crimson Tide', 'Sun Devils', 'Red Wolves', 'Black Knights', 'Golden Bears',
    'Blue Devils', 'Yellow Jackets', 'Fighting Illini', "Ragin' Cajuns", 'Golden Gophers',
    'Wolf Pack', 'Tar Heels', 'Mean Green', 'Fighting Irish', 'Nittany Lions', 'Scarlet Knights',
    'Horned Frogs', 'Red Raiders', 'Green Wave', 'Golden Hurricane', 'Demon Deacons', 'Trail Blazers'}


def normalize_team(value):
    return re.sub(r'[^a-z0-9]','',value.casefold())


@lru_cache(maxsize=3)
def team_aliases(sport):
    """Index static logo aliases once, retaining every ambiguous match."""
    files = {'NBA': NBA_LOGO_FILES, 'NFL': NFL_LOGO_FILES, 'NCAAF': NCAAF_LOGO_FILES}.get(sport, {})
    codes = {'NBA': NBA_TEAM_CODES, 'NFL': NFL_TEAM_CODES}.get(sport, {})
    aliases_by_name={}
    for canonical in sorted({Path(filename).stem for filename in files.values()}):
        mascot = next((value for value in MULTIWORD_MASCOTS if canonical.endswith(' '+value)), canonical.split()[-1])
        city = canonical[:-(len(mascot)+1)]
        code = codes.get(canonical, '')
        aliases = {canonical, code, city, mascot, f'{code} {mascot}',
                   f'{"".join(word[0] for word in city.split())} {mascot}',city.replace(' State',' St')} - {''}
        for alias in {normalize_team(alias) for alias in aliases}:
            aliases_by_name.setdefault(alias,[]).append((canonical,code))
    return aliases_by_name


@lru_cache(maxsize=256)
def team_identity(name, sport):
    """Resolve exact local name/code aliases, leaving ambiguous cities unbranded."""
    matches=team_aliases(sport).get(normalize_team(name),[])
    if len(matches) == 1:
        canonical, code = matches[0]
        return dict(name=canonical, abbreviation=code or ''.join(word[0] for word in canonical.split())[:4])
    return dict(name=name, abbreviation=''.join(word[0] for word in name.split())[:4].upper())


def matchup(game):
    fallback = None
    files = {'NBA': NBA_LOGO_FILES, 'NFL': NFL_LOGO_FILES, 'NCAAF': NCAAF_LOGO_FILES}.get(game['sport'], {})
    explicit = {t.get('ordering'):t for t in game.get('teams',[]) if isinstance(t,dict)}
    if {'away','home'} <= explicit.keys():
        teams=[]
        for side in ['away','home']:
            original=explicit[side]
            alias,name=original.get('alias') or '',original.get('name') or ''
            candidates=[alias,alias+' '+name,original.get('abbreviation') or '']
            if name.casefold() in files or game['sport']!='NCAAF':candidates.append(name)
            resolved=[team_identity(value.strip(),game['sport']) for value in candidates if value.strip()]
            team=next((t for t in resolved if t['name'].casefold() in files),
                      dict(name=alias or name,abbreviation=(original.get('abbreviation') or side).upper()))
            teams.append({**team,'logo_url':original.get('logo')})
        return teams,['Away','Home']
    for title in dict.fromkeys([game.get('matchup_title'), game.get('title')]):
        if not title: continue
        parts = re.split(r'\s+(at|@|vs\.?|versus)\s+', title, maxsplit=1, flags=re.I)
        if len(parts) != 3: continue
        teams = [team_identity(value.strip(), game['sport']) for value in (parts[0], parts[2])]
        labels = ['Away', 'Home'] if parts[1].lower() in {'at', '@'} else ['Matchup', 'Matchup']
        result = teams, labels
        if all(team['name'].casefold() in files for team in teams):
            return result
        fallback = fallback or result
    return fallback


def team_badge(team, sport):
    badge=logo_badge(team['name'],team['abbreviation'],sport)
    url=team.get('logo_url') or ''
    parsed=urlparse(url)
    if badge.className=='team-badge' and parsed.scheme=='https' and parsed.hostname in {
            'polymarket-upload.s3.us-east-2.amazonaws.com','images.polymarket.com'}:
        return html.Span(html.Img(src=url,alt=team['name']+' logo',className='team-logo'),className='team-badge team-badge-logo')
    return badge


def route(value):
    match = re.fullmatch(r'#(nba|nfl|ncaaf)-kalshi-(market|game-(.+))', value or '')
    if not match: return None
    sport = match[1].upper()
    identity = match[3]
    if identity and (not ticker(identity) or not identity.startswith(f'KX{sport}GAME-')): return None
    return sport, identity


def href(sport, game_id=None):
    return f'#{sport.lower()}-kalshi-' + ('game-' + game_id if game_id else 'market')


def field(label, control):
    return html.Div([html.Label(label, htmlFor=control.id), control], className='field')


def layout(provider='Kalshi'):
    prefix = provider.lower()
    polymarket = provider == 'Polymarket'
    sides = ('Outcome 1', 'Outcome 2') if polymarket else ('Yes', 'No')
    cents = Format(precision=2, scheme=Scheme.fixed, symbol=Symbol.yes, symbol_suffix='¢', symbol_prefix='')
    count = Format(precision=2, scheme=Scheme.fixed, group=True)
    columns = [dict(name='Event / market', id='event_title'), dict(name='Contract', id='title'),
               dict(name=sides[0], id='yes_label'), dict(name=sides[1], id='no_label')]
    columns += [dict(name=name, id=key, type='numeric', format=cents) for name, key in
                [(sides[0]+' bid', 'yes_bid'), (sides[0]+' ask', 'yes_ask'), (sides[1]+' bid', 'no_bid'),
                 (sides[1]+' ask', 'no_ask'), ('Last '+sides[0]+' trade', 'last_price')]]
    columns += [dict(name=name, id=key, type='numeric', format=count) for name, key in
                [('Volume (USD)' if polymarket else 'Contracts traded', 'volume'), ('24h volume', 'volume_24h'), ('Open interest', 'open_interest')]
                if not polymarket or key!='open_interest']
    columns += [dict(name='Status', id='status'), dict(name='Market ID' if polymarket else 'Ticker', id='ticker'), dict(name='Quote fetched (UTC)', id='quote_fetched_at')]
    if not polymarket:
        percent=Format(precision=2,scheme=Scheme.percentage)
        columns[4:4]=[dict(name=side.title()+' '+label,id=side+'_'+key,type='numeric',format=percent)
                      for side in ('yes','no') for label,key in [('market implied','implied'),
                          ('calculated probability','probability'),('edge','edge')]]
    return html.Section([
        dcc.Store(id=f'{prefix}-games'), dcc.Store(id=f'{prefix}-context'), dcc.Store(id=f'{prefix}-quotes'),
        dcc.Store(id=f'{prefix}-rules-signature'),
        *([dcc.Store(id='kalshi-models'),dcc.Store(id='kalshi-applied-criteria'),
           dcc.Store(id='kalshi-filter-quotes'), dcc.Store(id='kalshi-quick-scan')] if not polymarket else []),
        dcc.Interval(id=f'{prefix}-list-timer', interval=60000, disabled=True),
        dcc.Interval(id=f'{prefix}-quote-timer', interval=5000, disabled=True),
        dcc.Store(id=f'{prefix}-cache-state'),dcc.Interval(id=f'{prefix}-cache-poll',interval=1500,disabled=True),
        html.Section([
            html.Div([html.Div([html.Div('PUBLIC EXCHANGE DATA', className='eyebrow accent'),
                html.H2(provider+' Market'), html.P(f'Browse game contracts and pull current outcome quotes directly from {provider}.')]),
                html.A(f'Open {provider} ↗', href='https://polymarket.com/sports' if polymarket else 'https://kalshi.com/sports', target='_blank', rel='noopener noreferrer', className='market-link')],
                className='market-heading'),
            html.P('Public REST connection · No API key or paid odds subscription needed. '
                   'Game discovery refreshes every minute while this page is open. '
                   'Quotes refresh only for the selected game category; timestamps show when they were fetched.', className='kalshi-connection-note'),
        ], className='panel'),
        html.Div([
            html.Div([field('Game window', dcc.Dropdown(id=f'{prefix}-days', options=[dict(label='Today / next 24 hours', value=1),
                dict(label='Next 7 days', value=7), dict(label='Next 14 days', value=14)], value=7, clearable=False)),
                html.Button('Refresh games', id=f'{prefix}-list-refresh', n_clicks=0, className='export-button')], className='market-controls'),
            dcc.Loading(html.Div(id=f'{prefix}-list-status', role='status', className='load-status'),
                        color='#57dbc4',delay_show=500,overlay_style={'visibility':'visible'}),
            html.Div(id=f'{prefix}-game-cards'),
        ], id=f'{prefix}-list'),
        html.Section([
            html.Div([html.A(f'← Back to {provider} games', id=f'{prefix}-back', className='back-to-games'),
                html.Button('Pull Live Markets / Save snapshot', id=f'{prefix}-pull', n_clicks=0, className='pull-markets-button')],
                className='detail-page-actions'),
            html.Div(id=f'{prefix}-game-header'),
            html.Div([
                field('View', dcc.Dropdown(id=f'{prefix}-snapshot', options=[dict(label='Live quotes', value='live')], value='live', clearable=False)),
                field('Quote updates', dcc.Dropdown(id=f'{prefix}-frequency', options=[dict(label='Every 2 seconds', value=2),
                    dict(label='Every 5 seconds', value=5), dict(label='Every 10 seconds', value=10),
                    dict(label='Every 30 seconds', value=30), dict(label='Paused', value=0)], value=5, clearable=False)),
            ], className='kalshi-detail-controls'),
            dcc.Tabs(id=f'{prefix}-category', value='Game Lines', children=[], className='market-category-tabs'),
            filter_panel() if not polymarket else None,
            html.Div(id=f'{prefix}-quote-status', className='load-status', role='status'),
            html.Div(id='kalshi-model-status',className='forecast-history',role='status') if not polymarket else None,
            html.P('Market odds use American format, converted from each outcome’s quoted buying price divided by its payout. '
                   'Market implied probability uses that same price; each outcome has its own quote. '
                   'Raw bids, asks and last trades in cents are available in the detailed quote table. '
                   'Quotes are before fees and subject to available liquidity. Read each contract’s settlement rules.', className='kalshi-connection-note'),
            html.P('Market implied = ask price / contract payout. Edge = calculated probability minus market implied, before fees. '
                   'Yes and No have their own buying costs. Estimates describe full-game outcomes; live game state is not modeled.',
                   className='ppp-comparison-note') if not polymarket else None,
            html.Div(indicator('Loading the selected markets…'),className='exchange-sections-transition'),
            html.Div(id=f'{prefix}-market-sections'),
            html.Details([html.Summary('Detailed quote table & CSV export'),
              dash_table.DataTable(id=f'{prefix}-table', columns=columns, data=[], page_size=20, sort_action='native',
                filter_action='native', export_format='csv', tooltip_duration=None,
                style_table={'overflowX': 'auto'},
                style_cell={'backgroundColor': '#111d2e', 'color': '#b8c8dc', 'border': '1px solid #243249',
                            'padding': '11px', 'fontFamily': 'Segoe UI', 'fontSize': '12px', 'textAlign': 'left',
                            'minWidth': '95px', 'maxWidth': '290px', 'whiteSpace': 'normal', 'height': 'auto'},
                style_header={'backgroundColor': '#17253a', 'fontWeight': '600'})],
                id=f'{prefix}-detail-table',open=False,className='panel kalshi-detail-table'),
            html.Details([html.Summary('Contract rules, payout & settlement'), html.Div(id=f'{prefix}-rules')],
                         id=f'{prefix}-rules-panel',open=False,className='panel kalshi-rules'),
        ], id=f'{prefix}-detail', style={'display': 'none'}, className='sportsbook-view kalshi-sportsbook'),
        html.Details([html.Summary('API connection & update speed'),
            html.P([f'This page uses {provider}’s ', html.A('public market-data REST API',
                href='https://docs.polymarket.com/market-data/discover-markets' if polymarket else DOCS, target='_blank', rel='noopener noreferrer'),
                f'. No {provider} account, key or The Odds API credits are used to read these endpoints. '
                'The default 5-second interval is polling, not a continuous stream. You can select 2 seconds, '
                'pause updates or save a snapshot. Polling stops when you leave the Kalshi page or view a saved snapshot.']),
            html.P(['Polymarket discovery uses Gamma; quotes use public batched CLOB order books. '
                    'Gamma reference probabilities are not substituted for missing order-book prices. For streaming data, see ',
                    html.A('real-time data',href='https://docs.polymarket.com/market-data/realtime-data',target='_blank',rel='noopener noreferrer')]) if polymarket else
            html.P(['For streaming ticker/order-book updates, Kalshi requires a signed WebSocket handshake: '
                'a Kalshi API key ID and an RSA private key kept on the server. See ',
                html.A('WebSocket setup', href='https://docs.kalshi.com/getting_started/quick_start_websockets',
                       target='_blank', rel='noopener noreferrer'), '. Public polling works immediately without that setup.']),
            html.P(f'Only provider-linked game events are grouped together. Markets absent from {provider} are not synthesized. '
                   f'{provider} contract rules may differ from sportsbook settlement rules. Saved snapshots include full rules in JSON/CSV.')
        ], className='panel kalshi-api-info'),
    ], id=f'{prefix}-page', style={'display': 'none'}, className='kalshi-page')


def cards(catalog, provider='Kalshi', game_href=href):
    groups = {}
    for game in catalog.get('games', []):
        day = format_eastern(game['start']).split(' ')[0] if game.get('start') else 'Start time unavailable'
        groups.setdefault(day, []).append(game)
    content = []
    for day, games in groups.items():
        content.append(html.H3(day, className='game-date-heading'))
        content.append(html.Div([game_card(game,provider,game_href) for game in games], className='game-grid'))
    return content or html.P(f'No open {provider} game events in this window. Try 14 days or refresh the list.', className='saved-market-empty')


def game_card(game, provider='Kalshi', game_href=href):
    teams = matchup(game)
    rows = []
    if teams:
        for index, (team, label) in enumerate(zip(*teams)):
            if index: rows.append(html.Div('@' if label=='Home' else 'vs', className='game-at'))
            rows.append(html.Div([team_badge(team,game['sport']),
                html.Div([html.Strong(team['name']), html.Small(label)])], className='game-team'))
    else:
        rows.append(html.H3(game['title']))
    return html.Article([
        html.A(href=game_href(game['sport'],game['id']),className='game-card-hit',
               **{'aria-label':f"View {provider} markets for {game['title']}"}),
        html.Div([html.Span(game['sport']+' · '+provider.upper(),className='game-sport'),
                  html.Span(format_eastern(game['start']) if game.get('start') else 'Start time unavailable',
                            className='game-time')],className='game-card-heading'),
        *rows,
        html.Div([html.Small(f"{len(game['events'])} linked event groups"),
                  html.A('View / Pull markets →',href=game_href(game['sport'],game['id']))],className='game-card-footer'),
    ],className='panel game-card kalshi-game-card')


def game_header(game, provider='Kalshi', event_url=None):
    if not game: return html.P(f'Choose a game from the {provider} list, or refresh the games to resolve this link.')
    teams = matchup(game)
    def team(item):
        return html.Div([team_badge(item,game['sport']),
            html.Strong(item['abbreviation']),html.Small(item['name'])],className='matchup-team')
    banner = html.Div([team(teams[0][0]), html.Div([
        html.Strong(format_eastern(game['start']) if game.get('start') else 'Scheduled start unavailable'),
        html.Small(game['sport']+' · '+provider)],className='matchup-start'),team(teams[0][1])],className='matchup-banner') if teams else html.H2(game['title'])
    return html.Div([banner,
        html.A(f'Open this game on {provider} ↗', href=event_url or KalshiService.url(game['id']), target='_blank', rel='noopener noreferrer', className='market-link')],
        className='kalshi-matchup-header')


def categories(game):
    available = {category(game['sport'], t) for t in game['events']} if game else set()
    return [value for value in CATEGORIES if value in available]+([FILTER] if game else [])


def filter_panel():
    return html.Section([
        html.H2(FILTER),html.H3('Apply Criteria'),
        html.P('Choose the methods and thresholds used to highlight selections in the market tabs.',className='ppp-comparison-note'),
        html.Div([
            field('Game Outcome Forecast Method',dcc.Dropdown(id='kalshi-game-method',value='PPP',clearable=False,
                options=[dict(label=label,value=value) for label,value in [('PPP','PPP'),('EPA','EPA'),('ELO / Power Rating','ELO')]])),
            field('Player Forecast Method',dcc.Dropdown(id='kalshi-player-method',value='WHP',clearable=False,
                options=[dict(label=label,value=value) for label,value in [('Weighted Historical Performance (WHP)','WHP'),
                    ('Usage & Opportunity (UO)','UO'),('Opponent-Adjusted Matchup (OAM)','OAM'),('Pace & Game Environment (PGE)','PGE')]])),
        ],className='forecast-methods'),
        html.Div([
            field('Estimated Probability (%)',dcc.Input(id='kalshi-probability-threshold',type='number',value=75,min=0,max=100,step='any')),
            field('Edge (percentage points)',dcc.Input(id='kalshi-edge-threshold',type='number',value=5,min=-100,max=100,step='any')),
            html.Button('Apply Criteria',id='kalshi-apply-criteria',n_clicks=0,className='apply-criteria-button'),
        ],className='criteria-controls'),
        html.Div('Both thresholds must be exceeded. These criteria also highlight qualifying selections in the market tabs.',
                 id='kalshi-criteria-status',className='criteria-status',role='status'),
        dcc.Loading(html.Div(id='kalshi-filter-results'),delay_show=500,overlay_style={'visibility':'visible'},color='#57dbc4'),
        quick_controls('kalshi', 'kalshi-quick-results'),
    ],id='kalshi-filter-panel',className='panel market-criteria',style={'display':'none'})


def market_groups(snapshot):
    groups = OrderedDict()
    for row in (snapshot or {}).get('rows', []):
        groups.setdefault(row['event_ticker'], []).append(row)
    return groups


def market_rows(rows):
    if any(row.get('line') is not None for row in rows):
        return sorted(rows,key=lambda row:(row.get('line') is None,row.get('line') or 0,row['ticker']))
    return rows


def odds_text(probability):
    value=fair_american_odds(probability)
    if value is not None:return f'{value:+.0f}'
    return '+∞' if probability==0 else '−∞' if probability==1 else '—'


def comparison_rows(row,snapshot=None,provider='Kalshi'):
    polymarket=provider=='Polymarket'
    result=[]
    for side in ('yes','no'):
        probability=row.get(side+'_probability');market=implied(row,side)
        edge=probability-market if probability is not None and market is not None else None
        label=row.get(side+'_label') or side.title()
        result.append(html.Div([
            html.Div([html.Span(row['title']+' — '+(label if polymarket else side.title()),className='market-selection'),
                      html.Small(label,className='kalshi-outcome-labels') if not polymarket else None,
                      history_button(snapshot or {},row,provider,side),structure_button(snapshot or {},row,provider,side)],
                     title=f"{row['ticker']} · Payout/notional ${row['notional']:g} · {row['status']}"),
            html.Span(odds_text(market),className='market-price kalshi-price',
                      title='American odds equivalent of this outcome’s quoted buying price / payout, before fees.'),
            html.Span(probability_text(market),className='market-probability'),
            *([] if polymarket else [
                html.Span(probability_text(probability),className='market-probability',title=row.get('model_method','')+': '+row.get('model_detail','Estimate unavailable')),
                html.Span(odds_text(probability),className='market-probability'),
                html.Span(f'{100*edge:+.2f} pp' if edge is not None else '—',className='market-edge'),
            ]),
        ],**({'id':dict(type='kalshi-comparison-row',sport=snapshot['sport'],game=snapshot['game_id'],ticker=row['ticker'],side=side)}
             if not polymarket and snapshot else {}),
            className='market-outcome market-line-comparison'+(' polymarket-line-comparison' if polymarket else '')+
            (' market-criteria-match' if row.get(side+'_match') else ''),
            key=row['ticker']+'-'+side))
    return result


def market_sections(snapshot, provider='Kalshi'):
    """Sportsbook-style market ladders, retaining the exchange's binary quotes."""
    groups = market_groups(snapshot)
    polymarket = provider=='Polymarket'
    if not groups:
        return html.P('No contracts are available in this category.',className='saved-market-empty')

    def outcome(row):
        return html.Div(comparison_rows(row,snapshot,provider),className='kalshi-contract',key=row['ticker'])

    sections = []
    names = {'GAME':'Game winner', 'SPREAD':'Alternate spreads', 'TOTAL':'Total points', 'TEAMTOTAL':'Team totals'}
    for index,(event,rows) in enumerate(groups.items()):
        family = event.split('-',1)[0].removeprefix('KX'+snapshot['sport'])
        title = rows[0].get('section') or names.get(family) or (rows[0]['event_title'] or rows[0]['title']).replace(':',' -',1)
        rows = market_rows(rows)
        # Stable keys and unchanged open props preserve native accordion state
        # while live quote values update, including the nested "Show more" list.
        sections.append(html.Details([
            html.Summary([html.Strong(title),html.Span('⌄',className='market-chevron',**{'aria-hidden':'true'})]),
            html.Div([
                html.Div([html.Span(label) for label in
                    (['Selection','Market odds','Market implied probability','Calculated probability','Model fair odds','Edge'] if not polymarket else
                     ['Selection','Market odds','Market implied probability'])],
                    className='market-comparison-heading market-line-comparison'+(' polymarket-line-comparison' if polymarket else '')),
                html.Div([outcome(row) for row in rows[:6]],className='market-outcomes'),
                html.Details([html.Summary([
                    html.Span(f'Show more · {len(rows)-6} selections',className='market-show-more'),
                    html.Span('Show less',className='market-show-less')]),
                    html.Div([outcome(row) for row in rows[6:]])],className='more-market-lines',
                    open=False,key=event+'-more') if len(rows)>6 else None,
            ],className='kalshi-market-lines'),
        ],id='kalshi-section-'+event,key=event,open=index<2,className='sportsbook-market kalshi-market'))
    return sections


def table(snapshot, include_rules=True, provider='Kalshi', *, build_rows=True, build_tooltips=None):
    rows, tips, rules = [], [], []
    if build_tooltips is None: build_tooltips=include_rules
    for original in (snapshot or {}).get('rows', []):
        if build_rows:
            row = dict(original)
            for key in ['yes_bid', 'yes_ask', 'no_bid', 'no_ask', 'last_price']:
                row[key] = 100 * original[key] if original.get(key) is not None else None
            row['quote_fetched_at'] = (snapshot or {}).get('fetched_at')
            rows.append(row)
        if build_tooltips:
            detail = f"{original['rules_primary']}\n{original['rules_secondary']}\nContract payout/notional: ${original['notional']:g}"
            tips.append({key: dict(value=detail, type='text') for key in ['title', 'yes_label', 'no_label', 'ticker']})
        if not include_rules: continue
        rules.append(html.Details([html.Summary(original['title']), html.P(('Market ID: ' if provider=='Polymarket' else 'Ticker: ') + original['ticker']),
            html.P(f"Contract notional: ${original['notional']:g} · Status: {original['status']}"),
            html.P(original['rules_primary']), html.P(original['rules_secondary'])]))
    return rows, tips, rules
