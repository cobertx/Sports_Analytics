"""Dash presentation layer. Load data when the user opens or changes a scope."""

from datetime import date
from pathlib import Path

from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
import pandas as pd
import plotly.graph_objects as go

from .data import DataSourceError, META, Query, SOURCES
from .forecast import rolling_baseline
from .service import DataService


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


def create_app(service=None):
    service = service or DataService()
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
                               html.P("Explore game-level team and player statistics across three leagues.")]),
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
                            field("Team or player", dcc.Dropdown(id="entity", options=[], disabled=True,
                                                               placeholder="Loading teams or players...", clearable=False)),
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
                    ]), type="circle", color="#57dbc4"),
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

    @app.callback(Output("dataset", "data"), Output("load-status", "children"),
                  Output("dataset-summary", "children"), Output("export", "disabled"),
                  Input("load", "n_clicks"), Input("refresh", "n_clicks"), Input("sport", "value"),
                  Input("season", "value"), Input("entity-type", "value"), Input("season-type", "value"),
                  Input("college-team", "value"), Input("week", "value"),
                  running=[(Output("load", "disabled"), True, False), (Output("refresh", "disabled"), True, False)])
    def load_data(_load, _refresh, sport, season, kind, phase, team, week):
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
        return {"key": result.key, "sport": sport, "season": season, "kind": kind}, message, summary, False

    @app.callback(Output("entity", "options"), Output("entity", "value"),
                  Output("metric", "options"), Output("metric", "value"),
                  Output("entity", "disabled"), Output("metric", "disabled"),
                  Output("entity", "placeholder"), Output("metric", "placeholder"),
                  Input("dataset", "data"), State("entity", "value"), State("metric", "value"))
    def select_options(dataset, previous_entity, previous_metric):
        if not dataset:
            return [], None, [], None, True, True, "No data available — see source status", "No data available — see source status"
        try:
            frame = service.read(dataset["key"]).frame
        except DataSourceError:
            return [], None, [], None, True, True, "Reload the historical data", "Reload the historical data"
        entities = frame[["entity_id", "entity_name", "team"]].drop_duplicates("entity_id").sort_values("entity_name")
        options = [{"label": f"{r.entity_name} ({r.team})" if dataset["kind"] == "player" else r.entity_name,
                    "value": r.entity_id} for r in entities.itertuples()]
        metrics = [column for column in frame if column not in META and
                   pd.api.types.is_numeric_dtype(frame[column]) and frame[column].notna().any()]
        if not options or not metrics:
            return [], None, [], None, True, True, "No matching observations", "No numeric statistics available"
        preferred = next((name for name in ("pts", "points", "passing_yards", "passing_yds", "rushing_yds") if name in metrics), metrics[0])
        selected_entity = previous_entity if previous_entity in set(entities["entity_id"]) else options[0]["value"]
        selected_metric = previous_metric if previous_metric in metrics else preferred
        return (options, selected_entity, [{"label": c.replace("_", " ").title(), "value": c} for c in metrics],
                selected_metric, False, False, "Choose a team or player", "Choose a statistic")

    @app.callback(Output("trend", "figure"), Output("forecast-summary", "children"),
                  Output("game-log", "data"), Output("game-log", "columns"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"), Input("window", "value"))
    def analyze(dataset, entity, metric, window):
        blank = [kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                 kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                 kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]
        if not dataset or entity is None or metric is None:
            return empty_figure(), blank, [], []
        frame = service.read(dataset["key"]).frame
        if metric not in frame:
            return empty_figure(), blank, [], []
        selected = frame[frame["entity_id"] == entity]
        if selected.empty:
            return empty_figure(), blank, [], []
        history, prediction, mae, evaluated = rolling_baseline(selected, metric, window)
        if history.empty:
            return empty_figure("No observations of this statistic for the selected entity"), blank, [], []
        label = metric.replace("_", " ").title()
        x = list(range(1, len(history) + 1))
        figure = go.Figure()
        figure.add_trace(go.Scatter(x=x, y=history[metric], mode="lines+markers", name=label,
                                   line={"color": "#57dbc4", "width": 2}, marker={"size": 5},
                                   text=history["opponent"], hovertemplate="Appearance %{x}: %{y:.2f}<br>Opponent: %{text}<extra></extra>"))
        figure.add_trace(go.Scatter(x=x, y=history["baseline"], mode="lines", name=f"Prior {window}-game mean",
                                   line={"color": "#8197ff", "dash": "dot"}))
        if prediction is not None:
            figure.add_trace(go.Scatter(x=[len(history) + 1], y=[prediction], name="Next appearance baseline", mode="markers",
                                       marker={"color": "#f6c667", "size": 12, "symbol": "diamond"}))
        figure = style_figure(figure)
        figure.update_xaxes(title="Observed appearance (chronological)", dtick=max(1, len(history) // 10))
        figure.update_yaxes(title=label)
        cards = [kpi("OBSERVED AVERAGE", f"{history[metric].mean():,.2f}", f"{len(history):,} recorded appearances"),
                 kpi("NEXT APPEARANCE", f"{prediction:,.2f}" if prediction is not None else "—",
                     f"Mean of the last {window} appearances" if prediction is not None else f"Requires {window} recorded appearances"),
                 kpi("BACKTEST MAE", f"{mae:,.2f}" if mae is not None else "—",
                     f"{evaluated} past predictions evaluated" if evaluated else f"Requires more than {window} appearances")]
        table = history[["game_id", "event_date", "week", "team", "opponent", metric, "baseline"]].copy()
        table["event_date"] = table["event_date"].dt.strftime("%Y-%m-%d")
        table = table.sort_index(ascending=False).round(3)
        # JSON nulls rather than NaN/NaT; NFL dates remain blank by design.
        records = table.astype(object).where(pd.notna(table), None).to_dict("records")
        columns = [{"name": c.replace("_", " ").title(), "id": c} for c in table]
        return figure, cards, records, columns

    @app.callback(Output("download", "data"), Input("export", "n_clicks"), State("dataset", "data"), prevent_initial_call=True)
    def export_data(_clicks, dataset):
        if not dataset:
            return no_update
        frame = service.read(dataset["key"]).frame
        return dcc.send_data_frame(frame.to_csv, f"{dataset['sport']}_{dataset['season']}_{dataset['kind']}_stats.csv", index=False)

    return app
