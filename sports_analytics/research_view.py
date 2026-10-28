"""Strategy & Research navigation, methodology and parameter editor."""
import re
from dash import dcc,html
from .backtesting_view import CATALOG,GROUPS,find_model,model_name
from .research_config import default_config,parameter_fields

METHODS={
    'ppp':[
        'Estimate scoring efficiency and volume separately, then combine them into home and away score means. Recent observations receive exponentially decaying calendar weights.',
        'NFL fits ridge-regularized offense and defense log effects with offensive-play exposure. A shared drive forecast and each side’s expected plays per drive determine volume; observed non-offensive scoring is modeled separately.',
        'NBA uses additive offense plus opposing defense minus league points per possession, with league shrinkage and a shared possession estimate. College fits total scoreboard points per offensive play on its saved school cohort.',
        'Fit earlier weeks or dates again as held-out forecasts. Their paired score errors supply bias correction, margin/total covariance and predictive intervals. Actual target-game pace and scores never enter a forecast.'
    ],
    'epa':[
        'Separate expected-points efficiency from realized scoreboard scoring. Learn team and opponent effects from earlier observations, then translate those effects and projected volume into score means.',
        'NFL estimates separate passing and rushing EPA effects with play-count exposure, forecasts pass mix, and fits a nonnegative scoring bridge from drives and EPA advantage to offensive points.',
        'NBA estimates expected remaining possession points from reconciled pre-action states. Saved per-game state sufficient statistics fit EPV coefficients, then opponent-adjusted signed possession residuals supply team efficiency effects.',
        'NCAAF uses provider game PPA, opponent-adjusted ratings and a fitted total-scoring-per-play bridge. Each sport calibrates uncertainty on its own earlier held-out score pairs.'
    ],
    'elo':[
        'Process completed games in time order. Freeze predictions for simultaneous games and publish Elo updates only once a result is available. Season carry shrinks ratings toward 1500 at season transitions.',
        'Fit an opponent-adjusted ridge power model to score margins and a separate positive offense/defense scoring model with projected drives, possessions or college plays. Elo alone supplies no total-point forecast.',
        'Earlier held-forward games fit a regularized logistic correction to Elo win probabilities and determine the blend between Elo-implied and power-model margins.',
        'Reconcile the blended margin with the scoring model’s total. Historical paired residuals determine bias corrections, covariance, and spread/total probabilities.'
    ],
    'whp':[
        'Build a player-specific chronological series for the selected statistic. Retain observed zero production; exclude missing measurements and any result not available at the forecast cutoff.',
        'Take the configured recent appearance window and assign exponential, calendar, linear or uniform weights. Disjoint older appearances can provide an empirical player prior, with shrinkage controlled by effective sample size and prior strength.',
        'When opportunities are observed, beta blends the weighted production estimate with a rate-per-opportunity projection. Beta 1 is the direct production benchmark; smaller values require complete workloads.',
        'Replay earlier appearances to calibrate predictive error variance. Normal, count or empirical predictive distributions generate intervals and optional Over-line probabilities. This predicts recorded appearances, rather than the probability that a rostered player participates.'
    ],
    'uo':[
        'Bootstrap recent observed team states with a shared quantile, preserving each team’s within-game production mix and linking matchup volume scenarios.',
        'Estimate player role and opportunity shares from complete historical appearances, using separate role and efficiency windows. Allocate finite integer team pools to modeled players and an explicit Other bucket.',
        'Draw conversion probabilities and event outcomes from observed successes, trials and efficiency residuals. NBA uses minutes and shot/box-score channels; NFL uses passing, rushing and receiving opportunities; college uses its dedicated observed-box adapter.',
        'The empirical integer simulation distribution supplies means, quantiles and win/loss/push probabilities. Simulation count and seed are configurable; repeated settings and saved inputs remain reproducible.'
    ],
    'oam':[
        'Start with the UO workload and efficiency simulation, then construct chronological component-level defensive actual-versus-expected records.',
        'Estimate offensive and league priors before each defensive observation. Neutralize historical opponent effects so a difficult previous schedule is not counted again as the player’s underlying ability.',
        'Shrink the opposing defense’s factors toward neutral, using defense, offense and positional prior strengths and effective sample sizes. Clip factors to the configured floor and ceiling.',
        'Apply each component factor once inside opportunity/conversion modeling. OAM supplies defensive adjustments to UO; it does not also enable the separate PGE pace environment.'
    ],
    'pge':[
        'Derive paired team volume observations and scoring scripts. NBA estimates possessions and duration from box scores; saved NFL play-by-play can separate regulation, overtime, sacks and other non-attempt pass plays.',
        'Fit a chronologically pooled ridge pace model from earlier own-team and opponent volume tendencies. Generate forward residuals before allowing each held-out date into the coefficient fit.',
        'Sample paired historical residual scenarios to create shared game volume and score environments. Role smoothing blends scenario-conditioned role selection with ordinary recency weighting.',
        'Feed the environment into finite UO allocations once, then simulate efficiencies and outcomes. NBA duration can extend minutes in overtime; college uses full-game play volume with overtime unseparated.'
    ],
}

