"""Build the Dash workspace and wire up its sections."""

from dash import Dash, Input, Output, State, dcc, html
from dash._utils import to_json
import pandas as pd
from sports_analytics.data import SOURCES
from sports_analytics.career import CareerService
from app.sections.backtesting.backtesting_summary import register as register_backtest_summary
from app.sections.backtesting.performance_summary import register as register_performance
from sports_analytics.backtesting_service import BacktestingService
from app.sections.backtesting.backtesting_monitor import layout as backtesting_monitor_layout
from app.sections.historical import game_data_monitor
from app.sections.markets import quick_filter_monitor
from app.sections.research import research_view
from app.sections.research.comparison_callbacks import register as register_comparison_callbacks
from sports_analytics.in_game_data import InGameService
from app.sections.in_game.in_game_view import layout as in_game_layout
from app.sections.in_game.in_game_callbacks import register as register_in_game_callbacks
from sports_analytics.kalshi import KalshiService
from app.sections.exchanges.kalshi_view import layout as kalshi_layout
from app.sections.exchanges.kalshi_callbacks import register as register_kalshi_callbacks
from app.sections.exchanges.kalshi_analysis_callbacks import register as register_kalshi_analysis
from sports_analytics.polymarket import PolymarketService
from app.sections.exchanges import polymarket_view
from app.sections.market_summary.market_summary_view import layout as market_summary_layout
from app.sections.market_summary.market_summary_callbacks import register as register_market_summary
from sports_analytics.market_structure import MarketStructure
from app.sections.market_structure.market_structure_view import layout as structure_layout, register as register_structure
from sports_analytics.filtered_results import FilteredResults
from app.sections.markets.filtered_results_view import layout as filtered_results_layout, register as register_filtered_results
from sports_analytics.page_cache import PageTasks, QuotePages
from sports_analytics.history_updates import HistoryUpdater
from sports_analytics.game_data_status import GameDataStatus
from app.sections.markets.lazy_markets import register as register_lazy_markets
from sports_analytics.filter_jobs import FilterJobs
from app.shell.loading import configure as configure_loading, register as register_loading, indicator
from app.shell.progressive_loading import configure as configure_progressive, register as register_progressive
from app.sections.markets.player_distribution import register as register_player_distribution
from app.sections.markets.prop_probability import register as register_prop_probability
from app.sections.markets.quick_visibility import register as register_quick_visibility
from sports_analytics.service import DataService
from sports_analytics.schedule import ScheduleService
from sports_analytics.odds_api import OddsApiService
from sports_analytics.nfl_markets import NflMarketService
from sports_analytics.nba_markets import NbaMarketService
from sports_analytics.ncaaf_markets import NcaafMarketService
from sports_analytics.whp_service import WHPMarketService
from sports_analytics.usage_opportunity_service import UsageOpportunityMarketService
from sports_analytics.opponent_adjusted_service import OpponentAdjustedMarketService
from sports_analytics.pace_game_environment_service import PaceGameEnvironmentMarketService
from models.Player_Stats.Playing_Time_Rotation import PlayingTimeRotationMarketService
from models.Player_Stats.Injury_Lineup_Adjusted import InjuryLineupMarketService
from app.sections.parlay.parlay_view import slip_layout
from sports_analytics.ncaaf_model_data import NcaafModelData
from sports_analytics.ncaaf_forecast_service import NCAAFMarketForecastService
from app.shell.navigation import PAGES, register_shell, register_sidebar, register as register_navigation
from sports_analytics.display_time import format_eastern
from sports_analytics.information import InformationService
from sports_analytics.availability_adjustment import AvailabilityAdjuster
from app.sections.information.information_view import layout as information_layout,navigation as information_navigation,register as register_information
from sports_analytics.game_information import GameInformationService
from app.sections.information.game_information_view import register as register_game_information
from sports_analytics.pnl import Ledger
from app.sections.pnl.pnl_view import layout as pnl_layout,register as register_pnl
from sports_analytics.usage_log import ApiUsageLog
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.epa_service import EPAMarketService
from sports_analytics.nba_ppp_service import NBAPPPMarketService
from sports_analytics.nba_epa_service import NBAEPAMarketService
from sports_analytics.elo_service import ELOMarketService, NBAELOMarketService
from sports_analytics.nba_pace_service import NBAPaceMarketService
from sports_analytics.nba_adjusted_efficiency_service import NBAAdjustedEfficiencyMarketService
from sports_analytics.nba_four_factors_service import NBAFourFactorsMarketService
from app.paths import APP_DIRECTORY, PROJECT_DIRECTORY
from app.sections.markets import schedule as market_schedule
from app.sections.historical.layout import layout as historical_layout
from app.sections.markets.layout import layout as market_layout
from app.sections.backtesting.layout import layout as backtesting_layout
from app.sections.odds_api.layout import layout as odds_layout
from app.sections.backtesting.callbacks import register as register_backtesting
from app.sections.markets.callbacks import register as register_markets
from app.sections.odds_api.callbacks import register as register_odds_api
from app.sections.historical.callbacks import register as register_historical
from app.sections.markets.team_logos import register as register_team_logos


