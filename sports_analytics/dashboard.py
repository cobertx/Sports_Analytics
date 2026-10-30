"""Dash presentation layer. Load data when the user opens or changes a scope."""

from datetime import date, datetime
from collections import OrderedDict
import csv
import hashlib
import json
import math
import re
from pathlib import Path
from uuid import uuid4
from time import time
from threading import Lock

from dash import ALL, Dash, Input, MATCH, Output, State, ctx, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate, MissingCallbackContextException
from flask import abort, send_from_directory
import pandas as pd
import plotly.graph_objects as go

from .data import DataSourceError, PLAYER_SPORTS, Query, SOURCES
from .career import CareerService
from .forecast import rolling_baseline
from .backtesting_view import (backtesting_route,find_model,navigation as backtesting_navigation,
                               page as backtesting_page,summary_route as backtest_summary_route)
from .backtesting_summary import layout as backtest_summary_layout,register as register_backtest_summary
from .performance_summary import layout as performance_layout,register as register_performance
from .backtesting_view import PERFORMANCE_ROUTE
from .backtesting_service import BacktestingService,default_evaluation_games,default_lookback,export_filename,nfl_backtest_criteria
from .backtesting_results import layout as backtesting_results_layout,render as render_backtesting_results,empty as empty_backtesting_results
from .backtesting_results import evaluated_records
from .backtesting_monitor import layout as backtesting_monitor_layout,contents as backtesting_monitor_contents
from . import research_view
from .comparison_view import layout as comparison_layout
from .comparison_callbacks import register as register_comparison_callbacks
from .in_game_data import InGameService
from .in_game_view import layout as in_game_layout
from .in_game_callbacks import register as register_in_game_callbacks
from .kalshi import KalshiService
from .kalshi_view import layout as kalshi_layout, route as kalshi_route
from .kalshi_callbacks import register as register_kalshi_callbacks
from .kalshi_analysis_callbacks import register as register_kalshi_analysis
from .polymarket import PolymarketService
from . import polymarket_view
from . import market_summary
from .market_summary_view import layout as market_summary_layout
from .market_summary_callbacks import register as register_market_summary
from .market_structure import MarketStructure
from .market_structure_view import layout as structure_layout, register as register_structure
from .filtered_results import FilteredResults, route as filtered_results_route
from .filtered_results_view import layout as filtered_results_layout, register as register_filtered_results
from .page_cache import PageTasks,QuotePages,forecast_revision
from .loading import configure as configure_loading,register as register_loading,indicator,loading as action_loading
from .research_config import form_parameters
from .historical_view import historical_route,statistics_kind,navigation as historical_navigation
from .historical_links import history_target,matches_dataset,target_entity,target_metric,market_metrics,NBA_COMBINATIONS
from .prompt_history import prior_history,outcomes as prompt_outcomes,annotate as annotate_prompt
from .player_distribution import register as register_player_distribution
from .metrics import metric_label, preferred_metric, relevant_metrics, statistic_categories, statistic_category
from .service import DataService
from .schedule import FANDUEL_LINKS, SCHEDULE_LINKS, ScheduleService
from .odds_api import DOCUMENTATION as ODDS_DOCUMENTATION, OddsApiService
from .nfl_markets import NflMarketService
from .nba_markets import NBA_CATEGORIES, NBA_MARKETS, NbaMarketService
from .ncaaf_markets import NCAAF_MARKETS, NcaafMarketService
from .whp_service import WHPMarketService
from .usage_opportunity_service import UsageOpportunityMarketService
from .opponent_adjusted_service import OpponentAdjustedMarketService
from .pace_game_environment_service import PaceGameEnvironmentMarketService
from .parlay import build_leg,clean_state,quote_key,toggle_leg
from .parlay_view import slip_layout,slip_contents,odds as parlay_odds
from .ncaaf_model_data import NcaafModelData
from .ncaaf_forecast_service import NCAAFMarketForecastService
from models.season_weights import POLICY_VERSION
from .market_view import _market_probability,selection_text
from .market_view import CATEGORIES, NBA_LOGO_DIRECTORY, NBA_LOGO_FILES, NFL_LOGO_DIRECTORY, NFL_LOGO_FILES, NCAAF_LOGO_DIRECTORY, NCAAF_LOGO_FILES, game_header, latest_pull_status, latest_pull_text, logo_badge, market_sections
from .market_navigation import remembered_market
from .mispricing import (FILTER_CATEGORY, criteria_values, filter_context, rank_markets, results as mispricing_results,
                         quick_controls, selected_methods, run_forecasts, consensus_rankings, ALL_PROBABILITIES)
from .display_time import eastern_datetime, format_eastern
from .information import InformationService,route as information_route
from .information_view import layout as information_layout,navigation as information_navigation,register as register_information
from .game_information import ADDITIONAL_INFORMATION,GameInformationService
from .game_information_view import layout as game_information_layout,register as register_game_information
from .pnl import Ledger
from .pnl_view import layout as pnl_layout,register as register_pnl
from .usage_log import ApiUsageLog, SPORTS as USAGE_SPORTS, usage_summary
from .ppp_service import PPPMarketService
from .epa_service import EPAMarketService
from .nba_ppp_service import NBAPPPMarketService
from .nba_epa_service import NBAEPAMarketService
from .elo_service import ELOMarketService, NBAELOMarketService

ALL_TEAMS = "__all__"


def market_game_id(location_hash):
    match = re.fullmatch(r"#(?:nfl|nba|ncaaf)-game-(\d+)", location_hash or "")
    return match[1] if match else None


def market_game_sport(location_hash):
    return location_hash[1:].split("-game-", 1)[0].upper() if market_game_id(location_hash) else None


