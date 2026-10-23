"""Interactive, dimensioned Plotly courts/fields and in-game page controls."""
from datetime import date
from html import escape
import math
import re

from dash import dash_table, dcc, html
import plotly.graph_objects as go
from . import nfl_in_game

ALL = '__all__'
COLORS = {'made': '#127052', 'missed': '#c63b50', 'pass': '#67d8ff', 'run': '#ffcf67',
          'qb_kneel': '#d3bdff', 'qb_spike': '#ffa4c2'}
LANES = {'left': 42, 'middle': 29, 'right': 16, 'unknown': 5}


def field(label, control):
    return html.Div([html.Label(label, htmlFor=control.id), control], className='field')


def dropdown(identity, options=None, value=ALL):
    return dcc.Dropdown(id='in-game-' + identity, options=options or [], value=value, clearable=False)


def layout():
    return html.Section([
        dcc.Store(id='in-game-catalog'), dcc.Store(id='in-game-events'),
        html.Section([
            html.Div([html.Div([html.Div('PLAY BY PLAY', className='eyebrow accent'),
                                html.H2('Visualized In-Game Data'),
                                html.P('Explore recorded shots and offensive plays, one game at a time.')]),
                      html.Button('Refresh game list', id='in-game-reload-list', n_clicks=0, className='export-button')],
                     className='in-game-heading'),
            html.Div([
                field('Season starting year', dropdown('season', list(range(date.today().year, 1998, -1)), date.today().year - 1)),
                field('Season phase', dropdown('phase', [dict(label='Regular season', value='regular'),
                                                       dict(label='Postseason', value='postseason')], 'regular')),
                field('Team in game list', dropdown('team')),
                field('Game', dropdown('game', value=None)),
            ], className='in-game-scope'),
            html.P('NBA seasons use the starting year: 2025 means 2025–26. Saved files are reused. '
                   'Loading an uncached NFL season downloads its play-by-play once; later games use a local index. '
                   'Source coverage varies by season. These are recorded events, not a live tracking feed.', className='in-game-help'),
            dcc.Loading(html.Div(id='in-game-list-status', className='load-status', role='status'), color='#57dbc4'),
        ], className='panel in-game-setup'),
        dcc.Loading(html.Div([
            html.Div([html.H2(id='in-game-matchup'),
                      html.Button('Refresh game data', id='in-game-refresh', n_clicks=0, className='export-button')], className='in-game-heading'),
            html.Div(id='in-game-source', className='load-status', role='status'),
        ], className='in-game-game-heading'), color='#57dbc4'),
        html.Section([
            html.Div([
                field('Offense / shooting team', dropdown('offense')),
                field('Player', dropdown('player')),
                field('Quarter / overtime', dropdown('period')),
                html.Div(field('Shot result', dropdown('result', [dict(label='All shots', value=ALL),
                    dict(label='Made', value='made'), dict(label='Missed', value='missed')], ALL)), id='in-game-shot-filters'),
                html.Div(field('Court view', dropdown('court', [dict(label='Half court (heaves hidden)', value='half'),
                    dict(label='Full court', value='full')], 'half')), id='in-game-court-filter'),
                html.Div(field('Play type', dropdown('kind', [dict(label='All offensive plays', value=ALL),
                    dict(label='Pass (includes sacks)', value='pass'), dict(label='Run (includes scrambles)', value='run'),
                    dict(label='QB kneel', value='qb_kneel'), dict(label='QB spike', value='qb_spike')])), id='in-game-play-filters'),
                html.Div(field('Drive', dropdown('drive', value=None)), id='in-game-drive-filter'),
                field('Individual shot / play', dropdown('event-select', [dict(label='All filtered events', value=ALL)])),
            ], className='in-game-filters'),
            html.Div(id='in-game-drive-navigation'),
            html.Div(id='in-game-summary', className='in-game-summary'),
            dcc.Graph(id='in-game-chart', figure=court_figure([]), config={'displaylogo': False, 'scrollZoom': False,
                      'toImageButtonOptions': {'format': 'png', 'filename': 'in_game_chart', 'scale': 2}}),
            html.P(id='in-game-chart-note', className='in-game-help', role='status'),
        ], className='panel in-game-visual'),
        html.Section([
            html.Div([html.H2('Personnel, formations & play calling'),
                html.Button('Load / refresh personnel & coverage', id='in-game-charting-load', n_clicks=0,
                            className='export-button')], className='in-game-heading'),
            html.P('Participation charting from 2023 onward is published after the season’s postseason. '
                   'In-season man/zone or personnel may be unavailable. FTN’s public play-calling subset '
                   'can be available earlier. Unknown values remain explicit.', className='in-game-help'),
            dcc.Loading(html.Div(id='in-game-nfl-insights'), color='#57dbc4'),
            html.P([html.A('FTN Data via nflverse', href='https://nflreadr.nflverse.com/reference/load_participation.html',
                target='_blank', rel='noopener noreferrer'), ' (2023 onward participation; FTN charting from 2022) · ',
                'NFL NextGenStats via nflverse (participation through 2022) · ',
                html.A('CC BY-SA 4.0', href='https://creativecommons.org/licenses/by-sa/4.0/',
                       target='_blank', rel='noopener noreferrer'),
                '. Percentages and drive summaries are computed from the source records.'], className='in-game-help'),
        ], id='in-game-nfl-panel', className='panel in-game-setup', style={'display': 'none'}),
        html.Details([html.Summary('Event details & CSV export'),
            dash_table.DataTable(id='in-game-table', data=[], columns=[], page_size=12, sort_action='native',
                filter_action='native', export_format='csv', style_table={'overflowX': 'auto'},
                style_cell={'backgroundColor': '#111d2e', 'color': '#b8c8dc', 'border': '1px solid #243249',
                            'padding': '10px', 'textAlign': 'left', 'fontFamily': 'Segoe UI', 'fontSize': '12px',
                            'minWidth': '80px', 'maxWidth': '420px', 'whiteSpace': 'normal', 'height': 'auto'},
                style_header={'backgroundColor': '#17253a', 'fontWeight': '600'})], className='panel in-game-details'),
    ], id='in-game-page', style={'display': 'none'}, className='in-game-page')


