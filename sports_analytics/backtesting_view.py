"""Sport-specific model pages and the nested Backtesting navigation.

The catalog maps installed model packages to their public names. Rendering
reads configuration and saved-file counts. Backtest execution is explicit.
"""
from dataclasses import dataclass,asdict
from importlib import import_module
from pathlib import Path
import re
from dash import dcc,html

ROOT=Path(__file__).resolve().parents[1]
SPORTS={'NBA','NFL','NCAAF'}
GROUPS={'game-outcome':'Game Outcome','player-stats':'Player Stats'}
PERFORMANCE_ROUTE='#backtesting-performance'


@dataclass(frozen=True)
class BacktestingModel:
    group:str
    key:str
    folder:str
    label:str
    description:str
    evaluation:str
    inputs:str

    @property
    def directory(self):
        return ROOT/'models'/('Game_Outcome' if self.group=='game-outcome' else 'Player_Stats')/self.folder

    @property
    def href(self):
        return f'#backtesting-{self.group}-{self.key}'


CATALOG=(
    BacktestingModel('game-outcome','ppp','PPP','Points Per Play (PPP)',
        'Project each team’s scoring rate and game volume to estimate final scores, spreads and totals.',
        'Replay games chronologically. Fit each forecast from earlier available games, then compare projected '
        'margins and totals with the completed result. Held-forward errors determine predictive dispersion.',
        'Completed paired team games, scoring, offensive/defensive volume, opponents and availability dates.'),
    BacktestingModel('game-outcome','epa','EPA','Expected Points Added (EPA)',
        'Translate expected-points efficiency into projected team scores and spread/total probabilities.',
        'Keep expected-value training and score calibration before each test game. Evaluate future margin '
        'and total errors using the same sport-specific expected-points inputs as the market forecast.',
        'NFL play-by-play EPA; NBA reconciled possession/event EPV; or college CFBD game PPA, '
        'plus team volume, opponents, final scores and availability dates.'),
    BacktestingModel('game-outcome','elo','ELO','Elo / Power Rating (ELO)',
        'Combine sequential Elo ratings, point-margin power ratings and a scoring model for game lines.',
        'Update ratings after each available result. Reconcile Elo and point margins using earlier '
        'calibration games, then score held-forward margins and totals.',
        'Chronological completed team results, home/neutral venue, opponents, scoring and game volume.'),
    BacktestingModel('player-stats','whp','Weighted_Historical_Performance','Weighted Historical Performance (WHP)',
        'Weight recent player appearances and estimate uncertainty from earlier forecast errors.',
        'Walk forward one appearance at a time. Predict each game from prior observed performances, '
        'then report MAE, RMSE and bias. Supplied prop lines also support Brier score and log loss.',
        'Observed player/game identities, target statistics, dates and availability times. '
        'Market comparisons also require saved pregame lines and final player outcomes.'),
    BacktestingModel('player-stats','uo','Usage_Opportunity','Usage & Opportunity (UO)',
        'Simulate team workload, player opportunity shares and conversion efficiency for player props.',
        'Replay timestamped pregame snapshots against final player statistics. Evaluate point errors, '
        'predictive intervals, settlement probabilities and calibration with fixed model settings.',
        'Matched player/team box scores, observed workload and efficiency channels, saved pregame '
        'quotes, and final player/game/market outcomes.'),
    BacktestingModel('player-stats','oam','Opponent_Adjusted_Matchup','Opponent-Adjusted Matchup (OAM)',
        'Neutralize prior matchups and apply schedule-adjusted opposing defense to opportunity and efficiency.',
        'Build defensive component factors from earlier available games in every replay. Evaluate '
        'the same saved player quotes and outcomes used for WHP/UO comparison.',
        'Player/team production and exposures, actual opponent identities, enough prior defensive '
        'history, pregame quotes and final player outcomes.'),
    BacktestingModel('player-stats','pge','Pace_Game_Environment','Pace & Game Environment (PGE)',
        'Condition player workloads on projected game pace and shared scoring-script scenarios.',
        'Fit pace on earlier games and sample paired held-forward errors. Replay player props '
        'without using the test game’s realized opportunities, pace or scoring script.',
        'Paired team volume and scoring margins, player roles and efficiency, pregame quotes, '
        'and final player outcomes. Saved NFL play-by-play separates regulation and overtime.'),
)