SOURCES={
    'NBA':'Saved NBA Stats / SportsDataverse player and team box scores; paired estimated possessions for PPP/Elo; reconciled possession/event EPV state statistics for EPA.',
    'NFL':'Saved nflverse schedules, weekly player/team statistics and play-by-play. EPA requires signed passing/rushing EPA coverage; PPP/Elo use reconciled scoring and drive/play exposures.',
    'NCAAF':'Saved CollegeFootballData school/player game boxes and advanced PPA for the prepared school cohort. Priors describe that saved cohort, rather than an invented complete FBS population.',
}

HELP={
    'prior_season_weight':'Weight multiplier per older season, on top of recency. Default 0.2 gives an otherwise equal prior-season game one fifth of the current-season weight; two seasons ago gets 0.04. Set 1 to disable the season discount.',
    'half_life_days':'Calendar days required for an observation’s recency weight to halve.',
    'ridge_penalty':'Strength of regularization on fitted team effects.',
    'minimum_training_games':'Minimum distinct earlier games required for a ratings fit.',
    'minimum_team_games':'Minimum earlier appearances required for each matchup team.',
    'minimum_calibration_games':'Minimum valid held-forward games needed for score calibration.',
    'maximum_calibration_games':'Maximum most-recent held-forward errors retained.',
    'shrinkage_games':'Equivalent league-prior games stabilizing NBA efficiency and pace.',
    'bridge_penalty':'Regularization on the fitted efficiency-to-scoring conversion.',
    'state_ridge':'Regularization on NBA pre-action EPV state coefficients.',
    'team_ridge':'Regularization on NBA team possession-residual effects.',
    'k_factor':'Size of Elo updates after each available result.',
    'elo_scale':'Rating-point scale in the Elo logistic win probability.',
    'home_elo':'Home advantage in Elo rating points; neutral games omit it.',
    'season_carry':'Fraction of a team’s deviation from 1500 retained across a season.',
    'power_ridge':'Regularization on margin power ratings.',
    'scoring_ridge':'Regularization on positive offense/defense scoring effects.',
    'logistic_ridge':'Regularization toward identity for held-forward Elo probability calibration.',
    'window':'Maximum recent player appearances in the WHP point estimate.',
    'half_life':'WHP weight half-life in appearances, or days for calendar weighting.',
    'prior_strength':'Effective older-player prior strength for WHP shrinkage.',
    'beta':'Production weight in WHP’s production/workload blend; 0 to 1.',
    'minimum_games':'Minimum complete observed appearances per required feature family.',
    'minimum_calibration':'Minimum earlier WHP prediction errors required.',
    'maximum_calibration':'Maximum recent WHP calibration appearances retained.',
    'weighting':'How observations receive weights within the WHP window.',
    'distribution':'WHP predictive family; auto uses the statistic’s count/continuous classification.',
    'role_window':'Recent complete appearances used to estimate role and opportunity shares.',
    'role_half_life':'Appearance half-life for opportunity and role recency weights.',
    'efficiency_window':'Complete appearances used for conversion/efficiency estimation.',
    'efficiency_half_life':'Appearance half-life for conversion/efficiency recency weights.',
    'team_window':'Recent complete team games used for shared workload states.',
    'simulations':'Monte Carlo draws per matchup; validated range 1,000–200,000.',
    'seed':'Nonnegative reproducible random seed, combined with the matchup identity.',
    'defense_window':'Maximum earlier defensive component observations.',
    'defense_half_life':'Defensive appearance half-life for component weights.',
    'defense_prior':'Strength of shrinkage toward neutral defensive effects.',
    'offense_prior':'Strength of league shrinkage for neutral offensive expectations.',
    'position_prior':'Shrinkage strength for position-specific defensive factors.',
    'minimum_defense_games':'Minimum expected-versus-actual defensive observations.',
    'league_window':'Rolling league-prior / pace-scenario window; pace regression uses twice this many team rows.',
    'minimum_league_games':'Warmup requirement for league expectations and forward pace residuals.',
    'factor_floor':'Lower defensive-factor bound; greater than 0 and below 1.',
    'factor_ceiling':'Upper defensive-factor bound; greater than 1.',
    'pace_prior':'Effective league pooling strength for team pace features.',
    'ridge':'Regularization for the pace regression.',
    'scenario_half_life':'Recency half-life for paired game environments.',
    'role_smoothing':'Mixture of ordinary recency in scenario-conditioned role selection; greater than 0 through 1.',
    'sport':'Controlled by the research sport selector above.',
}


def route(value):
    match=re.fullmatch(r'#research-(game-outcome|player-stats)-([a-z0-9-]+)',value or '')
    return (match[1],match[2]) if match else None


