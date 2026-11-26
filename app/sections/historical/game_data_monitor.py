"""Sidebar summary of the latest saved historical game for each sport."""

from time import monotonic

from dash import Input, Output, ctx, dcc, html, no_update

from sports_analytics.data import SOURCES
from sports_analytics.display_time import eastern_datetime, format_eastern
from sports_analytics.page_cache import PageTasks


def _rows(asset_url, games=None, *, unavailable=False):
    rows = []
    for sport in SOURCES:
        game = games.get(sport) if games is not None else None
        if game:
            # Some historical feeds provide a date without a tipoff time.
            date = game.event_date
            if sport == 'NCAAF' or game.game_id.startswith('espn:') or date != date.normalize():
                date = eastern_datetime(date)
            saved = eastern_datetime(game.saved_at)
            saved_time = saved.strftime('%I:%M %p').lstrip('0')
            details = [
                html.Strong(game.matchup or 'Completed game'),
                html.Time(f'{date:%b} {date.day}, {date.year}', dateTime=date.date().isoformat()),
                html.Time(f'Saved {saved:%b} {saved.day}, {saved_time}',
                          dateTime=game.saved_at.isoformat(), title=format_eastern(game.saved_at)),
            ]
        else:
            message = 'Saved game status unavailable' if unavailable else (
                'Checking saved games…' if games is None else 'No saved games')
            details = [html.Span(message)]
        rows.append(html.Li([
            html.Img(src=asset_url(f'leagues/{sport.lower()}.png'), alt=sport, className='game-data-logo'),
            html.Div(details, className='game-data-details'),
        ]))
    return rows


def layout(asset_url):
    return html.Aside(html.Details([
        html.Summary([
            html.Span('Latest game data', className='backtest-monitor-heading'),
            html.Span('⌄', className='backtest-monitor-chevron', **{'aria-hidden': 'true'}),
        ], className='backtest-monitor-toggle'),
        html.Div(html.Ul(_rows(asset_url), id='game-data-rows', className='game-data-rows'),
                 className='game-data-body'),
        dcc.Interval(id='game-data-poll', interval=500, disabled=True),
    ], open=True), id='game-data-monitor', className='game-data-monitor',
       **{'aria-label': 'Latest saved game data'})


def register(app, history):
    tasks = PageTasks(capacity=1, workers=1)
    key = 'latest-saved-games'

    def read_games():
        return monotonic(), history.latest()

    @app.callback(Output('game-data-rows', 'children'), Output('game-data-poll', 'disabled'),
                  Input('history-update-poll', 'n_intervals'), Input('game-data-poll', 'n_intervals'))
    def update_game_data(_tick, _poll):
        previous = tasks.peek(key)
        # Share recent results across tabs while keeping the minute refresh.
        refresh = (ctx.triggered_id != 'game-data-poll' and previous is not None
                   and monotonic() - previous[0] >= 55)
        saved, pending, error = tasks.read(key, read_games, refresh=refresh)
        if saved is None:
            return (_rows(app.get_asset_url, unavailable=True) if error else no_update), not pending
        if pending and ctx.triggered_id == 'game-data-poll':
            return no_update, no_update
        return _rows(app.get_asset_url, saved[1]), not pending