def clock_label(value):
    match = re.fullmatch(r'PT(\d+)M([\d.]+)S', str(value))
    return f'{int(match[1]):02}:{int(float(match[2])):02}' if match else str(value)


def period_label(value):
    return f'Q{value}' if value <= 4 else f'OT{value - 4}'


def base_figure(height):
    figure = go.Figure()
    figure.update_layout(template='plotly_dark', paper_bgcolor='#111d2e', plot_bgcolor='#111d2e', height=height,
        font={'family': 'Segoe UI, sans-serif', 'color': '#b8c8dc'}, hovermode='closest',
        margin={'l': 20, 'r': 20, 't': 55, 'b': 35}, legend={'orientation': 'h', 'y': 1.06, 'x': .5, 'xanchor': 'center'})
    figure.update_xaxes(visible=False, fixedrange=False)
    figure.update_yaxes(visible=False, scaleanchor='x', scaleratio=1, fixedrange=False)
    return figure


def shape(figure, kind, x0, y0, x1, y1, color, fill=None, width=1.5):
    figure.append(dict(type=kind, x0=x0, y0=y0, x1=x1, y1=y1,
                       line={'color': color, 'width': width}, fillcolor=fill or 'rgba(0,0,0,0)', layer='below'))


def arc(figure, cx, cy, radius, start, end, color):
    points = [(cx + radius * math.cos(start + (end - start) * i / 80),
               cy + radius * math.sin(start + (end - start) * i / 80)) for i in range(81)]
    path = 'M ' + ' L '.join(f'{x:.5f},{y:.5f}' for x, y in points)
    figure.append(dict(type='path', path=path, line={'color': color, 'width': 1.8}, layer='below'))