def available_models(sport,group=None):
    if sport not in SPORTS:return []
    return [model for model in CATALOG if (group is None or model.group==group)
            and (model.directory/'__init__.py').is_file()]


def model_name(model,sport):
    return 'Points Per Possession (PPP)' if model.key=='ppp' and sport=='NBA' else model.label


def backtesting_route(value):
    match=re.fullmatch(r'#backtesting-(game-outcome|player-stats)-([a-z0-9-]+)',value or '')
    return (match[1],match[2]) if match else None


def summary_route(value):
    if value=='#backtesting-summary':return dict(sport=None,run_id=None)
    match=re.fullmatch(r'#backtesting-summary-(nba|nfl|ncaaf)-(game-outcome|player-stats)-([a-z]+)-([0-9a-f]{64})',value or '')
    if match and find_model(match[1].upper(),(match[2],match[3])):
        return dict(sport=match[1].upper(),group=match[2],model=match[3],run_id=match[4])
    return None


def find_model(sport,route):
    return next((model for model in available_models(sport) if route==(model.group,model.key)),None)


def navigation(sport,location_hash):
    route=backtesting_route(location_hash)
    summary=bool(summary_route(location_hash))
    return [html.A('Backtest Summary',href='#backtesting-summary',
                   className='backtesting-model-link'+(' active' if summary else ''),
                   **{'aria-current':'page' if summary else 'false'}),
            html.A('Performance Summary',href=PERFORMANCE_ROUTE,
                   className='backtesting-model-link'+(' active' if location_hash==PERFORMANCE_ROUTE else ''),
                   **{'aria-current':'page' if location_hash==PERFORMANCE_ROUTE else 'false'}),*[html.Details([
        html.Summary(label),
        html.Div([html.A(model_name(model,sport),href=model.href,
            className='backtesting-model-link'+(' active' if route==(group,model.key) else ''),
            **{'aria-current':'page' if route==(group,model.key) else 'false'})
            for model in available_models(sport,group)],className='backtesting-model-links'),
    ],open=bool(route and route[0]==group),className='backtesting-nav-group') for group,label in GROUPS.items()]]


def model_config(model,sport):
    if model.group=='player-stats':
        module,name={'whp':('core','WHPConfig'),'uo':('config','UsageOpportunityConfig'),
                     'oam':('config','MatchupConfig'),'pge':('config','PaceConfig')}[model.key]
        config=getattr(import_module(f'models.Player_Stats.{model.folder}.{module}'),name).for_sport(sport)
    else:
        module,name={
            ('ppp','NBA'):('PPP.NBA_PPP','NBAPPPModel'),('ppp','NFL'):('PPP.PPP','PPPModel'),
            ('ppp','NCAAF'):('PPP.NCAAF_PPP','NCAAFPPPModel'),
            ('epa','NBA'):('EPA.NBA_EPA','NBAEPAModel'),('epa','NFL'):('EPA.EPA','EPAModel'),
            ('epa','NCAAF'):('EPA.NCAAF_EPA','NCAAFEPAModel'),
            ('elo','NBA'):('ELO.ELO','ELOModel'),('elo','NFL'):('ELO.ELO','ELOModel'),
            ('elo','NCAAF'):('ELO.NCAAF_ELO','NCAAFELOModel'),
        }[model.key,sport]
        imported=import_module('models.Game_Outcome.'+module)
        config=(getattr(imported,name)(imported.ELOConfig.nba()).config if model.key=='elo' and sport=='NBA'
                else getattr(imported,name)().config)
    return asdict(config)


