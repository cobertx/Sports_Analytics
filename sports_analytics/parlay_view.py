"""Bottom-docked analysis slip, with no wager or cash-out controls."""
from collections import OrderedDict
from dash import html
from .display_time import format_eastern
from .parlay import parlay_summary


def probability(value):
    return f'{100*value:.2f}%' if value is not None else '—'


def odds(value):
    return f'{value:+,.0f}' if value is not None else '—'


def metric(label,value,kind='',title=None):
    return html.Div([html.Span(label,className='parlay-metric-label'),
                     html.Strong(value,className='parlay-metric-value '+kind)],className='parlay-metric',title=title)


def slip_layout():
    return html.Aside([
        html.Div([
            html.Button([html.Span('PARLAY',className='parlay-heading'),html.Span(id='parlay-count',className='parlay-count'),
                         html.Span(id='parlay-mini',className='parlay-mini'),
                         html.Span('⌄',className='parlay-chevron')],id='parlay-toggle',n_clicks=0,
                        className='parlay-toggle',**{'aria-expanded':'true','aria-controls':'parlay-body'}),
            html.Button('Clear all',id='parlay-clear',n_clicks=0,className='parlay-clear'),
        ],className='parlay-topbar'),
        html.Div(id='parlay-notice',role='status',className='parlay-notice',**{'aria-live':'polite'}),
        html.Div(id='parlay-body',className='parlay-body'),
    ],id='parlay-drawer',className='parlay-drawer is-empty',style={'display':'none'},
       **{'aria-label':'Parlay analysis slip'})


def slip_contents(legs):
    summary=parlay_summary(legs)
    if not legs:return [],summary
    groups=OrderedDict()
    for leg in legs:groups.setdefault((leg['sport'],leg['game_id']),[]).append(leg)
    sections=[]
    for (sport,game),rows in groups.items():
        first=rows[0]
        group_title=[html.Span(sport,className='parlay-sport'),html.Strong(f"{first['away']} @ {first['home']}")]
        if len(rows)>1:group_title.append(html.Span('SGP estimate',className='parlay-sgp'))
        cards=[]
        for leg in rows:
            explanation=leg.get('error') or f"{leg['method']} · probability conditional on no push. Saved quote {format_eastern(leg['snapshot_time'])}."
            cards.append(html.Div([
                html.Button('×',id={'type':'parlay-remove','key':leg['key']},n_clicks=0,
                            className='parlay-remove',**{'aria-label':f"Remove {leg['label']}"}),
                html.Div([html.Strong(leg['label']),html.Span(leg['market'],className='parlay-market-name'),
                          html.Small(explanation if leg.get('model_probability') is None else
                                     f"{leg['method']} · Saved {format_eastern(leg['snapshot_time'])}",className='parlay-leg-source')],
                         className='parlay-selection-info'),
                metric('Market odds',odds(leg['odds']),'parlay-market-odds'),
                metric('Market implied',probability(leg.get('market_probability'))),
                metric(f"{leg['method']} probability",probability(leg.get('model_probability')),'parlay-model-value',explanation),
                metric(f"{leg['method']} odds",odds(leg.get('model_odds')),'parlay-model-value',explanation),
            ],className='parlay-leg',role='listitem'))
        sections.append(html.Section([html.Div(group_title,className='parlay-game-heading'),
                                      html.Div(cards,role='list')],className='parlay-game'))
    notes=[]
    if summary['conflict']:
        notes.append(html.P('Conflicting selections: no outcome wins every leg. Remove opposing selections to price this combination.',className='parlay-conflict',role='alert'))
    if summary['same_game']:
        notes.append(html.P('Same-game price and probability are independent-leg estimates. FanDuel correlation-adjusted SGP pricing is not available from these individual quotes.',className='parlay-assumption'))
    else:
        notes.append(html.P('Combined odds multiply individual decimal odds. Calculated probability assumes independent legs.',className='parlay-assumption'))
    if summary['missing_models']:
        notes.append(html.P(f"{summary['missing_models']} leg(s) have no default-model estimate; combined calculated values remain unavailable.",className='parlay-assumption'))
    if summary['has_pushes']:
        notes.append(html.P('Calculated probability is conditional on no pushes. A pushed/void leg is removed and the settled parlay odds change.',className='parlay-assumption'))
    notes.append(html.P('Analysis of saved quotes · Default models: PPP game outcomes / WHP player stats',className='parlay-footnote'))
    totals=html.Div([
        metric('Est. market parlay odds' if summary['same_game'] else 'Combined market odds',odds(summary['market_odds']),'parlay-market-odds'),
        metric('Parlay market implied',probability(summary['market_probability'])),
        metric('Calculated parlay probability',probability(summary['model_probability']),'parlay-model-value','Independent-leg estimate, conditional on all legs settling without pushes.'),
        metric('Calculated parlay odds',odds(summary['model_odds']),'parlay-model-value'),
    ],className='parlay-totals',**{'aria-label':'Combined parlay estimates'})
    return [html.Div(sections,className='parlay-leg-list'),html.Div([totals,*notes],className='parlay-footer')],summary
