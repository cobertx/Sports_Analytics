"""Forecast and quoted win probabilities across a player's alternate lines."""

from collections import OrderedDict, defaultdict
import json
import math
from threading import RLock
from uuid import uuid4

from dash import Input, MATCH, Output, State, dcc, html
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go

from app.components.presentation import chart_style
from app.sections.markets.player_distribution import distribution, threshold_curve
from app.sections.markets.quick_visibility import slot


SOURCE_STYLES = {
    'FanDuel': ('#75baff', 'diamond', 'solid'),
    'DraftKings': ('#ffac74', 'triangle-down', 'dashdot'),
    'Kalshi': ('#8debab', 'square', 'dash'),
    'Polymarket': ('#b7b6ff', 'triangle-up', 'dot'),
}
SHARED_VIEWS = {'market','summary','kalshi','polymarket'}
FORECAST_COLORS = ('#57dbc4', '#f6c667', '#f599c9', '#ddd995', '#95d6e8', '#d6b1a1')
_views = OrderedDict()
_fits = OrderedDict()
_lock = RLock()


def player_line(comparison):
    key = (comparison or {}).get('key', ())
    return key if len(key) == 6 and key[0] == 'Player Props' else None


def sportsbook_line(row):
    market, line, side = row['market_key'], row.get('line'), row['selection']
    if not market.startswith('player_'):
        return None
    if market == 'player_anytime_td' and side in {'Yes', 'No'}:
        line, side = .5, 'Over' if side == 'Yes' else 'Under'
    if side not in {'Over', 'Under'} or not isinstance(line, (int, float)) or not math.isfinite(line):
        return None
    return ['Player Props', market.removesuffix('_alternate'), ' '.join(row['subject'].casefold().split()),
            line, 'gt' if side == 'Over' else 'lt', float(line).is_integer()]


def row_action(label, key, view, **context):
    if key is None or view is None:
        return label, {}
    return html.Button(label, type='button', className='prop-chart-button',
                       **{'aria-label': f'Compare forecast and market probabilities for {label}'}), block_action(key, view, **context)


def block_action(key, view, **context):
    return {'data-prop-chart': json.dumps(dict(key=key, view=view, **context), separators=(',', ':'))} if key and view else {}


def remember_fit(forecast):
    if forecast is None:
        return None
    identity=uuid4().hex
    with _lock:
        _fits[identity]=forecast
        if len(_fits)>64:_fits.popitem(last=False)
    return identity


def stored_fit(identity):
    with _lock:return _fits.get(identity)


def player_payloads(key, forecasts):
    payloads={}
    for method, forecast in forecasts.items():
        if isinstance(forecast,str):forecast=stored_fit(forecast)
        for (subject, market), fitted in getattr(forecast,'forecasts',{}).items():
            if market.removesuffix('_alternate')==key[1] and ' '.join(subject.casefold().split())==key[2]:
                try:
                    payloads[method]=distribution(fitted,[key[3]])
                except (ValueError,TypeError,OverflowError,AttributeError):
                    pass
                break
    return payloads


def mount(render):
    """Keep full ladders server-side, including lines outside the displayed page."""
    view = uuid4().hex
    with _lock:
        _views[view] = render
        if len(_views) > 64:
            _views.popitem(last=False)
    return view, outlet(view)


def outlet(view):
    shared = view in SHARED_VIEWS
    request = f'{view}-prop-request' if shared else dict(type='prop-chart-request', view=view)
    result = f'{view}-prop-result' if shared else dict(type='prop-chart-result', view=view)
    data = {'data-selection': None}
    if view in {'kalshi','market'}:
        data['data-model-revision'] = None
    attributes = {'data-prop-chart-result': view}
    result_panel = slot('summary', result, **attributes) if view == 'summary' else html.Div(id=result, **attributes)
    return html.Div([
        html.Div(id=request, style={'display': 'none'}, **data),
        result_panel,
    ])


def register(app):
    @app.callback(Output(dict(type='prop-chart-result', view=MATCH), 'children'),
                  Input(dict(type='prop-chart-request', view=MATCH), 'data-selection'),
                  State(dict(type='prop-chart-request', view=MATCH), 'id'), prevent_initial_call=True)
    def prop_line_probabilities(selection, identity):
        if not selection:
            raise PreventUpdate
        view = identity['view']
        with _lock:
            render = _views.get(view)
            if render is not None:
                _views.move_to_end(view)
        if render is None:
            content = message('This market view expired. Reopen the category or rerun Quick Filtering.')
        else:
            content = render(selection['key'], selection)
        return clicked(content, selection)


