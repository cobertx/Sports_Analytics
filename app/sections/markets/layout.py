"""Sportsbook game list and matchup workspace."""

from dash import dcc, html
from app.shell.loading import indicator
from sports_analytics.schedule import FANDUEL_LINKS
from app.sections.markets.market_view import market_provider_heading
from app.sections.markets.mispricing import quick_controls
from app.sections.markets import quick_methods
from app.sections.information.game_information_view import layout as game_information_layout
from app.components.controls import field


def layout():
    return html.Section([
        market_provider_heading("FanDuel", FANDUEL_LINKS["NBA"], link_id="fanduel-link"),
        html.Div([
            html.Div([
                field("Schedule window", dcc.Dropdown(id="market-days", value=7, clearable=False,
                      options=[{"label": "Next 7 days", "value": 7}, {"label": "Next 14 days", "value": 14}])),
                html.Button("Refresh upcoming games", id="market-refresh", n_clicks=0, className="export-button"),
            ], className="market-controls"),
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
            dcc.Tabs(id="market-category", value="Game", children=[], mobile_breakpoint=0, className="market-category-tabs"),
            game_information_layout(),
            html.Section([
            html.H2('Mispricing Filter'),
            html.H3('Apply Criteria'),
            html.P('Choose the methods and thresholds used to highlight selections in the market tabs.',className='ppp-comparison-note'),
            html.Div([
              html.Div([
                field("Game Outcome Forecast Method", dcc.Dropdown(id="market-forecast-method", value="PPP",
                           options=quick_methods.game_options(),
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
            html.Div(id="market-criteria-status", role="status", **{"aria-live": "polite"},
                     className="criteria-status"),
            quick_controls('market', 'mispricing-results'),
            ],id='mispricing-filter-panel',className='panel market-criteria'),
            html.Div([
                html.Div(indicator('Loading the selected markets…'),id='market-sections-transition',className='market-sections-transition'),
                dcc.Loading(html.Div(id="saved-market-sections"), color="#57dbc4"),
            ],id='market-quote-content'),
            ], className="sportsbook-view"),
        ], id="market-game-detail", className="market-detail-view", style={"display": "none"}),
    ], id="market-page", className="market-page", style={"display": "none"})
