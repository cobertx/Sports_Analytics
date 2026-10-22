"""Rank saved game and player selections with the same probabilities as market tabs."""

import math

from dash import html

from .market_view import MARKET_NAMES, _line_values, criteria_match, probability_text, selection_text


FILTER_CATEGORY = 'Mispricing'


def criteria_values(probability, edge):
    if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (probability, edge)) or not 0 <= probability <= 100 or not -100 <= edge <= 100):
        raise ValueError('Enter an Estimated Probability from 0 to 100 and an Edge from -100 to 100. Previous criteria remain applied.')
    return dict(probability=probability / 100, edge=edge / 100)


def filter_context(location_hash, selection, method, player_method, criteria):
    return dict(location=location_hash, snapshot=selection, game_method=method or 'PPP',
                player_method=player_method or 'WHP', criteria=criteria)


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
    def table(items,start):
        return html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label,scope='col') for label in
                ['Rank','Selection','Market','Method','Market odds','Market implied','Estimated probability','Edge (pp)']])),
            html.Tbody([html.Tr([
                html.Td(start+i),html.Td(selection_text(item['row'])),
                html.Td(MARKET_NAMES.get(item['row']['market_key'],(item['row']['market'],))[0]),
                html.Td(item['method']),html.Td(f"{item['row']['odds']:+g}"),
                html.Td(probability_text(item['implied'])),html.Td(probability_text(item['probability'])),
                html.Td(f"{item['edge']*100:+.2f}",className='mispricing-edge'),
            ]) for i,item in enumerate(items)] or [html.Tr(html.Td('No qualifying markets.',colSpan=8))]),
        ],className='mispricing-table'),className='mispricing-table-scroll')

    matches=ranked['matches']
    return html.Div([
        html.Div([
            html.Strong(f"{len(matches)} qualifying selections across this game's saved markets."),
            html.Span(f" Estimated probability > {criteria['probability']*100:g}% and edge > {criteria['edge']*100:g} percentage points. "
                      f"{ranked['evaluated']} quotes evaluated; {ranked['unavailable']} unavailable quotes excluded."),
        ],className='criteria-evaluation'+(' criteria-evaluation-no-match' if not matches else ''),role='status'),
        *[html.P(error,className='mispricing-error',role='status') for error in errors if error],
        html.Section([html.H3('Top 3 by edge'),table(matches[:3],1)],className='mispricing-top'),
        html.Section([html.H3('Remaining qualifying markets'),table(matches[3:],4)],className='mispricing-remaining'),
        html.P('Ranked by estimated probability minus market implied probability, highest first. '
               'Estimates use the selected game/player methods and exclude pushes, matching the market tabs. '
               'Alternate lines are separate selections.',className='ppp-comparison-note'),
    ],className='mispricing-results')