def clicked(content, selection):
    # Acknowledge the specific click so fast consecutive selections focus the latest chart.
    setattr(content, 'data-prop-chart-click', str(selection['click']))
    return content


def message(text):
    return html.P(text, className='chart-note prop-chart-message', role='status', tabIndex=-1)


def matching_side(key, row_key):
    return row_key and row_key[:3] == key[:3] and row_key[4] == key[4]


def chart(key, rows, *, waiting=False, fits=None):
    prices, forecasts = defaultdict(dict), defaultdict(dict)
    title = None
    for row in rows:
        outcome = row.get('comparison')
        row_key = player_line(outcome)
        if not matching_side(key, row_key):
            continue
        title = title or outcome['group']
        line, push = row_key[3], row_key[5]
        provider, price = row['provider'], row.get('implied')
        if price is not None and price < prices[provider].get(line, (math.inf, False))[0]:
            prices[provider][line] = (price, push)
        method, probability = row.get('method'), row.get('probability')
        if method and probability is not None:
            # Across duplicate quotes keep the estimate tied to the best price.
            rank = price if price is not None else math.inf
            if line not in forecasts[method] or rank < forecasts[method][line][2]:
                forecasts[method][line] = (probability, push, rank)

    direction = 'Over' if key[4] == 'gt' else 'Under'
    selected = key[3]
    figure = go.Figure()
    values = []

    def add_trace(name, points, color, symbol, dash, *, connect):
        lines = sorted(points)
        figure.add_trace(go.Scatter(x=lines, y=[points[line][0] for line in lines], name=name,
            mode='lines+markers' if connect else 'markers', connectgaps=False,
            line=dict(color=color, dash=dash, width=2.5), marker=dict(symbol=symbol, size=7, color=color),
            customdata=['Pushes excluded' if points[line][1] else 'Full settlement probability' for line in lines],
            hovertemplate=direction+' %{x:g}<br>%{y:.2%} · %{customdata}<extra>%{fullData.name}</extra>'))
        if selected in points:
            values.append(html.Li(f'{name} {points[selected][0]:.2%}'))

    payloads=player_payloads(key,fits or {})
    for index, method in enumerate(dict.fromkeys([*forecasts,*payloads])):
        color=FORECAST_COLORS[index % len(FORECAST_COLORS)]
        if method in forecasts:
            add_trace(f'{method} forecast', forecasts[method], color, 'circle', 'solid', connect=method not in payloads)
        if method in payloads:
            x,y=threshold_curve(payloads[method],above=key[4]=='gt')
            figure.add_trace(go.Scatter(x=x,y=y,mode='lines',name=f'{method} calculated distribution',
                line=dict(color=color,width=2.5,shape='hvh'),
                hovertemplate=direction+' %{x:g}<br>%{y:.2%}<extra>%{fullData.name}</extra>'))
    for provider, points in prices.items():
        color, symbol, dash = SOURCE_STYLES[provider]
        add_trace(f'{provider} implied', points, color, symbol, dash, connect=False)

    heading = f'{title or "Player prop"} · {direction} {selected:g}'
    figure.update_layout(**chart_style(), hovermode='x unified',
                         margin=dict(l=55, r=16, t=60, b=55),
                         legend=dict(orientation='h', y=1.04, yanchor='bottom', font=dict(size=11)))
    figure.update_xaxes(title=f'{direction} line', gridcolor='#223047', automargin=True)
    figure.update_yaxes(title='Win probability', tickformat='.0%', range=[0, 1], gridcolor='#223047', automargin=True)
    figure.add_vline(x=selected, line_color='#a8b8cc', line_dash='dot', layer='below')
    return html.Section([
        html.Div(html.H3(heading), className='panel-heading'),
        html.P('The model curve shows how win probability changes across stat outcomes. Market points use quoted thresholds. '
               'Sportsbook prices include bookmaker margin, and exchange prices are before fees. Whole-number sportsbook forecasts exclude pushes. '
               'For duplicate quotes at a line, each source shows its lowest implied probability.',
               className='chart-note'),
        html.P('Calculating the selected forecast…' if waiting else 'A forecast is unavailable for these lines.',
               className='chart-note', role='status') if not forecasts and not payloads else None,
        html.Ul(values, className='prop-chart-values', **{'aria-label': f'Probabilities at {direction} {selected:g}'}) if values else None,
        dcc.Graph(figure=figure, config={'displaylogo': False}, responsive=True, style={'height': '390px'})
            if figure.data else html.P('No quoted or forecast probabilities are available.', className='chart-note'),
    ], className='panel ppp-chart prop-probability-chart'+(' prop-quotes-only' if not forecasts and not payloads else ''), tabIndex=-1,
       **{'aria-label': heading+' probability comparison'})
