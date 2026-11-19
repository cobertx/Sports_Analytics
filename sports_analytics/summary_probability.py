"""Forecast and source-implied probabilities at comparable game-line thresholds."""
from collections import defaultdict
from math import inf

from dash import dcc, html
import plotly.graph_objects as go

from .kalshi_view import team_identity
from .market_view import criteria_match, probability_text
from .market_summary_view import odds_text, shared_estimate
from .presentation import chart_style


SOURCE_STYLES = {
    'FanDuel': ('#75baff', 'diamond', 'solid'),
    'DraftKings': ('#ffac74', 'triangle-down', 'dashdot'),
    'Kalshi': ('#8debab', 'square', 'dash'),
    'Polymarket': ('#b7b6ff', 'triangle-up', 'dot'),
}


def game_line_point(row):
    outcome = row.get('comparison')
    if not outcome or outcome['category'] != 'Game Lines':
        return None
    _, market, subject, _, side, _ = outcome['key']
    if market in {'alternate_spreads', 'alternate_totals'}:
        return (market, subject if market == 'alternate_spreads' else side), outcome['line']
    return None


def game_line_quotes(rows):
    groups = defaultdict(dict)
    for row in rows:
        point = game_line_point(row)
        if point is None:
            continue
        group,line = point
        outcome = row['comparison']
        _, _, quotes = groups[group].setdefault(line, (outcome['label'], outcome['key'][5], {}))
        provider = row['provider']
        previous = quotes.get(provider)
        if previous is None or (row['implied'] if row['implied'] is not None else inf) < (
            previous['implied'] if previous['implied'] is not None else inf
        ):
            quotes[provider] = row
    return groups


def best_matches(rows):
    groups = defaultdict(dict)
    for row in rows:
        point = game_line_point(row)
        if point is None:
            continue
        group,line = point
        best = groups[group].get(line)
        if best is None or row['edge'] > best['edge']:
            groups[group][line] = row
    return groups


def comparison_figure(points, method, axis_title, matches=None, match_label='Meets criteria'):
    figure = go.Figure()
    labels = [[label, 'Pushes excluded' if push else 'Full settlement probability']
              for label, push, _ in points.values()]
    forecast = [shared_estimate(quotes)['probability'] for _, _, quotes in points.values()]
    if any(value is not None for value in forecast):
        figure.add_trace(go.Scatter(
            x=list(points), y=forecast, name=f'{method} forecast', mode='lines+markers',
            line=dict(color='#57dbc4', width=2.5), marker=dict(size=5), connectgaps=False,
            fill='tozeroy', fillcolor='rgba(87, 219, 196, 0.13)', customdata=labels,
            hovertemplate='%{customdata[0]}<br>%{y:.2%} · %{customdata[1]}<extra>%{fullData.name}</extra>'))
    for provider, (color, symbol, dash) in SOURCE_STYLES.items():
        quoted = [(line, label, quotes[provider]) for line, (label, _, quotes) in points.items() if provider in quotes]
        if not any(row['implied'] is not None for _, _, row in quoted):
            continue
        figure.add_trace(go.Scatter(
            x=[line for line, _, _ in quoted], y=[row['implied'] for _, _, row in quoted],
            name=f'{provider} implied', mode='lines+markers', connectgaps=False,
            line=dict(color=color, width=2, dash=dash), marker=dict(symbol=symbol, size=7),
            customdata=[[label, odds_text(row['odds'], row['implied'])] for _, label, row in quoted],
            hovertemplate='%{customdata[0]}<br>%{y:.2%} · Odds %{customdata[1]}<extra>%{fullData.name}</extra>'))
    if matches:
        # A missing or unqualified line breaks the gold segment.
        qualified = [matches.get(line) for line in points]
        figure.add_trace(go.Scatter(
            x=list(points), y=[row['probability'] if row else None for row in qualified],
            name=match_label, mode='lines+markers', connectgaps=False,
            line=dict(color='#f6c667',width=4), marker=dict(color='#f6c667',size=9,symbol='circle',
            line=dict(color='#111d2e',width=1)), fill='tozeroy', fillcolor='rgba(246,198,103,.2)',
            customdata=[[label,row['provider'],row['method'],probability_text(row['implied']),f"{100*row['edge']:+.2f} pp"]
                        if row else None for (label,_,_),row in zip(points.values(),qualified)],
            hovertemplate='%{customdata[0]}<br>%{customdata[1]} · %{customdata[2]} %{y:.2%}'
                          '<br>Market %{customdata[3]}<br>Edge %{customdata[4]}<extra>%{fullData.name}</extra>'))
    figure.update_layout(**chart_style(), hovermode='x unified',
                         margin=dict(l=55, r=12, t=65, b=60),
                         legend=dict(orientation='h', y=1.04, yanchor='bottom', font=dict(size=11)))
    figure.update_xaxes(title=axis_title, gridcolor='#223047', automargin=True)
    figure.update_yaxes(title='Win probability', tickformat='.0%', range=[0, 1], gridcolor='#223047', automargin=True)
    return figure


