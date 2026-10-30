"""Rank saved game and player selections with the same probabilities as market tabs."""

import math
from concurrent.futures import ThreadPoolExecutor

from dash import dcc, html
from models.season_weights import POLICY_VERSION

from .market_view import MARKET_NAMES, _line_values, criteria_match, probability_text, selection_text
from .prompt_history import button as history_button
from .market_structure_link import button as structure_button
from .quick_methods import GAME_METHODS,PLAYER_METHODS


FILTER_CATEGORY = 'Mispricing'
ALL_PROBABILITIES = dict(probability=-1, edge=-2)


def quick_controls(prefix, result_id):
    def field(label, control):
        return html.Div([html.Label(label, htmlFor=control.id), control], className='field')
    return html.Section([
        html.H3('Quick Filtering'),
        html.P('Choose default methods below or assign methods to individual prompts under Methods by prompt. '
               'Each market must pass both thresholds under every method assigned to its prompt. '
               'These inputs are independent of Apply Criteria.', className='ppp-comparison-note'),
        html.Div([
            field('Default Game Outcome methods', dcc.Dropdown(id=prefix+'-quick-game-methods', multi=True,
                options=[dict(label=label,value=value) for label,value in
                         [('PPP','PPP'),('EPA','EPA'),('ELO / Power Rating','ELO')]], value=['PPP'], clearable=True)),
            field('Default Player Forecast methods', dcc.Dropdown(id=prefix+'-quick-player-methods', multi=True,
                options=[dict(label=label,value=value) for label,value in
                         [('Weighted Historical Performance (WHP)','WHP'),('Usage & Opportunity (UO)','UO'),
                          ('Opponent-Adjusted Matchup (OAM)','OAM'),('Pace & Game Environment (PGE)','PGE')]], value=['WHP'], clearable=True)),
        ], className='forecast-methods'),
        html.Details([
            html.Summary('Methods by prompt'),
            html.P('Assign methods separately to each team market or player statistic. “Use default methods” inherits '
                   'the selections above. Selecting specific methods overrides that default; clear a prompt to skip it. '
                   'All lines and sides of a prompt use its assigned methods, and every assigned method must pass.',
                   className='ppp-comparison-note'),
            dcc.Loading(html.Div(id=prefix+'-quick-prompts'),delay_show=500,color='#57dbc4'),
        ],id=prefix+'-quick-prompts-panel',open=False,className='quick-prompt-panel'),
        html.Div([
            field('Estimated Probability (%)', dcc.Input(id=prefix+'-quick-probability', type='number', value=75, min=0, max=100, step='any')),
            field('Edge (percentage points)', dcc.Input(id=prefix+'-quick-edge', type='number', value=5, min=-100, max=100, step='any')),
            html.Button('Quick Filtering', id=prefix+'-quick-filter', n_clicks=0, className='apply-criteria-button quick-filter-button'),
        ], className='criteria-controls'),
        html.Div(id=prefix+'-quick-status', className='criteria-status', role='status'),
        dcc.Loading(html.Div(id=result_id), delay_show=500, overlay_style={'visibility':'visible'}, color='#57dbc4'),
    ], className='quick-filter-section')


def selected_methods(game_methods, player_methods):
    def validate(values, supported):
        if not isinstance(values, list) or not values or any(value not in supported for value in values):
            raise ValueError('Select at least one supported Game Outcome method and one Player Forecast method.')
        return list(dict.fromkeys(values))
    return validate(game_methods, GAME_METHODS), validate(player_methods, PLAYER_METHODS)


def run_forecasts(tasks, sport):
    # College preparation writes shared historical files. Keep those fits serial.
    if sport == 'NCAAF':
        return {key: task() for key, task in tasks.items()}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='market-filter') as executor:
        pending = {key: executor.submit(task) for key, task in tasks.items()}
        return {key: task.result() for key, task in pending.items()}


def consensus_rankings(groups, criteria, selection_key):
    """Intersect available estimates within each market type, then apply strict criteria."""
    matches=[]; evaluated=unavailable=0
    for rankings in groups:
        if not rankings:continue
        indexed=[(method, {selection_key(item): item for item in ranked['matches']}) for method, ranked in rankings]
        available=set.intersection(*(set(items) for _, items in indexed))
        evaluated+=len(available)
        first=rankings[0][1]
        unavailable+=first['evaluated']+first['unavailable']-len(available)
        for key in sorted(available):
            estimates=[dict(method=method, probability=items[key]['probability'], edge=items[key]['edge']) for method, items in indexed]
            probability=min(item['probability'] for item in estimates)
            edge=min(item['edge'] for item in estimates)
            if criteria_match(probability, edge, criteria):
                matches.append({**indexed[0][1][key], 'probability':probability, 'edge':edge,
                                'method':', '.join(method for method, _ in indexed), 'estimates':estimates})
    matches.sort(key=lambda item:(-item['edge'], -item['probability'], selection_key(item)))
    return dict(matches=matches, evaluated=evaluated, unavailable=unavailable, consensus=True)


