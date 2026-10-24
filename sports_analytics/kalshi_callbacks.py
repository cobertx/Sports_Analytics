"""Shared exchange navigation and polling; inactive pages never fetch quotes."""
from datetime import datetime, timezone
import hashlib
import json

from dash import Input, Output, State, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from .data import DataSourceError
from .kalshi import SPORTS
from . import kalshi_view as default_view
from .display_time import format_eastern


def register(app, service, *, view=None, provider='Kalshi'):
    view = view or default_view
    prefix = provider.lower()
    def callback(*args, **kwargs):
        def decorate(function):
            function.__name__ = function.__name__.replace('kalshi_', prefix+'_', 1)
            return app.callback(*args, **kwargs)(function)
        return decorate
    @callback(Output(f'{prefix}-page', 'style'), Output(f'{prefix}-page-link', 'style'),
                  Output(f'{prefix}-page-link', 'className'), Output(f'{prefix}-page-link', 'href'),
                  Output(f'{prefix}-list', 'style'), Output(f'{prefix}-detail', 'style'), Output(f'{prefix}-back', 'href'),
                  Output(f'{prefix}-list-timer', 'disabled'), Output(f'{prefix}-quote-timer', 'disabled'),
                  Output(f'{prefix}-quote-timer', 'interval'),
                  Input('workspace-page', 'value'), Input('sport', 'value'), Input('market-url', 'hash'),
                  Input(f'{prefix}-frequency', 'value'), Input(f'{prefix}-snapshot', 'value'), Input(f'{prefix}-context', 'data'))
    def kalshi_visibility(page, sport, location_hash, frequency, selected, game):
        active = page == prefix and sport in SPORTS
        route = view.route(location_hash)
        detail = active and route and route[0] == sport and bool(route[1])
        frequency = frequency if frequency in {0, 2, 5, 10, 30} else 5
        live = active and detail and game and game.get('sport') == sport and selected == 'live' and frequency > 0
        return ({'display': 'grid' if active else 'none'}, {} if sport in SPORTS else {'display': 'none'},
                'sport-page-link' + (' active' if active else ''), view.href(sport),
                {'display': 'none' if detail else 'block'}, {'display': 'block' if detail else 'none'}, view.href(sport),
                not active, not bool(live), 1000 * (frequency or 5))

    @callback(Output(f'{prefix}-games', 'data'), Output(f'{prefix}-list-status', 'children'),
                  Output(f'{prefix}-game-cards', 'children'),
                  Input('workspace-page', 'value'), Input('sport', 'value'), Input(f'{prefix}-days', 'value'),
                  Input(f'{prefix}-list-refresh', 'n_clicks'), Input(f'{prefix}-list-timer', 'n_intervals'))
    def kalshi_games(page, sport, days, _refresh, _tick):
        if page != prefix or sport not in SPORTS: raise PreventUpdate
        try:
            catalog = service.games(sport, days, refresh=ctx.triggered_id == f'{prefix}-list-refresh')
            message = f"{len(catalog['games'])} {provider} game events · Fetched {format_eastern(catalog['fetched_at'])}."
            if catalog.get('notices'): message += ' ' + ' '.join(catalog['notices'])
            return catalog, message, view.cards(catalog)
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return dict(sport=sport, games=[]), str(exc), []

    @callback(Output(f'{prefix}-context', 'data'), Output(f'{prefix}-game-header', 'children'),
                  Output(f'{prefix}-category', 'children'), Output(f'{prefix}-category', 'value'),
                  Input(f'{prefix}-games', 'data'), Input('market-url', 'hash'), Input('sport', 'value'),
                  State(f'{prefix}-context', 'data'), State(f'{prefix}-category', 'value'))
    def kalshi_game(catalog, location_hash, sport, previous, selected):
        route = view.route(location_hash)
        if not catalog or catalog.get('sport') != sport or not route or route[0] != sport or not route[1]:
            return None, '', [], 'Game Lines'
        game = next((g for g in catalog['games'] if g['id'] == route[1]), None)
        choices = view.categories(game)
        selection = selected if selected in choices and previous and previous.get('id') == route[1] else choices[0] if choices else 'Game Lines'
        return (game if game != previous else no_update, view.game_header(game),
                [dcc.Tab(label=c, value=c, className='market-category-tab', selected_className='market-category-tab selected') for c in choices], selection)

    def history_options(game, category):
        options = [dict(label='Live quotes', value='live')]
        if game:
            try:
                for filename in service.history(game, category):
                    stamp = datetime.strptime(filename.split('_')[0], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
                    options.append(dict(label='Saved ' + format_eastern(stamp), value=filename))
            except (OSError, ValueError): pass
        return options

    @callback(Output(f'{prefix}-quotes', 'data'), Output(f'{prefix}-quote-status', 'children'),
                  Output(f'{prefix}-snapshot', 'options'), Output(f'{prefix}-snapshot', 'value'),
                  Input('workspace-page', 'value'), Input('sport', 'value'), Input(f'{prefix}-context', 'data'),
                  Input(f'{prefix}-category', 'value'), Input(f'{prefix}-snapshot', 'value'),
                  Input(f'{prefix}-quote-timer', 'n_intervals'), Input(f'{prefix}-pull', 'n_clicks'),
                  State(f'{prefix}-frequency', 'value'),
                  running=[(Output(f'{prefix}-pull', 'disabled'), True, False)])
    def kalshi_quotes(page, sport, game, category, selected, _tick, pulls, frequency):
        if page != prefix or sport not in SPORTS: raise PreventUpdate
        if not game or game.get('sport') != sport:
            return None, f'Choose a {provider} game to load its contracts.', history_options(None, category), 'live'
        pull = ctx.triggered_id == f'{prefix}-pull' and bool(pulls)
        if ctx.triggered_id == f'{prefix}-quote-timer' and (selected != 'live' or not frequency): raise PreventUpdate
        try:
            reload_history = ctx.triggered_id in {f'{prefix}-context', f'{prefix}-category', f'{prefix}-pull'} or selected is None
            options = history_options(game, category) if reload_history else no_update
            if reload_history and selected not in {o['value'] for o in options}:
                selected = 'live'
            archived = selected not in {None, 'live'} and not pull
            snapshot = service.saved(game, category, selected) if archived else service.quotes(game, category, refresh=pull, save=pull)
            stamp = format_eastern(snapshot['fetched_at']) if snapshot.get('fetched_at') else 'No live quote timestamp'
            status = ('SAVED SNAPSHOT' if archived else 'PUBLIC REST QUOTES') + f" · {len(snapshot['rows'])} contracts · Fetched {stamp}."
            if snapshot.get('notices'): status += ' ' + ' '.join(snapshot['notices'])
            if snapshot.get('saved'): status += ' JSON/CSV snapshot saved: ' + snapshot['saved']
            if not snapshot['rows']: status += f' {provider} has no contracts for this linked category.'
            if pull: options = history_options(game, category)
            return snapshot, status, options, 'live' if pull else selected if reload_history else no_update
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return None, f'{provider} quotes unavailable: ' + str(exc), no_update, no_update

    @callback(Output(f'{prefix}-table', 'data'), Output(f'{prefix}-table', 'tooltip_data'), Output(f'{prefix}-rules', 'children'),
                  Output(f'{prefix}-market-sections', 'children'),
                  Output(f'{prefix}-rules-signature', 'data'),
                  Input(f'{prefix}-quotes', 'data'), Input(f'{prefix}-context', 'data'), Input(f'{prefix}-category', 'value'),
                  State(f'{prefix}-rules-signature', 'data'))
    def kalshi_table(snapshot, game, category, previous_signature=None):
        if not snapshot or not game or (snapshot.get('sport'), snapshot.get('game_id'), snapshot.get('category')) != (game['sport'], game['id'], category):
            return [], [], [], [], None
        signature = hashlib.sha256(json.dumps([[r['ticker'], r['rules_primary'], r['rules_secondary'], r['status'], r['notional']]
            for r in snapshot['rows']], sort_keys=True).encode()).hexdigest()
        changed = signature != previous_signature
        rows, tips, rules = view.table(snapshot, include_rules=changed)
        return (rows, tips if changed else no_update, rules if changed else no_update,
                view.market_sections(snapshot), signature if changed else no_update)
