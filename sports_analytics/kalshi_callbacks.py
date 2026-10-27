"""Shared exchange navigation and polling; inactive pages never fetch quotes."""
from datetime import datetime, timezone
from threading import Lock
from time import time
import json

from dash import Input, Output, State, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from .data import DataSourceError
from .kalshi import SPORTS
from . import kalshi_view as default_view
from .display_time import format_eastern
from .exchange_render import render
from .kalshi_analysis import FILTER, decorate
from .page_cache import QuotePages


def register(app, service, *, view=None, provider='Kalshi',pages=None):
    view = view or default_view
    prefix = provider.lower()
    pages=pages or (QuotePages(service) if getattr(service,'background',False) is True else None)
    polling = {}
    polling_lock = Lock()
    app.clientside_callback(f'''function(page,selected,frequency,category,games,quotes){{
        const state=quotes?{{game:quotes.game_id,category:quotes.category,updating:!!quotes.updating,page_pending:!!quotes.page_pending}}:null;
        const waiting=(games&&games.updating)||(state&&state.updating&&selected==='live'&&(frequency>0||state.page_pending)&&category!=='Mispricing Filter');
        return [page!=={json.dumps(prefix)}||!waiting,state];
    }}''',Output(f'{prefix}-cache-poll','disabled'),Output(f'{prefix}-cache-state','data'),
        Input('workspace-page','value'),Input(f'{prefix}-snapshot','value'),Input(f'{prefix}-frequency','value'),
        Input(f'{prefix}-category','value'),Input(f'{prefix}-games','data'),Input(f'{prefix}-quotes','data'))
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
                  Input(f'{prefix}-frequency', 'value'), Input(f'{prefix}-snapshot', 'value'), Input(f'{prefix}-context', 'data'),
                  *([Input('kalshi-category','value')] if provider=='Kalshi' else []))
    def kalshi_visibility(page, sport, location_hash, frequency, selected, game, category=None):
        active = page == prefix and sport in SPORTS
        route = view.route(location_hash)
        detail = active and route and route[0] == sport and bool(route[1])
        frequency = frequency if frequency in {0, 2, 5, 10, 30} else 5
        live = active and detail and game and game.get('sport') == sport and selected == 'live' and frequency > 0 and category!=FILTER
        return ({'display': 'grid' if active else 'none'}, {} if sport in SPORTS else {'display': 'none'},
                'sport-page-link' + (' active' if active else ''), view.href(sport),
                {'display': 'none' if detail else 'block'}, {'display': 'block' if detail else 'none'}, view.href(sport),
                not active, not bool(live), 1000 * (frequency or 5))

    @callback(Output(f'{prefix}-games', 'data'), Output(f'{prefix}-list-status', 'children'),
                  Output(f'{prefix}-game-cards', 'children'),
                  Input('workspace-page', 'value'), Input('sport', 'value'), Input(f'{prefix}-days', 'value'),
                  Input(f'{prefix}-list-refresh', 'n_clicks'), Input(f'{prefix}-list-timer', 'n_intervals'),
                  State(f'{prefix}-games', 'data'),Input(f'{prefix}-cache-poll','n_intervals'))
    def kalshi_games(page, sport, days, _refresh, _tick, previous=None,_cache_tick=None):
        if page != prefix or sport not in SPORTS: raise PreventUpdate
        if ctx.triggered_id==f'{prefix}-cache-poll' and not (previous or {}).get('updating'):raise PreventUpdate
        if (ctx.triggered_id=='workspace-page' and previous and [previous.get('sport'),previous.get('window_days')]==[sport,days]
                and 0<=time()-previous.get('checked_at',0)<60):raise PreventUpdate
        try:
            catalog = service.games(sport, days, refresh=ctx.triggered_id == f'{prefix}-list-refresh')
            catalog={**catalog,'window_days':days,'checked_at':time()}
            message = f"{len(catalog['games'])} {provider} game events · Fetched {format_eastern(catalog['fetched_at'])}."
            if catalog.get('notices'): message += ' ' + ' '.join(catalog['notices'])
            same_games = previous and previous.get('sport')==sport and previous.get('games')==catalog['games']
            same_catalog = (same_games and previous.get('notices')==catalog.get('notices')
                            and previous.get('fetched_at')==catalog.get('fetched_at') and previous.get('window_days')==days
                            and bool(previous.get('updating'))==bool(catalog.get('updating')))
            return (no_update if same_catalog else catalog, message, no_update if same_games else view.cards(catalog))
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return dict(sport=sport, games=[]), str(exc), []

    @callback(Output(f'{prefix}-context', 'data'), Output(f'{prefix}-game-header', 'children'),
                  Output(f'{prefix}-category', 'children'), Output(f'{prefix}-category', 'value'),
                  Input(f'{prefix}-games', 'data'), Input('market-url', 'hash'), Input('sport', 'value'),
                  State(f'{prefix}-context', 'data'), State(f'{prefix}-category', 'value'))
    def kalshi_game(catalog, location_hash, sport, previous, selected):
        route = view.route(location_hash)
        if not route or route[0]!=sport or not route[1]:raise PreventUpdate
        if not catalog or catalog.get('sport') != sport:
            return None, '', [], 'Game Lines'
        game = next((g for g in catalog['games'] if g['id'] == route[1]), None)
        if game and game == previous:
            return no_update, no_update, no_update, no_update
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
                  State(f'{prefix}-frequency', 'value'),State('market-url','hash'),
                  Input(f'{prefix}-cache-poll','n_intervals'),State(f'{prefix}-cache-state','data'))
    def kalshi_quotes(page, sport, game, category, selected, _tick, pulls, frequency,location_hash=None,_cache_tick=None,cache_state=None):
        if page != prefix or sport not in SPORTS: raise PreventUpdate
        if location_hash is not None:
            route=view.route(location_hash)
            if not route or not game or (route[0],route[1])!=(sport,game.get('id')):raise PreventUpdate
        pull = bool(pulls) and ctx.triggered_id == f'{prefix}-pull'
        if provider=='Kalshi' and category==FILTER:
            # The deliberate all-category scan owns this fetch/save. Keep one
            # owner of the View dropdown while allowing Pull to leave an archive.
            if pull and selected!='live':return no_update,no_update,no_update,'live'
            raise PreventUpdate
        if not game or game.get('sport') != sport:
            return (None, f'Choose a {provider} game to load its contracts.', history_options(None, category),
                    'live' if selected!='live' else no_update)
        if ctx.triggered_id in {f'{prefix}-quote-timer',f'{prefix}-cache-poll'} and (selected != 'live' or
                (not frequency and not (ctx.triggered_id==f'{prefix}-cache-poll' and (cache_state or {}).get('page_pending')))):raise PreventUpdate
        if ctx.triggered_id==f'{prefix}-cache-poll' and (not cache_state or not cache_state.get('updating') or
                (cache_state.get('game'),cache_state.get('category'))!=(game['id'],category)):raise PreventUpdate
        key = (sport,game['id'],category,selected)
        automatic = ctx.triggered_id in {f'{prefix}-quote-timer',f'{prefix}-cache-poll'}
        with polling_lock:
            if automatic and key in polling: raise PreventUpdate
            # Every request is counted so a slow manual pull also suppresses polls.
            polling[key] = polling.get(key,0)+1
        selection_before = selected
        try:
            reload_history = ctx.triggered_id in {f'{prefix}-context', f'{prefix}-category', f'{prefix}-pull'} or selected is None
            options = history_options(game, category) if reload_history else no_update
            if reload_history and selected not in {o['value'] for o in options}:
                selected = 'live'
            archived = selected not in {None, 'live'} and not pull
            if archived:snapshot=service.saved(game,category,selected)
            elif pages is not None and not pull:snapshot=pages.read(game,category)
            else:
                snapshot=service.quotes(game,category,refresh=pull,save=pull)
                if pages is not None:pages.remember(game,category,snapshot)
            stamp = format_eastern(snapshot['fetched_at']) if snapshot.get('fetched_at') else 'No live quote timestamp'
            status = ('SAVED SNAPSHOT' if archived else 'PUBLIC REST QUOTES') + f" · {len(snapshot['rows'])} contracts · Fetched {stamp}."
            if snapshot.get('notices'): status += ' ' + ' '.join(snapshot['notices'])
            if snapshot.get('saved'): status += ' JSON/CSV snapshot saved: ' + snapshot['saved']
            if not snapshot['rows']:
                status += (' Loading this category…' if snapshot.get('updating') else f' {provider} has no contracts for this linked category.')
            if pull: options = history_options(game, category)
            chosen = 'live' if pull else selected
            # Re-emitting the dropdown's own input needlessly retriggers refreshes.
            return snapshot, status, options, chosen if chosen != selection_before else no_update
        except (DataSourceError, OSError, ValueError, TypeError) as exc:
            return None, f'{provider} quotes unavailable: ' + str(exc), no_update, no_update
        finally:
            with polling_lock:
                polling[key] -= 1
                if not polling[key]: del polling[key]

    @callback(Output(f'{prefix}-table', 'data'), Output(f'{prefix}-table', 'tooltip_data'), Output(f'{prefix}-rules', 'children'),
                  Output(f'{prefix}-market-sections', 'children'),
                  Output(f'{prefix}-rules-signature', 'data'),
                  Input(f'{prefix}-quotes', 'data'), Input(f'{prefix}-context', 'data'), Input(f'{prefix}-category', 'value'),
                  Input(f'{prefix}-detail-table','open'), Input(f'{prefix}-rules-panel','open'),
                  State(f'{prefix}-rules-signature', 'data'),State('workspace-page','value'),State('market-url','hash'),
                  # Quote rendering must not wait for the forecast callback.
                  *([State('kalshi-models','data'),State('kalshi-applied-criteria','data'),
                     State('kalshi-game-method','value'),State('kalshi-player-method','value'),State('kalshi-snapshot','value')]
                    if provider=='Kalshi' else []))
    def kalshi_table(snapshot, game, category, table_open=False, rules_open=False, previous_signature=None,
                     page=None,location_hash=None,models=None,criteria=None,method='PPP',player_method='WHP',selected='live'):
        if page is not None and page!=prefix:raise PreventUpdate
        if location_hash is not None:
            route=view.route(location_hash)
            if not route or not game or (route[0],route[1])!=(game['sport'],game['id']):raise PreventUpdate
        if provider=='Kalshi' and category==FILTER:return (no_update,)*5
        if not snapshot or not game or (snapshot.get('sport'), snapshot.get('game_id'), snapshot.get('category')) != (game['sport'], game['id'], category):
            return [], [], [], [], None
        if provider=='Kalshi':snapshot=decorate(snapshot,game,selected,method,player_method,models,criteria)
        snapshot={**snapshot,'game':game}
        return render(snapshot,previous_signature,table_open=table_open,rules_open=rules_open,provider=provider)
