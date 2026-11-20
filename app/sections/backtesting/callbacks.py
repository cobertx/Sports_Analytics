"""Backtesting and research page callbacks."""

from dash import ALL, Input, Output, State, ctx, dcc, no_update
from dash.exceptions import PreventUpdate
from flask import abort, send_from_directory
import pandas as pd
from sports_analytics.data import DataSourceError, SOURCES
from app.sections.backtesting.backtesting_view import backtesting_route, find_model, page as backtesting_page
from sports_analytics.backtesting_service import default_evaluation_games, default_lookback, export_filename, nfl_backtest_criteria
from app.sections.backtesting.backtesting_results import render as render_backtesting_results, empty as empty_backtesting_results
from app.sections.backtesting.backtesting_results import evaluated_records
from app.sections.backtesting.backtesting_monitor import contents as backtesting_monitor_contents
from app.sections.research import research_view
from app.sections.research.callbacks import register as register_research
from sports_analytics.research_config import form_parameters
from app.shell.navigation import scoped_callback
from sports_analytics.display_time import format_eastern


def register(app, backtesting_service):
    @app.server.get('/backtesting/docs/<group>/<model_key>')
    def backtesting_documentation(group,model_key):
        model=find_model('NBA',(group,model_key))
        if model is None or not (model.directory/'README.md').is_file():abort(404)
        return send_from_directory(model.directory,'README.md',as_attachment=True,
                                   download_name=f'{model.folder}_README.md')

    @scoped_callback(app,'backtesting',Output('backtesting-content','children'),Output('backtest-evaluation-panel','style'),
                  Input('workspace-page','value'),Input('sport','value'),
                  Input('market-url','hash'),Input('research-sport','value'))
    def show_backtesting(page,sport,location_hash,research_sport='NBA'):
        if sport=='RESEARCH':
            model=find_model(research_sport,research_view.route(location_hash))
            return research_view.page(research_sport,location_hash),{'display':'block' if model else 'none'}
        if page!='backtesting' or sport not in SOURCES:
            return no_update,{'display':'none'}
        model=find_model(sport,backtesting_route(location_hash))
        return backtesting_page(sport,location_hash),{'display':'block' if model else 'none'}

    register_research(app, backtesting_service)

    @scoped_callback(app,'backtesting',Output('backtest-context','data'),Output('backtest-entity','options'),Output('backtest-entity','value'),
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
            note=('NBA game backtests reuse saved inputs and download missing older history automatically. No Odds API credits are used.'
                  if sport=='NBA' and not player else 'Saved inputs only. Missing history is reported; source and odds APIs are not called.')
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
                  Input('backtest-playoffs','value'),Input('backtest-result','modified_timestamp'))
    def backtest_available(context,entity,market,start,end,playoffs,_updated=None):
        if not context or entity is None:return 'Select a team or player to count saved games.'
        try:
            extra=dict(team_filter=context.get('team_filter')) if context.get('workspace')=='research' else {}
            count=backtesting_service.available_games(context['sport'],context['group'],context['model'],entity,
                market,start,end,playoffs=='include',**extra)
            note=(' '+nfl_backtest_criteria(market)
                  if context['sport']=='NFL' and context['group']=='player-stats' else '')
            scope=('all teams' if entity=='__all__' else entity) if context['group']=='game-outcome' else (
                context.get('team_filter') or ('all players' if entity=='__all__' else 'this player'))
            return (f'{count:,} saved games available for {scope} with these filters. '
                    'Backtest Data Games selects the latest games within this scope. '
                    +('Missing older seasons and earlier training history download automatically when you run an NBA game backtest. '
                      if context['sport']=='NBA' and context['group']=='game-outcome' else
                      'If you request more than are saved, load more historical data. ')+
                    'Insufficient training history may cause skips.'+note)
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

    @scoped_callback(app,'backtesting',Output('backtest-results','children'),Output('backtest-export','disabled'),
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
