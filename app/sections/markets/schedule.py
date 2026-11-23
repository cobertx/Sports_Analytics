"""Sportsbook schedule and card callbacks."""

from datetime import datetime
from uuid import uuid4
from time import time
from dash import Input, MATCH, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from sports_analytics.data import DataSourceError, SOURCES
from app.shell.loading import loading as action_loading
from sports_analytics.schedule import SCHEDULE_LINKS
from sports_analytics.nba_markets import NBA_MARKETS
from app.sections.markets.market_view import latest_pull_status, logo_badge
from app.shell.navigation import scoped_callback
from sports_analytics.display_time import eastern_datetime, format_eastern


def game_card(game, sport, snapshot=None):
    live = sport in {"NFL", "NBA", "NCAAF"}
    prefix = sport.lower()
    credits = len(NBA_MARKETS) if sport == "NBA" else 17
    def team_row(side, label):
        team = game[side]
        badge = logo_badge(team["name"], team["abbreviation"], sport)
        return html.Div([badge,
                         html.Div([html.Strong(team["name"]), html.Small(label)])], className="game-team")
    return html.Article([
        html.A(href=f"#{prefix}-game-{game['id']}", className="game-card-hit",
               **{"aria-label": f"View saved markets for {game['away']['name']} at {game['home']['name']}"}) if live else None,
        html.Div([html.Span(sport, className="game-sport"),
                  html.Span("Time TBD" if game["time_tbd"] else format_eastern(game["start"]), className="game-time")],
                 className="game-card-heading"),
        team_row("away", "Designated away" if game["neutral"] else "Away"),
        html.Div("@", className="game-at", **{"aria-hidden": "true"}),
        team_row("home", "Designated home" if game["neutral"] else "Home"),
        html.Div([html.Strong(game["venue"]), html.Span(game["location"]),
                  html.Small("Neutral site") if game["neutral"] else None], className="game-venue"),
        html.Div([html.Div([
                  html.A("Game details →", href=f"#{prefix}-game-{game['id']}") if live else
                  html.A("Game details ↗", href=f"https://www.espn.com/{'nba' if sport == 'NBA' else 'college-football'}/game/_/gameId/{game['id']}",
                         target="_blank", rel="noopener noreferrer"),
                  action_loading(html.Div(latest_pull_status(snapshot), id={"type": f"{prefix}-live-markets", "game": game["id"]},
                             className="latest-pull-time", role="status", **{"aria-live": "polite"}),'Pulling live markets…') if live else None,
                  ], className="game-footer-left"),
                  html.Button("Pull Live Markets", id={"type": f"{prefix}-pull", "game": game["id"]}, n_clicks=0,
                              className="pull-markets-button", title=f"FanDuel only · up to {credits} credits per snapshot") if live else
                  html.Span("Scheduled", className="scheduled-label")], className="game-card-footer"),
        html.Div([
            html.Small(f"FanDuel only · Up to {credits} credits per pull", className="pull-credit-note"),
            dcc.Store(id={"type": f"{prefix}-game-context", "game": game["id"]}, data={**game, "sport": sport, "token": uuid4().hex}),
        ]) if live else None,
    ], className="panel game-card")

def schedule_blocks(games, sport, snapshots=None):
    dates = {}
    for game in games:
        dates.setdefault(eastern_datetime(game["start"]).date().isoformat(), []).append(game)
    return [html.Section([
        html.H3(datetime.fromisoformat(day).strftime("%m/%d/%Y") + " · EDT", className="game-date-heading"),
        html.Div([game_card(game, sport, (snapshots or {}).get(game["id"])) for game in daily], className="game-grid"),
    ], className="game-day") for day, daily in sorted(dates.items())]


