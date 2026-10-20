"""Dash presentation layer. Load data when the user opens or changes a scope."""

from datetime import date, datetime
import csv
import math
import re
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from dash import Dash, Input, MATCH, Output, State, ctx, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import abort, send_from_directory
import pandas as pd
import plotly.graph_objects as go

from .data import DataSourceError, PLAYER_SPORTS, Query, SOURCES
from .career import CareerService
from .forecast import rolling_baseline
from .metrics import metric_label, preferred_metric, relevant_metrics, statistic_categories, statistic_category
from .service import DataService
from .schedule import FANDUEL_LINKS, SCHEDULE_LINKS, ScheduleService
from .odds_api import DOCUMENTATION as ODDS_DOCUMENTATION, OddsApiService
from .nfl_markets import NflMarketService
from .market_view import CATEGORIES, NFL_LOGO_DIRECTORY, NFL_LOGO_FILES, game_header, latest_pull_status, latest_pull_text, market_sections
from .display_time import eastern_datetime, format_eastern
from .usage_log import ApiUsageLog, SPORTS as USAGE_SPORTS, usage_summary
from .ppp_service import PPPMarketService
from .epa_service import EPAMarketService

ALL_TEAMS = "__all__"


def market_game_id(location_hash):
    match = re.fullmatch(r"#nfl-game-(\d+)", location_hash or "")
    return match[1] if match else None


def supported_dataset(dataset):
    return bool(dataset) and (dataset["kind"] == "team" or dataset["sport"] in PLAYER_SPORTS)


def filter_team(frame, kind, team):
    if kind == "player" and team and team != ALL_TEAMS:
        return frame[frame["team"] == team]
    return frame


def empty_figure(message="Load historical data to explore a team or player"):
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                          font={"color": "#9caec5", "size": 14})
    return style_figure(figure)


def style_figure(figure):
    figure.update_layout(template="plotly_dark", paper_bgcolor="#111d2e", plot_bgcolor="#111d2e",
                         font={"family": "Segoe UI, sans-serif", "color": "#b8c8dc"},
                         margin={"l": 45, "r": 25, "t": 20, "b": 45}, height=350,
                         legend={"orientation": "h", "y": 1.12}, hovermode="x unified")
    figure.update_xaxes(gridcolor="#223047", zeroline=False)
    figure.update_yaxes(gridcolor="#223047", zeroline=False)
    return figure


def field(label, component, hint=None):
    return html.Div([html.Label(label, htmlFor=component.id), component,
                     html.Small(hint) if hint else None], className="field")


def kpi(label, value, detail):
    return html.Div([html.Span(label, className="eyebrow"), html.Strong(value),
                     html.Small(detail)], className="kpi")


def game_labels(history):
    labels = []
    for row in history.itertuples():
        if pd.notna(row.event_date):
            played = row.event_date.strftime("%Y-%m-%d")
        elif pd.notna(row.week):
            played = f"{row.season} · Week {int(row.week)}"
        else:
            played = f"{row.season} · Date unavailable"
        matchup = getattr(row, "matchup", None)
        if pd.isna(matchup) or not matchup:
            matchup = f"{row.team} vs {row.opponent} (venue unavailable)"
        labels.append((played, matchup))
    return labels


def performance_figure(history, metric, window, prediction):
    label = metric_label(metric, history.iloc[0]["sport"])
    x = list(range(1, len(history) + 1))
    details = game_labels(history)
    figure = go.Figure()
    hover = "%{customdata[0]}<br>%{customdata[1]}<br>%{y:.2f}<extra>%{fullData.name}</extra>"
    figure.add_trace(go.Scatter(x=x, y=history[metric], mode="lines+markers", name=label,
                               line={"color": "#57dbc4", "width": 2}, marker={"size": 5},
                               customdata=details, hovertemplate=hover))
    figure.add_trace(go.Scatter(x=x, y=history["baseline"], mode="lines", name=f"Prior {window}-game mean",
                               line={"color": "#8197ff", "dash": "dot"}, customdata=details, hovertemplate=hover))
    step = max(1, math.ceil(len(history) / 10))
    indices = sorted(set(range(0, len(history), step)) | {len(history) - 1})
    ticks = [x[i] for i in indices]
    labels = [f"{details[i][0]}<br>{details[i][1]}" for i in indices]
    if prediction is not None:
        figure.add_trace(go.Scatter(x=[len(history) + 1], y=[prediction], name="Next appearance baseline", mode="markers",
                                   marker={"color": "#f6c667", "size": 12, "symbol": "diamond"},
                                   hovertemplate="Next appearance<br>Date and matchup not yet known<br>%{y:.2f}<extra>Baseline</extra>"))
        ticks.append(len(history) + 1)
        labels.append("Next appearance<br>Matchup TBD")
    figure = style_figure(figure)
    figure.update_layout(height=440, margin={"l": 55, "r": 25, "t": 35, "b": 115})
    figure.update_xaxes(title="Game date · Matchup (away @ home)", tickmode="array", tickvals=ticks,
                        ticktext=labels, tickangle=-35, automargin=True)
    figure.update_yaxes(title=label)
    return figure


def career_figure(career, sport, metric, name):
    if career.frame.empty:
        return empty_figure("No career season summaries available for this player and statistic")
    history = career.frame.set_index("season")
    years = list(range(int(history.index.min()), int(history.index.max()) + 1))
    history = history.reindex(years)
    # Missing seasons remain gaps rather than being plotted as zero or joined across.
    values = history["value"].astype(object).where(history["value"].notna(), None).tolist()
    teams = history["team"].fillna("No recorded data").tolist()
    seasons = [f"{year}–{str(year + 1)[-2:]}" if sport == "NBA" else str(year) for year in years]
    figure = go.Figure(go.Scatter(x=years, y=values, mode="lines+markers", connectgaps=False,
                                 line={"color": "#8197ff", "width": 2}, marker={"size": 7},
                                 name=name, customdata=list(zip(seasons, teams)),
                                 hovertemplate="%{customdata[0]} · %{customdata[1]}<br>%{y:.2f}<extra>%{fullData.name}</extra>"))
    figure = style_figure(figure)
    figure.update_xaxes(title="Career season", tickmode="array", tickvals=years, ticktext=seasons,
                        tickangle=-35, automargin=True)
    figure.update_yaxes(title=metric_label(metric, sport) + " · Season statistic")
    return figure