def court_background(figure, full=False):
    shapes = []
    line = '#704c2e'
    top = 88.75 if full else 41.75
    shape(shapes, 'rect', -25, -5.25, 25, top, line, '#dfb77b')
    # Alternating maple boards keep the background crisp at every zoom level.
    for i in range(25):
        shape(shapes, 'rect', -25 + i * 2, -5.25, -23 + i * 2, top, '#b9905d',
              '#e8c58b' if i % 2 else '#dcb278', .3)
    shape(shapes, 'rect', -25, -5.25, 25, top, line, width=2.3)
    for opposite in ([False, True] if full else [False]):
        def y(value):
            return 83.5 - value if opposite else value
        shape(shapes, 'rect', -8, min(y(-5.25), y(13.75)), 8, max(y(-5.25), y(13.75)), line, '#8dafae')
        shape(shapes, 'rect', -6, min(y(-5.25), y(13.75)), 6, max(y(-5.25), y(13.75)), line)
        shape(shapes, 'circle', -6, y(13.75) - 6, 6, y(13.75) + 6, line)
        angle = math.acos(22 / 23.75)
        if opposite:
            arc(shapes, 0, 83.5, 23.75, math.pi + angle, 2 * math.pi - angle, line)
            arc(shapes, 0, 83.5, 4, math.pi, 2 * math.pi, line)
        else:
            arc(shapes, 0, 0, 23.75, angle, math.pi - angle, line)
            arc(shapes, 0, 0, 4, 0, math.pi, line)
        for x in [-22, 22]:
            shape(shapes, 'line', x, y(-5.25), x, y(math.sqrt(23.75 ** 2 - 22 ** 2)), line)
        shape(shapes, 'line', -3, y(-1.25), 3, y(-1.25), '#f6f1df', width=3)
        shape(shapes, 'circle', -.75, y(0) - .75, .75, y(0) + .75, '#df712d', width=2.5)
    shape(shapes, 'line', -25, 41.75, 25, 41.75, line)
    shape(shapes, 'circle', -6, 35.75, 6, 47.75, line)
    figure.update_layout(shapes=shapes)
    figure.update_xaxes(range=[-27, 27], constrain='domain')
    figure.update_yaxes(range=[-7.5, top + 2], constrain='domain')


def located_shot(row):
    return row.get('x') is not None and row.get('y') is not None and -25 <= row['x'] <= 25 and -5.25 <= row['y'] <= 88.75


def court_figure(events, full=False):
    figure = base_figure(760 if full else 650)
    court_background(figure, full)
    for made, name, color, symbol in [(True, 'Made', COLORS['made'], 'circle'),
                                       (False, 'Missed', COLORS['missed'], 'x'),
                                       (None, 'Result unavailable', '#554c73', 'diamond')]:
        rows = [r for r in events if r.get('made') is made and located_shot(r) and (full or r['y'] <= 41.75)]
        if not rows:
            continue
        figure.add_trace(go.Scatter(x=[r['x'] for r in rows], y=[r['y'] for r in rows], name=name, mode='markers',
            marker={'color': color, 'size': 10, 'symbol': symbol, 'opacity': .86, 'line': {'color': '#fff2d6', 'width': .7}},
            customdata=[[escape(r['player']), escape(r['team']), period_label(r['period']) if r['period'] else '?',
                         clock_label(r['clock']), escape(r['description'])] for r in rows],
            hovertemplate='<b>%{customdata[0]} · %{customdata[1]}</b><br>%{customdata[2]} · %{customdata[3]}'
                          '<br>%{customdata[4]}<br>Basket-relative: %{x:.1f}, %{y:.1f} ft<extra>' + name + '</extra>'))
    figure.update_layout(uirevision='nba-full' if full else 'nba-half')
    return figure