def register(app, schedule_service, market_services):
    def register_card_pull(league):
        prefix = league.lower()

        @app.callback(Output({"type": f"{prefix}-live-markets", "game": MATCH}, "children"),
                      Input({"type": f"{prefix}-pull", "game": MATCH}, "n_clicks"),
                      State({"type": f"{prefix}-game-context", "game": MATCH}, "data"),
                      State("sport", "value"), State("workspace-page", "value"), prevent_initial_call=True,
                      running=[(Output({"type": f"{prefix}-pull", "game": MATCH}, "disabled"), True, False)])
        def pull_markets(clicks, game, sport, page):
            if not clicks or sport != league or page != "market" or not game or game.get("sport", league) != league:
                raise PreventUpdate
            if not isinstance(ctx.triggered_id, dict) or str(ctx.triggered_id.get("game")) != str(game.get("id")):
                raise PreventUpdate
            markets = market_services[league]
            try:
                snapshot = markets.pull(game, f"{game['id']}:{game['token']}:{clicks}")
            except DataSourceError as exc:
                saved = markets.latest([game["id"]]).get(str(game["id"]))
                return [latest_pull_status(saved), html.Span(str(exc), className="market-pull-error")]
            return latest_pull_status(snapshot)

    for league in market_services:
        register_card_pull(league)

    app.clientside_callback('function(scope){return scope&&scope.updating?1500:300000;}',
        Output('market-timer','interval'),Input('market-schedule-scope','data'))

    @scoped_callback(app,'market',Output("market-games", "children"), Output("market-status", "children"), Output("market-schedule", "data"),
                  Output('market-schedule-scope','data'),
                  Input("workspace-page", "value"), Input("sport", "value"), Input("market-days", "value"),
                  Input("market-refresh", "n_clicks"), Input("market-timer", "n_intervals"),
                  Input("market-refresh-signal", "data"), State("market-schedule", "data"),State('market-schedule-scope','data'),
                  running=[(Output("market-refresh", "disabled"), True, False)])
    def upcoming_games(page, sport, days, _refresh, _timer, _refreshed, previous_games,scope=None):
        if page != "market" or sport not in SOURCES:
            raise PreventUpdate
        if (ctx.triggered_id in {'workspace-page','nav-market-active'} and scope and [scope.get('sport'),scope.get('days')]==[sport,days]
                and 0<=time()-scope.get('checked_at',0)<300):raise PreventUpdate
        if ctx.triggered_id == "market-refresh-signal" and sport in market_services and previous_games:
            saved = market_services[sport].latest([game["id"] for game in previous_games])
            return schedule_blocks(previous_games, sport, saved), no_update, no_update,no_update
        try:
            schedule = schedule_service.load(sport, days, refresh=ctx.triggered_id == "market-refresh", local_first=True)
        except (DataSourceError, ValueError) as exc:
            return html.Div([html.H3("Schedule unavailable"), html.P(str(exc)),
                             html.A("Check ESPN schedule ↗", href=SCHEDULE_LINKS[sport], target="_blank", rel="noopener noreferrer")],
                            className="panel market-empty"), "Upcoming games could not be loaded.", [],dict(sport=sport,days=days,checked_at=0)
        updating=getattr(schedule,'updating',False)
        status = f"{sport} · {len(schedule.games)} upcoming games · ESPN"
        if schedule.fetched_at:
            status += f" · Oldest schedule update: {format_eastern(schedule.fetched_at)} · Refreshes every 5 minutes."
        if updating:
            status += " · Checking for schedule updates."
        if schedule.stale_days:
            status += " Saved schedules shown after a source error for: " + ", ".join(schedule.stale_days) + "."
        if schedule.failed_days:
            status += " Incomplete coverage; unavailable dates: " + ", ".join(schedule.failed_days) + "."
        new_scope = dict(sport=sport, days=days, checked_at=time(), updating=bool(updating),
                         pending=bool(updating and not schedule.fetched_at))
        if (ctx.triggered_id == 'market-timer' and scope and scope.get('updating')
                and updating and scope.get('sport') == sport and scope.get('days') == days
                and schedule.games == previous_games and new_scope['pending'] == scope.get('pending')):
            return no_update, status, no_update, new_scope
        saved = market_services[sport].latest([game["id"] for game in schedule.games]) if schedule.games and sport in market_services else {}
        blocks = schedule_blocks(schedule.games, sport, saved)
        if not blocks and not updating:
            blocks = html.Div([html.H3("No upcoming games found"),
                               html.P("No scheduled games were returned for the loaded dates. Try the 14-day window or refresh the schedule.")],
                              className="panel market-empty")
        return blocks, status, schedule.games, new_scope
