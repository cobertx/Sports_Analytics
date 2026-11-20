"""Sidebar summary of the latest saved historical game for each sport."""

from dash import Input, Output, html

from sports_analytics.data import SOURCES
from sports_analytics.display_time import eastern_datetime, format_eastern


def _rows(asset_url, games=None):
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
            details = [html.Span('Checking saved games…' if games is None else 'No saved games')]
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
    ], open=True), id='game-data-monitor', className='game-data-monitor',
       **{'aria-label': 'Latest saved game data'})


def register(app, history):
    @app.callback(Output('game-data-rows', 'children'), Input('history-update-poll', 'n_intervals'))
    def update_game_data(_tick):
        return _rows(app.get_asset_url, history.latest())
