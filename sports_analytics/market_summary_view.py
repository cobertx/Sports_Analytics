"""Upcoming game cards and FanDuel-style quote ladders for all providers."""
from collections import OrderedDict
from dash import dcc,html

from .display_time import format_eastern
from .market_view import logo_badge,probability_text
from .mispricing import GAME_METHODS,PLAYER_METHODS
from .prompt_history import button as history_button
from .market_summary import PROVIDERS


def field(label,control):
    return html.Div([html.Label(label,htmlFor=control.id),control],className='field')


def layout():
    return html.Section([
        dcc.Store(id='summary-catalog'),dcc.Store(id='summary-quotes'),dcc.Store(id='summary-models'),dcc.Store(id='summary-rendered'),
        dcc.Interval(id='summary-cache-poll',interval=1500,disabled=True),
        html.Section([
            html.Div([html.Div('MARKET DATA',className='eyebrow accent'),html.H2('Market Summary'),
                      html.P('Compare FanDuel, Kalshi and Polymarket quotes for one upcoming game.')],className='market-heading'),
            html.Div([
                field('Game window',dcc.Dropdown(id='summary-days',options=[dict(label='Next 7 days',value=7),dict(label='Next 14 days',value=14)],value=7,clearable=False)),
                html.Button('Refresh upcoming games',id='summary-refresh',n_clicks=0,className='export-button'),
            ],className='market-controls'),
            html.Div(id='summary-catalog-status',className='load-status',role='status'),
            field('Upcoming game',dcc.Dropdown(id='summary-game',options=[],placeholder='Select a game to compare its markets',clearable=True)),
        ],className='panel summary-heading'),
        html.Div(id='summary-game-cards'),
        html.Section([
            html.Div([
                html.Button('Back to upcoming games',id='summary-back',n_clicks=0,className='export-button'),
                html.Div([html.Button('Pull Live Markets',id='summary-pull',n_clicks=0,className='pull-markets-button'),
                          html.Small(id='summary-credit-note')],className='detail-pull-control'),
            ],className='detail-page-actions'),
            html.Div(id='summary-game-header'),
            html.Div([
                field('Game Outcome Forecast Method',dcc.Dropdown(id='summary-game-method',options=list(GAME_METHODS),value='PPP',clearable=False)),
                field('Player Forecast Method',dcc.Dropdown(id='summary-player-method',options=list(PLAYER_METHODS),value='WHP',clearable=False)),
                html.Button('Recalculate estimates',id='summary-recalculate',n_clicks=0,className='export-button'),
            ],className='forecast-methods'),
            html.P('Saved FanDuel quotes load without spending API credits. Kalshi and Polymarket use public quotes. '
                   'Pull Live Markets refreshes all three and saves their snapshots. Odds use American format; exchange buying prices '
                   'are also shown in cents per contract/share. Edge is before fees.',className='ppp-comparison-note'),
            html.P('Compare each selection across source columns. Green highlights the best available price for the same outcome. '
                   'Equivalent integer thresholds share a row; hover over a quote for its original wording. '
                   'Lines with different push rules stay separate. A dash means no matching quote.',className='summary-comparison-note'),
            dcc.Loading(html.Div(id='summary-quote-status',className='load-status',role='status'),delay_show=500,color='#57dbc4',overlay_style={'visibility':'visible'}),
            html.Div(id='summary-model-status',className='forecast-history',role='status'),
            dcc.Tabs(id='summary-category',value='All Markets',children=[dcc.Tab(label=value,value=value) for value in
                     ['All Markets','Game Lines','Player Props','Other Markets']],className='market-category-tabs'),
            dcc.Loading(html.Div(id='summary-market-sections'),delay_show=500,color='#57dbc4',overlay_style={'visibility':'visible'}),
        ],id='summary-detail',style={'display':'none'}),
    ],id='market-summary-page',className='market-summary-page',style={'display':'none'})


def cards(games,sport):
    return html.Div([html.Article([
        html.Button('',id={'type':'summary-game-choice','game':str(game['id'])},n_clicks=0,className='game-card-hit',
                    **{'aria-label':'Compare markets for '+game['away']['name']+' at '+game['home']['name']}),
        html.Div(sport+' - MARKET SUMMARY',className='game-sport'),
        *[html.Div([logo_badge(game[side]['name'],game[side].get('abbreviation',''),sport),
                    html.Div([html.Strong(game[side]['name']),html.Small(side.title())])],className='game-team') for side in ('away','home')],
        html.Div(format_eastern(game['start']),className='game-card-footer'),
    ],className='panel game-card') for game in games],className='game-grid')


