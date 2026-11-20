"""Odds API quota and usage history workspace."""

from dash import dash_table, dcc, html
from app.components.presentation import table_cell, table_filter, table_header
from sports_analytics.odds_api import DOCUMENTATION as ODDS_DOCUMENTATION
from sports_analytics.usage_log import SPORTS as USAGE_SPORTS
from app.components.charts import empty_figure
from app.components.controls import kpi


def layout():
    return html.Section([
        html.Div([
            html.Div([html.H2("API credit usage"),
                      html.P("Combined credits and individual balances for your configured API connections.")]),
            html.Button("Connect / Refresh usage", id="odds-refresh", n_clicks=0, className="export-button"),
        ], className="panel odds-heading"),
        dcc.Loading(html.Div([
            html.Div("Open Odds API to connect.", id="odds-status", role="status",
                     **{"aria-live": "polite"}, className="load-status"),
            html.Div([
                html.Div([
                    html.Div(html.H2("Combined account quota"), className="panel-heading"),
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
                {"name": "Used at (EDT)", "id": "timestamp"}, {"name": "API connection", "id": "account"}, {"name": "Sport", "id": "sport"},
                {"name": "Game", "id": "game"}, {"name": "Game time (EDT)", "id": "game_time"},
                {"name": "Credits", "id": "credits", "type": "numeric"},
                {"name": "Charge", "id": "charge"}, {"name": "Status", "id": "status"},
                {"name": "Requested markets", "id": "markets"}, {"name": "Source", "id": "source"},
                {"name": "Game ID", "id": "game_id"}, {"name": "Odds event ID", "id": "event_id"}],
                page_size=10, sort_action="native", filter_action="native", export_format="csv",
                style_table={"overflowX": "auto"},
                style_cell=table_cell(minWidth='95px', maxWidth='360px', whiteSpace='normal', height='auto'),
                style_header=table_header(),
                style_filter=table_filter()),
        ], className="panel table-panel"),
        html.Div([
            html.H2("Connection setup"),
            html.P("Add your primary key and optional backup to this project's .env file, then restart python app/index.py. The backup is used when the primary exhausts its credits."),
            html.Pre("ODDS_API_KEY=your_primary_api_key_here\nODDS_API_KEY_2=your_backup_api_key_here", className="odds-setup-code"),
            html.P("Keys stay on the server. Both balances refresh every five minutes while this section is open. These checks use the sports-list endpoint and do not consume credits. The original key is preferred again after its allowance replenishes."),
            html.A("The Odds API documentation ↗", href=ODDS_DOCUMENTATION,
                   target="_blank", rel="noopener noreferrer"),
        ], className="panel odds-setup"),
    ], id="odds-api-page", style={"display": "none"})
