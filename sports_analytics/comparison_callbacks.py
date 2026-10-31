"""Dash orchestration for persistent, independently running method comparisons."""
import json

import pandas as pd
from .navigation import scoped_callback

from dash import ALL,Input,Output,State,ctx,dcc,html,no_update
from dash.exceptions import PreventUpdate

from .comparison import Comparisons,methods,page_route,selected_methods,compare_reports
from .comparison_view import render
from .backtesting_results import render as full_report,field
from .data import DataSourceError
from .display_time import format_eastern


def register(app,backtesting):
    comparisons=Comparisons(backtesting)

    @scoped_callback(app,'backtesting',Output('research-comparison','style'),Output('research-model-settings','style'),
                  Output('comparison-context','data'),Output('comparison-methods','options'),Output('comparison-methods','value'),
                  Output('comparison-team-field','style'),Output('comparison-statistic-field','style'),
                  Output('comparison-line-field','style'),Output('comparison-target-field','style'),Output('comparison-entity-label','children'),
                  Input('sport','value'),Input('market-url','hash'),Input('research-sport','value'))
    def configure_comparison(tab,location_hash,sport):
        context=page_route(tab,location_hash,sport);hidden={'display':'none'}
        if not context:return hidden,{},None,[],[],hidden,hidden,hidden,hidden,'Team'
        player=context['group']=='player-stats'
        models=methods(context['group']);player_style={'display':'block' if player else 'none'}
        return ({'display':'block'},hidden,context,[dict(label=m.label,value=m.key) for m in models],[m.key for m in models],
                player_style,player_style,player_style,{'display':'none' if player else 'block'},'Player cohort' if player else 'Team')

    @app.callback(Output('comparison-methods','value',allow_duplicate=True),Input('comparison-all','n_clicks'),
                  State('comparison-context','data'),prevent_initial_call=True)
    def comparison_select_all(clicks,context):
        if not clicks or not context:raise PreventUpdate
        return [m.key for m in methods(context['group'])]

    @app.callback(Output('comparison-entity','options'),Output('comparison-entity','value'),
                  Output('comparison-team','options'),Output('comparison-team','value'),Output('comparison-input-status','children'),
                  Input('comparison-context','data'),State('comparison-entity','value'),State('comparison-team','value'))
    def comparison_entities(context,previous,previous_team):
        all_teams=[dict(label='All teams',value='__all__')]
        if not context:return [],None,all_teams,'__all__',''
        options={};errors=[];teams=all_teams
        if context['group']=='game-outcome':
            options['__all__']=dict(label='All teams',value='__all__')
            for model in methods(context['group']):
                try:
                    entities,_,_=backtesting.options(context['sport'],context['group'],model.key)
                    for entity in entities:options[entity['value']]=entity
                except (DataSourceError,OSError,ValueError,TypeError) as exc:errors.append(model.key.upper()+': '+str(exc))
        else:
            options['__all__']=dict(label='All players',value='__all__')
            try:
                entities,_,frame=backtesting.options(context['sport'],context['group'],'whp','__all__')
                for entity in entities:options[entity['value']]=entity
                teams+= [dict(label=team,value=team) for team in sorted(frame.team.dropna().astype(str).unique())]
            except (DataSourceError,OSError,ValueError,TypeError) as exc:errors.append(str(exc))
        value=previous if previous in options else '__all__'
        team=previous_team if previous_team in {o['value'] for o in teams} else '__all__'
        return list(options.values()),value,teams,team,('Saved inputs only. '+(' · '.join(errors) if errors else 'All methods use the shared filters below.'))

    @app.callback(Output('comparison-statistic','options'),Output('comparison-statistic','value'),
                  Input('comparison-context','data'),Input('comparison-methods','value'),Input('comparison-entity','value'),
                  State('comparison-statistic','value'))
    def comparison_statistics(context,keys,entity,previous):
        if not context or context['group']!='player-stats' or entity is None:return [],None
        try:
            keys=selected_methods(context['group'],keys)
            choices=[backtesting.options(context['sport'],context['group'],key,entity)[1] for key in keys]
            shared=set.intersection(*({o['value'] for o in options} for options in choices))
            options=[o for o in choices[0] if o['value'] in shared]
        except (DataSourceError,OSError,ValueError,TypeError):return [],None
        return options,previous if previous in shared else options[0]['value'] if options else None

    @app.callback(Output('comparison-saved-selectors','children'),Input('comparison-context','data'),
                  Input('comparison-methods','value'),Input('comparison-history-panel','open'),
                  State({'type':'comparison-saved-run','method':ALL},'id'),State({'type':'comparison-saved-run','method':ALL},'value'))
    def comparison_saved_selectors(context,keys,opened,identities,values):
        if not context or not opened:return []
        previous={item['method']:value for item,value in zip(identities or [],values or [])}
        try:keys=selected_methods(context['group'],keys)
        except ValueError:return html.P('Select methods above to choose previous simulations.',className='backtest-status')
        controls=[]
        for model in methods(context['group']):
            if model.key not in keys:continue
            reports=backtesting.history(context['sport'],context['group'],model.key)
            options=[dict(label=f"{format_eastern(r['created_at'])} · {r['entity_label']} · {r.get('market') or 'Game outcomes'} · {r['evaluated']} evaluated · {r['run_id'][:8]}",
                          value=r['run_id']) for r in reports]
            controls.append(field(model.label,dcc.Dropdown(id={'type':'comparison-saved-run','method':model.key},options=options,
                value=previous.get(model.key) if previous.get(model.key) in {o['value'] for o in options} else None,
                placeholder='Choose a saved simulation')))
        return controls

    @app.callback(Output('comparison-history','options'),Output('comparison-history','value'),Input('comparison-context','data'),
                  Input('comparison-history-panel','open'),Input('comparison-state','data'),State('comparison-history','value'))
    def previous_comparisons(context,opened,_state,previous):
        if not context or not opened:return [],None
        options=[dict(label=f"{format_eastern(m['created_at'])} · {', '.join(k.upper() for k in m['entries'])} · {m['source']}",value=m['id'])
                 for m in comparisons.history(context)]
        return options,previous if previous in {o['value'] for o in options} else None

    @app.callback(Output('comparison-state','data'),Output('comparison-status','children'),Output('comparison-pages','data'),Output('comparison-poll','disabled'),
                  Input('comparison-run','n_clicks'),Input('comparison-context','data'),Input('comparison-poll','n_intervals'),
                  Input('comparison-history-open','n_clicks'),Input('comparison-saved-open','n_clicks'),
                  State('comparison-methods','value'),State('comparison-entity','value'),State('comparison-team','value'),
                  State('comparison-statistic','value'),State('comparison-start','date'),State('comparison-end','date'),
                  State('comparison-limit','value'),State('comparison-playoffs','value'),State('comparison-lookback','value'),
                  State('comparison-tolerance','value'),State('comparison-line','value'),State('comparison-state','data'),
                  State('comparison-pages','data'),State('comparison-history','value'),
                  State({'type':'comparison-saved-run','method':ALL},'id'),State({'type':'comparison-saved-run','method':ALL},'value'))
    def run_comparison(clicks,context,tick,history_clicks,saved_clicks,keys,entity,team,market,start,end,limit,playoffs,lookback,
                       tolerance,line,current,pages,history_id,saved_ids,saved_values):
        if not context:return None,'',no_update,True
        previous=pages or {};pages=dict(previous);page_key=context['sport']+'/'+context['group']
        try:
            triggered=ctx.triggered_id
            if triggered=='comparison-run':
                if not clicks:raise PreventUpdate
                manifest=comparisons.launch(context,keys,entity=entity,market=market,team_filter=team if team!='__all__' else None,
                    start=start,end=end,limit=limit,tolerance=tolerance,line=line,include_playoffs=playoffs=='include',lookback=lookback)
            elif triggered=='comparison-saved-open':
                if not saved_clicks:raise PreventUpdate
                refs={item['method']:value for item,value in zip(saved_ids or [],saved_values or [])}
                manifest=comparisons.from_saved(context,keys,refs)
            else:
                identity=history_id if triggered=='comparison-history-open' else pages.get(page_key)
                if triggered=='comparison-history-open' and not identity:raise ValueError('Choose a saved comparison first.')
                if not identity:
                    history=comparisons.history(context)
                    identity=history[0]['id'] if history else None
                manifest=comparisons.refresh(context,identity) if identity else None
            if not manifest:
                return None,'Choose methods and run a comparison, or open previous simulations.',no_update,True
            pages[page_key]=manifest['id']
            active=sum(e['state'] in {'queued','running'} for e in manifest['entries'].values())
            complete=sum(e['state']=='completed' for e in manifest['entries'].values())
            failed=len(manifest['entries'])-active-complete
            status=f"{complete} of {len(manifest['entries'])} methods completed · {active} running · {failed} failed/interrupted."
            if active:status+=' Simulations continue when you visit another page.'
            elif complete:status+=' Comparison and method reports saved locally.'
            if triggered=='comparison-poll' and manifest==current:raise PreventUpdate
            return manifest,status,pages if pages!=previous else no_update,not bool(active)
        except (ValueError,TypeError,OSError,DataSourceError) as exc:
            return no_update,'Check comparison settings: '+str(exc),no_update,no_update

    @app.callback(Output('comparison-progress','children'),Input('comparison-state','data'))
    def comparison_progress(manifest):
        if not manifest:return []
        return [html.Div([html.Strong(key.upper()),html.Progress(value=entry.get('percent',100 if entry['state']=='completed' else 0),max=100),
            html.Span(entry['state'].capitalize()+' · '+entry.get('message',''))],className='comparison-progress-row')
                for key,entry in manifest['entries'].items()]

    @scoped_callback(app,'backtesting',Output('comparison-results','children'),Output('comparison-export','disabled'),
                  Output('comparison-detail-method','options'),Output('comparison-detail-method','value'),
                  Output('comparison-rendered','data'),
                  Input('comparison-state','data'),Input('comparison-context','data'),Input('comparison-target','value'),
                  Input('comparison-basis','value'),Input('comparison-tolerance','value'),State('comparison-detail-method','value'),
                  State('comparison-rendered','data'))
    def show_comparison(manifest,context,target,basis,tolerance,previous,rendered=None):
        if not manifest or manifest.get('context')!=context:return [],True,[],None,None
        signature=[manifest['id'],{key:entry.get('run_id') for key,entry in manifest['entries'].items()},target,basis,tolerance]
        if signature==rendered:raise PreventUpdate
        reports=comparisons.reports(manifest)
        if not reports:return html.P('Results appear as each method completes. Failed methods and reasons are shown above.',className='backtest-status'),True,[],None,signature
        options=[dict(label=model.label,value=model.key) for model in methods(context['group']) if model.key in reports]
        try:content,_=render(context,reports,target,basis,tolerance)
        except (ValueError,TypeError,KeyError) as exc:return html.P(str(exc),className='backtest-status'),True,options,options[0]['value'],signature
        settings=manifest.get('settings',{})
        scope=(f"{settings.get('entity','All teams/players')} · {settings.get('team_filter') or 'All evaluation teams'} · "
               f"{settings.get('market') or 'Game outcomes'} · last {settings.get('limit')} eligible games · "
               f"{settings.get('start') or 'Any start'} to {settings.get('end') or 'Any end'}" if manifest['source']=='new' else
               'Selected previous simulations; each original evaluation scope and configuration is preserved.')
        content=html.Div([html.P(f"Saved comparison: {format_eastern(manifest['created_at'])} · "
            +', '.join(key.upper() for key in manifest['entries'])+' · '+scope+'. Run again to apply changed run filters.',className='backtest-applied'),content])
        return content,False,options,previous if previous in reports else options[0]['value'],signature

    @scoped_callback(app,'backtesting',Output('comparison-detail','children'),Output('comparison-detail-rendered','data'),
                  Input('comparison-state','data'),Input('comparison-context','data'),
                  Input('comparison-detail-method','value'),Input('comparison-target','value'),State('comparison-detail-rendered','data'))
    def comparison_details(manifest,context,key,target,rendered=None):
        if not manifest or manifest.get('context')!=context or key not in manifest['entries']:return [],None
        run_id=manifest['entries'][key].get('run_id');signature=[manifest['id'],key,run_id,target]
        if signature==rendered:raise PreventUpdate
        report=backtesting.saved(context['sport'],context['group'],key,run_id)
        return (full_report(report,target) if report and report['records'] else []),signature

    @app.callback(Output('comparison-download','data'),Input('comparison-export','n_clicks'),State('comparison-state','data'),
                  State('comparison-context','data'),State('comparison-target','value'),State('comparison-basis','value'),
                  State('comparison-tolerance','value'),prevent_initial_call=True)
    def export_comparison(clicks,manifest,context,target,basis,tolerance):
        if not clicks or not manifest or manifest.get('context')!=context:raise PreventUpdate
        reports=comparisons.reports(manifest)
        try:data=compare_reports(context,reports,target,basis,tolerance)
        except (ValueError,TypeError):raise PreventUpdate
        rows=[]
        for row in data['summary']:
            report=reports[row['method'].lower()]
            rows.append({**context,**{k:v for k,v in row.items() if k!='reliability'},'basis':basis,
                'target':target if context['group']=='game-outcome' else report['context']['market'],
                'comparison_id':manifest['id'],'original_context':json.dumps(report['context'],sort_keys=True),
                'model_parameters':json.dumps(report.get('config',{}),sort_keys=True)})
        return dcc.send_data_frame(pd.DataFrame(rows).to_csv,
            f"{context['sport'].lower()}_{context['group']}_comparison_{manifest['id'][:8]}.csv",index=False)
