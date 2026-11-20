"""Bind filter output to the game and snapshot that produced it."""

import json

from dash import Input, Output, html


PROMPTS = {
    'market': 'Click Quick Filtering to show the top three selections and all remaining matches.',
    'kalshi': 'Click Quick Filtering to compare all selected methods and see the top three edges.',
    'summary': 'Choose methods and thresholds, then click Run Quick Filtering.',
}


def scoped(content, location, selection):
    setattr(content, 'data-quick-scope', json.dumps([location, None if selection is None else str(selection)],
                                                   separators=(',', ':'), ensure_ascii=False))
    return content


def status(message, location, selection):
    return scoped(html.Span(message), location, selection) if message else ''


def summary_location(bundle, sport):
    mode = 'draftkings-market' if (bundle or {}).get('mode') == 'draftkings' else 'market-summary'
    return f'#{sport.lower()}-{mode}'


def slot(provider, identifier, *, class_name='', **kwargs):
    return html.Div(id=identifier, className=f'quick-result-slot {class_name}'.strip(),
                    **{'data-quick-provider': provider}, **kwargs)


def feedback(provider, identifier, *, starting=None):
    return html.Div([
        *([slot(provider, f'{provider}-quick-start-slot', children=starting)] if starting is not None else []),
        slot(provider, f'{provider}-quick-status', class_name='criteria-status', role='status'),
        slot(provider, identifier, class_name='quick-filter-output'),
        html.P(PROMPTS[provider], className='saved-market-empty quick-filter-placeholder'),
    ], id=f'{provider}-quick-feedback', className='quick-filter-feedback', **{'data-quick-selection': None})


def register(app):
    for provider, control in [('market', 'market-snapshot'), ('kalshi', 'kalshi-snapshot'), ('summary', 'summary-game')]:
        fallback = '"live"' if provider == 'kalshi' else 'null'
        app.clientside_callback(f'function(value){{return value==null?{fallback}:String(value);}}',
            Output(f'{provider}-quick-feedback', 'data-quick-selection'), Input(control, 'value'))
