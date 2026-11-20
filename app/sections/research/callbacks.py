"""Research parameter and team selection callbacks."""

from dash import Input, Output, State, ctx, no_update

from app.sections.research import research_view
from app.shell.navigation import register_research_visibility, scoped_callback
from sports_analytics.data import DataSourceError


def register(app, backtesting_service):
    register_research_visibility(app,Output('historical-menu','style'),Output('backtesting-menu','style'),
                  Output('research-navigation','style'),Output('research-setup','style'),Output('research-team-field','style'))

    @app.callback(Output('research-parameters','children'),Output('research-parameter-note','children'),
                  Output('research-editor-context','data'),Input('backtest-context','data'),Input('research-reset','n_clicks'),
                  State('research-editor-context','data'))
    def research_parameters(context,_reset,previous_key=None):
        if not context or context.get('workspace')!='research':return [],'Choose a strategy from the left to edit its parameters.',None
        key=[context[k] for k in ['sport','group','model']]
        if key==previous_key and ctx.triggered_id!='research-reset':return no_update,no_update,no_update
        controls=research_view.editor(context['sport'],context['group'],context['model'])
        return controls,f'{len(controls)} configuration fields · sport-specific defaults. Evaluation controls below restrict the test sample; model parameters govern estimation and simulation.',key

    @scoped_callback(app,'backtesting',Output('research-team','options'),Output('research-team','value'),
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