def field_background(figure):
    shapes = []
    white = '#e7f0da'
    shape(shapes, 'rect', -10, 0, 110, 53.3333, white, '#236943', 2)
    for start in range(0, 100, 5):
        shape(shapes, 'rect', start, 0, start + 5, 53.3333, 'rgba(0,0,0,0)', '#2d7a4c' if start % 10 else '#286f46', 0)
    for start in [-10, 100]:
        shape(shapes, 'rect', start, 0, start + 10, 53.3333, white, '#163e38', 1)
    for yard in range(0, 101, 5):
        shape(shapes, 'line', yard, 0, yard, 53.3333, white, width=1.3 if yard % 10 else 2)
    # NFL hash marks are 18 feet, 6 inches apart (6 1/6 yards).
    for yard in range(1, 100):
        for y in [23.5833, 29.75]:
            shape(shapes, 'line', yard, y - .3, yard, y + .3, white, width=.7)
    for yard in range(10, 100, 10):
        for y, angle in [(9, 0), (44.3333, 180)]:
            figure.add_annotation(x=yard, y=y, text=str(min(yard, 100 - yard)), showarrow=False,
                                  font={'size': 17, 'color': white}, textangle=angle, opacity=.65)
    for x, label in [(-5, 'OWN END ZONE'), (105, 'OPP END ZONE')]:
        figure.add_annotation(x=x, y=26.6667, text=label, textangle=-90, showarrow=False,
                              font={'size': 13, 'color': white})
    for lane, y in LANES.items():
        figure.add_annotation(x=-12, y=y, text=lane.title() if lane != 'unknown' else 'N/A', showarrow=False,
                              xanchor='right', font={'size': 11, 'color': '#b8c8dc'})
    figure.add_annotation(x=50, y=57, text='OFFENSE ATTACKS →  ·  ALL DRIVES NORMALIZED', showarrow=False,
                          font={'size': 11, 'color': '#b8c8dc'})
    figure.update_layout(shapes=shapes)
    figure.update_xaxes(range=[-22, 114], constrain='domain')
    figure.update_yaxes(range=[-2, 60], constrain='domain')


def located_play(row):
    return row.get('start') is not None and row.get('end') is not None and -10 <= row['end'] <= 110


def football_figure(events):
    figure = base_figure(620)
    field_background(figure)
    for kind, label in [('pass', 'Pass / sack'), ('run', 'Run / scramble'), ('qb_kneel', 'QB kneel'), ('qb_spike', 'QB spike')]:
        rows = [r for r in events if r['kind'] == kind and located_play(r)]
        if not rows:
            continue
        xs, ys = [], []
        for r in rows:
            xs += [r['start'], r['end'], None]
            ys += [LANES[r['direction']], LANES[r['direction']], None]
        figure.add_trace(go.Scatter(x=xs, y=ys, mode='lines', line={'color': COLORS[kind], 'width': 2},
                                    opacity=.5, showlegend=False, hoverinfo='skip'))
        single_drive = len({nfl_in_game.drive_key(r) for r in events}) == 1
        figure.add_trace(go.Scatter(x=[r['end'] for r in rows], y=[LANES[r['direction']] for r in rows],
            mode='markers+text' if single_drive else 'markers', name=label,
            text=[str(r.get('drive_play_number', '')) for r in rows], textposition='top center',
            textfont={'color': '#fff3d6', 'size': 12},
            marker={'size': 11, 'color': COLORS[kind], 'line': {'color': '#173d2a', 'width': 1},
                'symbol': ['star' if r['touchdown'] else 'x' if r['interception'] or r['fumble_lost'] else
                           'triangle-left' if r['yards'] < 0 else 'circle' if r['yards'] == 0 else 'triangle-right' for r in rows]},
            customdata=[[escape(r['team']), period_label(r['period']) if r['period'] else '?', clock_label(r['clock']),
                         r['drive'], r['down'], r['to_go'], r['yards'], r['epa'], escape(r['description']),
                         escape(r.get('defense', '')), r.get('drive_play_number', ''),
                         escape(r.get('offense_personnel') or 'Unknown'), escape(r.get('defense_personnel') or 'Unknown'),
                         escape(r.get('coverage_family') or 'Unknown'), escape(r.get('coverage_scheme') or 'Unknown')] for r in rows],
            hovertemplate='<b>%{customdata[0]}</b> · %{customdata[1]} · %{customdata[2]}<br>Drive %{customdata[3]}'
                ' · Offensive play %{customdata[10]} · Defense %{customdata[9]}'
                '<br>Down %{customdata[4]} · %{customdata[5]} to go<br>Gain: %{customdata[6]} yd · EPA: %{customdata[7]}'
                '<br>Offensive personnel: %{customdata[11]}<br>Defensive personnel: %{customdata[12]}'
                '<br>Coverage: %{customdata[13]} · %{customdata[14]}'
                '<br>%{customdata[8]}<extra>' + label + '</extra>'))
        figure.add_trace(go.Scatter(x=[r['start'] for r in rows], y=[LANES[r['direction']] for r in rows],
            mode='markers', marker={'size': 5, 'color': COLORS[kind], 'symbol': 'circle-open'},
            showlegend=False, hoverinfo='skip'))
    figure.update_layout(uirevision='nfl-field')
    return figure