def create_app(service=None, career_service=None, schedule_service=None, odds_service=None, nfl_market_service=None,
               api_usage_log=None, ppp_service=None, epa_service=None, nba_market_service=None, nba_ppp_service=None,
               nba_epa_service=None, elo_service=None, nba_elo_service=None, ncaaf_market_service=None,
               whp_service=None, usage_opportunity_service=None, opponent_adjusted_service=None, pace_game_environment_service=None,
               ncaaf_model_data=None,ncaaf_forecast_services=None,backtesting_service=None,in_game_service=None,kalshi_service=None,
               polymarket_service=None,information_service=None,ledger=None,structure_service=None,filtered_results_service=None,
               game_information_service=None,availability_service=None,draftkings_services=None,nba_pace_service=None,
               nba_aode_service=None,nba_four_factors_service=None,playing_time_rotation_service=None,
               injury_lineup_service=None):
    service = service or DataService()
    history_updater=HistoryUpdater(data=service)
    filtered_results_service = filtered_results_service or FilteredResults(service.path.with_name('filtered_results.sqlite3'))
    information_service=information_service or InformationService()
    availability_service=availability_service or AvailabilityAdjuster(data=service)
    game_information_service=game_information_service or GameInformationService(information_service)
    ledger=ledger or Ledger()
    in_game_service = in_game_service or InGameService(data=service)
    kalshi_service = kalshi_service or KalshiService(background=True)
    polymarket_service = polymarket_service or PolymarketService(background=True)
    structure_service = structure_service or MarketStructure(kalshi=kalshi_service,polymarket=polymarket_service)
    market_page_tasks=PageTasks(capacity=16)
    career_tasks=PageTasks(capacity=8)
    filter_jobs=FilterJobs()
    exchange_pages={name:QuotePages(source) for name,source in [('Kalshi',kalshi_service),('Polymarket',polymarket_service)]
                    if getattr(source,'background',False) is True}
    # Only configure the backend we create. Injected services own their inputs,
    # clocks and refresh policy, including offline and historical replays.
    backtesting_service=backtesting_service or BacktestingService(data=service,isolated=True,
        history_updater=history_updater if isinstance(service,DataService) else None)
    career_service = career_service or CareerService(service.path.with_name("career.sqlite3"))
    schedule_service = schedule_service or ScheduleService(service.path.with_name("schedules.sqlite3"),background=True)
    odds_service = odds_service or OddsApiService()
    ppp_service = ppp_service or PPPMarketService()
    epa_service = epa_service or EPAMarketService()
    nba_ppp_service = nba_ppp_service or NBAPPPMarketService()
    nba_epa_service = nba_epa_service or NBAEPAMarketService()
    elo_service = elo_service or ELOMarketService()
    nba_elo_service = nba_elo_service or NBAELOMarketService()
    nba_pace_service = nba_pace_service or NBAPaceMarketService()
    pace_data = nba_pace_service.data if isinstance(nba_pace_service, NBAPaceMarketService) else None
    nba_aode_service = nba_aode_service or NBAAdjustedEfficiencyMarketService(data=pace_data)
    nba_four_factors_service = nba_four_factors_service or NBAFourFactorsMarketService(data=pace_data)
    whp_service = whp_service or WHPMarketService()
    usage_opportunity_service = usage_opportunity_service or UsageOpportunityMarketService()
    opponent_adjusted_service = opponent_adjusted_service or OpponentAdjustedMarketService()
    pace_game_environment_service = pace_game_environment_service or PaceGameEnvironmentMarketService()
    playing_time_rotation_service = playing_time_rotation_service or PlayingTimeRotationMarketService(
        availability=getattr(availability_service,'store',None))
    injury_lineup_service = injury_lineup_service or InjuryLineupMarketService(
        availability=getattr(availability_service,'store',None))
    ncaaf_model_data = ncaaf_model_data or NcaafModelData()
    ncaaf_forecast_services = ncaaf_forecast_services or {
        method:NCAAFMarketForecastService(method,data=ncaaf_model_data) for method in ['PPP','EPA','ELO']}
    game_services={
        'NBA':{'PPP':nba_ppp_service,'EPA':nba_epa_service,'ELO':nba_elo_service,
               'PACE':nba_pace_service,'AODE':nba_aode_service,'FF':nba_four_factors_service},
        'NFL':{'PPP':ppp_service,'EPA':epa_service,'ELO':elo_service},
        'NCAAF':ncaaf_forecast_services,
    }
    data_directory = PROJECT_DIRECTORY / "data"
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
    if draftkings_services is None:
        draftkings_services = {
            sport: service_type(odds_service,service.path.with_name(sport.lower()+"_markets.sqlite3"),
                                data_directory/"market_data",usage_log=api_usage_log,bookmaker='draftkings')
            for sport,service_type in [('NFL',NflMarketService),('NBA',NbaMarketService),('NCAAF',NcaafMarketService)]
        }
    app = Dash(__name__, assets_folder=str(APP_DIRECTORY / "assets"),
               title="Sports Analytics", update_title=None)
    register_lazy_markets(app)
    quick_filter_monitor.register(app, filter_jobs.tasks)
    app._filter_jobs=filter_jobs
    app._page_tasks=market_page_tasks
    app._career_tasks=career_tasks

    register_team_logos(app)

    research_menus=to_json({sport:research_view.navigation(sport) for sport in SOURCES})
    app.clientside_callback('function(sport){const menus='+research_menus+';return menus[sport]||[];}',
                            Output('research-navigation','children'),Input('research-sport','value'))

    app.layout = html.Div([
        dcc.Interval(id='history-update-poll',interval=60_000,n_intervals=0),
        html.Div(id='history-update-status',role='status',className='forecast-history'),
        dcc.Store(id="dataset"), dcc.Download(id="download"),
        dcc.Store(id='historical-cache-pending',data=False),dcc.Interval(id='historical-cache-poll',interval=500,disabled=True),
        dcc.Store(id='historical-render-scope'),dcc.Store(id='career-job'),
        dcc.Interval(id='career-poll',interval=500,n_intervals=0,disabled=True),
        dcc.Store(id='nav-active'),*[dcc.Store(id='nav-'+page+suffix) for page in PAGES for suffix in ('','-active')],
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
        html.Button('Skip to page content',id='skip-navigation',className='skip-navigation'),
        html.Header([
            html.Div("RX", className="brand-mark", role="img", **{"aria-label": "RX"}),
            html.Span("Sports Analytics", className="brand-name"),
        ]),
        html.Main([
            dcc.Tabs(id="sport", value="NBA", mobile_breakpoint=0, children=[
                dcc.Tab(label="NBA · Basketball", value="NBA", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NFL · Pro Football", value="NFL", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="NCAAF · College Football", value="NCAAF", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label="Odds API", value="ODDS_API", className="sport-tab", selected_className="sport-tab selected"),
                dcc.Tab(label='Strategy & Research',value='RESEARCH',className='sport-tab',selected_className='sport-tab selected'),
                dcc.Tab(label='P&L History',value='PNL',className='sport-tab',selected_className='sport-tab selected'),
            ], className="sport-tabs"),
            html.Div([
                html.Aside([
                    html.Nav([
                        html.Details([
                            html.Summary('Historical Data',id='historical-page-link',className='active'),
                            html.Div([
                                html.Div(id='historical-navigation'),
                                html.A('Visualized In-Game Data',id='in-game-page-link',href='#nba-in-game-data',
                                       className='sport-page-link'),
                            ],className='sidebar-subpages'),
                        ],id='historical-menu',open=False,className='sidebar-group'),
                        html.Details([
                            html.Summary('Market Data'),
                            html.Div([
                                html.A('FanDuel',id='market-page-link',href='#nba-games',className='sport-page-link'),
                                html.A('DraftKings',id='draftkings-page-link',href='#nba-draftkings-market',className='sport-page-link'),
                                html.A('Kalshi',id='kalshi-page-link',href='#nba-kalshi-market',className='sport-page-link'),
                                html.A('Polymarket',id='polymarket-page-link',href='#nba-polymarket-market',className='sport-page-link'),
                                html.A('Market Summary',id='market-summary-page-link',href='#nba-market-summary',className='sport-page-link'),
                                html.A('Filtered Results',id='filtered-results-page-link',href='#nba-filtered-results',className='sport-page-link'),
                            ],className='sidebar-subpages'),
                        ],id='market-data-navigation',open=False,className='sidebar-group'),
                        html.Details([
                            html.Summary('Backtesting',id='backtesting-page-link'),
                            html.Div(id='backtesting-navigation',className='sidebar-subpages'),
                        ],id='backtesting-menu',open=False,className='sidebar-group'),
                        html.Div(research_view.navigation(),id='research-navigation',style={'display':'none'}),
                        information_navigation(),
                    ], className="page-toolbar", **{"aria-label": "Sport pages"}),
                    html.Div([game_data_monitor.layout(app.get_asset_url), quick_filter_monitor.layout(),
                              backtesting_monitor_layout()],
                             id='dashboard-monitors', className='dashboard-monitors'),
                ], className='dashboard-sidebar', **{'aria-label':'Dashboard sidebar'}),
                html.Div([
                    html.Div([
                        html.P(id='navigation-feedback',className='navigation-feedback',role='status',**{'aria-live':'polite'}),
                        html.P(id='market-return-status',className='market-return-status chart-note',role='status'),
                        html.Div([html.Span(indicator('Loading the selected market…'),className='market-transition-loading'),
                                  html.Span('This game is unavailable in the loaded catalog. Return to the market’s game list and refresh.',
                                            className='market-transition-unavailable')],
                                 id='market-transition-status',role='status',className='market-transition-status'),
                        historical_layout(),
                        market_layout(),
                        backtesting_layout(),
                        in_game_layout(),
                        kalshi_layout(),
                        polymarket_view.layout(),
                        market_summary_layout(),
                        filtered_results_layout(),
                        information_layout(),
                    ], id="sport-pages"),
                    odds_layout(),
                    pnl_layout(),
                ], className="page-content"),
            ], className="page-shell"),
            html.Footer("NBA / NFL / NCAAF · Displayed times in EDT (UTC−4)")
        ],id='dashboard-main',tabIndex=-1,**{'aria-label':'Sports analytics content'}),
        slip_layout(),
        structure_layout(),
    ], id='app-shell',className="app-shell")
    app.layout=configure_loading(configure_progressive(app.layout))
    register_navigation(app)
    register_loading(app)
    register_player_distribution(app)
    register_prop_probability(app)
    register_quick_visibility(app)
    register_progressive(app)

    register_shell(app,Output("historical-page", "style"), Output("market-page", "style"),
                  Output("fanduel-link", "href"), Output("market-timer", "disabled"),
                  Output("sport-pages", "style"), Output("odds-api-page", "style"), Output("odds-timer", "disabled"),
                  Output('backtesting-page','style'))

    register_sidebar(app,Output('historical-navigation','children'),Output('historical-navigation','style'),
                  Output('historical-page-link','className'),Output('market-page-link','className'),
                  Output('backtesting-page-link','className'),Output('market-page-link','href'))

    app.clientside_callback(
        '''function(hash,snapshot,category,ks,kc,ps,pc,gm,pm,kg,kp,context,kcontext,pcontext,saved){
          return window.dash_clientside.market_navigation.remember(hash,snapshot,category,saved,{
            market:{snapshot,category,method:gm,player_method:pm,context},
            kalshi:{snapshot:ks,category:kc,method:kg,player_method:kp,context:kcontext},
            polymarket:{snapshot:ps,category:pc,context:pcontext}});}''',
        Output('market-return','data'),Input('market-url','hash'),Input('market-snapshot','value'),
        Input('market-category','value'),Input('kalshi-snapshot','value'),Input('kalshi-category','value'),
        Input('polymarket-snapshot','value'),Input('polymarket-category','value'),
        Input('market-forecast-method','value'),Input('player-forecast-method','value'),
        Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),
        Input('market-detail-context','data'),Input('kalshi-context','data'),Input('polymarket-context','data'),State('market-return','data'))

    register_backtesting(app, backtesting_service)

    player_services = {"WHP": whp_service, "UO": usage_opportunity_service,
                       "OAM": opponent_adjusted_service, "PGE": pace_game_environment_service,
                       "PTR": playing_time_rotation_service, "ILA": injury_lineup_service}
    forecast_game_markets, forecast_player_markets, exchange_source_revision = register_markets(
        app, market_services, game_services, player_services, ncaaf_model_data=ncaaf_model_data,
        availability_service=availability_service, market_page_tasks=market_page_tasks,
        filter_jobs=filter_jobs, filtered_results_service=filtered_results_service,
    )

    register_odds_api(app, odds_service, api_usage_log, market_services)

    market_schedule.register(app, schedule_service, market_services)

    register_historical(app, service, career_service, career_tasks, ncaaf_model_data, backtesting_service)

    register_backtest_summary(app,backtesting_service)
    register_performance(app,backtesting_service)
    register_information(app,information_service)
    register_game_information(app,game_information_service)
    register_pnl(app,ledger)
    register_structure(app,structure_service)
    register_comparison_callbacks(app,backtesting_service)
    register_in_game_callbacks(app,in_game_service)
    register_kalshi_callbacks(app,kalshi_service,pages=exchange_pages.get('Kalshi'))
    register_kalshi_analysis(app,kalshi_service,forecast_game_markets,forecast_player_markets,filtered_results_service,
                            jobs=filter_jobs,tasks=market_page_tasks,source_revision=exchange_source_revision)
    register_kalshi_callbacks(app,polymarket_service,view=polymarket_view,provider='Polymarket',pages=exchange_pages.get('Polymarket'))
    polymarket_view.register_distributions(app,forecast_game_markets,forecast_player_markets,market_page_tasks)
    register_market_summary(app,schedule_service,market_services,kalshi_service,polymarket_service,forecast_game_markets,forecast_player_markets,
                            pages=exchange_pages,tasks=market_page_tasks,source_revision=exchange_source_revision,
                            draftkings=draftkings_services,quick_jobs=filter_jobs.tasks)
    register_filtered_results(app,filtered_results_service)
    app._history_updates=history_updater
    game_data_monitor.register(app, GameDataStatus(service, history_updater.root/'data/modeling'))

    @app.server.get('/api/history-updates')
    def history_update_health():
        return history_updater.status()

    @app.callback(Output('history-update-status','children'),Input('history-update-poll','n_intervals'))
    def history_update_status(_tick):
        state=history_updater.status()
        if not state.get('checked_at'):return ''
        failures=[name for name,record in state.get('sources',{}).items() if record.get('error')]
        if failures:
            return 'Historical data update needs attention: '+', '.join(failures)+'. Updates will retry automatically.'
        if (pd.Timestamp.now(tz='UTC')-pd.Timestamp(state['checked_at'])).total_seconds()>900:
            return 'Historical data updates are overdue. Last check '+format_eastern(state['checked_at'])+'.'
        return 'Historical data updates every 5 minutes · Last checked '+format_eastern(state['checked_at'])+'.'

    return app