def settings_rows(values,prefix=''):
    rows=[]
    for key,value in values.items():
        label=(prefix+' / ' if prefix else '')+key.replace('_',' ').capitalize()
        if isinstance(value,dict):rows.extend(settings_rows(value,label))
        else:rows.append(html.Tr([html.Th(label,scope='row'),html.Td(str(value))]))
    return rows


def overview(sport):
    return html.Div([
        html.Div([html.Div(f'{sport} · BACKTESTING',className='eyebrow accent'),
            html.H2('Choose a model to evaluate'),
            html.P('Explore Game Outcome and Player Stats models using the menu or the cards below.')],
            className='panel backtesting-heading'),
        *[html.Section([
            html.H3(label),
            html.Div([html.A([html.Strong(model_name(model,sport)),html.P(model.description),
                html.Span('Open model →',className='backtesting-card-action')],href=model.href,
                className='panel backtesting-model-card') for model in available_models(sport,group)],
                className='backtesting-model-grid'),
        ],className='backtesting-model-group') for group,label in GROUPS.items()]],className='backtesting-overview')


def model_page(sport,model):
    snapshots=sum(1 for _ in (ROOT/'data/market_data').glob(sport.lower()+'_*.csv'))
    inputs=model.inputs
    if model.key=='epa':
        inputs={'NBA':'Reconciled possession/event EPV, team volume, opponents, final scores and availability dates.',
                'NFL':'Play-by-play EPA, team volume, opponents, final scores and availability dates.',
                'NCAAF':'CFBD game PPA, team volume, opponents, final scores and availability dates.'}[sport]
    if model.key=='pge' and sport!='NFL':
        inputs=inputs.replace(' Saved NFL play-by-play separates regulation and overtime.','')
    college=(' College uses school-scoped CFBD history. Receiver roles use completion shares.'
             if sport=='NCAAF' and model.group=='player-stats' and model.key!='whp' else '')
    if sport=='NCAAF' and model.key=='pge':college+=' Volume includes unseparated overtime.'
    return html.Div([
        html.A('← All backtesting models',href='#backtesting',className='back-to-games'),
        html.Div([html.Div(f'{sport} / BACKTESTING / {GROUPS[model.group].upper()}',className='eyebrow accent'),
            html.H2(model_name(model,sport)),html.P(model.description),
            html.A('Download model documentation',href=f'/backtesting/docs/{model.group}/{model.key}',
                   className='export-button backtesting-documentation')],className='panel backtesting-heading'),
        html.Details([html.Summary('Model methodology and current settings'),html.Div([
            html.Section([html.H3('Evaluation approach'),html.P(model.evaluation),
                html.P('Use only observations available before each forecast cutoff. Keep final outcomes '
                       'separate from forecast inputs.')],className='panel backtesting-info'),
            html.Section([html.H3('Required inputs'),html.P(inputs+college),
                html.P(f'{snapshots:,} saved {sport} market snapshot'+('s' if snapshots!=1 else '')+' available locally. '
                       'Market replay also needs matching completed outcomes; saved odds alone are not results.')],
                className='panel backtesting-info'),
        ],className='backtesting-info-grid'),
        html.Section([html.Div([html.H3('Current model settings'),
            html.Span(sport,className='eyebrow accent')],className='panel-heading'),
            html.P('Defaults from the model used for this sport’s forecasts.',className='chart-note'),
            html.Div(html.Table([html.Thead(html.Tr([html.Th('Setting',scope='col'),html.Th('Value',scope='col')])),
                html.Tbody(settings_rows(model_config(model,sport)))],className='backtesting-settings'),
                className='backtesting-settings-scroll'),
        ],className='panel backtesting-settings-panel'),
        ],className='backtesting-model-notes'),
    ],className='backtesting-model-page',**{'data-model':model.key,'data-group':model.group,'data-sport':sport})


def page(sport,location_hash):
    if summary_route(location_hash) or location_hash==PERFORMANCE_ROUTE:return []
    model=find_model(sport,backtesting_route(location_hash))
    return model_page(sport,model) if model else overview(sport)