def filter_events(events, sport, offense=ALL, player=ALL, period=ALL, result=ALL, kind=ALL, drive=ALL):
    rows = [r for r in events if (offense == ALL or r['team'] == offense) and
            (period == ALL or str(r['period']) == str(period))]
    if sport == 'NBA':
        return [r for r in rows if (player == ALL or r['player_id'] == player) and
                (result == ALL or r['made'] is (result == 'made'))]
    return [r for r in rows if (player == ALL or player in {r['passer'], r['rusher'], r['receiver']}) and
            (kind == ALL or r['kind'] == kind) and (drive == ALL or r['drive'] == drive or nfl_in_game.drive_key(r) == drive)]


def summary(events, sport):
    if sport == 'NBA':
        made = sum(r['made'] is True for r in events)
        known = sum(r['made'] is not None for r in events)
        values = [('Attempts', str(len(events))), ('Made / missed', f'{made} / {known - made}'),
                  ('FG % (known results)', f'{made / known:.1%}' if known else '—'),
                  ('3PT made / attempts', f"{sum(r['made'] is True and r['value'] == 3 for r in events)} / {sum(r['value'] == 3 for r in events)}")]
    else:
        yards = [r['yards'] for r in events if r['yards'] is not None]
        epa = [r['epa'] for r in events if r['epa'] is not None]
        values = [('Plays', str(len(events))), ('Recorded net yards', f'{sum(yards):g}' if yards else '—'),
                  ('Yards / recorded play', f'{sum(yards) / len(yards):.1f}' if yards else '—'),
                  ('EPA / recorded play', f'{sum(epa) / len(epa):+.2f}' if epa else '—')]
    return [html.Div([html.Span(label), html.Strong(value)], className='in-game-stat') for label, value in values]


def event_table(events, sport):
    common = [('Event', 'id'), ('Team', 'team'), ('Period', 'period'), ('Clock', 'clock')]
    fields = common + ([('Player', 'player'), ('Result', 'result'), ('Points attempted', 'value'),
                       ('X (ft)', 'x'), ('Y (ft)', 'y')] if sport == 'NBA' else
                      [('Drive', 'drive'), ('Type', 'kind'), ('Down', 'down'), ('To go', 'to_go'),
                       ('Passer', 'passer'), ('Rusher', 'rusher'), ('Receiver / target', 'receiver'),
                       ('Direction', 'direction'), ('LOS from own goal (yd)', 'start'),
                       ('Yards gained', 'yards'), ('EPA', 'epa'), ('Defense', 'defense'), ('Play call', 'call'),
                       ('Offensive personnel', 'offense_personnel'), ('Defensive personnel', 'defense_personnel'),
                       ('Offensive formation', 'offense_formation'), ('Defensive formation', 'defense_formation'),
                       ('Man / zone', 'coverage_family'), ('Coverage scheme', 'coverage_scheme'),
                       ('Defenders in box', 'defenders_in_box'), ('Pass rushers', 'pass_rushers'), ('Blitzers', 'blitzers'),
                       ('Play action', 'play_action'), ('RPO', 'rpo')]) + [('Description', 'description')]
    rows = []
    for r in events:
        row = {key: r.get(key) for _, key in fields}
        row['clock'] = clock_label(r['clock'])
        if sport == 'NBA':
            row['result'] = 'Made' if r['made'] is True else 'Missed' if r['made'] is False else 'Unavailable'
        rows.append(row)
    return [dict(name=label, id=key) for label, key in fields], rows