def navigation(sport,location_hash):
    selected=route(location_hash)
    return [html.Details([html.Summary(label),html.Div([
        html.A(model.label,href=f'#research-{group}-{model.key}',
            className='backtesting-model-link'+(' active' if selected==(group,model.key) else ''),
            **{'aria-current':'page' if selected==(group,model.key) else 'false'})
        for model in CATALOG if model.group==group]+[
        html.A('Methodology Comparison',href=f'#research-{group}-comparison',
            className='backtesting-model-link'+(' active' if selected==(group,'comparison') else ''),
            **{'aria-current':'page' if selected==(group,'comparison') else 'false'})],className='backtesting-model-links')],
        open=bool(selected and selected[0]==group),className='backtesting-nav-group') for group,label in GROUPS.items()]


def page(sport,location_hash):
    selected=route(location_hash)
    if selected and selected[1]=='comparison':
        return html.Div([html.Div(f'STRATEGY & RESEARCH / {GROUPS[selected[0]].upper()}',className='eyebrow accent'),
            html.H2('Methodology Comparison'),html.P('Compare all methods or a selected subset on one sport. Run new simulations with shared evaluation filters, or compare previous saved simulations.'),
            html.P('New runs use each method’s sport-specific defaults. The common evaluated sample compares identical games/player appearances and outcomes; method-specific results expose coverage and skips.')],className='panel backtesting-heading')
    model=find_model(sport,route(location_hash))
    if model is None:
        return html.Div([html.H2('Strategy & Research'),html.P('Choose Game Outcome or Player Stats on the left, then select a strategy to study its methodology and test your own parameters.'),
            html.P('Experiments use saved historical observations and preserve the exact settings in each report.')],className='panel backtesting-heading')
    inputs=(model.inputs if model.group=='game-outcome' else
        'WHP requires dated player identities and observed target statistics. Complete observed workloads are additionally required when beta blends in opportunity-based estimates.' if model.key=='whp' else
        'This method requires matched player/team box scores, actual opponents, complete observed workload and efficiency channels, and enough earlier team/league history for its configured features.')
    return html.Div([
        html.Div([html.Div(f'STRATEGY & RESEARCH / {GROUPS[model.group].upper()}',className='eyebrow accent'),
            html.H2(model_name(model,sport)),html.P(model.description)],className='panel backtesting-heading'),
        html.Section([html.H3('How the methodology works'),html.Ol([html.Li(text) for text in METHODS[model.key]]),
            html.H3('Data used for '+sport),html.P(SOURCES[sport]),html.P(inputs),
            html.P('Current-season performance is emphasized: each older season receives a 0.2 multiplier in addition to recency weighting. Older observations still support minimum history and uncertainty calibration. Adjust prior season weight in the parameters below; Elo also exposes season carry.'),
            html.P('These experiments replay saved outcomes. Historical sportsbook quotes are not required; an optional fixed player reference line supports probability metrics.'),
            html.P('The sport-specific prepared sources and publication/availability lags define the eligible history. Evaluation dates and team selections restrict the test cohort; eligible league/opponent history still supports fitting. A lookback cap additionally bounds appearances used for estimation.'),
            html.H3('Reading the experiment'),html.P('Every forecast uses only observations strictly available before its historical cutoff. Game Outcome evaluates final margin and total; Player Stats evaluates the chosen statistic per observed appearance. All-player/team experiments use the last requested distinct games and keep each player’s training history separate.'),
            html.P('Compare MAE, RMSE, bias, interval coverage and probability calibration across saved runs. Parameters are research hypotheses: use a separate chronological evaluation interval when selecting settings. Missing inputs and insufficient warmup are reported as skips.'),
            html.A('Download full implementation documentation',href=f'/backtesting/docs/{model.group}/{model.key}',className='export-button backtesting-documentation'),
        ],className='panel research-methodology'),
    ],className='research-model-page',**{'data-model':model.key,'data-group':model.group,'data-sport':sport})


def editor(sport,group,key):
    config=default_config(sport,group,key);controls=[]
    choices={'weighting':['exponential','calendar','linear','uniform'],'distribution':['auto','normal','count','empirical']}
    for path,definition,value in parameter_fields(config):
        name=path.split('.')[-1];ident={'type':'research-parameter','path':path}
        if name=='sport':control=dcc.Dropdown(id=ident,options=[sport],value=sport,clearable=False,disabled=True)
        elif name in choices:control=dcc.Dropdown(id=ident,options=choices[name],value=value,clearable=False)
        else:control=dcc.Input(id=ident,type='number',value=value,step=1 if definition.type is int else 'any',debounce=True)
        controls.append(html.Div([html.Label(path.replace('.',' / ').replace('_',' ').capitalize()),control,
            html.Small(HELP.get(name,'Model configuration value.'))],className='field'))
    return controls