def game_card(game, sport, snapshot=None):
    def team_row(side, label):
        team = game[side]
        logo = NFL_LOGO_FILES.get(team["name"].casefold()) if sport == "NFL" else None
        badge = html.Span(
            html.Img(src="/images/nfl/team-logos/" + quote(logo), alt=team["name"] + " logo",
                     className="team-logo") if logo else team["abbreviation"],
            className="team-badge team-badge-logo" if logo else "team-badge")
        return html.Div([badge,
                         html.Div([html.Strong(team["name"]), html.Small(label)])], className="game-team")
    return html.Article([
        html.A(href=f"#nfl-game-{game['id']}", className="game-card-hit",
               **{"aria-label": f"View saved markets for {game['away']['name']} at {game['home']['name']}"}) if sport == "NFL" else None,
        html.Div([html.Span(sport, className="game-sport"),
                  html.Span("Time TBD" if game["time_tbd"] else format_eastern(game["start"]), className="game-time")],
                 className="game-card-heading"),
        team_row("away", "Designated away" if game["neutral"] else "Away"),
        html.Div("@", className="game-at", **{"aria-hidden": "true"}),
        team_row("home", "Designated home" if game["neutral"] else "Home"),
        html.Div([html.Strong(game["venue"]), html.Span(game["location"]),
                  html.Small("Neutral site") if game["neutral"] else None], className="game-venue"),
        html.Div([html.Div([
                  html.A("Game details →", href=f"#nfl-game-{game['id']}") if sport == "NFL" else
                  html.A("Game details ↗", href=f"https://www.espn.com/{'nba' if sport == 'NBA' else 'college-football'}/game/_/gameId/{game['id']}",
                         target="_blank", rel="noopener noreferrer"),
                  html.Div(latest_pull_status(snapshot), id={"type": "nfl-live-markets", "game": game["id"]},
                             className="latest-pull-time", role="status", **{"aria-live": "polite"}) if sport == "NFL" else None,
                  ], className="game-footer-left"),
                  html.Button("Pull Live Markets", id={"type": "nfl-pull", "game": game["id"]}, n_clicks=0,
                              className="pull-markets-button", title="FanDuel only · up to 17 credits per snapshot") if sport == "NFL" else
                  html.Span("Scheduled", className="scheduled-label")], className="game-card-footer"),
        html.Div([
            html.Small("FanDuel only · Up to 17 credits per pull", className="pull-credit-note"),
            dcc.Store(id={"type": "nfl-game-context", "game": game["id"]}, data={**game, "token": uuid4().hex}),
        ]) if sport == "NFL" else None,
    ], className="panel game-card")


def schedule_blocks(games, sport, snapshots=None):
    dates = {}
    for game in games:
        dates.setdefault(eastern_datetime(game["start"]).date().isoformat(), []).append(game)
    return [html.Section([
        html.H3(datetime.fromisoformat(day).strftime("%m/%d/%Y") + " · EDT", className="game-date-heading"),
        html.Div([game_card(game, sport, (snapshots or {}).get(game["id"])) for game in daily], className="game-grid"),
    ], className="game-day") for day, daily in sorted(dates.items())]


def credit_figure(usage):
    if not usage.total:
        return empty_figure("Connected · The API reports a zero credit allocation")
    figure = go.Figure(go.Pie(labels=["Used", "Unused"], values=[usage.used, usage.remaining],
                             hole=0.73, sort=False, direction="clockwise", textinfo="none",
                             marker={"colors": ["#f6c667", "#57dbc4"], "line": {"color": "#111d2e", "width": 3}},
                             hovertemplate="%{label}: %{value:,.0f} credits (%{percent})<extra></extra>"))
    figure = style_figure(figure)
    figure.update_layout(height=370, margin={"l": 24, "r": 24, "t": 24, "b": 45}, hovermode="closest",
                         legend={"orientation": "h", "x": 0.5, "xanchor": "center", "y": -0.05},
                         annotations=[{"text": f"<b>{100 * usage.used / usage.total:.1f}%</b><br>used",
                                       "x": 0.5, "y": 0.5, "showarrow": False, "font": {"size": 23, "color": "#e7edf6"}}])
    return figure


def usage_figures(summary):
    colors = ["#57dbc4", "#8197ff", "#f6c667"]
    values = [summary["monthly"][sport]["credits"] for sport in USAGE_SPORTS]
    composition = empty_figure("No reported credit usage logged this month")
    if sum(values):
        composition = style_figure(go.Figure(go.Pie(labels=list(USAGE_SPORTS), values=values,
                                                   sort=False, textinfo="percent+label", marker={"colors": colors},
                                                   hovertemplate="%{label}: %{value:,.0f} credits (%{percent})<extra></extra>")))
        composition.update_layout(hovermode="closest", height=370, margin={"l": 24, "r": 24, "t": 24, "b": 45},
                                  legend={"orientation": "h", "x": .5, "xanchor": "center", "y": -.05})
    daily = empty_figure("Pull live markets to start your daily usage history")
    if summary["daily"]:
        daily = go.Figure()
        dates = [row["date"] for row in summary["daily"]]
        totals = [sum(row[sport] for sport in USAGE_SPORTS) for row in summary["daily"]]
        daily.add_trace(go.Scatter(x=dates, y=totals, name="Total", mode="lines+markers",
                                   line={"color": "#e7edf6", "width": 3},
                                   hovertemplate="%{x|%m/%d/%Y}<br>%{y:,.0f} credits<extra>Total</extra>"))
        for sport, color in zip(USAGE_SPORTS, colors):
            daily.add_trace(go.Scatter(x=dates, y=[row[sport] for row in summary["daily"]],
                                       name=sport, mode="lines+markers", visible="legendonly", line={"color": color, "width": 2},
                                       hovertemplate="%{x|%m/%d/%Y}<br>%{y:,.0f} credits<extra>%{fullData.name}</extra>"))
        daily = style_figure(daily)
        daily.update_xaxes(title="Date (EDT)", type="date", tickformat="%m/%d/%Y")
        daily.update_yaxes(title="Credits used", rangemode="tozero", dtick=1 if max(totals) < 5 else None)
    return composition, daily