def supported_dataset(dataset):
    return bool(dataset) and (dataset["kind"] == "team" or dataset["sport"] in PLAYER_SPORTS or dataset.get('saved_players'))


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
    components=NBA_COMBINATIONS.get(metric,()) if history.iloc[0]['sport']=='NBA' else ()
    if components and set(components)<=set(history):
        colors={'pts':'#57dbc4','reb':'#8197ff','ast':'#f6c667'}
        for component in components:
            figure.add_trace(go.Bar(x=x,y=history[component],name=metric_label(component,'NBA'),width=.65,
                marker={'color':colors[component]},customdata=[(*detail,float(total)) for detail,total in zip(details,history[metric])],
                hovertemplate=hover.replace('<extra>','<br>Combined total: %{customdata[2]:.2f}<extra>')))
    else:
        components=()
        figure.add_trace(go.Bar(x=x, y=history[metric], name=label, width=.65,
                               marker={"color": "#57dbc4"}, customdata=details, hovertemplate=hover))
    figure.add_trace(go.Scatter(x=x, y=history["baseline"], mode="lines", name=f"Prior {window}-game mean",
                               line={"color": "#8197ff", "dash": "dot"}, customdata=details, hovertemplate=hover))
    step = max(1, math.ceil(len(history) / 10))
    indices = sorted(set(range(0, len(history), step)) | {len(history) - 1})
    ticks = [x[i] for i in indices]
    labels = [f"{details[i][0]}<br>{details[i][1]}" for i in indices]
    if prediction is not None:
        figure.add_trace(go.Bar(x=[len(history) + 1], y=[prediction], name="Next appearance baseline", width=.65,
                               marker={"color": "#f6c667"},
                               hovertemplate="Next appearance<br>Date and matchup not yet known<br>%{y:.2f}<extra>Baseline</extra>"))
        ticks.append(len(history) + 1)
        labels.append("Next appearance<br>Matchup TBD")
    figure = style_figure(figure)
    figure.update_layout(height=440, barmode="stack" if components else "overlay",
                         meta={'stat_components':list(components)},margin={"l": 55, "r": 25, "t": 35, "b": 115})
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
               api_usage_log=None, ppp_service=None, epa_service=None, nba_market_service=None, nba_ppp_service=None,
               nba_epa_service=None, elo_service=None, nba_elo_service=None, ncaaf_market_service=None,
               whp_service=None, usage_opportunity_service=None, opponent_adjusted_service=None, pace_game_environment_service=None,
               ncaaf_model_data=None,ncaaf_forecast_services=None,backtesting_service=None,in_game_service=None,kalshi_service=None,
               polymarket_service=None,information_service=None,ledger=None,structure_service=None,filtered_results_service=None,
               game_information_service=None):
    service = service or DataService()
    filtered_results_service = filtered_results_service or FilteredResults(service.path.with_name('filtered_results.sqlite3'))
    information_service=information_service or InformationService()
    game_information_service=game_information_service or GameInformationService(information_service)
    ledger=ledger or Ledger()
    in_game_service = in_game_service or InGameService(data=service)
    kalshi_service = kalshi_service or KalshiService(background=True)
    polymarket_service = polymarket_service or PolymarketService(background=True)
    structure_service = structure_service or MarketStructure(kalshi=kalshi_service,polymarket=polymarket_service)
    market_page_tasks=PageTasks(capacity=16)
    exchange_pages={name:QuotePages(source) for name,source in [('Kalshi',kalshi_service),('Polymarket',polymarket_service)]
                    if getattr(source,'background',False) is True}
    backtesting_service=backtesting_service or BacktestingService(data=service,isolated=True)
    career_service = career_service or CareerService(service.path.with_name("career.sqlite3"))
    schedule_service = schedule_service or ScheduleService(service.path.with_name("schedules.sqlite3"),background=True)
    odds_service = odds_service or OddsApiService()
    ppp_service = ppp_service or PPPMarketService()
    epa_service = epa_service or EPAMarketService()
    nba_ppp_service = nba_ppp_service or NBAPPPMarketService()
    nba_epa_service = nba_epa_service or NBAEPAMarketService()
    elo_service = elo_service or ELOMarketService()
    nba_elo_service = nba_elo_service or NBAELOMarketService()
    whp_service = whp_service or WHPMarketService()
    usage_opportunity_service = usage_opportunity_service or UsageOpportunityMarketService()
    opponent_adjusted_service = opponent_adjusted_service or OpponentAdjustedMarketService()
    pace_game_environment_service = pace_game_environment_service or PaceGameEnvironmentMarketService()
    ncaaf_model_data = ncaaf_model_data or NcaafModelData()
    ncaaf_forecast_services = ncaaf_forecast_services or {
        method:NCAAFMarketForecastService(method,data=ncaaf_model_data) for method in ['PPP','EPA','ELO']}
    data_directory = Path(__file__).resolve().parent.parent / "data"
    nfl_market_service = nfl_market_service or NflMarketService(odds_service, service.path.with_name("nfl_markets.sqlite3"),
                                                              data_directory / "market_data", usage_log=api_usage_log)
    market_log = getattr(nfl_market_service, "usage_log", None)
    api_usage_log = api_usage_log or (market_log if isinstance(market_log, ApiUsageLog) else
                                     ApiUsageLog(data_directory / "API Log" / "api_credit_usage.csv"))
    nba_market_service = nba_market_service or NbaMarketService(odds_service, service.path.with_name("nba_markets.sqlite3"),
                                                              data_directory / "market_data", usage_log=api_usage_log)
    ncaaf_market_service = ncaaf_market_service or NcaafMarketService(odds_service, service.path.with_name("ncaaf_markets.sqlite3"),
                                                                  data_directory / "market_data", usage_log=api_usage_log)
    market_services = {"NFL": nfl_market_service, "NBA": nba_market_service, "NCAAF": ncaaf_market_service}
    app = Dash(__name__, assets_folder=str(Path(__file__).parent / "assets"),
               title="Sports Analytics", update_title=None)

    def historical_target(location_hash):
        target=history_target(location_hash)
        if target and target['sport']=='NCAAF' and target['kind']=='team':
            try:target={**target,'name':ncaaf_model_data.resolve_school(target['name'])}
            except (DataSourceError,OSError,ValueError):pass
        return target

    @app.server.get("/images/nfl/team-logos/<filename>")
    def nfl_team_logo(filename):
        if filename not in NFL_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NFL_LOGO_DIRECTORY, filename, mimetype="image/png")

    @app.server.get("/images/nba/team-logos/<filename>")
    def nba_team_logo(filename):
        if filename not in NBA_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NBA_LOGO_DIRECTORY, filename, mimetype="image/png")

    @app.server.get("/images/ncaaf/team-logos/<filename>")
    def ncaaf_team_logo(filename):
        if filename not in NCAAF_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NCAAF_LOGO_DIRECTORY, filename, mimetype="image/png")

    @app.server.get('/backtesting/docs/<group>/<model_key>')
    def backtesting_documentation(group,model_key):
        model=find_model('NBA',(group,model_key))
        if model is None or not (model.directory/'README.md').is_file():abort(404)
        return send_from_directory(model.directory,'README.md',as_attachment=True,
                                   download_name=f'{model.folder}_README.md')

    app.layout = html.Div([
        dcc.Store(id="dataset"), dcc.Download(id="download"),
        dcc.Store(id='historical-cache-pending',data=False),dcc.Interval(id='historical-cache-poll',interval=500,disabled=True),
        html.Div([dcc.Input(id='workspace-page',type='hidden',value='historical'),
                  dcc.Input(id='entity-type',type='hidden',value='team')],style={'display':'none'}),
        dcc.Location(id="market-url", refresh=False), dcc.Store(id="market-schedule", data=[]),
        dcc.Store(id="market-detail-context"), dcc.Store(id="market-refresh-signal"),
        dcc.Store(id='market-schedule-scope'),
        dcc.Store(id="market-applied-criteria"),
        dcc.Store(id='market-return',data={},storage_type='session'),
        dcc.Store(id='mispricing-request'),
        dcc.Store(id='mispricing-forecast-history'),
        dcc.Store(id="market-forecast-history"),
        dcc.Interval(id='market-model-poll',interval=1000,disabled=True),
        dcc.Store(id="parlay-store",storage_type="session",data={"version":1,"legs":[],"collapsed":False}),
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
                dcc.Tab(label='Strategy & Research',value='RESEARCH',className='sport-tab',selected_className='sport-tab selected'),
                dcc.Tab(label='P&L History',value='PNL',className='sport-tab',selected_className='sport-tab selected'),
            ], className="sport-tabs"),
            html.Div([
                html.Nav([
                    html.A('Historical Data',id='historical-page-link',href='#historical-team',className='sport-page-link active'),
                    html.Div(id='historical-navigation',className='historical-navigation backtesting-model-links'),
                    html.Details([
                        html.Summary('Market Data'),
                        html.Div([
                            html.A('FanDuel',id='market-page-link',href='#nba-games',className='sport-page-link'),
                            html.A('Kalshi',id='kalshi-page-link',href='#nba-kalshi-market',className='sport-page-link'),
                            html.A('Polymarket',id='polymarket-page-link',href='#nba-polymarket-market',className='sport-page-link'),
                            html.A('Market Summary',id='market-summary-page-link',href='#nba-market-summary',className='sport-page-link'),
                            html.A('Filtered Results',id='filtered-results-page-link',href='#nba-filtered-results',className='sport-page-link'),
                        ],className='market-provider-pages'),
                    ],id='market-data-navigation',open=True,className='market-data-navigation'),
                    html.A([html.Span('↺',className='navigation-icon',**{'aria-hidden':'true'}),'Backtesting'],
                           id='backtesting-page-link',href='#backtesting',className='sport-page-link'),
                    html.Div(id='backtesting-navigation',className='backtesting-navigation',style={'display':'none'}),
                    html.A('Visualized In-Game Data', id='in-game-page-link', href='#nba-in-game-data', className='sport-page-link'),
                    information_navigation(),
                ], className="page-toolbar", **{"aria-label": "Sport pages"}),
                html.Div([
                    html.Div([html.Span(indicator('Loading the selected market…'),className='market-transition-loading'),
                              html.Span('This game is unavailable in the loaded catalog. Return to the market’s game list and refresh.',
                                        className='market-transition-unavailable')],
                             id='market-transition-status',role='status',className='market-transition-status'),
                    html.Div([
                        html.Section([
                            html.Div([html.Div("BUILD YOUR DATASET", className="eyebrow"),
                                      html.H2("Historical scope")], className="dataset-builder-heading"),
                            html.Div([
                                field("Season starting year", dcc.Dropdown(id="season", options=list(range(date.today().year, 1998, -1)),
                                                                           value=date.today().year - 1, clearable=False),
                                      "NBA: 2024 means 2024–25. Football: 2024 means the 2024 season."),
                                field("Season phase", dcc.Dropdown(id="season-type", options=[{"label": "Regular season", "value": "regular"},
                                                                                              {"label": "Postseason", "value": "postseason"}],
                                                                   value="regular", clearable=False)),
                                html.Div([
                                    field("College team", dcc.Input(id="college-team", type="text", value="Michigan", placeholder="e.g. Michigan", debounce=True),
                                          "NCAAF requires a school name or a week. Use the source's school name."),
                                ], id="college-fields", className="dataset-scope-field", style={"display": "none"}),
                                html.Div([field("Week (optional)", dcc.Input(id="week", type="number", min=0, max=25, step=1,
                                                                             placeholder="All weeks", debounce=True))],
                                         id="week-fields", className="dataset-scope-field", style={"display": "none"}),
                                html.Div([
                                    html.Button("Load historical data →", id="load", n_clicks=0, className="primary-button"),
                                    html.Button("Refresh from source", id="refresh", n_clicks=0, className="secondary-button"),
                                ], className="dataset-actions"),
                            ], className="dataset-controls"),
                        ], className="dataset-builder panel"),
                        html.Section([
                            html.Div(id="load-status", role="status", **{"aria-live": "polite"}, className="load-status"),
                            html.Div(id="historical-link-status", role="status", **{"aria-live": "polite"}, className="chart-note"),
                            dcc.Loading(html.Div(id="ncaaf-history-status",role="status",className="chart-note"),color="#57dbc4"),
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
                                          action_loading(dcc.Graph(id="trend", figure=empty_figure(), config={"displaylogo": False}),
                                                         'Loading historical performance…'),
                                          html.P("Baseline uses only prior appearances. This reference forecast does not account for opponent, injuries, minutes, or roster changes.",
                                                 className="chart-note")], id='historical-performance-panel',className="panel chart-panel"),
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
                            html.P("Schedule data: ESPN · Times in EDT (UTC−4). Pull Live Markets saves a FanDuel snapshot: NFL/NCAAF up to 17 credits; NBA up to 13. College prop availability varies by event and jurisdiction.",
                                   className="market-source-note"),
                            dcc.Loading(html.Div(id="market-status", role="status", **{"aria-live": "polite"}, className="load-status"),
                                        type="circle",color="#57dbc4",delay_show=500,overlay_style={'visibility':'visible'}),
                            html.Div(id="market-games"),
                        ], id="market-list"),
                        html.Div([
                            html.Div([
                            html.Div([
                                html.A("← Back to upcoming games", id="market-back-link", href="#nfl-games", className="back-to-games"),
                                html.Div([
                                    html.Button("Pull Live Markets", id="market-detail-pull", n_clicks=0, className="pull-markets-button"),
                                    html.Small("FanDuel only · Up to 17 credits per new pull", id="market-detail-credit"),
                                ], className="detail-pull-control"),
                            ], className="detail-page-actions"),
                            html.Div(id="market-detail-pull-status", role="status", **{"aria-live": "polite"}),
                            html.Div([html.Div(id="market-game-header"),html.Div(id='market-game-conditions')],className='market-game-heading'),
                            html.Div([
                                field("Saved snapshot", dcc.Dropdown(id="market-snapshot", options=[], clearable=False,
                                                                     placeholder="No saved snapshots")),
                                html.Div(id="market-snapshot-status", role="status", className="snapshot-description"),
                            ], className="snapshot-controls"),
                            dcc.Tabs(id="market-category", value="Game", children=[], className="market-category-tabs"),
                            game_information_layout(),
                            html.Section([
                            html.H2('Mispricing Filter'),
                            html.H3('Apply Criteria'),
                            html.P('Choose the methods and thresholds used to highlight selections in the market tabs.',className='ppp-comparison-note'),
                            html.Div([
                              html.Div([
                                field("Game Outcome Forecast Method", dcc.Dropdown(id="market-forecast-method", value="PPP",
                                           options=[{"label": "PPP", "value": "PPP"}, {"label": "EPA", "value": "EPA"},
                                                    {"label": "ELO / Power Rating", "value": "ELO"}],
                                           clearable=False)),
                                html.P("Latest historical game used: Not calculated yet", id="game-forecast-history",
                                       className="forecast-history", role="status"),
                              ], className="forecast-method-control"),
                              html.Div([
                                field("Player Forecast Method", dcc.Dropdown(id="player-forecast-method", value="WHP",
                                      options=[{"label": "Weighted Historical Performance (WHP)", "value": "WHP"},
                                               {"label": "Usage & Opportunity (UO)", "value": "UO"},
                                               {"label": "Opponent-Adjusted Matchup (OAM)", "value": "OAM"},
                                               {"label": "Pace & Game Environment (PGE)", "value": "PGE"}], clearable=False)),
                                html.P("Latest historical performance used: Not calculated yet", id="player-forecast-history",
                                       className="forecast-history", role="status"),
                              ], className="forecast-method-control"),
                            ], className="forecast-methods"),
                            html.Div([
                                field("Estimated Probability (%)", dcc.Input(id="market-probability-threshold", type="number",
                                      value=75, min=0, max=100, step="any")),
                                field("Edge (%)", dcc.Input(id="market-edge-threshold", type="number",
                                      value=5, min=-100, max=100, step="any")),
                                html.Button("Apply Criteria", id="market-apply-criteria", n_clicks=0, className="apply-criteria-button"),
                            ], className="criteria-controls"),
                            html.Div('Rows must exceed both thresholds. Edge is measured in percentage points.',
                                     id="market-criteria-status", role="status", **{"aria-live": "polite"}, className="criteria-status"),
                            quick_controls('market', 'mispricing-results'),
                            ],id='mispricing-filter-panel',className='panel market-criteria',style={'display':'none'}),
                            html.Div(indicator('Loading the selected markets…'),id='market-sections-transition',className='market-sections-transition'),
                            dcc.Loading(html.Div(id="saved-market-sections"), color="#57dbc4"),
                            ], className="sportsbook-view"),
                        ], id="market-game-detail", className="market-detail-view", style={"display": "none"}),
                    ], id="market-page", className="market-page", style={"display": "none"}),
                    html.Section([html.Div(id='backtesting-content'),backtest_summary_layout(),performance_layout(),
                        html.Section([
                            dcc.Store(id='research-editor-context'),html.H3('Research experiment'),
                            html.Div([
                                field('Sport',dcc.Dropdown(id='research-sport',options=['NBA','NFL','NCAAF'],value='NBA',clearable=False)),
                                html.Div(field('Evaluation team',dcc.Dropdown(id='research-team',options=[dict(label='All teams',value='__all__')],value='__all__',clearable=False)),id='research-team-field'),
                            ],className='research-scope-controls'),
                            html.Div([
                            html.Div([html.H3('Model parameters'),html.Button('Reset sport defaults',id='research-reset',n_clicks=0,className='export-button')],className='research-parameter-heading'),
                            html.P('Every configuration field is shown below. Values are validated together before a run starts. Model sport follows the sport selector. Saved results retain the exact effective parameters.',className='backtest-status'),
                            html.Div(id='research-parameters',className='research-parameters'),
                            html.P(id='research-parameter-note',className='backtest-status'),
                            ],id='research-model-settings'),
                        ],id='research-setup',className='panel research-setup',style={'display':'none'}),
                        comparison_layout(),
                        backtesting_results_layout()],
                                 id='backtesting-page',className='backtesting-page',style={'display':'none'}),
                    in_game_layout(),
                    kalshi_layout(),
                    polymarket_view.layout(),
                    market_summary_layout(),
                    filtered_results_layout(),
                    information_layout(),
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
            pnl_layout(),
            html.Footer("NBA / NFL / NCAAF · Displayed times in EDT (UTC−4)")
        ]),
        slip_layout(),
        backtesting_monitor_layout(),
        structure_layout(),
    ], id='app-shell',className="app-shell")
    app.layout=configure_loading(app.layout)
    register_loading(app)
    register_player_distribution(app)

    app.clientside_callback(
        'function(hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory) {'
        ' return window.dash_clientside.market_transition.reconcile(hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory); }',
        Output('app-shell','className'),Input('market-url','hash'),Input('sport','value'),
        Input('market-detail-context','data'),Input('kalshi-context','data'),Input('polymarket-context','data'),
        Input('market-schedule-scope','data'),Input('kalshi-games','data'),Input('polymarket-games','data'),
        Input('workspace-page','value'),Input('market-forecast-history','data'),Input('market-snapshot','value'),Input('market-category','value'),
        Input('kalshi-rules-signature','data'),Input('kalshi-category','value'),Input('polymarket-rules-signature','data'),Input('polymarket-category','value'))

    app.clientside_callback('''function(page,history,hash,snapshot,category){
        return page!=='market'||category==='Mispricing'||category==='Additional Information'||!history||!history.pending||
            JSON.stringify(history.context)!==JSON.stringify([hash,snapshot]);
    }''',Output('market-model-poll','disabled'),Input('workspace-page','value'),Input('market-forecast-history','data'),
        Input('market-url','hash'),Input('market-snapshot','value'),Input('market-category','value'))

    @app.callback(Output("historical-page", "style"), Output("market-page", "style"),
                  Output("market-title", "children"), Output("fanduel-link", "href"), Output("market-timer", "disabled"),
                  Output("page-eyebrow", "children"), Output("page-title", "children"),
                  Output("page-description", "children"),
                  Output("sport-pages", "style"), Output("odds-api-page", "style"), Output("odds-timer", "disabled"),
                  Output('backtesting-page','style'),
                  Input("workspace-page", "value"), Input("sport", "value"),Input('entity-type','value'))
    def navigate(page, sport,kind='team'):
        odds = sport == "ODDS_API"
        personal=sport=='PNL'
        research=sport=='RESEARCH'
        market = page == "market" and not odds and not research and not personal
        backtesting = research or (page == 'backtesting' and not odds and not personal)
        in_game = page == 'in-game' and sport in {'NBA','NFL'}
        kalshi = page == 'kalshi' and sport in SOURCES
        polymarket = page == 'polymarket' and sport in SOURCES
        summary = page == 'market-summary' and sport in SOURCES
        filtered = page == 'filtered-results' and sport in SOURCES
        information=page=='information' and sport in {'NBA','NFL'}
        exchange = 'Polymarket' if polymarket else 'Kalshi' if kalshi else None
        intro = ('P&L HISTORY','Your betting history and recorded balances.',
                 'Track actual stakes, settlements and cash transfers across your accounts.') if personal else ('STRATEGY & RESEARCH','Study the method. Test your parameters.',
                 'Explore forecasting strategies and run reproducible historical experiments across NBA, NFL and NCAAF.') if research else ("THE ODDS API", "Your monthly credits at a glance.",
                 "Monitor your account's credit usage and connection status.") if odds else (
                'THE BACKTESTING LAB','Explore your forecasting models.',
                f'Game Outcome and Player Stats evaluation methods for {sport}.') if backtesting else (
                'INFORMATION VARIABLE' if information else 'MARKET DATA' if summary or filtered else 'THE '+exchange.upper()+' EXCHANGE' if exchange else "ON THE COURT / ON THE FIELD" if in_game else "THE UPCOMING SLATE" if market else "THE HISTORICAL LAB",
                f'{sport} · Filtered Results' if filtered else f'{sport} · Information Variable' if information else f'{sport} · Market Summary' if summary else f'{sport} · {exchange} Market' if exchange else f'{sport} · Visualized In-Game Data' if in_game else "The next game starts here." if market else f'{sport} · {"Player" if kind=="player" else "Team"} Statistics',
                'Browse saved Quick Filtering matches across upcoming games.' if filtered else 'Read official league reports, attributed news and NFL weather information.' if information else 'Compare market odds, implied probabilities and model forecasts across all three providers.' if summary else f'Browse game contracts and current exchange quotes from {exchange}’s public API.' if exchange else 'See recorded shots and offensive plays on an interactive court or field.' if in_game else "Explore upcoming NBA, NFL, and college football matchups." if market else
                ('Explore saved college player performances and game logs.' if sport=='NCAAF' else
                 'Explore historical player performances and career statistics.') if kind=='player' else
                'Explore historical team performances and statistics.')
        return ({"display": "none" if market or odds or backtesting or in_game or exchange or summary or filtered or information or personal else "grid"}, {"display": "block" if market else "none"},
                f"{sport} · Upcoming games", FANDUEL_LINKS.get(sport, FANDUEL_LINKS["NBA"]), not market,
                *intro, {"display": "none" if odds or personal else "grid"}, {"display": "block" if odds else "none"}, not odds,
                {'display':'block' if backtesting else 'none'})

    @app.callback(Output('historical-navigation','children'),Output('historical-navigation','style'),
                  Output('historical-page-link','className'),Output('market-page-link','className'),
                  Output('backtesting-page-link','className'),Output('market-page-link','href'),
                  Input('workspace-page','value'),Input('sport','value'),Input('entity-type','value'),Input('market-return','data'))
    def sport_page_navigation(page,sport,kind,market_return=None):
        visible=page=='historical' and sport in SOURCES
        active=lambda value:'sport-page-link'+(' active' if page==value else '')
        links=historical_navigation(sport,kind)
        if sport=='NCAAF' and kind=='player':
            links=[html.A('Team Statistics',href='#historical-team',className='backtesting-model-link',**{'aria-current':'false'})]
            links.append(html.Span('Player Statistics (saved)',className='backtesting-model-link active',**{'aria-current':'page'}))
        remembered=remembered_market(market_return,sport)
        return (links,{'display':'grid' if visible else 'none'},
            active('historical'),active('market'),active('backtesting'),remembered['route'] if remembered else f'#{sport.lower()}-games')

    app.clientside_callback(
        'function(hash, snapshot, category, saved) { return window.dash_clientside.market_navigation.remember(hash, snapshot, category, saved); }',
        Output('market-return','data'),Input('market-url','hash'),Input('market-snapshot','value'),
        Input('market-category','value'),State('market-return','data'))

    @app.callback(Output('backtesting-navigation','children'),Output('backtesting-navigation','style'),
                  Output('backtesting-content','children'),Output('backtest-evaluation-panel','style'),
                  Input('workspace-page','value'),Input('sport','value'),
                  Input('market-url','hash'),Input('research-sport','value'))
    def show_backtesting(page,sport,location_hash,research_sport='NBA'):
        if sport=='RESEARCH':
            model=find_model(research_sport,research_view.route(location_hash))
            return (research_view.navigation(research_sport,location_hash),{'display':'block','margin':0,'padding':0,'border':0},
                research_view.page(research_sport,location_hash),{'display':'block' if model else 'none'})
        if page!='backtesting' or sport not in SOURCES:
            return no_update,{'display':'none'},no_update,{'display':'none'}
        model=find_model(sport,backtesting_route(location_hash))
        return (backtesting_navigation(sport,location_hash),{'display':'block'},backtesting_page(sport,location_hash),
                {'display':'block' if model else 'none'})

    @app.callback(Output('historical-page-link','style'),Output('market-page-link','style'),
                  Output('backtesting-page-link','style'),Output('research-setup','style'),Output('research-team-field','style'),
                  Input('sport','value'),Input('market-url','hash'))
    def research_visibility(sport,location_hash):
        research=sport=='RESEARCH';hidden={'display':'none'}
        route=research_view.route(location_hash)
        return (*(hidden if research else {} for _ in range(3)),{'display':'block' if research else 'none'},
                {'display':'block' if research and route and route[0]=='player-stats' and route[1]!='comparison' else 'none'})

    @app.callback(Output('research-parameters','children'),Output('research-parameter-note','children'),
                  Output('research-editor-context','data'),Input('backtest-context','data'),Input('research-reset','n_clicks'),
                  State('research-editor-context','data'))
    def research_parameters(context,_reset,previous_key=None):
        if not context or context.get('workspace')!='research':return [],'Choose a strategy from the left to edit its parameters.',None
        key=[context[k] for k in ['sport','group','model']]
        if key==previous_key and ctx.triggered_id!='research-reset':return no_update,no_update,no_update
        controls=research_view.editor(context['sport'],context['group'],context['model'])
        return controls,f'{len(controls)} configuration fields · sport-specific defaults. Evaluation controls below restrict the test sample; model parameters govern estimation and simulation.',key

    @app.callback(Output('research-team','options'),Output('research-team','value'),
                  Input('sport','value'),Input('research-sport','value'),Input('market-url','hash'),State('research-team','value'))
    def research_teams(tab,sport,location_hash,previous):
        options=[dict(label='All teams',value='__all__')]
        route=research_view.route(location_hash)
        if tab=='RESEARCH' and route and route[0]=='player-stats':
            try:
                frame=backtesting_service.player_inputs(sport)
                options.extend(dict(label=team,value=team) for team in sorted(frame.team.dropna().astype(str).unique()))
            except (DataSourceError,OSError,ValueError):pass
        return options,previous if previous in {o['value'] for o in options} else '__all__'

    @app.callback(Output('backtest-context','data'),Output('backtest-entity','options'),Output('backtest-entity','value'),
                  Output('backtest-entity-label','children'),Output('backtest-statistic-field','style'),
                  Output('backtest-line-field','style'),Output('backtest-target-field','style'),
                  Output('backtest-input-status','children'),Input('sport','value'),Input('market-url','hash'),
                  Input('workspace-page','value'),State('backtest-entity','value'),
                  Input('research-sport','value'),Input('research-team','value'))
    def configure_backtest(sport,location_hash,page,previous,research_sport='NBA',research_team='__all__'):
        research=sport=='RESEARCH'
        model=find_model(research_sport if research else sport,research_view.route(location_hash) if research else backtesting_route(location_hash))
        hidden={'display':'none'}
        if (not research and page!='backtesting') or model is None:
            return None,[],None,'Team',hidden,hidden,hidden,''
        if research:sport=research_sport
        context=dict(sport=sport,group=model.group,model=model.key)
        player=model.group=='player-stats'
        if research:context.update(workspace='research',team_filter=research_team if player and research_team!='__all__' else None)
        try:
            options,_,_=backtesting_service.options(sport,model.group,model.key,previous)
            if research and player:options.insert(0,dict(label='All players',value='__all__'))
            chosen=previous if previous in {o['value'] for o in options} else options[0]['value'] if options else None
            note='Saved inputs only. Missing history is reported; source and odds APIs are not called.'
        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            options=[];chosen=None;note=str(exc)
        return (context,options,chosen,'Player cohort' if research and player else 'Player' if player else 'Team',{'display':'block' if player else 'none'},
                {'display':'block' if player else 'none'},{'display':'none' if player else 'block'},note)

    @app.callback(Output('backtest-statistic','options'),Output('backtest-statistic','value'),
                  Input('backtest-context','data'),Input('backtest-entity','value'),State('backtest-statistic','value'))
    def backtest_statistics(context,entity,previous):
        if not context or context['group']!='player-stats' or entity is None:return [],None
        try:
            _,options,_=backtesting_service.options(context['sport'],context['group'],context['model'],entity)
        except (DataSourceError,OSError,ValueError,TypeError):return [],None
        return options,previous if previous in {o['value'] for o in options} else options[0]['value'] if options else None

    @app.callback(Output('backtest-limit','value'),Input('backtest-context','data'),Input('backtest-entity','value'))
    def backtest_sample_default(context,entity):
        if not context or context['sport'] not in {'NBA','NFL','NCAAF'}:return no_update
        return default_evaluation_games(context['sport'],entity)

    @app.callback(Output('backtest-lookback','value'),Output('backtest-lookback-note','children'),
                  Input('backtest-context','data'))
    def backtest_lookback_default(context):
        if not context:return 'All available (Model Default)',''
        if context.get('workspace')=='research':return 'Model Default','Use the configured model windows above. Enter a whole number to additionally cap eligible input histories.'
        return default_lookback(context['sport'],context['group'],context['model'])

    @app.callback(Output('backtest-available','children'),Input('backtest-context','data'),Input('backtest-entity','value'),
                  Input('backtest-statistic','value'),Input('backtest-start','date'),Input('backtest-end','date'),
                  Input('backtest-playoffs','value'))
    def backtest_available(context,entity,market,start,end,playoffs):
        if not context or entity is None:return 'Select a team or player to count saved games.'
        try:
            extra=dict(team_filter=context.get('team_filter')) if context.get('workspace')=='research' else {}
            count=backtesting_service.available_games(context['sport'],context['group'],context['model'],entity,
                market,start,end,playoffs=='include',**extra)
            note=(' '+nfl_backtest_criteria(market)
                  if context['sport']=='NFL' and context['group']=='player-stats' else '')
            return f'{count:,} saved games available with these filters. Insufficient training history may cause skips.'+note
        except (DataSourceError,OSError,ValueError,TypeError) as exc:return str(exc)

    @app.callback(Output('backtest-monitor-jobs','data'),Input('backtest-monitor-timer','n_intervals'),
                  Input('backtest-job','data'),State('backtest-monitor-jobs','data'))
    def poll_backtest_monitor(_tick,_job,previous):
        jobs=backtesting_service.active_jobs()
        if jobs==previous:raise PreventUpdate
        return jobs

    @app.callback(Output('backtest-monitor-collapsed','data'),Input('backtest-monitor-toggle','n_clicks'),
                  State('backtest-monitor-collapsed','data'),prevent_initial_call=True)
    def toggle_backtest_monitor(clicks,collapsed):
        if not clicks:raise PreventUpdate
        return not bool(collapsed)

    @app.callback(Output('backtest-monitor-body','children'),Output('backtest-monitor-count','children'),
                  Output('backtest-monitor','className'),Output('backtest-monitor-toggle','aria-expanded'),
                  Output('backtest-monitor-toggle','aria-label'),
                  Input('backtest-monitor-jobs','data'),Input('backtest-monitor-collapsed','data'))
    def render_backtest_monitor(jobs,collapsed):
        jobs=jobs or []
        return (backtesting_monitor_contents(jobs),str(len(jobs)),
            'backtest-monitor'+(' is-collapsed' if collapsed else ''),'false' if collapsed else 'true',
            ('Expand' if collapsed else 'Minimize')+' backtesting processes')

    @app.callback(Output('backtest-history','options'),Output('backtest-history','value'),
                  Output('backtest-history-status','children'),Output('backtest-history-location','children'),
                  Input('backtest-context','data'),Input('backtest-history-panel','open'),
                  Input('backtest-result','modified_timestamp'),State('backtest-history','value'))
    def previous_simulations(context,opened,_updated,selected):
        if not context:return [],None,'Choose a backtesting method to see its saved simulations.',''
        location=str(backtesting_service.root/context['sport'].lower()/context['group']/context['model'])
        if not opened:return [],None,'Expand this section to browse saved simulations.',location
        reports=backtesting_service.history(context['sport'],context['group'],context['model'])
        options=[]
        for report in reports:
            market=(report.get('market') or '').removeprefix('player_').replace('_',' ').title()
            label=(f'{format_eastern(report["created_at"])} · {report["entity_label"]}'
                   +(' · '+market if market else '')
                   +(' · Research' if report.get('workspace')=='research' else '')
                   +f' · {report["evaluated"]:,} evaluated / {report["limit"]:,} requested'
                   +f' · Lookback {report["lookback"] or "default"} · {report["run_id"][:8]}')
            options.append(dict(label=label,value=report['run_id']))
        value=selected if selected in {o['value'] for o in options} else None
        message=(f'{len(options):,} saved simulation'+('s' if len(options)!=1 else '')+'. Choose one and select Open simulation.'
                 if options else 'No completed simulations saved for this sport and method yet.')
        return options,value,message,location

    @app.callback(Output('backtest-history-open','disabled'),Input('backtest-history','value'))
    def enable_previous_simulation(run_id):
        return not bool(run_id)

    @app.callback(Output('backtest-result','data'),Output('backtest-run-status','children'),
                  Output('backtest-job','data'),Output('backtest-poll','disabled'),Output('backtest-progress','value'),
                  Output('backtest-progress-label','children'),Output('backtest-run','disabled'),Output('backtest-run','children'),
                  Output('backtest-pages','data'),
                  Input('backtest-run','n_clicks'),Input('backtest-context','data'),Input('backtest-poll','n_intervals'),
                  State('backtest-entity','value'),State('backtest-statistic','value'),
                  State('backtest-start','date'),State('backtest-end','date'),State('backtest-limit','value'),
                  State('backtest-tolerance','value'),State('backtest-line','value'),
                  State('backtest-playoffs','value'),State('backtest-lookback','value'),State('backtest-job','data'),
                  State('backtest-entity','options'),State('backtest-pages','data'),
                  Input('backtest-history-open','n_clicks'),State('backtest-history','value'),
                  State({'type':'research-parameter','path':ALL},'value'),State({'type':'research-parameter','path':ALL},'id'))
    def run_backtest(clicks,context,tick,entity,market,start,end,limit,tolerance,line,playoffs,lookback,job_id,entity_options=None,pages=None,history_clicks=None,history_run=None,parameter_values=None,parameter_ids=None):
        if not context:return None,'',None,True,0,'Ready',False,'Run backtest',no_update
        previous=pages or {};pages=dict(previous)
        page_key='/'.join(context[k] for k in ['sport','group','model'])
        if context.get('workspace')=='research':page_key='research/'+page_key
        entry=dict(pages.get(page_key) or {})
        restoring=ctx.triggered_id not in {'backtest-run','backtest-poll'}
        def remember(*values):
            pages[page_key]=entry
            return (*values,pages if pages!=previous else no_update)
        def saved_result():
            result=backtesting_service.saved(context['sport'],context['group'],context['model'],entry.get('run_id'))
            if result is None:result=backtesting_service.latest(context['sport'],context['group'],context['model'])
            if result and result.get('run_id'):entry['run_id']=result['run_id']
            return result
        if ctx.triggered_id=='backtest-history-open':
            if not history_clicks:raise PreventUpdate
            result=backtesting_service.saved(context['sport'],context['group'],context['model'],history_run)
            if result is None:
                return remember(no_update,'That saved simulation is unavailable. Choose another previous simulation.',
                    no_update,no_update,no_update,no_update,no_update,no_update)
            entry['run_id']=result['run_id']
            entry.pop('job_id',None)
            try:
                directory=backtesting_service.ensure_exports(result)
                message=f'Opened saved simulation from {format_eastern(result["created_at"])}. CSV and JSON files: {directory}'
            except OSError as exc:
                message=f'Opened saved simulation. CSV exports could not be saved: {exc}'
            return remember(result,message,None,True,100,'Saved simulation',False,'Run backtest')
        if ctx.triggered_id=='backtest-run':
            if not clicks:raise PreventUpdate
            if entity is None:return remember(no_update,'Choose a team or player with saved history first.',job_id,not bool(job_id),0,'Ready',False,'Run backtest')
            label=next((o['label'] for o in entity_options or [] if o['value']==entity),None)
            extra={}
            if context.get('workspace')=='research':
                try:parameters=form_parameters(context['sport'],context['group'],context['model'],parameter_ids or [],parameter_values or [])
                except (ValueError,TypeError) as exc:
                    return remember(no_update,'Check model parameters: '+str(exc),no_update,no_update,no_update,no_update,False,'Run backtest')
                extra=dict(config_overrides=parameters,team_filter=context.get('team_filter'),workspace='research')
                if context.get('team_filter'):label=(label or 'All players')+' · '+context['team_filter']
            job_id=backtesting_service.start(sport=context['sport'],group=context['group'],key=context['model'],
                entity=entity,market=market,start=start,end=end,limit=limit,tolerance=tolerance,
                line=line if context['group']=='player-stats' else None,include_playoffs=playoffs=='include',lookback=lookback,
                display_label=label,**extra)
            entry['job_id']=job_id
            return remember(no_update,'Running backtest. You can change the controls and start another run.',job_id,False,0,'0% · Preparing saved inputs',False,'Run another backtest')
        # A page's remembered job takes precedence over the shared visible job
        # slot, which can still belong to the page we just navigated away from.
        job_id=entry.get('job_id') or (None if restoring else job_id)
        if not job_id and not restoring:raise PreventUpdate
        job=backtesting_service.progress(job_id) if job_id else None
        if job is not None and any(job['parameters'].get(k)!=context[c] for k,c in [('sport','sport'),('group','group'),('key','model')]):
            raise PreventUpdate
        if job is None:
            saved=saved_result()
            entry.pop('job_id',None)
            if not restoring and job_id:
                return remember(saved,'This run was interrupted. Previous saved results are shown when available.',None,True,0,'Interrupted',False,'Run backtest')
            message='Restored saved results. Use Run backtest to apply the current controls.' if saved else 'Choose a scope and run a chronological backtest.'
            return remember(saved,message,None,True,100 if saved else 0,'Saved result' if saved else 'Ready',False,'Run backtest')
        entry['job_id']=job_id
        if job['state']=='completed':
            result=job['result']
            if result.get('run_id'):entry['run_id']=result['run_id']
            message=f'Completed {len(result["records"])} evaluated games; {len(result["skipped"])} skipped. '
            message+='Reused an identical saved run.' if job['reused'] else 'Results and CSV reports saved locally.'
            return remember(result,message,job_id,True,100,'100% · Complete'+(' (saved run)' if job['reused'] else ''),False,'Run backtest')
        prior=saved_result() if restoring else no_update
        if job['state']=='failed':return remember(prior,job['message'],job_id,True,job['percent'],'Failed',False,'Run backtest')
        return remember(prior,job['message'],job_id,False,job['percent'],f'{job["percent"]}% · {job["message"]}',False,'Run another backtest')

    @app.callback(Output('backtest-results','children'),Output('backtest-export','disabled'),
                  Input('backtest-result','data'),Input('backtest-context','data'),Input('backtest-target','value'))
    def show_backtest_results(result,context,target):
        if (not result or not context or any(result.get('context',{}).get(k)!=context[k] for k in ['sport','group','model'])):
            return empty_backtesting_results(),True
        return render_backtesting_results(result,target if target in {'margin','total'} else 'margin'),False

    @app.callback(Output('backtest-download','data'),Input('backtest-export','n_clicks'),
                  State('backtest-result','data'),State('backtest-context','data'),State('backtest-target','value'),prevent_initial_call=True)
    def export_backtest(clicks,result,context,target):
        if not clicks or not result or not context or any(result['context'].get(k)!=context[k] for k in ['sport','group','model']):
            raise PreventUpdate
        return dcc.send_data_frame(pd.DataFrame(evaluated_records(result,target if target in {'margin','total'} else 'margin')).to_csv,
            export_filename(result,target if target in {'margin','total'} else 'margin'),index=False)

    @app.callback(Output("sport", "value"), Output("workspace-page", "value"), Input("market-url", "hash"),
                  State('sport','value'),State('workspace-page','value'))
    def restore_game_route(location_hash,sport=None,page=None):
        def destination(next_sport,next_page):
            # Re-emitting an unchanged sport wakes every sport-dependent page.
            return (no_update if next_sport==sport else next_sport,
                    no_update if next_page==page else next_page)
        if location_hash=='#pnl-history':return destination('PNL','pnl')
        if market_summary.route(location_hash):return destination(market_summary.route(location_hash),'market-summary')
        if filtered_results_route(location_hash):return destination(filtered_results_route(location_hash),'filtered-results')
        if information_route(location_hash):return destination(information_route(location_hash)['sport'],'information')
        if kalshi_route(location_hash):return destination(kalshi_route(location_hash)[0],'kalshi')
        if polymarket_view.route(location_hash):return destination(polymarket_view.route(location_hash)[0],'polymarket')
        in_game = re.fullmatch(r'#(nba|nfl)-in-game-data', location_hash or '')
        if in_game:return destination(in_game[1].upper(),'in-game')
        if location_hash=='#research' or research_view.route(location_hash):return destination('RESEARCH','backtesting')
        if historical_route(location_hash):
            target=history_target(location_hash)
            return destination(target['sport'] if target else no_update,'historical')
        summary=backtest_summary_route(location_hash)
        if summary:return destination(summary['sport'] or no_update,'backtesting')
        if location_hash in {'#backtesting',PERFORMANCE_ROUTE} or backtesting_route(location_hash):
            return destination(no_update,'backtesting')
        schedule=re.fullmatch(r'#(nba|nfl|ncaaf)-games',location_hash or '')
        if schedule:return destination(schedule[1].upper(),'market')
        if not market_game_id(location_hash):
            raise PreventUpdate
        return destination(market_game_sport(location_hash), "market")

    @app.callback(Output("market-back-link", "href"), Output("market-detail-credit", "children"),
                  Input("market-url", "hash"))
    def market_detail_labels(location_hash):
        sport = market_game_sport(location_hash) or "NFL"
        credits = len(NBA_MARKETS) if sport == "NBA" else len(NCAAF_MARKETS) if sport == "NCAAF" else 17
        return f"#{sport.lower()}-games", f"FanDuel only · Up to {credits} credits per new pull"

    @app.callback(Output("market-forecast-method", "options"), Output("market-forecast-method", "value"),
                  Input("market-url", "hash"), State("market-forecast-method", "value"))
    def forecast_methods(location_hash, previous):
        options = [{"label": "PPP", "value": "PPP"},
                   {"label": "EPA", "value": "EPA"},
                   {"label": "ELO / Power Rating", "value": "ELO"}]
        return options, previous if previous in {"PPP", "EPA", "ELO"} else "PPP"

    @app.callback(Output("market-list", "style"), Output("market-game-detail", "style"),
                  Output("market-game-header", "children"), Output("market-snapshot", "options"), Output("market-snapshot", "value"),
                  Output("market-detail-context", "data"),
                  Input("market-url", "hash"), Input("sport", "value"), Input("workspace-page", "value"),
                  Input("market-schedule", "data"), Input("market-refresh-signal", "data"),
                  State("market-snapshot", "value"), State("market-detail-context", "data"),State('market-return','data'))
    def open_game(location_hash, sport, page, games, refreshed, previous, previous_context,market_return=None):
        game_id = market_game_id(location_hash)
        if page!='market' or sport not in market_services:raise PreventUpdate
        if not game_id or market_game_sport(location_hash)!=sport:
            return {"display":"block"},{"display":"none"},no_update,no_update,no_update,no_update
        if (previous_context and previous_context.get('sport')==sport and str(previous_context.get('id'))==game_id and previous
                and set(ctx.triggered_prop_ids)<={'market-url.hash','workspace-page.value'}):
            return (no_update,)*6
        snapshots = market_services[sport].history(game_id)
        game = next((game for game in (games or []) if str(game["id"]) == game_id), None)
        if not game and snapshots:
            saved = snapshots[0]
            game = saved.get("game") or {"id": game_id, "start": saved["commence_time"],
                "home": {"name": saved["home_team"], "abbreviation": saved.get("home_abbreviation", "HOME")},
                "away": {"name": saved["away_team"], "abbreviation": saved.get("away_abbreviation", "AWAY")}, "time_tbd": False}
        context = {**game, "sport": sport, "token": previous_context["token"] if previous_context and previous_context.get("sport", "NFL") == sport and str(previous_context.get("id")) == game_id
                   else uuid4().hex} if game else None
        options = [{"label": format_eastern(item["pulled_at"]),
                    "value": item.get("csv_file", item["pulled_at"])} for item in snapshots]
        available={option['value'] for option in options}
        remembered=remembered_market(market_return,sport,location_hash)
        restored=remembered.get('snapshot') if remembered else None
        chosen = previous if previous in available else restored if restored in available else options[0]['value'] if options else None
        if ("market-refresh-signal.data" in ctx.triggered_prop_ids and refreshed and
                refreshed.get("sport", "NFL") == sport and str(refreshed.get("game_id")) == game_id and refreshed.get("snapshot") in {item["value"] for item in options}):
            chosen = refreshed["snapshot"]
        return {"display": "none"}, {"display": "block"}, game_header(game, snapshots[0] if snapshots else None, sport), options, chosen, context

    @app.callback(Output("market-refresh-signal", "data"),
                  Input("market-detail-pull", "n_clicks"), State("market-detail-context", "data"),
                  State("market-url", "hash"), State("sport", "value"), State("workspace-page", "value"),
                  prevent_initial_call=True, running=[(Output("market-detail-pull", "disabled"), True, False)])
    def pull_game_detail(clicks, game, location_hash, sport, page):
        game_id = market_game_id(location_hash)
        if not clicks or sport not in market_services or market_game_sport(location_hash) != sport or page != "market" or not game_id:
            raise PreventUpdate
        if not game or game.get("sport", "NFL") != sport or str(game.get("id")) != game_id:
            return {"sport": sport, "game_id": game_id, "error": "Game information is not loaded yet. Refresh the upcoming schedule before pulling."}
        try:
            snapshot = market_services[sport].pull(game, f"detail:{game_id}:{game['token']}:{clicks}")
        except DataSourceError as exc:
            return {"sport": sport, "game_id": game_id, "error": str(exc), "attempt": clicks}
        return {"sport": sport, "game_id": game_id, "snapshot": snapshot.get("csv_file", snapshot["pulled_at"]), "pulled_at": snapshot["pulled_at"]}

    @app.callback(Output("market-detail-pull-status", "children"), Input("market-refresh-signal", "data"), Input("market-url", "hash"))
    def detail_pull_status(result, location_hash):
        if not result or result.get("sport", "NFL") != market_game_sport(location_hash) or str(result.get("game_id")) != market_game_id(location_hash):
            return []
        if result.get("error"):
            return html.P(result["error"], className="market-pull-error")
        return html.P("Saved new snapshot · " + format_eastern(result["pulled_at"]), className="detail-pull-success")

    def saved_snapshot(location_hash, selection):
        game_id = market_game_id(location_hash)
        if not game_id or not selection:
            return None
        return next((item for item in market_services[market_game_sport(location_hash)].history(game_id)
                     if item.get("csv_file", item["pulled_at"]) == selection), None)

    @app.callback(Output('parlay-store','data'),Output('parlay-notice','children'),
                  Input({'type':'parlay-add','key':ALL},'n_clicks'),
                  Input({'type':'parlay-remove','key':ALL},'n_clicks'),
                  Input('parlay-clear','n_clicks'),Input('parlay-toggle','n_clicks'),
                  State({'type':'parlay-add','key':ALL},'id'),State({'type':'parlay-remove','key':ALL},'id'),
                  State('market-url','hash'),State('market-snapshot','value'),State('parlay-store','data'),
                  prevent_initial_call=True)
    def update_parlay(add_clicks,remove_clicks,clear_clicks,toggle_clicks,add_ids,remove_ids,
                      location_hash,selection,stored):
        triggered=ctx.triggered_id
        state=clean_state(stored)
        if triggered=='parlay-clear' and clear_clicks:
            return dict(version=1,legs=[],collapsed=False),'Selections cleared.'
        if triggered=='parlay-toggle' and toggle_clicks:
            state['collapsed']=not state['collapsed']
            return state,no_update
        if not isinstance(triggered,dict):raise PreventUpdate
        kind,key=triggered.get('type'),triggered.get('key')
        ids,counts=(add_ids,add_clicks) if kind=='parlay-add' else (remove_ids,remove_clicks)
        if not any(identity==triggered and count for identity,count in zip(ids or [],counts or [])):
            raise PreventUpdate
        if kind=='parlay-remove':
            state['legs']=[leg for leg in state['legs'] if leg['key']!=key]
            return state,'Selection removed.'
        if kind!='parlay-add':raise PreventUpdate
        snapshot=saved_snapshot(location_hash,selection)
        if not snapshot:return no_update,'That saved snapshot is no longer selected. Click the displayed quote again.'
        snapshot={**snapshot,'sport':market_game_sport(location_hash)}
        match=next(((index,row) for index,row in enumerate(snapshot['rows']) if quote_key(snapshot,row,index)==key),None)
        if match is None:return no_update,'The displayed snapshot changed. Click a quote in the current snapshot.'
        index,row=match
        # Exact same quote toggles without fetching/refitting its default model.
        previous=next((leg for leg in state['legs'] if leg['quote_key']==key),None)
        if previous:return toggle_leg(state,previous)
        estimate,error,latest=None,None,None
        try:
            if row['market_key'].startswith('player_'):
                if snapshot['sport']=='NCAAF':
                    ncaaf_model_data.prepare([snapshot['home_team'],snapshot['away_team']],snapshot['commence_time'])
                    ncaaf_model_data.prepare_players(snapshot,[row])
                forecast=whp_service.forecast({**snapshot,'rows':[row]})
                estimate=forecast.selection(row)
                if estimate is None:error=forecast.detail(row)
                fitted=forecast.forecasts.get((row['subject'],row['market_key'].removesuffix('_alternate')))
                latest=fitted.diagnostics.get('last_game_date') if fitted else None
            elif row['market_key'] in {'alternate_spreads','alternate_totals'} and snapshot['sport'] in {'NBA','NFL','NCAAF'}:
                forecast={'NBA':nba_ppp_service,'NFL':ppp_service,'NCAAF':ncaaf_forecast_services['PPP']}[snapshot['sport']].forecast(snapshot)
                estimate=_market_probability(row,forecast)
                latest=forecast.diagnostics.get('last_game_date')
            else:error='PPP is unavailable for this sport/market.'
        except (DataSourceError,OSError,ValueError) as exc:
            error=str(exc)
        if estimate is None and not error:error='Default-model estimate unavailable for this selection.'
        try:
            leg=build_leg(snapshot,row,index,selection_text(row),estimate,error,latest)
        except (TypeError,ValueError) as exc:return no_update,str(exc)
        return toggle_leg(state,leg)

    @app.callback(Output('parlay-body','children'),Output('parlay-drawer','className'),Output('parlay-drawer','style'),
                  Output('parlay-count','children'),Output('parlay-mini','children'),Output('parlay-toggle','aria-expanded'),
                  Input('parlay-store','data'),Input('workspace-page','value'),Input('sport','value'))
    def render_parlay(stored,page,sport):
        state=clean_state(stored)
        body,summary=slip_contents(state['legs'])
        count=summary['legs']
        return (body,'parlay-drawer'+(' is-collapsed' if state['collapsed'] else '')+(' is-empty' if not count else ''),
                {'display':'block' if count and page=='market' and sport!='ODDS_API' else 'none'},f'{count} '+('leg' if count==1 else 'legs'),
                ('Est. ' if summary.get('same_game') else '')+parlay_odds(summary.get('market_odds')),
                'false' if state['collapsed'] else 'true')

    @app.callback(Output({'type':'parlay-add','key':ALL},'className'),
                  Output({'type':'parlay-add','key':ALL},'aria-pressed'),
                  Input('parlay-store','data'),Input({'type':'parlay-add','key':ALL},'id'))
    def selected_parlay_prices(stored,identities):
        selected={leg['quote_key'] for leg in clean_state(stored)['legs']}
        flags=[identity['key'] in selected for identity in identities]
        return (['market-price market-price-button'+(' is-selected' if flag else '') for flag in flags],
                ['true' if flag else 'false' for flag in flags])

    @app.callback(Output("market-category", "children"), Output("market-category", "value"), Output("market-snapshot-status", "children"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), State("market-category", "value"),State('market-return','data'))
    def snapshot_categories(selection, location_hash, previous,market_return=None):
        if not market_game_id(location_hash):raise PreventUpdate
        sport=market_game_sport(location_hash)
        information_tab=(dcc.Tab(label=ADDITIONAL_INFORMATION,value=ADDITIONAL_INFORMATION,className='market-category-tab',
                                selected_className='market-category-tab selected') if sport in {'NBA','NFL'} else None)
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return ([information_tab],ADDITIONAL_INFORMATION,"No saved markets yet. Additional Information is available without pulling odds.") if information_tab is not None else (
                [], None, "No saved markets yet. Use Pull Live Markets above to download a snapshot.")
        available = {row["group"] for row in snapshot["rows"]}
        available.add(FILTER_CATEGORY)
        if information_tab is not None:available.add(ADDITIONAL_INFORMATION)
        categories = NBA_CATEGORIES if market_game_sport(location_hash) == "NBA" else CATEGORIES
        tabs = [dcc.Tab(label=label, value=key, className="market-category-tab", selected_className="market-category-tab selected")
                for key, label in categories if key in available]
        if information_tab is not None:tabs.append(information_tab)
        tabs.append(dcc.Tab(label='Mispricing Filter',value=FILTER_CATEGORY,className='market-category-tab',
                            selected_className='market-category-tab selected'))
        remembered=remembered_market(market_return,market_game_sport(location_hash),location_hash)
        restored=remembered.get('category') if remembered and remembered.get('snapshot')==selection else None
        value = restored if restored in available else previous if previous in available else tabs[0].value if tabs else None
        cost = f"{snapshot['cost']} credits" if snapshot["cost"] is not None else "Cost not reported"
        return tabs, value, [html.Span(latest_pull_text(snapshot) + f" · {cost}"),
                             html.Small("Saved in data/market_data", title=snapshot.get("csv_file", "Saved locally"))]

    @app.callback(Output("market-applied-criteria", "data"), Output("market-criteria-status", "children"),
                  Output("market-category", "value", allow_duplicate=True),
                  Input("market-apply-criteria", "n_clicks"), State("market-probability-threshold", "value"),
                  State("market-edge-threshold", "value"), State("market-category", "value"), prevent_initial_call=True)
    def apply_market_criteria(clicks, probability, edge, category):
        if not clicks:raise PreventUpdate
        try:criteria=criteria_values(probability,edge)
        except ValueError as exc:return no_update,str(exc),no_update
        return criteria, (
            f"Applied to all calculated selections: estimated probability > {probability:g}% and Edge > {edge:g} percentage points. Player tabs and Game Lines use their selected forecast methods."), category or FILTER_CATEGORY

    @app.callback(Output('mispricing-request','data'),Output('market-quick-status','children'),
                  Input('market-quick-filter','n_clicks'),State('market-quick-probability','value'),State('market-quick-edge','value'),
                  State('market-quick-game-methods','value'),State('market-quick-player-methods','value'),
                  State('market-url','hash'),State('market-snapshot','value'),prevent_initial_call=True)
    def request_quick_filter(clicks,probability,edge,methods,player_methods,location_hash,selection):
        if not clicks:raise PreventUpdate
        try:
            criteria=criteria_values(probability,edge)
            methods,player_methods=selected_methods(methods,player_methods)
            if not market_game_id(location_hash) or not selection:
                raise ValueError('Choose a saved game snapshot before using Quick Filtering.')
            return {**filter_context(location_hash,selection,methods,player_methods,criteria),'sequence':clicks}, 'All selected methods must pass both Quick Filtering thresholds.'
        except ValueError as exc:return no_update,str(exc)

    @app.callback(Output('mispricing-filter-panel','style'),Output('saved-market-sections','style'),Input('market-category','value'))
    def mispricing_visibility(category):
        selected=category==FILTER_CATEGORY
        return {'display':'block' if selected else 'none'},{'display':'none' if selected or category==ADDITIONAL_INFORMATION else 'block'}

    @app.callback(Output("player-forecast-method", "options"), Output("player-forecast-method", "value"),
                  Input("market-url", "hash"), State("player-forecast-method", "value"))
    def player_forecast_methods(location_hash, previous):
        if not market_game_sport(location_hash):return no_update,no_update
        supported = market_game_sport(location_hash) in {"NBA", "NFL", "NCAAF"}
        options = [{"label": "Weighted Historical Performance (WHP)", "value": "WHP"},
                   {"label": "Usage & Opportunity (UO)",
                    "value": "UO", "disabled": not supported},
                   {"label": "Opponent-Adjusted Matchup (OAM)",
                    "value": "OAM", "disabled": not supported},
                   {"label": "Pace & Game Environment (PGE)",
                    "value": "PGE", "disabled": not supported}]
        return options, previous if previous == "WHP" or previous in {"UO", "OAM", "PGE"} and supported else "WHP"

    @app.callback(Output("game-forecast-history", "children"), Output("player-forecast-history", "children"),
                  Input("market-forecast-history", "data"), Input("market-url", "hash"),
                  Input("market-snapshot", "value"), Input("market-forecast-method", "value"),
                  Input("player-forecast-method", "value"),Input('mispricing-forecast-history','data'))
    def forecast_history(history, location_hash, selection, game_method, player_method,quick_history=None):
        current = history if history and history.get("context") == [location_hash, selection] else {}
        quick=quick_history if quick_history and quick_history.get('context')==[location_hash,selection] else {}
        current={**current,'game':{**quick.get('game',{}),**current.get('game',{})},
                 'player':{**quick.get('player',{}),**current.get('player',{})}}
        def label(kind, method, prefix):
            if not selection:
                return prefix + "No saved snapshot selected"
            if current.get('pending') and ((current.get('view_key') or [None])[0]=='Game')==(kind=='game'):
                return prefix + 'Calculating…'
            record = current.get(kind, {}).get(method)
            if record is None:
                return prefix + ("Calculating…" if current.get('pending') else "Not calculated yet")
            try:
                last = date.fromisoformat(record["latest_date"]).strftime("%m/%d/%Y")
            except (ValueError, TypeError, KeyError):
                return prefix + "Unavailable"
            if kind == "player":
                last += " · " + record["category"] + ("" if record["category"]=='All player props' else " markets")
            return prefix + last
        return (label("game", game_method or "PPP", "Latest historical game used: "),
                label("player", player_method or "WHP", "Latest historical performance used: "))

    def forecast_player_markets(snapshot,player_method,category=None):
        forecast,error=None,None
        rows=[row for row in snapshot['rows'] if row['market_key'].startswith('player_') and
              (category is None or row['group']==category)]
        if not rows:return None,None,{'latest_date':None,'category':'All player props' if category is None else category}
        try:
            if snapshot['sport']=='NCAAF':
                names=[snapshot['home_team'],snapshot['away_team']]
                ncaaf_model_data.prepare(names,snapshot['commence_time'])
                if player_method!='WHP':ncaaf_model_data.prepare_player_inputs(names,snapshot['commence_time'])
            if player_method in {'UO','OAM','PGE'}:
                # One joint game forecast preserves the same simulated volumes
                # and player correlations across tabs and the cross-market filter.
                forecast={'UO':usage_opportunity_service,'OAM':opponent_adjusted_service,
                          'PGE':pace_game_environment_service}[player_method].forecast(snapshot)
            else:
                if snapshot['sport']=='NCAAF':ncaaf_model_data.prepare_players(snapshot,rows)
                forecast=whp_service.forecast({**snapshot,'rows':rows})
        except (DataSourceError,OSError,ValueError) as exc:
            error=str(exc)
            if 'No saved player history.' in error:
                error='Player estimates are unavailable because no saved historical player data is available.'
        dates=[item.diagnostics.get('last_game_date') for item in forecast.forecasts.values()] if forecast else []
        dates=[value for value in dates if value]
        return forecast,error,dict(latest_date=max(dates) if dates else None,category='All player props' if category is None else category)

    def forecast_game_markets(snapshot,method):
        if not any(row['market_key'] in {'alternate_spreads','alternate_totals'} for row in snapshot['rows']):return None,None
        try:
            if snapshot['sport']=='NCAAF':service=ncaaf_forecast_services[method]
            elif snapshot['sport']=='NBA':service={'PPP':nba_ppp_service,'EPA':nba_epa_service,'ELO':nba_elo_service}[method]
            else:service={'PPP':ppp_service,'EPA':epa_service,'ELO':elo_service}[method]
            return service.forecast(snapshot),None
        except (DataSourceError,OSError,ValueError) as exc:return None,str(exc)

    @app.callback(Output('mispricing-results','children'),Output('mispricing-forecast-history','data'),
                  Input('mispricing-request','data'),Input('market-category','value'),Input('market-url','hash'),
                  Input('market-snapshot','value'),Input('market-quick-game-methods','value'),Input('market-quick-player-methods','value'),
                  Input('market-quick-probability','value'),Input('market-quick-edge','value'),
                  State('mispricing-forecast-history','data'),State('workspace-page','value'),prevent_initial_call=True,
                  running=[(Output('market-quick-filter','disabled'),True,False)])
    def quick_filter_markets(request,category,location_hash,selection,methods,player_methods,probability,edge,previous_history,page=None):
        if (page is not None and page!='market') or not market_game_id(location_hash):raise PreventUpdate
        prompt=html.P('Click Quick Filtering to show the top three selections and all remaining matches.',className='saved-market-empty')
        if category!=FILTER_CATEGORY:return prompt,no_update
        try:
            methods,player_methods=selected_methods(methods,player_methods)
            criteria=criteria_values(probability,edge)
        except ValueError:return prompt,no_update
        expected=filter_context(location_hash,selection,methods,player_methods,criteria)
        if not request or any(request.get(key)!=value for key,value in expected.items()):return prompt,no_update
        if (page is not None and ctx.triggered_id=='market-url' and previous_history
                and previous_history.get('context')==[location_hash,selection] and previous_history.get('filter_key')==request):
            return no_update,no_update
        snapshot=saved_snapshot(location_hash,selection)
        if not snapshot:return html.P('That saved snapshot is unavailable. Choose a snapshot and run Quick Filtering again.',className='saved-market-empty'),no_update
        snapshot={**snapshot,'sport':market_game_sport(location_hash)}
        tasks={method:lambda method=method:forecast_game_markets(snapshot,method) for method in methods}
        tasks.update({method:lambda method=method:forecast_player_markets(snapshot,method) for method in player_methods})
        forecasts=run_forecasts(tasks,snapshot['sport'])
        game_rows={**snapshot,'rows':[row for row in snapshot['rows'] if not row['market_key'].startswith('player_')]}
        player_rows={**snapshot,'rows':[row for row in snapshot['rows'] if row['market_key'].startswith('player_')]}
        rankings=[[(method,rank_markets(game_rows,forecasts[method][0],None,ALL_PROBABILITIES,method)) for method in methods],
                  [(method,rank_markets(player_rows,None,forecasts[method][0],ALL_PROBABILITIES,player_method=method)) for method in player_methods]]
        ranked=consensus_rankings(rankings,criteria,lambda item:(item['row']['market_key'],item['row']['subject'],
                                                                item['row']['selection'],str(item['row']['line'])))
        context=[location_hash,selection]
        previous=previous_history if previous_history and previous_history.get('context')==context else {}
        history=dict(context=context,filter_key=request,game=dict(previous.get('game',{})),player=dict(previous.get('player',{})),weighting_policy=POLICY_VERSION)
        for method in methods:
            forecast=forecasts[method][0]
            history['game'][method]={'latest_date':forecast.diagnostics.get('last_game_date') if forecast else None}
        for method in player_methods:history['player'][method]=forecasts[method][2]
        errors=[f'{method} forecast: {record[1]}' for method,record in forecasts.items() if record[1]]
        try:
            filtered_results_service.save(snapshot,ranked,criteria,request=request)
        except (DataSourceError,OSError,ValueError) as exc:
            errors.append('Quick Filtering completed, but its report was not saved: '+str(exc))
        return mispricing_results(snapshot,ranked,criteria,errors),history

    @app.callback(Output("saved-market-sections", "children"), Output("market-forecast-history", "data"),
                  Input("market-category", "value"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), Input("market-applied-criteria", "data"),
                  Input("market-forecast-method", "value"), Input("player-forecast-method", "value"),
                  State("market-forecast-history", "data"),State('workspace-page','value'),Input('market-model-poll','n_intervals'))
    def show_saved_markets(category, selection, location_hash, criteria, method, player_method, previous_history,page=None,_model_tick=None):
        if (page is not None and page!='market') or not market_game_id(location_hash):raise PreventUpdate
        if category is None:
            raise PreventUpdate
        if category in {FILTER_CATEGORY,ADDITIONAL_INFORMATION}:return [],no_update
        method = method or "PPP"
        if method not in ("PPP", "EPA", "ELO"):
            raise PreventUpdate
        context = [location_hash, selection]
        previous = previous_history if previous_history and previous_history.get("context") == context else {}
        view_key=[category,method,player_method,criteria,POLICY_VERSION]
        if (page is not None and ctx.triggered_id=='market-model-poll' and previous.get('view_key')==view_key
                and previous.get('pending') and previous.get('job')
                and market_page_tasks.queue.state(previous['job'])['pending']):
            # Polling a running fit needs no snapshot reads, filesystem scans,
            # hashes or quote/accordion reconstruction.
            return no_update,no_update
        if page is not None and ctx.triggered_id=='market-url' and previous.get('view_key')==view_key and not previous.get('pending'):
            return no_update,no_update
        history = {"context": context, "view_key":view_key,"game": dict(previous.get("game", {})), "player": dict(previous.get("player", {}))}
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return html.P("Use Pull Live Markets above to download this game's first snapshot.", className="saved-market-empty"), history
        if not snapshot["rows"]:
            return html.P("The saved response contains no FanDuel selections.", className="saved-market-empty"), history
        snapshot = {**snapshot, "sport": market_game_sport(location_hash)}
        player_method=player_method or 'WHP'
        if category!='Game' and player_method not in {'WHP','UO','OAM','PGE'}:raise PreventUpdate
        def calculate_view():
            if category!='Game':return forecast_player_markets(snapshot,player_method,category)
            forecast,error=forecast_game_markets(snapshot,method)
            return forecast,error,{'latest_date':forecast.diagnostics.get('last_game_date') if forecast else None}
        if page is not None:
            # Highlight thresholds and the other market type's method do not
            # change a forecast. Reuse the exact saved-snapshot calculation.
            forecast_scope=['game',method] if category=='Game' else ['player',player_method,category if player_method=='WHP' else 'all']
            if category=='Game':
                sources=ncaaf_forecast_services if snapshot['sport']=='NCAAF' else (
                    {'PPP':nba_ppp_service,'EPA':nba_epa_service,'ELO':nba_elo_service} if snapshot['sport']=='NBA' else
                    {'PPP':ppp_service,'EPA':epa_service,'ELO':elo_service})
                source=sources[method]
            else:source={'WHP':whp_service,'UO':usage_opportunity_service,'OAM':opponent_adjusted_service,'PGE':pace_game_environment_service}[player_method]
            revision=forecast_revision(source,snapshot['sport'])
            if snapshot['sport']=='NCAAF' and category!='Game':revision+=forecast_revision(ncaaf_model_data,'NCAAF')
            key=hashlib.sha256(json.dumps([context,forecast_scope,POLICY_VERSION,revision,snapshot],sort_keys=True).encode()).hexdigest()
            # Preparing a cold forecast can itself create input files. Finish
            # that job rather than submitting another one on every poll.
            if ctx.triggered_id=='market-model-poll' and previous.get('pending') and previous.get('job') and previous.get('view_key')==view_key:
                key=previous['job']
            resume=previous.get('pending') and previous.get('job')==key
            calculated,pending,error=market_page_tasks.read(key,calculate_view)
            history.update(pending=pending,job=key)
            if pending and resume and ctx.triggered_id=='market-model-poll':return no_update,no_update
            if pending or calculated is None:
                return market_sections(snapshot,category,model_error=error,method=player_method if category!='Game' else method,
                    criteria=criteria,loading=pending),history
            forecast,model_error,record=calculated
        else:forecast,model_error,record=calculate_view()
        if category != "Game":
            record={**record,'category':category}
            history['player'][player_method]=record
            return market_sections(snapshot, category, forecast, model_error, criteria, player_method), history
        history['game'][method]=record
        return market_sections(snapshot, category, forecast, model_error, criteria, method), history

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
            for markets in market_services.values():
                if isinstance(markets, NflMarketService):
                    markets.export_existing()
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

    @app.callback(Output("market-games", "children"), Output("market-status", "children"), Output("market-schedule", "data"),
                  Output('market-schedule-scope','data'),
                  Input("workspace-page", "value"), Input("sport", "value"), Input("market-days", "value"),
                  Input("market-refresh", "n_clicks"), Input("market-timer", "n_intervals"),
                  Input("market-refresh-signal", "data"), State("market-schedule", "data"),State('market-schedule-scope','data'),
                  running=[(Output("market-refresh", "disabled"), True, False)])
    def upcoming_games(page, sport, days, _refresh, _timer, _refreshed, previous_games,scope=None):
        if page != "market" or sport not in SOURCES:
            raise PreventUpdate
        if (ctx.triggered_id=='workspace-page' and scope and [scope.get('sport'),scope.get('days')]==[sport,days]
                and 0<=time()-scope.get('checked_at',0)<300):raise PreventUpdate
        if ctx.triggered_id == "market-refresh-signal" and sport in market_services and previous_games:
            saved = market_services[sport].latest([game["id"] for game in previous_games])
            return schedule_blocks(previous_games, sport, saved), no_update, no_update,no_update
        try:
            schedule = schedule_service.load(sport, days, refresh=ctx.triggered_id == "market-refresh")
        except (DataSourceError, ValueError) as exc:
            return html.Div([html.H3("Schedule unavailable"), html.P(str(exc)),
                             html.A("Check ESPN schedule ↗", href=SCHEDULE_LINKS[sport], target="_blank", rel="noopener noreferrer")],
                            className="panel market-empty"), "Upcoming games could not be loaded.", [],dict(sport=sport,days=days,checked_at=0)
        status = f"{sport} · {len(schedule.games)} upcoming games · ESPN · Oldest schedule update: {format_eastern(schedule.fetched_at)} · Refreshes every 5 minutes."
        updating=getattr(schedule,'updating',False)
        if updating:status+=' Saved schedules are available now; checking for updates in the background.'
        if schedule.stale_days:
            status += " Saved schedules shown after a source error for: " + ", ".join(schedule.stale_days) + "."
        if schedule.failed_days:
            status += " Incomplete coverage; unavailable dates: " + ", ".join(schedule.failed_days) + "."
        saved = market_services[sport].latest([game["id"] for game in schedule.games]) if sport in market_services else {}
        blocks = schedule_blocks(schedule.games, sport, saved)
        if not blocks:
            blocks = html.Div([html.H3("No upcoming games found"),
                               html.P("No scheduled games were returned for the loaded dates. Try the 14-day window or refresh the schedule.")],
                              className="panel market-empty")
        return blocks, status, schedule.games,dict(sport=sport,days=days,checked_at=time(),updating=bool(updating))

    @app.callback(Output("college-fields", "style"), Output("week-fields", "style"), Input("sport", "value"),Input('entity-type','value'))
    def source_details(sport,kind='team'):
        if sport not in SOURCES:
            raise PreventUpdate
        return ({"display": "block" if sport == "NCAAF" and kind!='player' else "none"},
                {"display": "block" if sport != "NBA" else "none"})

    @app.callback(Output("entity-type", "value"),Input("sport", "value"),
                  Input('market-url','hash'),State("entity-type", "value"))
    def statistics_modes(sport,location_hash,kind):
        if sport not in SOURCES:
            raise PreventUpdate
        target=history_target(location_hash)
        if target and target['sport']==sport:return target['kind']
        return statistics_kind(sport,historical_route(location_hash) or kind)

    @app.callback(Output('season','value'),Output('season-type','value'),Output('college-team','value'),
                  Output('week','value'),Input('market-url','hash'))
    def historical_market_scope(location_hash):
        target=historical_target(location_hash)
        if not target:raise PreventUpdate
        return (target.get('season',no_update),'regular',
                target['name'] if target['sport']=='NCAAF' and target['kind']=='team' else no_update,None)

    def college_player_history(season,phase,week):
        frame=backtesting_service.player_inputs('NCAAF')
        frame=frame[pd.to_numeric(frame.season,errors='coerce').eq(int(season)) & frame.season_type.eq(phase)].copy()
        if week is not None:
            if 'week' not in frame:raise DataSourceError('Week metadata is unavailable in saved college player history. Clear the week filter.')
            frame=frame[pd.to_numeric(frame.week,errors='coerce').eq(int(week))]
        if frame.empty:raise DataSourceError('No saved college player history matches this season and phase. Prepare college player history or choose another season.')
        if 'matchup' not in frame:frame['matchup']=frame.team+' vs '+frame.opponent
        return frame.assign(sport='NCAAF',entity_type='player')

    app.clientside_callback("function(page,pending){return page!=='historical'||!pending;}",
        Output('historical-cache-poll','disabled'),Input('workspace-page','value'),Input('historical-cache-pending','data'))

    @app.callback(Output("dataset", "data"), Output("load-status", "children"),
                  Output("dataset-summary", "children"), Output("export", "disabled"),
                  Output('historical-cache-pending','data'),
                  Input("load", "n_clicks"), Input("refresh", "n_clicks"), Input("sport", "value"),
                  Input("season", "value"), Input("entity-type", "value"), Input("season-type", "value"),
                  Input("college-team", "value"), Input("week", "value"), Input("workspace-page", "value"), State('dataset','data'),
                  Input('market-url','hash'),
                  Input('historical-cache-poll','n_intervals'),
                  running=[(Output("load", "disabled"), True, False), (Output("refresh", "disabled"), True, False)])
    def load_data(_load, _refresh, sport, season, kind, phase, team, week, page, previous=None,location_hash=None,_cache_tick=None):
        if page != "historical" or sport not in SOURCES:
            raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        target=historical_target(location_hash)
        if target and ctx.triggered_id in {'market-url','workspace-page','sport','entity-type'}:
            # Resolve the incoming link before the visible controls finish updating.
            # Explicit changes to season/week/phase remain normal historical filters.
            if ctx.triggered_id in {'market-url','workspace-page'} or sport==target['sport']:
                sport,kind=target['sport'],target['kind']
                season=target.get('season',season);phase='regular';week=None
                if sport=='NCAAF' and kind=='team':team=target['name']
        elif target and sport==target['sport']:kind=target['kind']
        if sport not in PLAYER_SPORTS and sport!='NCAAF':
            kind = "team"
        try:
            if season is None or int(season) != float(season):
                raise ValueError("Choose a whole season year.")
            if sport != "NBA" and week is not None and int(week) != float(week):
                raise ValueError("Choose a whole week number.")
            if sport=='NCAAF' and kind=='player':
                frame=college_player_history(season,phase,week)
                return (dict(key='saved-college-players',sport=sport,season=season,kind=kind,phase=phase,week=week,saved_players=True),
                        'Saved CollegeFootballData player boxes. This view reads prepared history; it does not download data.',
                        f'{sport} · {season} · {phase} · {len(frame):,} appearances · {frame.entity_id.nunique():,} players',False,False)
            query = Query(sport, int(season), kind, phase, (team or "").strip() if sport == "NCAAF" else "",
                          int(week) if week is not None and sport != "NBA" else None)
            result = service.load(query, refresh=ctx.triggered_id == "refresh",local_first=True)
        except (DataSourceError, ValueError, TypeError) as exc:
            return None, str(exc), "No dataset loaded", True,False
        if result.status=='loading cache':
            name=target['name'] if target else f'{sport} {kind} statistics'
            return None,f'Loading historical data for {name}…',f'{sport} · {season} · {phase} · Loading saved/source data',True,True
        summary = (f"{sport} · {season} · {phase} · {len(result.frame):,} appearances · "
                   f"{result.frame['entity_id'].nunique():,} {kind}s")
        message = f"{result.status.capitalize()} · Retrieved {format_eastern(result.fetched_at)}."
        source=getattr(result,'source',None)
        if isinstance(source,str) and source:message+=' '+source+'.'
        if result.notice:
            message += f" Showing previously saved data. {result.notice}"
        dataset={"key": result.key, "sport": sport, "season": season, "kind": kind, "phase": phase}
        revision=getattr(result,'revision',None)
        if isinstance(revision,str) and revision:dataset['revision']=revision
        # Still check source freshness above, but do not rebuild every dependent
        # selector and chart just because an unchanged page was reopened.
        unchanged=previous==dataset and bool(dataset.get('revision'))
        return no_update if unchanged else dataset, message, summary, False,result.status=='updating cache'

    historical_frames=OrderedDict()
    historical_frames_lock=Lock()
    def historical_frame(dataset):
        frame=(college_player_history(dataset['season'],dataset['phase'],dataset.get('week')) if dataset.get('saved_players') else
               service.read(dataset['key']).frame)
        key=(dataset['key'],dataset['sport'],dataset['kind'])
        with historical_frames_lock:
            cached=historical_frames.get(key)
            if cached is None or cached[0] is not frame:
                cached=(frame,market_metrics(frame,dataset['sport'],dataset['kind']))
                historical_frames[key]=cached
            historical_frames.move_to_end(key)
            while len(historical_frames)>4:historical_frames.popitem(last=False)
            return cached[1]

    def linked_metric(dataset,entity,metrics,location_hash):
        target=historical_target(location_hash)
        if matches_dataset(target,dataset):
            frame=historical_frame(dataset)
            if target_entity(frame,target)==entity:
                return target_metric(target,metrics,frame[frame.entity_id.eq(entity)]),True
        return None,False

    @app.callback(Output('historical-link-status','children'),Input('dataset','data'),Input('entity','value'),
                  Input('metric','value'),Input('market-url','hash'),Input('load-status','children'))
    def historical_market_status(dataset,entity,metric,location_hash,load_status=None):
        target=historical_target(location_hash)
        if not target:return ''
        if not dataset and load_status:
            if isinstance(load_status,str) and load_status.startswith('Loading historical data'):return load_status
            return f"Historical data for {target['name']} could not be loaded. {load_status}"
        if not matches_dataset(target,dataset):return f"Loading historical data for {target['name']}…"
        try:
            resolved=target_entity(historical_frame(dataset),target)
        except DataSourceError:return 'Reload historical data to open this market’s history.'
        if resolved is None:
            return f"No unique match for {target['name']} in this season’s saved data. Choose another season or select a name manually."
        if entity!=resolved:return ''
        if metric is None:return f"No recorded statistic for this market is available for {target['name']} in this scope. Choose another season or statistic."
        note=' '+target.get('note','')
        if target['market'] in {'player_1st_td','player_last_td'} and not target.get('note'):note+=' Touchdown counts are shown; first/last scorer order is not available in these box scores.'
        if 'line' in target:note+=' The chart marks the prompt threshold and counts hits, misses and pushes on earlier game dates in the selected scope. These are descriptive threshold outcomes, not a forecast or settlement audit.'
        return f"{target['name']} · {metric_label(metric,dataset['sport'])} historical game log."+note

    @app.callback(Output('ncaaf-history-status','children'),Input('dataset','data'),Input('entity','value'),
                  Input('refresh','n_clicks'),State('workspace-page','value'))
    def prepare_college_selection(dataset,entity,_refresh,page):
        if page!='historical' or not dataset or dataset.get('sport')!='NCAAF' or dataset['kind']!='team':return ''
        if not entity:return 'Choose a college school to prepare reusable team/player forecasting CSVs.'
        try:
            frame=service.read(dataset['key']).frame
            selected=frame[frame.entity_id.eq(str(entity))]
            if selected.empty:return ''
            school=str(selected.iloc[-1].entity_name)
            return ncaaf_model_data.prepare_team(school,f"{int(dataset['season'])}-12-31T00:00:00+00:00",
                                                 refresh=ctx.triggered_id=='refresh')
        except (DataSourceError,OSError,ValueError) as exc:
            return f'Historical statistics remain available; forecast preparation needs attention: {exc}'

    @app.callback(Output("entity", "options"), Output("entity", "value"),
                  Output("entity", "disabled"), Output("entity", "placeholder"),
                  Input("dataset", "data"), Input("player-team", "value"), State("entity", "value"),Input('market-url','hash'))
    def select_options(dataset, team, previous_entity,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "No data available — see source status"
        try:
            frame = historical_frame(dataset)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        if matches_dataset(historical_target(location_hash),dataset):team=ALL_TEAMS
        frame = filter_team(frame, dataset["kind"], team)
        entities = frame.reindex(columns=["entity_id", "entity_name", "team", "position"]).drop_duplicates("entity_id").sort_values("entity_name")
        options = [{"label": (f"{r.entity_name} ({r.team}" + (f" · {r.position}" if pd.notna(r.position) and r.position else "") + ")")
                    if dataset["kind"] == "player" else r.entity_name,
                    "value": r.entity_id} for r in entities.itertuples()]
        if not options:
            return [], None, True, "No matching observations"
        selected_entity = previous_entity if previous_entity in set(entities["entity_id"]) else options[0]["value"]
        target=historical_target(location_hash)
        placeholder='Choose a team or player'
        if matches_dataset(target,dataset):
            selected_entity=target_entity(frame,target)
            if selected_entity is None:placeholder=f"No unique match for {target['name']} — select manually"
        return options, selected_entity, False, placeholder

    @app.callback(Output("player-team-field", "style"), Output("identity-filters", "className"),
                  Output("entity-label", "children"), Input("entity-type", "value"), Input("sport", "value"))
    def show_player_team(kind, sport):
        if kind == "player" and sport in {*PLAYER_SPORTS,'NCAAF'}:
            return {"display": "block"}, "analysis-filters identity-filters", "Player"
        return {"display": "none"}, "analysis-filters identity-filters single-entity", "Team"

    @app.callback(Output("player-team", "options"), Output("player-team", "value"),
                  Output("player-team", "disabled"), Output("player-team", "placeholder"),
                  Input("dataset", "data"), State("player-team", "value"),Input('market-url','hash'))
    def select_player_teams(dataset, previous_team,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset) or dataset["kind"] != "player":
            return [], None, True, "Choose player statistics to filter by team"
        try:
            frame = historical_frame(dataset)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        teams = sorted(team for team in frame["team"].dropna().unique() if str(team).strip())
        if not teams:
            return [], None, True, "No team data available"
        options = [{"label": "All teams", "value": ALL_TEAMS}] + [{"label": team, "value": team} for team in teams]
        selected = previous_team if previous_team == ALL_TEAMS or previous_team in teams else ALL_TEAMS
        if matches_dataset(history_target(location_hash),dataset):selected=ALL_TEAMS
        return options, selected, False, "Choose a team"

    def available_metrics(dataset, entity, team):
        if not supported_dataset(dataset) or entity is None:
            return []
        frame = historical_frame(dataset)
        frame = filter_team(frame, dataset["kind"], team)
        selected = frame[frame["entity_id"] == entity]
        return relevant_metrics(selected, dataset["sport"], dataset["kind"])

    @app.callback(Output("metric-group", "options"), Output("metric-group", "value"),
                  Output("metric-group", "disabled"), Output("metric-group", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  State("metric-group", "value"), State("metric", "value"),Input('market-url','hash'))
    def select_categories(dataset, entity, team, previous_category, previous_metric,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "Load historical data — see source status above"
        if entity is None:
            return [], None, True, "Choose a team or player first"
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
        requested,linked=linked_metric(dataset,entity,metrics,location_hash)
        if linked:value=statistic_category(requested,dataset['sport']) if requested else None
        return options, value, False, "Choose a statistic type"

    @app.callback(Output("metric", "options"), Output("metric", "value"),
                  Output("metric", "disabled"), Output("metric", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  Input("metric-group", "value"), State("metric", "value"),Input('market-url','hash'))
    def select_metrics(dataset, entity, team, category, previous_metric,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "Load historical data — see source status above"
        if entity is None:
            return [], None, True, "Choose a team or player first"
        if category is None:
            return [], None, True, "Choose a statistic type first"
        try:
            metrics = available_metrics(dataset, entity, team)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        metrics = [metric for metric in metrics if statistic_category(metric, dataset["sport"]) == category]
        if not metrics:
            return [], None, True, "Choose an available statistic type"
        value = previous_metric if previous_metric in metrics else preferred_metric(metrics)
        requested,linked=linked_metric(dataset,entity,metrics,location_hash)
        if linked and requested in metrics:value=requested
        return [{"label": metric_label(c, dataset["sport"]), "value": c} for c in metrics], value, False, "Choose a statistic"

    @app.callback(Output("trend", "figure"), Output("forecast-summary", "children"),
                  Output("game-log", "data"), Output("game-log", "columns"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"), Input("window", "value"),
                  Input("player-team", "value"),Input('market-url','hash'),State('workspace-page','value'))
    def analyze(dataset, entity, metric, window, team,location_hash=None,page=None):
        if page is not None and page!='historical':raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        blank = [kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                 kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                 kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]
        if not supported_dataset(dataset):
            return empty_figure(), blank, [], []
        frame = historical_frame(dataset)
        target=historical_target(location_hash)
        try:trigger=ctx.triggered_id
        except MissingCallbackContextException:trigger=None
        if matches_dataset(target,dataset) and trigger in {'dataset','market-url','entity'}:
            resolved=target_entity(frame,target)
            if trigger!='entity' or entity==resolved:
                entity=resolved;team=ALL_TEAMS
                own=frame[frame.entity_id.eq(entity)]
                metric=target_metric(target,relevant_metrics(own,dataset['sport'],dataset['kind']),own)
        if entity is None or metric is None:
            return empty_figure(),blank,[],[]
        frame = filter_team(frame, dataset["kind"], team)
        if metric not in frame:
            return empty_figure(), blank, [], []
        selected = frame[frame["entity_id"] == entity]
        if not matches_dataset(target,dataset) or target_entity(frame,target)!=entity:target=None
        selected=prior_history(selected,target)
        if selected.empty and target:return empty_figure('No prior appearances in the selected historical scope'),blank,[],[]
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
        fields = ["event_date"] + ([] if dataset["sport"] == "NBA" else ["week"])
        components=list((figure.layout.meta or {}).get('stat_components',[]))
        table = history.reindex(columns=fields + ["team", "opponent", "matchup",*components,metric,"baseline"]).copy()
        table["event_date"] = table["event_date"].dt.strftime("%Y-%m-%d")
        table = table.sort_index(ascending=False).round(3)
        # JSON nulls rather than NaN/NaT if schedule metadata is unavailable.
        records = table.astype(object).where(pd.notna(table), None).to_dict("records")
        columns = [{"name": metric_label(c, dataset["sport"]) if c == metric or c in components else c.replace("_", " ").title(), "id": c}
                   for c in table]
        result=prompt_outcomes(history,metric,target)
        if result is not None:
            hits,count,pushes=annotate_prompt(figure,history,metric,target,result)
            detail=[f'{hits} hits / {count} recorded appearances · {pushes} pushes. ',
                    html.A('Back to market',href=target['origin'],className='market-history-button')] if target.get('origin') else f'{hits} hits / {count} appearances · {pushes} pushes'
            cards.append(kpi('PROMPT HIT RATE',f'{100*hits/count:.1f}%' if count else '—',detail))
            for record,index in zip(records,table.index):record['prompt_result']=result.loc[index]
            columns.append({'name':'Prompt result','id':'prompt_result'})
        return figure, cards, records, columns

    @app.callback(Output("career-panel", "style"), Input("entity-type", "value"), Input("sport", "value"))
    def show_career(kind, sport):
        return {"display": "block" if kind == "player" and sport in PLAYER_SPORTS else "none"}

    @app.callback(Output("career-trend", "figure"), Output("career-status", "children"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"),
                  Input('workspace-page','value'),State('market-url','hash'),State('sport','value'))
    def analyze_career(dataset, entity, metric,page=None,location_hash=None,sport=None):
        if page is not None and page!='historical':raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if dataset and sport is not None and dataset.get('sport')!=sport:raise PreventUpdate
        if not supported_dataset(dataset) or dataset["kind"] != "player" or entity is None or metric is None:
            return empty_figure("Choose a player and statistic to load career history"), ""
        try:
            frame = historical_frame(dataset)
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
        frame = historical_frame(dataset)
        return dcc.send_data_frame(frame.to_csv, f"{dataset['sport']}_{dataset['season']}_{dataset['kind']}_stats.csv", index=False)

    register_backtest_summary(app,backtesting_service)
    register_performance(app,backtesting_service)
    register_information(app,information_service)
    register_game_information(app,game_information_service)
    register_pnl(app,ledger)
    register_structure(app,structure_service)
    register_comparison_callbacks(app,backtesting_service)
    register_in_game_callbacks(app,in_game_service)
    register_kalshi_callbacks(app,kalshi_service,pages=exchange_pages.get('Kalshi'))
    register_kalshi_analysis(app,kalshi_service,forecast_game_markets,forecast_player_markets,filtered_results_service)
    register_kalshi_callbacks(app,polymarket_service,view=polymarket_view,provider='Polymarket',pages=exchange_pages.get('Polymarket'))
    register_market_summary(app,schedule_service,market_services,kalshi_service,polymarket_service,forecast_game_markets,forecast_player_markets,pages=exchange_pages)
    register_filtered_results(app,filtered_results_service)
    return app
