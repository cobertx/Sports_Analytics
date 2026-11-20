"""Historical team and player workspaces."""

from datetime import date
from dash import dash_table, dcc, html
from app.components.presentation import table_cell, table_filter, table_header
from app.shell.loading import indicator
from app.components.charts import empty_figure
from app.components.controls import field
from app.components.controls import kpi


def layout():
    current_year = date.today().year
    seasons = [{"label": f"{year}-{year + 1}", "value": year} for year in range(current_year, 1998, -1)]
    return html.Div([
        html.Section([
            html.Div([html.Div("BUILD YOUR DATASET", className="eyebrow"),
                      html.H2("Historical scope")], className="dataset-builder-heading"),
            html.Div([
                field("Season", dcc.Dropdown(id="season", options=seasons, value=current_year - 1, clearable=False)),
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
        dcc.Loading(html.Section([
            html.Div([
                html.Div(id="load-status", role="status", **{"aria-live": "polite"}, className="load-status"),
                html.Div(id="historical-link-status", role="status", **{"aria-live": "polite"}, className="chart-note"),
                html.Div(id="ncaaf-history-status", role="status", className="chart-note"),
            ], className="historical-status"),
            html.Div([
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
                    field("Baseline window", dcc.Dropdown(id="window", options=[{"label": f"{n} games", "value": n} for n in (3, 5, 10)],
                                                         value=5, clearable=False)),
                ], className="analysis-filters"),
                html.Div([html.Div(className='history-skeleton-cards'), html.Div(className='history-skeleton-chart')],
                         className='historical-skeleton', **{'aria-hidden': 'true'}),
                html.Div(id="forecast-summary", className="kpi-grid", children=[
                    kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                    kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                    kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]),
                html.Div([html.Div([html.H2("Performance over time"), html.Span("GAME HISTORY", className="eyebrow")],
                                   className="panel-heading"),
                          dcc.Graph(id="trend", figure=empty_figure(), config={"displaylogo": False}),
                          html.P("Hover over a game to see its date, matchup and recorded statistic.",
                                 className="chart-note")], id='historical-performance-panel',className="panel chart-panel"),
                html.Div([
                    html.Div([html.H2("Player career"), html.Span("ALL AVAILABLE SEASONS", className="eyebrow")],
                             className="panel-heading"),
                    html.P("The first career load may take a moment. Later selections reuse saved history.",
                           className="chart-note"),
                    html.Div([
                        dcc.Graph(id="career-trend", figure=empty_figure("Choose a player to load career history"),
                                  config={"displaylogo": False}),
                        html.P(id="career-status", className="chart-note", role="status"),
                    ], className="career-results"),
                ], id="career-panel", className="panel chart-panel career-panel", style={"display": "none"}),
                html.Div([
                    html.Div([html.H2("Game log"), html.Button("Export loaded dataset ↓", id="export", disabled=True,
                                                              className="export-button")], className="panel-heading"),
                    dash_table.DataTable(id="game-log", data=[], columns=[], page_size=12,
                                         sort_action="native", filter_action="native",
                                         style_table={"overflowX": "auto"},
                                         style_cell=table_cell(minWidth='95px'),
                                         style_header=table_header(),
                                         style_filter=table_filter()),
                ], className="panel table-panel"),
            ],className='historical-results'),
        ], className="analysis"), id="historical-loading",
            target_components={"load-status": "children", "ncaaf-history-status": "children",
                               "entity": "options", "player-team": "options",
                               "metric-group": "options", "metric": "options",
                               "trend": "figure", "career-trend": "figure"},
            custom_spinner=indicator("Loading historical data…", overlay=True)),
    ], id="historical-page", className="workspace")