def create_app(service=None, career_service=None, schedule_service=None, odds_service=None, nfl_market_service=None,
               api_usage_log=None, ppp_service=None, epa_service=None):
    service = service or DataService()
    career_service = career_service or CareerService(service.path.with_name("career.sqlite3"))
    schedule_service = schedule_service or ScheduleService(service.path.with_name("schedules.sqlite3"))
    odds_service = odds_service or OddsApiService()
    ppp_service = ppp_service or PPPMarketService()
    epa_service = epa_service or EPAMarketService()
    data_directory = Path(__file__).resolve().parent.parent / "data"
    nfl_market_service = nfl_market_service or NflMarketService(odds_service, service.path.with_name("nfl_markets.sqlite3"),
                                                              data_directory / "market_data", usage_log=api_usage_log)
    market_log = getattr(nfl_market_service, "usage_log", None)
    api_usage_log = api_usage_log or (market_log if isinstance(market_log, ApiUsageLog) else
                                     ApiUsageLog(data_directory / "API Log" / "api_credit_usage.csv"))
    app = Dash(__name__, assets_folder=str(Path(__file__).parent / "assets"),
               title="Sports Analytics", update_title="Loading…")

    @app.server.get("/images/nfl/team-logos/<filename>")
    def nfl_team_logo(filename):
        if filename not in NFL_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NFL_LOGO_DIRECTORY, filename, mimetype="image/png")

    app.layout = html.Div([
        dcc.Store(id="dataset"), dcc.Download(id="download"),
        dcc.Location(id="market-url", refresh=False), dcc.Store(id="market-schedule", data=[]),
        dcc.Store(id="market-detail-context"), dcc.Store(id="market-refresh-signal"),
        dcc.Store(id="market-applied-criteria"),
        dcc.Interval(id="market-timer", interval=5 * 60 * 1000, disabled=True),
        dcc.Interval(id="odds-timer", interval=5 * 60 * 1000, disabled=True),
        html.Header([
            html.Div("RX", className="brand-mark", role="img", **{"aria-label": "RX"}),
        ]),
        html.Main([
            html.Div([html.Div([html.Div("THE HISTORICAL LAB", id="page-eyebrow", className="eyebrow accent"),
                               html.H1("From past performance to the next game.", id="page-title"),
                               html.P("Explore team statistics across three leagues and player statistics for NBA and NFL.", id="page-description")])], className="intro"),
            dcc.Tabs(id="sport", value="NBA", children=[
                dcc.Tab(label="NBA · Basketball", value="NBA", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NFL · Pro Football", value="NFL", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NCAAF · College Football", value="NCAAF", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="Odds API", value="ODDS_API", className="sport-tab", selected_className="sport-tab selected"),
            ], className="sport-tabs"),
            html.Div([
                html.Nav([
                    dcc.RadioItems(id="workspace-page", value="historical", options=[
                        {"label": "Historical Data", "value": "historical"},
                        {"label": "FanDuel Market", "value": "market"},
                    ], className="page-navigation"),
                ], className="page-toolbar", **{"aria-label": "Sport pages"}),
                html.Div([
                    html.Div([
                        html.Aside([
                            html.Div("BUILD YOUR DATASET", className="eyebrow"),
                            html.H2("Historical scope"),
                            field("Season starting year", dcc.Dropdown(id="season", options=list(range(date.today().year, 1998, -1)),
                                                                       value=date.today().year - 1, clearable=False),
                                  "NBA: 2024 means 2024–25. Football: 2024 means the 2024 season."),
                            field("Statistics", dcc.RadioItems(id="entity-type", options=[{"label": "Teams", "value": "team"},
                                                                                         {"label": "Players", "value": "player"}],
                                                               value="team", inline=True, className="radio-group")),
                            field("Season phase", dcc.Dropdown(id="season-type", options=[{"label": "Regular season", "value": "regular"},
                                                                                          {"label": "Postseason", "value": "postseason"}],
                                                               value="regular", clearable=False)),
                            html.Div([
                                field("College team", dcc.Input(id="college-team", type="text", value="Michigan", placeholder="e.g. Michigan", debounce=True),
                                      "NCAAF requires a school name or a week. Use the source's school name."),
                            ], id="college-fields", style={"display": "none"}),
                            html.Div([field("Week (optional)", dcc.Input(id="week", type="number", min=0, max=25, step=1,
                                                                         placeholder="All weeks", debounce=True))],
                                     id="week-fields", style={"display": "none"}),
                            html.Button("Load historical data →", id="load", n_clicks=0, className="primary-button"),
                            html.Button("Refresh from source", id="refresh", n_clicks=0, className="secondary-button"),
                            html.Div(id="source-info", className="source-info"),
                            html.Div([html.Div("A FOUNDATION FOR FORECASTING", className="eyebrow"),
                                      html.P("Start with an auditable rolling average. Compare future models against this baseline using past games only.")],
                                     className="research-note"),
                        ], className="sidebar panel"),
                        html.Section([
                            html.Div(id="load-status", role="status", **{"aria-live": "polite"}, className="load-status"),
                            dcc.Loading(html.Div([
                                html.Div(id="dataset-summary", className="dataset-summary"),
                                html.Div([
                                    html.Div(field("Team", dcc.Dropdown(id="player-team", options=[], disabled=True,
                                                                       placeholder="Loading teams...", clearable=False)),
                                             id="player-team-field", style={"display": "none"}),
                                    field(html.Span("Team", id="entity-label"), dcc.Dropdown(id="entity", options=[], disabled=True,
                                                                       placeholder="Loading teams or players...", clearable=False)),
                                ], id="identity-filters", className="analysis-filters identity-filters single-entity"),
                                html.Div([
                                    field("Statistic type", dcc.Dropdown(id="metric-group", options=[], disabled=True,
                                                                        placeholder="Loading statistic types...", clearable=False)),
                                    field("Statistic", dcc.Dropdown(id="metric", options=[], disabled=True,
                                                                   placeholder="Loading statistics...", clearable=False)),
                                    field("Rolling window", dcc.Dropdown(id="window", options=[{"label": f"{n} games", "value": n} for n in (3, 5, 10)],
                                                                         value=5, clearable=False)),
                                ], className="analysis-filters"),
                                html.Div(id="forecast-summary", className="kpi-grid", children=[
                                    kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                                    kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                                    kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]),
                                html.Div([html.Div([html.H2("Performance over time"), html.Span("HISTORY + BASELINE", className="eyebrow")],
                                                   className="panel-heading"),
                                          dcc.Graph(id="trend", figure=empty_figure(), config={"displaylogo": False}),
                                          html.P("Baseline uses only prior appearances. This reference forecast does not account for opponent, injuries, minutes, or roster changes.",
                                                 className="chart-note")], className="panel chart-panel"),
                                html.Div([
                                    html.Div([html.H2("Player career"), html.Span("ALL AVAILABLE SEASONS", className="eyebrow")],
                                             className="panel-heading"),
                                    html.P("The first career load may take a moment. Later selections reuse saved history.",
                                           className="chart-note"),
                                    dcc.Loading(html.Div([
                                        dcc.Graph(id="career-trend", figure=empty_figure("Choose a player to load career history"),
                                                  config={"displaylogo": False}),
                                        html.P(id="career-status", className="chart-note", role="status"),
                                    ]), color="#8197ff"),
                                ], id="career-panel", className="panel chart-panel career-panel", style={"display": "none"}),
                                html.Div([
                                    html.Div([html.H2("Game log"), html.Button("Export loaded dataset ↓", id="export", disabled=True,
                                                                              className="export-button")], className="panel-heading"),
                                    dash_table.DataTable(id="game-log", data=[], columns=[], page_size=12,
                                                         sort_action="native", filter_action="native",
                                                         style_table={"overflowX": "auto"},
                                                         style_cell={"backgroundColor": "#111d2e", "color": "#b8c8dc", "border": "none",
                                                                     "borderBottom": "1px solid #243249", "padding": "12px", "fontFamily": "Segoe UI",
                                                                     "textAlign": "left", "fontSize": "13px", "minWidth": "95px"},
                                                         style_header={"backgroundColor": "#17253a", "color": "#e2eaf4", "fontWeight": "600"},
                                                         style_filter={"backgroundColor": "#17253a", "color": "#b8c8dc"}),
                                ], className="panel table-panel"),
                            ]), type="circle", color="#57dbc4", target_components={"dataset-summary": "children"}),
                        ], className="analysis"),
                    ], id="historical-page", className="workspace"),
                    html.Section([
                        html.Div([
                            html.Div([
                                html.Div([html.Div("FANDUEL MARKET", className="eyebrow accent"),
                                          html.H2("NBA · Upcoming games", id="market-title"),
                                          html.P("Browse scheduled matchups, then open FanDuel to explore available markets.")]),
                                html.A("Open FanDuel ↗", id="fanduel-link", href=FANDUEL_LINKS["NBA"],
                                       target="_blank", rel="noopener noreferrer", className="market-link"),
                            ], className="panel market-heading"),
                            html.Div([
                                field("Schedule window", dcc.Dropdown(id="market-days", value=7, clearable=False,
                                      options=[{"label": "Next 7 days", "value": 7}, {"label": "Next 14 days", "value": 14}])),
                                html.Button("Refresh upcoming games", id="market-refresh", n_clicks=0, className="export-button"),
                            ], className="market-controls"),
                            html.P("Schedule data: ESPN · Times in EDT (UTC−4). NFL: use Pull Live Markets on a game to save its FanDuel snapshot (up to 17 credits). NBA and NCAAF show schedules only.",
                                   className="market-source-note"),
                            dcc.Loading(html.Div([
                                html.Div(id="market-status", role="status", **{"aria-live": "polite"}, className="load-status"),
                                html.Div(id="market-games"),
                            ]), type="circle", color="#57dbc4"),
                        ], id="market-list"),
                        html.Div([
                            html.Section([
                                html.Div([
                                    field("Estimated Probability (%)", dcc.Input(id="market-probability-threshold", type="number",
                                          value=75, min=0, max=100, step="any")),
                                    field("Edge (%)", dcc.Input(id="market-edge-threshold", type="number",
                                          value=5, min=-100, max=100, step="any")),
                                    html.Button("Apply Criteria", id="market-apply-criteria", n_clicks=0, className="apply-criteria-button"),
                                ], className="criteria-controls"),
                                html.Div("Applies to all calculated markets: alternate spreads and alternate totals (Over and Under), using the selected forecast method. Rows must exceed both thresholds. Edge is measured in percentage points.",
                                         id="market-criteria-status", role="status", **{"aria-live": "polite"}, className="criteria-status"),
                            ], className="panel market-criteria"),
                            html.Div([
                            html.Div([
                                html.A("← Back to upcoming games", href="#nfl-games", className="back-to-games"),
                                html.Div([
                                    html.Button("Pull Live Markets", id="market-detail-pull", n_clicks=0, className="pull-markets-button"),
                                    html.Small("FanDuel only · Up to 17 credits per new pull"),
                                ], className="detail-pull-control"),
                            ], className="detail-page-actions"),
                            html.Div(id="market-detail-pull-status", role="status", **{"aria-live": "polite"}),
                            html.Div(id="market-game-header"),
                            html.Div([
                                field("Saved snapshot", dcc.Dropdown(id="market-snapshot", options=[], clearable=False,
                                                                     placeholder="No saved snapshots")),
                                html.Div(id="market-snapshot-status", role="status", className="snapshot-description"),
                            ], className="snapshot-controls"),
                            html.Div(field("Forecast method", dcc.Dropdown(id="market-forecast-method", value="PPP",
                                           options=[{"label": "PPP", "value": "PPP"}, {"label": "EPA", "value": "EPA"}],
                                           clearable=False)), className="forecast-method-control"),
                            dcc.Tabs(id="market-category", value="Game", children=[], className="market-category-tabs"),
                            dcc.Loading(html.Div(id="saved-market-sections"), color="#57dbc4"),
                            ], className="sportsbook-view"),
                        ], id="market-game-detail", className="market-detail-view", style={"display": "none"}),
                    ], id="market-page", className="market-page", style={"display": "none"}),
                ], className="page-content"),
            ], id="sport-pages", className="page-shell"),
            html.Section([
                html.Div([
                    html.Div([html.H2("Monthly credit usage"),
                              html.P("Your account's used and unused credits across all sports.")]),
                    html.Button("Connect / Refresh usage", id="odds-refresh", n_clicks=0, className="export-button"),
                ], className="panel odds-heading"),
                dcc.Loading(html.Div([
                    html.Div("Open Odds API to connect.", id="odds-status", role="status",
                             **{"aria-live": "polite"}, className="load-status"),
                    html.Div([
                        html.Div([
                            html.Div(html.H2("Account quota"), className="panel-heading"),
                            dcc.Graph(id="odds-usage", figure=empty_figure("Connect to view your credit usage"),
                                      config={"displaylogo": False, "displayModeBar": False}),
                            html.P("Usage follows your subscription's current quota cycle. The API does not provide a reset date in these usage headers.",
                                   className="chart-note"),
                        ], className="panel odds-chart"),
                        html.Div([
                            html.Div(html.H2("Usage by sport this month"), className="panel-heading"),
                            dcc.Graph(id="odds-composition", figure=empty_figure("Open Odds API to view logged usage"),
                                      config={"displaylogo": False, "displayModeBar": False}),
                            html.P("Calendar-month credits recorded by this app. Account activity outside this app is not assigned to a sport.",
                                   className="chart-note"),
                        ], className="panel odds-chart"),
                    ], className="odds-usage-grid"),
                    html.Div(id="odds-summary", className="odds-summary", children=[
                        kpi("CREDITS USED", "—", "Current quota cycle"),
                        kpi("CREDITS UNUSED", "—", "Remaining before quota resets"),
                        kpi("TOTAL CREDITS", "—", "Used + unused credits"),
                    ]),
                ]), color="#57dbc4"),
                html.Div("Open Odds API to load your saved credit history.", id="odds-log-status", role="status",
                         className="chart-note usage-log-note"),
                html.Div(id="odds-sport-summary", className="kpi-grid", children=[
                    kpi(sport + " CREDITS THIS MONTH", "0", "No logged pulls") for sport in USAGE_SPORTS]),
                html.Div([
                    html.Div(html.H2("Daily credit usage history"), className="panel-heading"),
                    dcc.Graph(id="odds-history-chart", figure=empty_figure("Open Odds API to view usage history"),
                              config={"displaylogo": False}),
                    html.P("Daily totals in EDT, including days between pulls with zero usage. Select a sport in the legend to see its history. Unreported charges are excluded from totals.",
                           className="chart-note"),
                ], className="panel odds-chart"),
                html.Div([
                    html.Div(html.H2("API credit usage history"), className="panel-heading"),
                    dash_table.DataTable(id="odds-history-table", data=[], columns=[
                        {"name": "Used at (EDT)", "id": "timestamp"}, {"name": "Sport", "id": "sport"},
                        {"name": "Game", "id": "game"}, {"name": "Game time (EDT)", "id": "game_time"},
                        {"name": "Credits", "id": "credits", "type": "numeric"},
                        {"name": "Charge", "id": "charge"}, {"name": "Status", "id": "status"},
                        {"name": "Requested markets", "id": "markets"}, {"name": "Source", "id": "source"},
                        {"name": "Game ID", "id": "game_id"}, {"name": "Odds event ID", "id": "event_id"}],
                        page_size=10, sort_action="native", filter_action="native", export_format="csv",
                        style_table={"overflowX": "auto"},
                        style_cell={"backgroundColor": "#111d2e", "color": "#b8c8dc", "border": "none",
                                    "borderBottom": "1px solid #243249", "padding": "12px", "fontFamily": "Segoe UI",
                                    "textAlign": "left", "fontSize": "12px", "minWidth": "95px", "maxWidth": "360px",
                                    "whiteSpace": "normal", "height": "auto"},
                        style_header={"backgroundColor": "#17253a", "color": "#e2eaf4", "fontWeight": "600"},
                        style_filter={"backgroundColor": "#17253a", "color": "#b8c8dc"}),
                ], className="panel table-panel"),
                html.Div([
                    html.H2("Connection setup"),
                    html.P("Add your API key to this project's .env file, then restart python index.py."),
                    html.Pre("ODDS_API_KEY=your_api_key_here", className="odds-setup-code"),
                    html.P("The key stays on the server. Usage refreshes every five minutes while this section is open. This check uses the sports-list endpoint and does not consume credits."),
                    html.A("The Odds API documentation ↗", href=ODDS_DOCUMENTATION,
                           target="_blank", rel="noopener noreferrer"),
                ], className="panel odds-setup"),
            ], id="odds-api-page", style={"display": "none"}),
            html.Footer("NBA / NFL / NCAAF · Displayed times in EDT (UTC−4)")
        ])
    ], className="app-shell")

    @app.callback(Output("historical-page", "style"), Output("market-page", "style"),
                  Output("market-title", "children"), Output("fanduel-link", "href"), Output("market-timer", "disabled"),
                  Output("page-eyebrow", "children"), Output("page-title", "children"),
                  Output("page-description", "children"),
                  Output("sport-pages", "style"), Output("odds-api-page", "style"), Output("odds-timer", "disabled"),
                  Input("workspace-page", "value"), Input("sport", "value"))
    def navigate(page, sport):
        odds = sport == "ODDS_API"
        market = page == "market" and not odds
        intro = ("THE ODDS API", "Your monthly credits at a glance.",
                 "Monitor your account's credit usage and connection status.") if odds else (
                "THE UPCOMING SLATE" if market else "THE HISTORICAL LAB",
                "The next game starts here." if market else "From past performance to the next game.",
                "Explore upcoming NBA, NFL, and college football matchups." if market else
                "Explore team statistics across three leagues and player statistics for NBA and NFL.")
        return ({"display": "none" if market or odds else "grid"}, {"display": "block" if market else "none"},
                f"{sport} · Upcoming games", FANDUEL_LINKS.get(sport, FANDUEL_LINKS["NBA"]), not market,
                *intro, {"display": "none" if odds else "grid"}, {"display": "block" if odds else "none"}, not odds)

    @app.callback(Output("sport", "value"), Output("workspace-page", "value"), Input("market-url", "hash"))
    def restore_game_route(location_hash):
        if not market_game_id(location_hash):
            raise PreventUpdate
        return "NFL", "market"

    @app.callback(Output("market-list", "style"), Output("market-game-detail", "style"),
                  Output("market-game-header", "children"), Output("market-snapshot", "options"), Output("market-snapshot", "value"),
                  Output("market-detail-context", "data"),
                  Input("market-url", "hash"), Input("sport", "value"), Input("workspace-page", "value"),
                  Input("market-schedule", "data"), Input("market-refresh-signal", "data"),
                  State("market-snapshot", "value"), State("market-detail-context", "data"))
    def open_game(location_hash, sport, page, games, refreshed, previous, previous_context):
        game_id = market_game_id(location_hash)
        if not game_id or sport != "NFL" or page != "market":
            return {"display": "block"}, {"display": "none"}, [], [], None, None
        snapshots = nfl_market_service.history(game_id)
        game = next((game for game in (games or []) if str(game["id"]) == game_id), None)
        if not game and snapshots:
            saved = snapshots[0]
            game = saved.get("game") or {"id": game_id, "start": saved["commence_time"],
                "home": {"name": saved["home_team"], "abbreviation": saved.get("home_abbreviation", "HOME")},
                "away": {"name": saved["away_team"], "abbreviation": saved.get("away_abbreviation", "AWAY")}, "time_tbd": False}
        context = {**game, "token": previous_context["token"] if previous_context and str(previous_context.get("id")) == game_id
                   else uuid4().hex} if game else None
        options = [{"label": format_eastern(item["pulled_at"]),
                    "value": item.get("csv_file", item["pulled_at"])} for item in snapshots]
        chosen = previous if previous in {option["value"] for option in options} else options[0]["value"] if options else None
        if ("market-refresh-signal.data" in ctx.triggered_prop_ids and refreshed and
                str(refreshed.get("game_id")) == game_id and refreshed.get("snapshot") in {item["value"] for item in options}):
            chosen = refreshed["snapshot"]
        return {"display": "none"}, {"display": "block"}, game_header(game, snapshots[0] if snapshots else None), options, chosen, context

    @app.callback(Output("market-refresh-signal", "data"),
                  Input("market-detail-pull", "n_clicks"), State("market-detail-context", "data"),
                  State("market-url", "hash"), State("sport", "value"), State("workspace-page", "value"),
                  prevent_initial_call=True, running=[(Output("market-detail-pull", "disabled"), True, False)])
    def pull_game_detail(clicks, game, location_hash, sport, page):
        game_id = market_game_id(location_hash)
        if not clicks or sport != "NFL" or page != "market" or not game_id:
            raise PreventUpdate
        if not game or str(game.get("id")) != game_id:
            return {"game_id": game_id, "error": "Game information is not loaded yet. Refresh the upcoming schedule before pulling."}
        try:
            snapshot = nfl_market_service.pull(game, f"detail:{game_id}:{game['token']}:{clicks}")
        except DataSourceError as exc:
            return {"game_id": game_id, "error": str(exc), "attempt": clicks}
        return {"game_id": game_id, "snapshot": snapshot.get("csv_file", snapshot["pulled_at"]), "pulled_at": snapshot["pulled_at"]}

    @app.callback(Output("market-detail-pull-status", "children"), Input("market-refresh-signal", "data"), Input("market-url", "hash"))
    def detail_pull_status(result, location_hash):
        if not result or str(result.get("game_id")) != market_game_id(location_hash):
            return []
        if result.get("error"):
            return html.P(result["error"], className="market-pull-error")
        return html.P("Saved new snapshot · " + format_eastern(result["pulled_at"]), className="detail-pull-success")

    def saved_snapshot(location_hash, selection):
        game_id = market_game_id(location_hash)
        if not game_id or not selection:
            return None
        return next((item for item in nfl_market_service.history(game_id)
                     if item.get("csv_file", item["pulled_at"]) == selection), None)

    @app.callback(Output("market-category", "children"), Output("market-category", "value"), Output("market-snapshot-status", "children"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), State("market-category", "value"))
    def snapshot_categories(selection, location_hash, previous):
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return [], None, "No saved markets yet. Use Pull Live Markets above to download a snapshot."
        available = {row["group"] for row in snapshot["rows"]}
        tabs = [dcc.Tab(label=label, value=key, className="market-category-tab", selected_className="market-category-tab selected")
                for key, label in CATEGORIES if key in available]
        value = previous if previous in available else tabs[0].value if tabs else None
        cost = f"{snapshot['cost']} credits" if snapshot["cost"] is not None else "Cost not reported"
        return tabs, value, [html.Span(latest_pull_text(snapshot) + f" · {cost}"),
                             html.Small("Saved in data/market_data", title=snapshot.get("csv_file", "Saved locally"))]

    @app.callback(Output("market-applied-criteria", "data"), Output("market-criteria-status", "children"),
                  Input("market-apply-criteria", "n_clicks"), State("market-probability-threshold", "value"),
                  State("market-edge-threshold", "value"), prevent_initial_call=True)
    def apply_market_criteria(clicks, probability, edge):
        if not clicks:
            raise PreventUpdate
        if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    for value in (probability, edge)) or not 0 <= probability <= 100 or not -100 <= edge <= 100):
            return no_update, "Enter an Estimated Probability from 0 to 100 and an Edge from -100 to 100. Previous criteria remain applied."
        return {"probability": probability / 100, "edge": edge / 100}, (
            f"Applied to all calculated markets (alternate spreads and alternate totals): estimated probability > {probability:g}% and Edge > {edge:g} percentage points. Matching rows have a highlighted border using the selected forecast method.")

    @app.callback(Output("saved-market-sections", "children"), Input("market-category", "value"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), Input("market-applied-criteria", "data"),
                  Input("market-forecast-method", "value"))
    def show_saved_markets(category, selection, location_hash, criteria, method):
        method = method or "PPP"
        if method not in ("PPP", "EPA"):
            raise PreventUpdate
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return html.P("Use Pull Live Markets above to download this game's first snapshot.", className="saved-market-empty")
        if not snapshot["rows"]:
            return html.P("The saved response contains no FanDuel selections.", className="saved-market-empty")
        forecast, model_error = None, None
        if category == "Game" and any(row["market_key"] in {"alternate_spreads", "alternate_totals"} for row in snapshot["rows"]):
            try:
                forecast = (epa_service if method == "EPA" else ppp_service).forecast(snapshot)
            except DataSourceError as exc:
                model_error = str(exc)
        return market_sections(snapshot, category, forecast, model_error, criteria, method)

    @app.callback(Output("odds-composition", "figure"), Output("odds-sport-summary", "children"),
                  Output("odds-history-chart", "figure"), Output("odds-history-table", "data"),
                  Output("odds-log-status", "children"),
                  Input("sport", "value"), Input("odds-refresh", "n_clicks"), Input("odds-timer", "n_intervals"),
                  Input("market-refresh-signal", "data"))
    def logged_usage(sport, _refresh, _timer, _market_pull):
        if sport != "ODDS_API":
            raise PreventUpdate
        try:
            account = odds_service.account_id()
            if not isinstance(account, str):
                raise DataSourceError("Connect your account to view its recorded credit history.")
            # Import older saved pulls once; reading stored snapshots never requests odds.
            if isinstance(nfl_market_service, NflMarketService):
                nfl_market_service.export_existing()
            summary = usage_summary(api_usage_log.records(account))
        except (DataSourceError, OSError, ValueError, csv.Error) as exc:
            return (empty_figure("Credit history unavailable"),
                    [kpi(sport + " CREDITS THIS MONTH", "—", "History unavailable") for sport in USAGE_SPORTS],
                    empty_figure("Credit history unavailable"), [], f"Credit history unavailable. {exc}")
        composition, daily = usage_figures(summary)
        metrics = []
        for sport in USAGE_SPORTS:
            item = summary["monthly"][sport]
            detail = f"{summary['month']} · {item['pulls']:,} logged pulls"
            if item["unknown"]:
                detail += f" · {item['unknown']:,} unreported charges"
            metrics.append(kpi(sport + " CREDITS THIS MONTH", f"{item['credits']:,}", detail))
        note = (f"{summary['month']} (EDT) · {len(summary['history']):,} recorded pulls · "
                "Saved in data/API Log/api_credit_usage.csv. Totals include reported charges only.")
        if summary["unknown"]:
            note += f" {summary['unknown']:,} pulls have unreported charges."
        return composition, metrics, daily, summary["history"], note

    @app.callback(Output("odds-usage", "figure"), Output("odds-summary", "children"), Output("odds-status", "children"),
                  Input("sport", "value"), Input("odds-refresh", "n_clicks"), Input("odds-timer", "n_intervals"),
                  running=[(Output("odds-refresh", "disabled"), True, False)])
    def odds_usage(sport, _refresh, _timer):
        if sport != "ODDS_API":
            raise PreventUpdate
        try:
            usage = odds_service.usage(refresh=ctx.triggered_id == "odds-refresh")
        except DataSourceError as exc:
            return empty_figure("Connect to The Odds API to view monthly credits"), [
                kpi("CREDITS USED", "—", "Awaiting connection"), kpi("CREDITS UNUSED", "—", "Awaiting connection"),
                kpi("TOTAL CREDITS", "—", "Awaiting connection")], str(exc)
        status = ("Saved usage snapshot" if usage.stale else "Connected to The Odds API") + f" · Last checked {format_eastern(usage.checked_at)}."
        if usage.notice:
            status += " " + usage.notice
        return credit_figure(usage), [
            kpi("CREDITS USED", f"{usage.used:,}", "Current quota cycle"),
            kpi("CREDITS UNUSED", f"{usage.remaining:,}", "Remaining before quota resets"),
            kpi("TOTAL CREDITS", f"{usage.total:,}", "Used + unused credits")], status

    @app.callback(Output({"type": "nfl-live-markets", "game": MATCH}, "children"),
                  Input({"type": "nfl-pull", "game": MATCH}, "n_clicks"),
                  State({"type": "nfl-game-context", "game": MATCH}, "data"),
                  State("sport", "value"), State("workspace-page", "value"), prevent_initial_call=True,
                  running=[(Output({"type": "nfl-pull", "game": MATCH}, "disabled"), True, False)])
    def pull_nfl_markets(clicks, game, sport, page):
        if not clicks or sport != "NFL" or page != "market" or not game:
            raise PreventUpdate
        if not isinstance(ctx.triggered_id, dict) or str(ctx.triggered_id.get("game")) != str(game.get("id")):
            raise PreventUpdate
        try:
            snapshot = nfl_market_service.pull(game, f"{game['id']}:{game['token']}:{clicks}")
        except DataSourceError as exc:
            saved = nfl_market_service.latest([game["id"]]).get(str(game["id"]))
            return [latest_pull_status(saved), html.Span(str(exc), className="market-pull-error")]
        return latest_pull_status(snapshot)

    @app.callback(Output("market-games", "children"), Output("market-status", "children"), Output("market-schedule", "data"),
                  Input("workspace-page", "value"), Input("sport", "value"), Input("market-days", "value"),
                  Input("market-refresh", "n_clicks"), Input("market-timer", "n_intervals"),
                  Input("market-refresh-signal", "data"), State("market-schedule", "data"),
                  running=[(Output("market-refresh", "disabled"), True, False)])
    def upcoming_games(page, sport, days, _refresh, _timer, _refreshed, previous_games):
        if page != "market" or sport not in SOURCES:
            raise PreventUpdate
        if ctx.triggered_id == "market-refresh-signal" and sport == "NFL" and previous_games:
            saved = nfl_market_service.latest([game["id"] for game in previous_games])
            return schedule_blocks(previous_games, sport, saved), no_update, no_update
        try:
            schedule = schedule_service.load(sport, days, refresh=ctx.triggered_id == "market-refresh")
        except (DataSourceError, ValueError) as exc:
            return html.Div([html.H3("Schedule unavailable"), html.P(str(exc)),
                             html.A("Check ESPN schedule ↗", href=SCHEDULE_LINKS[sport], target="_blank", rel="noopener noreferrer")],
                            className="panel market-empty"), "Upcoming games could not be loaded.", []
        status = f"{sport} · {len(schedule.games)} upcoming games · ESPN · Oldest schedule update: {format_eastern(schedule.fetched_at)} · Refreshes every 5 minutes."
        if schedule.stale_days:
            status += " Saved schedules shown after a source error for: " + ", ".join(schedule.stale_days) + "."
        if schedule.failed_days:
            status += " Incomplete coverage; unavailable dates: " + ", ".join(schedule.failed_days) + "."
        saved = nfl_market_service.latest([game["id"] for game in schedule.games]) if sport == "NFL" else {}
        blocks = schedule_blocks(schedule.games, sport, saved)
        if not blocks:
            blocks = html.Div([html.H3("No upcoming games found"),
                               html.P("No scheduled games were returned for the loaded dates. Try the 14-day window or refresh the schedule.")],
                              className="panel market-empty")
        return blocks, status, schedule.games

    @app.callback(Output("college-fields", "style"), Output("week-fields", "style"),
                  Output("source-info", "children"), Input("sport", "value"))
    def source_details(sport):
        if sport not in SOURCES:
            raise PreventUpdate
        name, url = SOURCES[sport]
        notes = {"NBA": "NBA Stats game logs and SportsDataverse historical box-score archives. No API key.",
                 "NFL": "Weekly nflverse team and player box scores. No API key. Coverage varies by statistic.",
                 "NCAAF": "FBS game box scores. Requires CFBD_API_KEY. API quotas and historical coverage depend on your access."}
        return ({"display": "block" if sport == "NCAAF" else "none"},
                {"display": "block" if sport != "NBA" else "none"},
                [html.Div("DATA SOURCE", className="eyebrow"), html.A(name + " ↗", href=url, target="_blank", rel="noreferrer"),
                 html.P(notes[sport]), html.Small("Cached locally for 24 hours. Refresh bypasses the cache.")])

    @app.callback(Output("entity-type", "options"), Output("entity-type", "value"),
                  Input("sport", "value"), State("entity-type", "value"))
    def statistics_modes(sport, kind):
        if sport not in SOURCES:
            raise PreventUpdate
        options = [{"label": "Teams", "value": "team"}]
        if sport in PLAYER_SPORTS:
            options.append({"label": "Players", "value": "player"})
        return options, kind if kind in {option["value"] for option in options} else "team"

    @app.callback(Output("dataset", "data"), Output("load-status", "children"),
                  Output("dataset-summary", "children"), Output("export", "disabled"),
                  Input("load", "n_clicks"), Input("refresh", "n_clicks"), Input("sport", "value"),
                  Input("season", "value"), Input("entity-type", "value"), Input("season-type", "value"),
                  Input("college-team", "value"), Input("week", "value"), Input("workspace-page", "value"),
                  running=[(Output("load", "disabled"), True, False), (Output("refresh", "disabled"), True, False)])
    def load_data(_load, _refresh, sport, season, kind, phase, team, week, page):
        if page != "historical" or sport not in SOURCES:
            raise PreventUpdate
        if sport not in PLAYER_SPORTS:
            kind = "team"
        try:
            if season is None or int(season) != float(season):
                raise ValueError("Choose a whole season year.")
            if sport != "NBA" and week is not None and int(week) != float(week):
                raise ValueError("Choose a whole week number.")
            query = Query(sport, int(season), kind, phase, (team or "").strip() if sport == "NCAAF" else "",
                          int(week) if week is not None and sport != "NBA" else None)
            result = service.load(query, refresh=ctx.triggered_id == "refresh")
        except (DataSourceError, ValueError, TypeError) as exc:
            return None, str(exc), "No dataset loaded", True
        summary = (f"{sport} · {season} · {phase} · {len(result.frame):,} appearances · "
                   f"{result.frame['entity_id'].nunique():,} {kind}s · {result.source}")
        message = f"{result.status.capitalize()} · Retrieved {format_eastern(result.fetched_at)}."
        if result.notice:
            message += f" Showing previously saved data. {result.notice}"
        return {"key": result.key, "sport": sport, "season": season, "kind": kind, "phase": phase}, message, summary, False

    @app.callback(Output("entity", "options"), Output("entity", "value"),
                  Output("entity", "disabled"), Output("entity", "placeholder"),
                  Input("dataset", "data"), Input("player-team", "value"), State("entity", "value"))
    def select_options(dataset, team, previous_entity):
        if not supported_dataset(dataset):
            return [], None, True, "No data available — see source status"
        try:
            frame = service.read(dataset["key"]).frame
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        frame = filter_team(frame, dataset["kind"], team)
        entities = frame.reindex(columns=["entity_id", "entity_name", "team", "position"]).drop_duplicates("entity_id").sort_values("entity_name")
        options = [{"label": (f"{r.entity_name} ({r.team}" + (f" · {r.position}" if pd.notna(r.position) and r.position else "") + ")")
                    if dataset["kind"] == "player" else r.entity_name,
                    "value": r.entity_id} for r in entities.itertuples()]
        if not options:
            return [], None, True, "No matching observations"
        selected_entity = previous_entity if previous_entity in set(entities["entity_id"]) else options[0]["value"]
        return options, selected_entity, False, "Choose a team or player"

    @app.callback(Output("player-team-field", "style"), Output("identity-filters", "className"),
                  Output("entity-label", "children"), Input("entity-type", "value"), Input("sport", "value"))
    def show_player_team(kind, sport):
        if kind == "player" and sport in PLAYER_SPORTS:
            return {"display": "block"}, "analysis-filters identity-filters", "Player"
        return {"display": "none"}, "analysis-filters identity-filters single-entity", "Team"

    @app.callback(Output("player-team", "options"), Output("player-team", "value"),
                  Output("player-team", "disabled"), Output("player-team", "placeholder"),
                  Input("dataset", "data"), State("player-team", "value"))
    def select_player_teams(dataset, previous_team):
        if not supported_dataset(dataset) or dataset["kind"] != "player":
            return [], None, True, "Choose player statistics to filter by team"
        try:
            frame = service.read(dataset["key"]).frame
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        teams = sorted(team for team in frame["team"].dropna().unique() if str(team).strip())
        if not teams:
            return [], None, True, "No team data available"
        options = [{"label": "All teams", "value": ALL_TEAMS}] + [{"label": team, "value": team} for team in teams]
        selected = previous_team if previous_team == ALL_TEAMS or previous_team in teams else ALL_TEAMS
        return options, selected, False, "Choose a team"

    def available_metrics(dataset, entity, team):
        if not supported_dataset(dataset) or entity is None:
            return []
        frame = service.read(dataset["key"]).frame
        frame = filter_team(frame, dataset["kind"], team)
        selected = frame[frame["entity_id"] == entity]
        return relevant_metrics(selected, dataset["sport"], dataset["kind"])

    @app.callback(Output("metric-group", "options"), Output("metric-group", "value"),
                  Output("metric-group", "disabled"), Output("metric-group", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  State("metric-group", "value"), State("metric", "value"))
    def select_categories(dataset, entity, team, previous_category, previous_metric):
        try:
            metrics = available_metrics(dataset, entity, team)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        if not metrics:
            return [], None, True, "No relevant statistic types available"
        options = statistic_categories(metrics, dataset["sport"])
        categories = {option["value"] for option in options}
        preferred = previous_metric if previous_metric in metrics else preferred_metric(metrics)
        value = previous_category if previous_category in categories else statistic_category(preferred, dataset["sport"])
        return options, value, False, "Choose a statistic type"

    @app.callback(Output("metric", "options"), Output("metric", "value"),
                  Output("metric", "disabled"), Output("metric", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  Input("metric-group", "value"), State("metric", "value"))
    def select_metrics(dataset, entity, team, category, previous_metric):
        try:
            metrics = available_metrics(dataset, entity, team)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        metrics = [metric for metric in metrics if statistic_category(metric, dataset["sport"]) == category]
        if not metrics:
            return [], None, True, "Choose an available statistic type"
        value = previous_metric if previous_metric in metrics else preferred_metric(metrics)
        return [{"label": metric_label(c, dataset["sport"]), "value": c} for c in metrics], value, False, "Choose a statistic"

    @app.callback(Output("trend", "figure"), Output("forecast-summary", "children"),
                  Output("game-log", "data"), Output("game-log", "columns"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"), Input("window", "value"),
                  Input("player-team", "value"))
    def analyze(dataset, entity, metric, window, team):
        blank = [kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                 kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                 kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]
        if not supported_dataset(dataset) or entity is None or metric is None:
            return empty_figure(), blank, [], []
        frame = service.read(dataset["key"]).frame
        frame = filter_team(frame, dataset["kind"], team)
        if metric not in frame:
            return empty_figure(), blank, [], []
        selected = frame[frame["entity_id"] == entity]
        if selected.empty or metric not in relevant_metrics(selected, dataset["sport"], dataset["kind"]):
            return empty_figure(), blank, [], []
        history, prediction, mae, evaluated = rolling_baseline(selected, metric, window)
        if history.empty:
            return empty_figure("No observations of this statistic for the selected entity"), blank, [], []
        figure = performance_figure(history, metric, window, prediction)
        cards = [kpi("OBSERVED AVERAGE", f"{history[metric].mean():,.2f}", f"{len(history):,} recorded appearances"),
                 kpi("NEXT APPEARANCE", f"{prediction:,.2f}" if prediction is not None else "—",
                     f"Mean of the last {window} appearances" if prediction is not None else f"Requires {window} recorded appearances"),
                 kpi("BACKTEST MAE", f"{mae:,.2f}" if mae is not None else "—",
                     f"{evaluated} past predictions evaluated" if evaluated else f"Requires more than {window} appearances")]
        table = history.reindex(columns=["game_id", "event_date", "week", "team", "opponent", "matchup", metric, "baseline"]).copy()
        table["event_date"] = table["event_date"].dt.strftime("%Y-%m-%d")
        table = table.sort_index(ascending=False).round(3)
        # JSON nulls rather than NaN/NaT if schedule metadata is unavailable.
        records = table.astype(object).where(pd.notna(table), None).to_dict("records")
        columns = [{"name": c.replace("_", " ").title(), "id": c} for c in table]
        return figure, cards, records, columns

    @app.callback(Output("career-panel", "style"), Input("entity-type", "value"), Input("sport", "value"))
    def show_career(kind, sport):
        return {"display": "block" if kind == "player" and sport in PLAYER_SPORTS else "none"}

    @app.callback(Output("career-trend", "figure"), Output("career-status", "children"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"))
    def analyze_career(dataset, entity, metric):
        if not supported_dataset(dataset) or dataset["kind"] != "player" or entity is None or metric is None:
            return empty_figure("Choose a player and statistic to load career history"), ""
        try:
            frame = service.read(dataset["key"]).frame
            selected = frame[frame["entity_id"] == entity]
            if selected.empty or metric not in relevant_metrics(selected, dataset["sport"], "player"):
                return empty_figure("Choose a statistic relevant to this player"), ""
            name = str(selected.iloc[0]["entity_name"])
            career = career_service.load(dataset["sport"], entity, name, metric, dataset.get("phase", "regular"))
        except DataSourceError as exc:
            return empty_figure("Career history could not be loaded"), str(exc)
        phase_label = "Regular season" if career.phase == "regular" else "Postseason"
        status = (f"{name} · {len(career.frame)} recorded seasons · {phase_label} · "
                  f"{career.source}. Counting statistics use season totals; rates retain source units. "
                  "Includes all teams and ignores the selected season and week filters.")
        if career.searched:
            status += f" Source coverage checked: {min(career.searched)}–{max(career.searched)}."
        if career.failed:
            status += " Incomplete coverage: could not load " + ", ".join(map(str, career.failed)) + "."
        return career_figure(career, dataset["sport"], metric, name), status

    @app.callback(Output("download", "data"), Input("export", "n_clicks"), State("dataset", "data"), prevent_initial_call=True)
    def export_data(_clicks, dataset):
        if not supported_dataset(dataset):
            return no_update
        frame = service.read(dataset["key"]).frame
        return dcc.send_data_frame(frame.to_csv, f"{dataset['sport']}_{dataset['season']}_{dataset['kind']}_stats.csv", index=False)

    return app