def odds_text(value,probability=None):
    if value is not None:return f'{value:+.0f}'
    return '+∞' if probability==0 else '−∞' if probability==1 else '—'


def model_value(row,metric):
    if metric=='probability':return [probability_text(row['probability']),html.Small(row['method'] or '')],row['detail']
    if metric=='odds':return odds_text(row['model_odds'],row['probability']),''
    return f"{100*row['edge']:+.2f} pp" if row['edge'] is not None else '—',''


def comparison_rows(rows,category):
    groups=OrderedDict()
    for row in rows:
        outcome=row.get('comparison') or dict(key=[row['provider'],row['index'],row['side']],category=row['category'],
            group=row['group'],label=row['selection'],line=row['line'],note='Provider-specific contract')
        if category!='All Markets' and outcome['category']!=category:continue
        markets=groups.setdefault((outcome['category'],outcome['group']),OrderedDict())
        matches=markets.setdefault(tuple(outcome['key']),[])
        # Retain every contract if a source lists more than one for an outcome.
        match=next((item for item in matches if row['provider'] not in item['quotes']),None)
        if match is None:
            match=dict(outcome=outcome,quotes={});matches.append(match)
        match['quotes'][row['provider']]=row
    return {key:sorted([item for matches in markets.values() for item in matches],
                       key=lambda item:(item['outcome']['line'] if item['outcome']['line'] is not None else 0,item['outcome']['label']))
            for key,markets in groups.items()}


def quote_cell(bundle,row,snapshot,best):
    if row is None:
        return html.Td(html.Span('—',title='No matching quote from this source'),className='summary-missing')
    original=snapshot['rows'][row['index']]
    return html.Td(html.Div([
        html.Div([html.Strong(odds_text(row['odds'],row['implied']),className='summary-price'),
                  html.Span('Best price',className='summary-best-label') if best else None],className='summary-price-line'),
        html.Div('Market implied '+probability_text(row['implied']),className='summary-implied'),
        html.Small(f"{100*row['ask']:.2f}¢ ask" if row['ask'] is not None else row['status'],className='summary-quote-detail'),
        html.Dl([html.Div([html.Dt(label),html.Dd(html.Span(model_value(row,metric)[0],title=model_value(row,metric)[1],
            id=dict(type='summary-model-cell',bundle=bundle['id'],provider=row['provider'],index=row['index'],side=row['side'] or '',metric=metric)))])
            for metric,label in [('probability','Calculated probability'),('odds','Model fair odds'),('edge','Edge')]],className='summary-model-values'),
        history_button(snapshot,original,row['provider'],row['side'] or 'yes'),
    ],title=row['selection'],className='summary-quote'+(' summary-best' if best else '')),className='summary-source-cell')


def sections(bundle,rows,category):
    groups=comparison_rows(rows,category);categories=OrderedDict();index=0
    snapshots={name:{**record['snapshot'],'game':record.get('game')} for name,record in bundle['providers'].items() if record.get('snapshot')}
    for (group,title),items in groups.items():
        content=[]
        for item in items:
            outcome=item['outcome'];quotes=item['quotes']
            priced=[row['implied'] for row in quotes.values() if row['implied'] is not None]
            best=min(priced) if len(priced)>1 else None
            content.append(html.Tr([
                html.Th([html.Span(outcome['label']),html.Small(outcome['note']) if outcome['note'] else None],scope='row',className='summary-selection'),
                *[quote_cell(bundle,quotes.get(provider),snapshots.get(provider),best is not None and
                             quotes.get(provider,{}).get('implied')==best) for provider in PROVIDERS],
            ]))
        table=html.Details([
            html.Summary([html.Strong(title),html.Span(f'{len(items)} selections',className='market-chevron')]),
            html.Div(html.Table([
                html.Caption(title+' — Market odds by source',className='summary-table-caption'),
                html.Thead(html.Tr([html.Th('Market / Selection',scope='col'),*[html.Th(html.Span(provider,
                    className='summary-source-name summary-source-'+provider.lower()),scope='col') for provider in PROVIDERS]])),
                html.Tbody(content),
            ],className='summary-table'),className='summary-table-scroll',tabIndex=0,role='region',**{'aria-label':title+' source comparison'}),
        ],open=index<2,key=group+'-'+title,className='sportsbook-market summary-market')
        categories.setdefault(group,[]).append(table);index+=1
    return [html.Section([html.H3(group,className='summary-category-heading'),*categories[group]],className='summary-category')
            for group in ('Game Lines','Player Props','Other Markets') if group in categories] or html.P(
                'No quoted selections are available in this category.',className='saved-market-empty')
