"""Dash presentation layer. Load data when the user opens or changes a scope."""

from datetime import date
import math
from pathlib import Path

from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
import pandas as pd
import plotly.graph_objects as go

from .data import DataSourceError, PLAYER_SPORTS, Query, SOURCES
from .career import CareerService
from .forecast import rolling_baseline
from .metrics import metric_label, preferred_metric, relevant_metrics, statistic_categories, statistic_category
from .service import DataService

ALL_TEAMS = "__all__"


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


def create_app(service=None, career_service=None):
    service = service or DataService()
    career_service = career_service or CareerService(service.path.with_name("career.sqlite3"))
    app = Dash(__name__, assets_folder=str(Path(__file__).parent / "assets"),
               title="Sports Analytics | Historical Lab", update_title="Loading statistics…")
    app.layout = html.Div([
        dcc.Store(id="dataset"), dcc.Download(id="download"),
        html.Header([
            html.Div([html.Div("SA", className="brand-mark"), html.Div([
                html.Div("SPORTS ANALYTICS", className="brand"),
                html.Div("Historical data. Measurable predictions.", className="subtitle")])], className="identity"),
            html.Div([html.Span(className="status-dot"), "LOCAL RESEARCH WORKSPACE"], className="workspace-badge")
        ]),
        html.Main([
            html.Div([html.Div([html.Div("THE HISTORICAL LAB", className="eyebrow accent"),
                               html.H1("From past performance to the next game."),
                               html.P("Explore team statistics across three leagues and player statistics for NBA and NFL.")]),
                      html.Span("01 / DATA EXPLORER", className="chapter")], className="intro"),
            dcc.Tabs(id="sport", value="NBA", children=[
                dcc.Tab(label="NBA · Basketball", value="NBA", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NFL · Pro Football", value="NFL", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NCAAF · College Football", value="NCAAF", className="sport-tab", selected_className="sport-tab selected"),
            ], className="sport-tabs"),
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
            ], className="workspace"),
            html.Footer("NBA / NFL / NCAAF · Source-specific statistics and coverage · Timestamps shown in UTC")
        ])
    ], className="app-shell")

    @app.callback(Output("college-fields", "style"), Output("week-fields", "style"),
                  Output("source-info", "children"), Input("sport", "value"))
    def source_details(sport):
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
        options = [{"label": "Teams", "value": "team"}]
        if sport in PLAYER_SPORTS:
            options.append({"label": "Players", "value": "player"})
        return options, kind if kind in {option["value"] for option in options} else "team"

    @app.callback(Output("dataset", "data"), Output("load-status", "children"),
                  Output("dataset-summary", "children"), Output("export", "disabled"),
                  Input("load", "n_clicks"), Input("refresh", "n_clicks"), Input("sport", "value"),
                  Input("season", "value"), Input("entity-type", "value"), Input("season-type", "value"),
                  Input("college-team", "value"), Input("week", "value"),
                  running=[(Output("load", "disabled"), True, False), (Output("refresh", "disabled"), True, False)])
    def load_data(_load, _refresh, sport, season, kind, phase, team, week):
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
        message = f"{result.status.capitalize()} · Retrieved {result.fetched_at}."
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