def method_details(item):
    if 'estimates' not in item:return item['method']
    return [html.Div(f"{value['method']}: {probability_text(value['probability'])} / {100*value['edge']:+.2f} pp")
            for value in item['estimates']]


def criteria_values(probability, edge):
    if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (probability, edge)) or not 0 <= probability <= 100 or not -100 <= edge <= 100):
        raise ValueError('Enter an Estimated Probability from 0 to 100 and an Edge from -100 to 100. Previous criteria remain applied.')
    return dict(probability=probability / 100, edge=edge / 100)


def filter_context(location_hash, selection, method, player_method, criteria):
    return dict(location=location_hash, snapshot=selection, game_method=method or 'PPP',
                player_method=player_method or 'WHP', criteria=criteria,weighting_policy=POLICY_VERSION)


def rank_markets(snapshot, game_forecast, player_forecast, criteria, method='PPP', player_method='WHP'):
    matches=[]
    evaluated=unavailable=0
    for index,row in enumerate(snapshot['rows']):
        player=row['market_key'].startswith('player_')
        forecast=player_forecast if player else game_forecast
        try:
            implied,estimate,edge=_line_values(row,forecast)
            probability=estimate.probability if estimate is not None else None
        except (TypeError,ValueError,OverflowError):
            implied=probability=edge=None
        if not all(isinstance(value,(int,float)) and math.isfinite(value) for value in (implied,probability,edge)):
            unavailable+=1
            continue
        if not (0<=implied<=1 and 0<=probability<=1):
            unavailable+=1
            continue
        evaluated+=1
        if criteria_match(probability,edge,criteria):
            matches.append(dict(index=index,row=row,probability=probability,implied=implied,edge=edge,
                                method=player_method if player else method))
    matches.sort(key=lambda item:(-item['edge'],-item['probability'],item['index']))
    # A duplicated provider quote should not occupy two of the top three slots.
    seen=set();unique=[]
    for item in matches:
        row=item['row']
        identity=(row['market_key'],row['subject'],row['selection'],row['line'])
        if identity in seen:continue
        seen.add(identity);unique.append(item)
    return dict(matches=unique,evaluated=evaluated,unavailable=unavailable,total=len(snapshot['rows']))


def results(snapshot, ranked, criteria, errors=()):
    consensus=ranked.get('consensus',False)
    def table(items,start):
        return html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label,scope='col') for label in
                ['Rank','Selection','Market','Method (probability / edge)' if consensus else 'Method','Market odds','Market implied',
                 'Lowest probability' if consensus else 'Estimated probability','Lowest edge (pp)' if consensus else 'Edge (pp)']])),
            html.Tbody([html.Tr([
                html.Td(start+i),html.Td([selection_text(item['row']),history_button(snapshot,item['row']),structure_button(snapshot,item['row'],surface='quick-filter')]),
                html.Td(MARKET_NAMES.get(item['row']['market_key'],(item['row']['market'],))[0]),
                html.Td(method_details(item)),html.Td(f"{item['row']['odds']:+g}"),
                html.Td(probability_text(item['implied'])),html.Td(probability_text(item['probability'])),
                html.Td(f"{item['edge']*100:+.2f}",className='mispricing-edge'),
            ]) for i,item in enumerate(items)] or [html.Tr(html.Td('No qualifying markets.',colSpan=8))]),
        ],className='mispricing-table'),className='mispricing-table-scroll')

    matches=ranked['matches']
    return html.Div([
        html.Div([
            html.Strong(f"{len(matches)} qualifying selections across this game's saved markets."),
            html.Span(f" Estimated probability > {criteria['probability']*100:g}% and edge > {criteria['edge']*100:g} percentage points. "
                      f"{ranked['evaluated']} quotes evaluated; {ranked['unavailable']} unavailable quotes excluded."
                      +(f" {ranked['excluded']} quotes skipped by prompt selection." if ranked.get('excluded') else '')),
        ],className='criteria-evaluation'+(' criteria-evaluation-no-match' if not matches else ''),role='status'),
        *[html.P(error,className='ppp-comparison-note' if error.startswith('Game injuries:') else 'mispricing-error',role='status') for error in errors if error],
        html.Section([html.H3('Top 3 by edge'),table(matches[:3],1)],className='mispricing-top'),
        html.Section([html.H3('Remaining qualifying markets'),table(matches[3:],4)],className='mispricing-remaining'),
        html.P('Ranked by estimated probability minus market implied probability, highest first. '
               'Estimates use the selected game/player methods and exclude pushes, matching the market tabs. '
               'Alternate lines are separate selections. '+
               ('Every method assigned to a prompt must pass both thresholds. Ranking uses the lowest edge across those methods; '
                'missing estimates from any selected method exclude that selection.' if consensus else ''),className='ppp-comparison-note'),
    ],className='mispricing-results')
