"""Isolated callbacks for recorded NBA/NFL in-game exploration."""
from dash import ALL, Input, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate

from .data import DataSourceError
from .in_game_data import NFL_VERSION
from . import in_game_view as view
from . import nfl_in_game


def register(app, service):
    @app.callback(Output('workspace-page', 'value', allow_duplicate=True),
                  Input('sport', 'value'), State('workspace-page', 'value'), prevent_initial_call=True)
    def in_game_supported_page(sport, page):
        if page=='pnl' and sport!='PNL':return 'historical'
        if page in {'in-game','information'} and sport not in {'NBA', 'NFL'}:
            return 'historical'
        raise PreventUpdate

    @app.callback(Output('in-game-page', 'style'), Output('in-game-page-link', 'style'),
                  Output('in-game-page-link', 'href'), Output('in-game-page-link', 'className'),
                  Output('in-game-shot-filters', 'style'), Output('in-game-court-filter', 'style'),
                  Output('in-game-play-filters', 'style'), Output('in-game-drive-filter', 'style'),
                  Output('in-game-nfl-panel', 'style'),
                  Input('workspace-page', 'value'), Input('sport', 'value'))
    def in_game_visibility(page, sport):
        supported = sport in {'NBA', 'NFL'}
        nba, nfl = ({}, {'display': 'none'}) if sport == 'NBA' else ({'display': 'none'}, {})
        return ({'display': 'grid' if supported and page == 'in-game' else 'none'},
                {} if supported else {'display': 'none'}, f'#{sport.lower()}-in-game-data',
                'sport-page-link' + (' active' if page == 'in-game' else ''), nba, nba, nfl, nfl,
                {} if sport == 'NFL' else {'display': 'none'})

    @app.callback(Output('in-game-catalog', 'data'), Output('in-game-list-status', 'children'),
                  Input('workspace-page', 'value'), Input('sport', 'value'), Input('in-game-season', 'value'),
                  Input('in-game-phase', 'value'), Input('in-game-reload-list', 'n_clicks'),
                  State('in-game-catalog', 'data'))
    def in_game_catalog(page, sport, season, phase, _reload, previous):
        if page != 'in-game' or sport not in {'NBA', 'NFL'}:
            raise PreventUpdate
        scope = dict(sport=sport, season=season, phase=phase)
        refresh = ctx.triggered_id == 'in-game-reload-list'
        if not refresh and previous and all(previous.get(k) == v for k, v in scope.items()):
            raise PreventUpdate
        try:
            result = service.catalog(sport, int(season), phase, refresh=refresh)
            count = len(result['games'])
            status = f'{count} recorded games. Select a team and game below.' if count else 'No recorded games are available for this season and phase.'
            return result, status + (' ' + result['notice'] if result.get('notice') else '')
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return {**scope, 'games': []}, f'Game list unavailable: {exc} Use Refresh game list to retry.'

    @app.callback(Output('in-game-team', 'options'), Output('in-game-team', 'value'),
                  Input('in-game-catalog', 'data'), State('in-game-team', 'value'))
    def in_game_teams(catalog, previous):
        teams = sorted({team for game in (catalog or {}).get('games', []) for team in game['teams']})
        return [dict(label='All teams', value=view.ALL)] + [dict(label=t, value=t) for t in teams], previous if previous in teams else view.ALL

    @app.callback(Output('in-game-game', 'options'), Output('in-game-game', 'value'),
                  Input('in-game-catalog', 'data'), Input('in-game-team', 'value'), State('in-game-game', 'value'))
    def in_game_games(catalog, team, previous):
        games = [g for g in (catalog or {}).get('games', []) if team == view.ALL or team in g['teams']]
        options = [dict(label=f"{g['date']} · {g['matchup']}" + (f" · Week {g['week']}" if g.get('week') else ''), value=g['id']) for g in games]
        identities = {g['id'] for g in games}
        return options, previous if previous in identities else games[0]['id'] if games else None

    @app.callback(Output('in-game-events', 'data'), Output('in-game-source', 'children'),
                  Output('in-game-matchup', 'children'),
                  Input('in-game-game', 'value'), Input('in-game-catalog', 'data'), Input('in-game-refresh', 'n_clicks'),
                  Input('workspace-page', 'value'), Input('sport', 'value'),
                  Input('in-game-charting-load', 'n_clicks'), State('in-game-events', 'data'),
                  running=[(Output('in-game-charting-load', 'disabled'), True, False)])
    def in_game_load(game_id, catalog, _refresh, page, sport, _charting=0, previous=None):
        if page != 'in-game' or sport not in {'NBA', 'NFL'}:
            raise PreventUpdate
        games = (catalog or {}).get('games', [])
        game = next((g for g in games if g['id'] == game_id), None)
        if not catalog or catalog.get('sport') != sport or not game:
            return None, 'Choose a recorded game to load its events.', ''
        refresh = ctx.triggered_id == 'in-game-refresh'
        charting = sport == 'NFL' and ctx.triggered_id == 'in-game-charting-load'
        if (not refresh and not charting and previous and (sport != 'NFL' or previous.get('version') == NFL_VERSION)
                and (previous.get('sport'), previous.get('season'), previous.get('game_id')) == (sport, catalog['season'], game_id)):
            return no_update, no_update, f"{game['date']} · {game['matchup']}"
        try:
            result = service.game(sport, catalog['season'], game_id, refresh=refresh, **({'charting': True} if charting else {}))
            status = f"{result['source']} · Source saved/checked {result['source_at']} · {len(result['events'])} events."
            if not result['events']:
                status += ' No shot/play events are available for this game yet; try another game or refresh later.'
            if result.get('notice'):
                status += ' ' + result['notice']
            return result, status, f"{game['date']} · {game['matchup']}"
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return None, f'Game data unavailable: {exc} Try another game or Refresh game data.', f"{game['date']} · {game['matchup']}"

    @app.callback(Output('in-game-offense', 'options'), Output('in-game-offense', 'value'),
                  Output('in-game-period', 'options'), Output('in-game-period', 'value'),
                  Input('in-game-events', 'data'), State('in-game-offense', 'value'), State('in-game-period', 'value'))
    def in_game_event_options(data, previous_team, previous_period):
        events = (data or {}).get('events', [])
        teams = sorted({r['team'] for r in events if r['team']})
        periods = sorted({r['period'] for r in events if r['period'] is not None})
        return ([dict(label='Both teams', value=view.ALL)] + [dict(label=t, value=t) for t in teams],
                previous_team if previous_team in teams else view.ALL,
                [dict(label='All periods', value=view.ALL)] + [dict(label=view.period_label(p), value=str(p)) for p in periods],
                previous_period if str(previous_period) in {str(p) for p in periods} else view.ALL)

    @app.callback(Output('in-game-player', 'options'), Output('in-game-player', 'value'),
                  Output('in-game-drive', 'options'), Output('in-game-drive', 'value'),
                  Input('in-game-events', 'data'), Input('in-game-offense', 'value'),
                  State('in-game-player', 'value'), State('in-game-drive', 'value'))
    def in_game_player_options(data, offense, previous_player, previous_drive):
        sport = (data or {}).get('sport', 'NBA')
        events = [r for r in (data or {}).get('events', []) if offense == view.ALL or r['team'] == offense]
        players = {r['player_id']: r['player'] for r in events} if sport == 'NBA' else {
            r[k]: r[k] for r in events for k in ['passer', 'rusher', 'receiver'] if r[k]}
        drives = nfl_in_game.drives(events) if sport == 'NFL' else {}
        selected_drive = previous_drive if previous_drive == view.ALL or previous_drive in drives else next(iter(drives), view.ALL)
        return ([dict(label='All players', value=view.ALL)] + [dict(label=name, value=key) for key, name in sorted(players.items(), key=lambda pair: pair[1])],
                previous_player if previous_player in players else view.ALL,
                [dict(label='All drives', value=view.ALL)] + [dict(label=nfl_in_game.drive_label(rows), value=key) for key, rows in drives.items()],
                selected_drive if sport == 'NFL' else None)

    @app.callback(Output('in-game-drive', 'value', allow_duplicate=True),
                  Input({'type': 'in-game-drive-button', 'drive': ALL}, 'n_clicks'),
                  State('in-game-events', 'data'), State('in-game-offense', 'value'), prevent_initial_call=True)
    def in_game_choose_drive(clicks, data, offense):
        clicked = ctx.triggered_id
        if not isinstance(clicked, dict) or not any(clicks or []):
            raise PreventUpdate
        events = [r for r in (data or {}).get('events', []) if offense == view.ALL or r['team'] == offense]
        if clicked['drive'] == view.ALL or clicked['drive'] in nfl_in_game.drives(events):
            return clicked['drive']
        raise PreventUpdate

    @app.callback(Output('in-game-drive-navigation', 'children'),
                  Input('in-game-events', 'data'), Input('in-game-offense', 'value'),
                  Input('in-game-drive', 'value'), Input('sport', 'value'))
    def in_game_drive_navigation(data, offense, drive, sport):
        if sport != 'NFL' or not data or data.get('sport') != 'NFL':
            return []
        events = [r for r in data['events'] if offense == view.ALL or r['team'] == offense]
        return nfl_in_game.drive_navigation(events, drive)

    @app.callback(Output('in-game-nfl-insights', 'children'),
                  Input('in-game-events', 'data'), Input('in-game-offense', 'value'), Input('in-game-player', 'value'),
                  Input('in-game-period', 'value'), Input('in-game-kind', 'value'), Input('in-game-drive', 'value'),
                  Input('in-game-event-select', 'value'), Input('sport', 'value'), Input('in-game-game', 'value'))
    def in_game_nfl_insights(data, offense, player, period, kind, drive, event_id, sport, game_id):
        if sport != 'NFL' or not data or data.get('sport') != 'NFL' or data.get('game_id') != game_id:
            return []
        events = view.filter_events(data['events'], 'NFL', offense, player, period, kind=kind, drive=drive)
        if event_id != view.ALL:
            events = [r for r in events if r['id'] == event_id]
        return nfl_in_game.insights(events, data)

    @app.callback(Output('in-game-event-select', 'options'), Output('in-game-event-select', 'value'),
                  Input('in-game-events', 'data'), Input('in-game-offense', 'value'), Input('in-game-player', 'value'),
                  Input('in-game-period', 'value'), Input('in-game-result', 'value'), Input('in-game-kind', 'value'),
                  Input('in-game-drive', 'value'), State('in-game-event-select', 'value'))
    def in_game_event_choices(data, offense, player, period, result, kind, drive, previous):
        options = [dict(label='All filtered events', value=view.ALL)]
        if not data:
            return options, view.ALL
        rows = view.filter_events(data['events'], data['sport'], offense, player, period, result, kind, drive)
        options += [dict(label=f"#{r['id']} · {view.period_label(r['period']) if r['period'] else '?'} "
                              f"{view.clock_label(r['clock'])} · {r['description'][:150]}", value=r['id']) for r in rows]
        return options, previous if ctx.triggered_id != 'in-game-events' and previous in {r['id'] for r in rows} else view.ALL

    @app.callback(Output('in-game-chart', 'figure'), Output('in-game-summary', 'children'),
                  Output('in-game-chart-note', 'children'), Output('in-game-table', 'columns'), Output('in-game-table', 'data'),
                  Input('in-game-events', 'data'), Input('in-game-offense', 'value'), Input('in-game-player', 'value'),
                  Input('in-game-period', 'value'), Input('in-game-result', 'value'), Input('in-game-kind', 'value'),
                  Input('in-game-drive', 'value'), Input('in-game-court', 'value'), Input('sport', 'value'),
                  Input('in-game-game', 'value'), Input('in-game-catalog', 'data'), Input('in-game-event-select', 'value'))
    def in_game_render(data, offense, player, period, result, kind, drive, court, sport, game_id, catalog, event_id=view.ALL):
        nba = sport != 'NFL'
        valid = data and catalog and (data.get('sport'), data.get('season'), data.get('game_id')) == (sport, catalog.get('season'), game_id)
        events = view.filter_events(data['events'], sport, offense, player, period, result, kind, drive) if valid else []
        if event_id != view.ALL:
            events = [r for r in events if r['id'] == event_id]
        figure = view.court_figure(events, court == 'full') if nba else view.football_figure(events)
        if not events:
            figure.add_annotation(x=.5, y=.5, xref='paper', yref='paper', text='No events for this game / filter selection.',
                                  showarrow=False, bgcolor='#111d2e', borderpad=12, font={'color': '#e7edf6'})
        if nba:
            missing = sum(not view.located_shot(r) for r in events)
            heaves = sum(view.located_shot(r) and r['y'] > 41.75 for r in events) if court != 'full' else 0
            note = ('Green circles = made; red crosses = missed. Recorded field-goal attempts only (free throws excluded). '
                    'Coordinates are in feet relative to the attacking basket; all shots face the same basket. '
                    'Summary and table include every filtered attempt, including attempts outside the displayed view. '
                    f'{missing} attempts lack a valid on-court location and are omitted from the plot. ')
            if heaves:
                note += f'{heaves} attempts beyond half court are hidden; choose Full court to see them. '
        else:
            missing = sum(not view.located_play(r) for r in events)
            note = ('Open circle = line of scrimmage; filled marker = end of recorded yardage; star = offensive TD; X = turnover. '
                    'Numbers show the offensive-play sequence within a single selected drive. '
                    'All offenses advance toward the right. Horizontal position and gain use recorded yards. '
                    'Vertical lanes are schematic left/middle/right categories, not measured routes or player tracking; N/A means direction unavailable. '
                    'Passes include sacks; runs include scrambles. Incomplete passes use recorded gain, not intended air yards. '
                    'Penalties marked no-play and special teams are excluded. Use team/drive filters or select an individual play to separate overlapping plays. '
                    f'{missing} plays lack usable field position/gain and are omitted from the plot. '
                    'Summary averages use only nonmissing yards/EPA within the filtered sample.')
        columns, table = view.event_table(events, 'NBA' if nba else 'NFL')
        figure.update_layout(uirevision=f'{sport}:{game_id}:{court if nba else "field"}')
        return figure, view.summary(events, 'NBA' if nba else 'NFL'), note, columns, table