def panel(bundle, rows, method, waiting=False, *, criteria=None, quick_rows=None, quick_status=None):
    groups = game_line_quotes(rows)
    quick = quick_rows is not None
    candidates = quick_rows if quick else (
        (row for row in rows if criteria_match(row['probability'],row['edge'],criteria)) if criteria else ())
    matches = best_matches(candidates)
    teams = [team_identity(bundle['game'][side]['name'], bundle['sport'])['name'] for side in ('home', 'away')]
    specs = [
        *((('alternate_spreads', ' '.join(team.casefold().split())), f'Spread · {team}', 'Team handicap (points)') for team in teams),
        (('alternate_totals', 'gt'), 'Game total · Over', 'Game total line (points)'),
        (('alternate_totals', 'lt'), 'Game total · Under', 'Game total line (points)'),
    ]
    charts = []
    has_forecast = False
    matched = 0
    for key, title, axis_title in specs:
        points = groups.get(key)
        if not points:
            continue
        qualifying = {line:row for line,row in matches.get(key,{}).items() if line in points}
        matched += len(qualifying)
        figure = comparison_figure(dict(sorted(points.items())), method, axis_title, qualifying,
                                   'Quick Filtering matches' if quick else 'Meets criteria')
        if figure.data:
            has_forecast |= figure.data[0].name == f'{method} forecast'
            charts.append(html.Section([
                html.Div(html.H3(title), className='panel-heading'),
                dcc.Graph(figure=figure, config={'displaylogo': False}, responsive=True, style={'height': '370px'}),
            ], className='panel ppp-chart', role='group', **{'aria-label':title + ' probability comparison'}))
    if not charts:
        return html.P('No comparable spread or game total probabilities are available for this game.', className='chart-note')
    status = quick_status
    if criteria and status is None:
        source = 'Quick Filtering' if quick else 'Apply Criteria'
        rule = f"{source} · Probability > {100*criteria['probability']:g}% and edge > {100*criteria['edge']:g} pp"
        if quick or has_forecast:
            status = rule+f' · {matched:,} qualifying '+('line' if matched==1 else 'lines')+'.'
        else:
            status = rule+'. Highlights appear when forecast and quote values are available.'
    return [
        html.P('Each point shows the chance of winning at that line. Whole-point sportsbook forecasts exclude pushes. '
               'Sportsbook implied probabilities include bookmaker margin. Exchange implied probabilities use the buying price '
               'before fees. When a source lists multiple equivalent quotes, the chart uses its lowest implied probability.',
               className='chart-note'),
        html.P('Calculating the selected forecast…' if waiting else f'{method} forecast unavailable for these lines.',
               className='chart-note', role='status') if not has_forecast else None,
        html.P(status+' Gold shows the highest-edge qualifying quote at each line.' if matched else status,
               className='summary-chart-match-status',role='status') if status else None,
        html.Div(charts, className='ppp-comparison-grid'),
    ]
