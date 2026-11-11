"""Forecast only changed contract definitions; scan all categories on explicit clicks."""
from .navigation import scoped_callback
from .presentation import signed_state
from copy import deepcopy

from dash import ALL,Input,Output,State,ctx,html,no_update,set_props
from dash.exceptions import PreventUpdate

from .data import DataSourceError
from .display_time import format_eastern
from .kalshi import CATEGORIES
from .kalshi_analysis import FILTER,GAME_METHODS,PLAYER_METHODS,identity,quick_scope,model_matches,definition,calculate,decorate,rank,quick_calculate,prompt_targets
from . import quick_methods
from .market_view import probability_text
from .mispricing import criteria_values, selected_methods, method_details
from .kalshi_view import route,odds_text,comparison_rows,table as quote_table
from .kalshi_labels import selection,selection_rows
from .prompt_history import button as history_button
from .market_structure_link import button as structure_button
from .filter_jobs import FilterJobs,request_key
from .lazy_markets import markets as lazy_markets
from .loading import indicator


def results(snapshot, ranked, criteria, models):
    consensus=ranked.get('consensus',False)
    def table(items,start):
        return html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label,scope='col') for label in
                ['Rank','Selection','Category','Method (probability / edge)' if consensus else 'Method','Market odds','Market implied',
                 'Lowest probability' if consensus else 'Calculated probability','Lowest edge (pp)' if consensus else 'Edge (pp)']])),
            html.Tbody([html.Tr([
                html.Td(start+i),html.Td([selection(item['row'],item['side'],snapshot)[0],history_button(snapshot,item['row'],'Kalshi',item['side']),
                    structure_button(snapshot,item['row'],'Kalshi',item['side'],surface='quick-filter' if consensus else 'apply-criteria')]),
                html.Td(item['row']['category']),html.Td(method_details(item)),
                html.Td(odds_text(item['implied'])),
                html.Td(probability_text(item['implied'])),html.Td(probability_text(item['probability'])),
                html.Td(f"{100*item['edge']:+.2f}",className='mispricing-edge',**{'data-sign':signed_state(item['edge'])}),
            ]) for i,item in enumerate(items)] or [html.Tr(html.Td('No qualifying markets.',colSpan=8))]),
        ],className='mispricing-table'),className='mispricing-table-scroll',tabIndex=0,role='region',
            **{'aria-label':f'Filtered selections starting at rank {start}'})
    matches=ranked['matches']
    return html.Div([
        html.Div(f"{len(matches)} qualifying selections. Estimated probability > {100*criteria['probability']:g}% and "
                 f"edge > {100*criteria['edge']:g} pp. {ranked['evaluated']} evaluated; "
                 f"{ranked['unavailable']} unavailable or inactive selections excluded."
                 +(f" {ranked['excluded']} selections skipped by prompt selection." if ranked.get('excluded') else ''),className='criteria-evaluation',role='status'),
        html.P('Quote snapshot fetched '+(format_eastern(snapshot['fetched_at']) if snapshot.get('fetched_at') else 'timestamp unavailable')+
               '. Click '+('Quick Filtering' if consensus else 'Apply Criteria')+' to update these results.',className='forecast-history'),
        *[html.P(message,className='ppp-comparison-note' if message.startswith('Game injuries:') else 'mispricing-error',role='status') for message in [*snapshot.get('notices',[]),*models.get('errors',[])]],
        html.Section([html.H3('Top 3 by edge'),table(matches[:3],1)],className='mispricing-top'),
        html.Section([html.H3('Remaining qualifying markets'),
            lazy_markets.panel(lazy_markets.view(),0,lambda count:table(matches[3:3+count],4),
                               max(0,len(matches)-3),size=50)],className='mispricing-remaining'),
        html.P('Edge uses each side’s ask / payout, before trading fees. Calculated probability retains binary settlement equality outcomes. '
               'Unsupported rules, missing history and unavailable prices are excluded. '+
               ('Every method assigned to a prompt must pass both thresholds. Ranking uses the lowest edge across those methods; '
                'a missing estimate from any selected method excludes that selection.' if consensus else ''),className='ppp-comparison-note'),
    ],className='mispricing-results')


def filter_snapshot(service,game,selected,pull=False,*,current=False):
    if selected!='live':
        # Saved prices remain immutable; never mix them with live categories.
        found=next((category for category in CATEGORIES if selected in service.history(game,category)),None)
        if not found:raise ValueError('The selected saved snapshot is unavailable.')
        return service.saved(game,found,selected),'Applied to the selected saved category only. Select Live quotes to scan all categories.'
    # An explicit scan runs in a worker. Resolve its quotes there, rather than
    # ranking a placeholder returned by the navigation cache's background mode.
    refresh=pull or (current and getattr(service,'background',False) is True)
    snapshot=service.all_quotes(game,refresh=refresh,save=pull)
    status='Applied to all linked game categories. Market edges are measured before fees.'
    if pull:status+=' JSON/CSV snapshots saved under data/kalshi_market; reopen them from the category View dropdown.'
    return snapshot,status


def register(app,service,game_forecast,player_forecast,filtered_results=None,*,jobs=None,tasks=None):
    jobs=jobs or FilterJobs()
    prompt_fields=dict(type='kalshi-quick-prompt-method',scope=ALL,prompt=ALL)

    @scoped_callback(app,'kalshi',Output('kalshi-quick-prompts','children'),Input('kalshi-quick-prompts-panel','open'),
                  Input('kalshi-context','data'),Input('kalshi-snapshot','value'),Input('kalshi-category','value'),
                  Input('workspace-page','value'))
    def kalshi_prompt_methods(opened,game,selected,category,page):
        if ctx.triggered_id=='kalshi-quick-prompts-panel' and not opened:return no_update
        if not opened or page!='kalshi' or not game or category!=FILTER:return []
        try:
            snapshot,_=filter_snapshot(service,game,selected)
            targets=prompt_targets(game,snapshot)
            return quick_methods.controls('kalshi',targets.values(),quick_methods.scope('kalshi',game['sport'],game['id']))
        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return html.P(str(exc),className='mispricing-error')

    @app.callback(Output('kalshi-filter-panel','style'),Output('kalshi-market-sections','style'),
                  Output('kalshi-detail-table','style'),Output('kalshi-rules-panel','style'),
                  Output('kalshi-quote-status','style'),Input('kalshi-category','value'))
    def kalshi_filter_visibility(category):
        shown=category==FILTER
        return ({'display':'block' if shown else 'none'},*({'display':'none' if shown else 'block'},)*4)

    @scoped_callback(app,'kalshi',Output('kalshi-models','data'),Input('kalshi-quotes','data'),Input('kalshi-filter-quotes','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input('workspace-page','value'),Input('kalshi-category','value'),State('kalshi-models','data'),State('market-url','hash'),
                  *([Input('kalshi-model-poll','n_intervals'),Input('kalshi-model-retry','n_clicks')] if tasks is not None else []))
    def kalshi_models(snapshot,scan,game,method,player_method,selected,page,category,previous,location_hash=None,_model_tick=None,_retry=None):
        if page!='kalshi' or not game:raise PreventUpdate
        if location_hash is not None and route(location_hash)!=(game['sport'],game['id']):raise PreventUpdate
        if category==FILTER:
            if not scan or scan['selection']!=selected:raise PreventUpdate
            snapshot=scan['snapshot']
        if not snapshot or (snapshot['sport'],snapshot['game_id'])!=(game['sport'],game['id']):raise PreventUpdate
        if not snapshot['rows'] and snapshot.get('updating'):raise PreventUpdate
        if category!=FILTER and snapshot['category']!=category:raise PreventUpdate
        if method not in GAME_METHODS or player_method not in PLAYER_METHODS:raise PreventUpdate
        before=previous
        context=identity(game,selected,method,player_method)
        if (category==FILTER and previous and previous.get('context')==identity(game,selected,method,player_method)
                and previous.get('scan_sequence')!=scan.get('sequence')):
            # Deliberate rescans may retry missing history; routine price polls do not refit.
            previous={**previous,'predictions':{key:value for key,value in previous.get('predictions',{}).items()
                                              if value.get('probability') is not None},'errors':[]}
        def compute():
            try:
                record=calculate(game,snapshot,selected,method,player_method,game_forecast,player_forecast,previous,context=context)
            except (DataSourceError,OSError,ValueError,TypeError) as exc:
                record=dict(context=context,predictions={},errors=[str(exc)])
            if not record.get('context'):
                record=dict(record,context=context,predictions={},
                            errors=[snapshot['error']] if snapshot.get('error') else [])
            if category==FILTER:record={**record,'scan_sequence':scan.get('sequence')}
            return record
        if tasks is not None and _model_tick is not None:
            request=request_key(['kalshi-model-v2',context[:7]+context[8:],category,
                [definition(row) for row in snapshot['rows']],scan.get('sequence') if category==FILTER else None,snapshot.get('error'),_retry])
            key=request_key([request,context[7]])
            if previous and previous.get('pending') and previous.get('request')==request:
                # A source revision can change while history is being prepared.
                # Finish the submitted job before considering another revision.
                key=previous['job']
                state=tasks.queue.state(key)
                if state['pending']:return no_update
                record=tasks.peek(key);pending=False;error=state['error']
                if record is None and not error:record,pending,error=tasks.read(key,compute)
            else:record,pending,error=tasks.read(key,compute)
            if pending:
                retained=previous if model_matches(previous,game,selected,method,player_method) else {}
                keep_visible=any(prediction.get('probability') is not None for prediction in retained.get('predictions',{}).values())
                record={**retained,'context':context,'job':key,'request':request,'pending':True,
                        'keep_visible':keep_visible,'status':'pending'}
            else:
                if error:record=dict(context=context,predictions={},errors=[error])
                failed=bool(error or record and record.get('errors') and
                            not any(p.get('probability') is not None for p in record.get('predictions',{}).values()))
                record={**(record or dict(context=context,predictions={},errors=[error or 'Calculation unavailable. Retry this selection.'])),
                        'job':key,'request':request,'pending':False,'keep_visible':False,'status':'error' if failed else 'complete'}
        else:record=compute()
        return no_update if record==before else record

    @app.callback(Output('kalshi-model-status','children'),Input('kalshi-models','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'))
    def kalshi_model_status(models,game,method,player_method,selected):
        if not game:return ''
        if not model_matches(models,game,selected,method,player_method):
            return 'Calculated probabilities: waiting for the selected category and methods.'
        if models.get('pending'):
            return ('Updating the forecast; previously calculated values remain visible.' if models.get('keep_visible') else
                    'Quotes available · Calculating probabilities in the background…')
        history=models.get('history',{})
        priced=sum(p.get('probability') is not None for p in models.get('predictions',{}).values())
        total=len(models.get('predictions',{}))
        return [html.Span(f"{method} latest historical game: {history.get('game') or 'unavailable'} · "
                          f"{player_method} latest player history: {history.get('player') or 'unavailable'}"),
                html.P(f'{priced} of {total} contracts have calculated estimates. '+
                       ('Unavailable estimates retain their contract-specific explanation.' if priced<total else ''),className='ppp-comparison-note'),
                *[html.P(error,className='ppp-comparison-note' if error.startswith('Game injuries:') else 'mispricing-error') for error in models.get('errors',[])]]

    rows=dict(type='kalshi-comparison-row',sport=ALL,game=ALL,ticker=ALL,side=ALL)
    @app.callback(Output(rows,'children'),Output(rows,'className'),
                  Input('kalshi-models','data'),Input('kalshi-applied-criteria','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input(rows,'id'),State('kalshi-quotes','data'),State('kalshi-context','data'),
                  State('kalshi-category','value'),State('workspace-page','value'),State('market-url','hash'),
                  State('kalshi-detail-table','open'),Input('kalshi-rules-signature','data'),prevent_initial_call=False)
    def kalshi_model_rows(models,criteria,method,player_method,selected,identities,snapshot,game,category,page,location_hash,table_open,view_state=None):
        # Update only existing rows after forecasts finish, preserving accordions.
        if (page!='kalshi' or not game or not snapshot or category==FILTER or not identities
                or route(location_hash)!=(game['sport'],game['id'])
                or (snapshot['sport'],snapshot['game_id'],snapshot['category'])!=(game['sport'],game['id'],category)):
            raise PreventUpdate
        shown={**decorate(snapshot,game,selected,method,player_method,models,criteria),'game':game}
        if view_state and view_state.get('context')==[game['sport'],game['id'],category]:
            from .kalshi_view import market_groups,market_rows,market_body
            for index,group in enumerate(market_groups(shown).values()):
                ordered=selection_rows(group,shown)
                lazy_markets.update(view_state.get('lazy_view'),index,lambda count,ordered=ordered:
                                    market_body(ordered,shown,'Kalshi',count))
        rendered={}
        wanted={(item['ticker'],item['side']) for item in identities}
        for row in shown['rows']:
            if not any((row['ticker'],side) in wanted for side in ('yes','no')):continue
            for side,value in zip(('yes','no'),comparison_rows(row,shown)):
                rendered[(row['ticker'],side)]=value
        children=[];classes=[]
        for identity in identities:
            value=(rendered.get((identity['ticker'],identity['side']))
                   if (identity['sport'],identity['game'])==(game['sport'],game['id']) else None)
            children.append(value.children if value is not None else no_update)
            classes.append(value.className if value is not None else no_update)
        if table_open:
            # Keep an already-open export table current even when polling is paused.
            set_props('kalshi-table',{'data':quote_table(shown,include_rules=False,build_tooltips=False)[0]})
        return children,classes

    @app.callback(Output('kalshi-filter-quotes','data'),Output('kalshi-applied-criteria','data'),Output('kalshi-criteria-status','children'),
                  Input('kalshi-apply-criteria','n_clicks'),Input('kalshi-pull','n_clicks'),
                  State('kalshi-probability-threshold','value'),State('kalshi-edge-threshold','value'),
                  State('kalshi-context','data'),State('kalshi-category','value'),State('kalshi-snapshot','value'),
                  State('workspace-page','value'),prevent_initial_call=True,
                  running=[(Output('kalshi-apply-criteria','disabled'),True,False)])
    def kalshi_scan(applies,pulls,probability,edge,game,category,selected,page):
        if page!='kalshi' or not game or category!=FILTER:raise PreventUpdate
        if not (applies or pulls):raise PreventUpdate
        try:
            criteria=criteria_values(probability,edge)
            pull=ctx.triggered_id=='kalshi-pull'
            if pull:selected='live'
            snapshot,status=filter_snapshot(service,game,selected,pull)
            return dict(snapshot=snapshot,selection=selected,sequence=[applies,pulls]),criteria,status
        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return no_update,no_update,str(exc)+' Previous criteria and results remain applied.'

    @scoped_callback(app,'kalshi',Output('kalshi-filter-results','children'),
                  Input('kalshi-filter-quotes','data'),Input('kalshi-applied-criteria','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input('kalshi-category','value'),Input('workspace-page','value'),Input('kalshi-models','data'),prevent_initial_call=True)
    def kalshi_filter_results(scan,criteria,game,method,player_method,selected,category,page,models):
        if page!='kalshi' or category!=FILTER:raise PreventUpdate
        prompt=html.P('Click Apply Criteria to compare linked markets using the single methods above.',className='saved-market-empty')
        if not scan or not criteria or not game:return prompt
        snapshot=scan['snapshot']
        if (snapshot['sport'],snapshot['game_id'],scan['selection'])!=(game['sport'],game['id'],selected):return prompt
        if method not in GAME_METHODS or player_method not in PLAYER_METHODS:return prompt
        if not models or models.get('pending') or not model_matches(models,game,selected,method,player_method):
            return html.P('Calculating the selected forecast methods…',className='saved-market-empty')
        displayed={**decorate(snapshot,game,selected,method,player_method,models,criteria),'game':game}
        return results(displayed,rank(displayed,criteria),criteria,models)

    @app.callback(Output('kalshi-quick-scan','data'),Output('kalshi-quick-status','children'),
                  Input('kalshi-quick-filter','n_clicks'),State('kalshi-quick-probability','value'),State('kalshi-quick-edge','value'),
                  State('kalshi-quick-game-methods','value'),State('kalshi-quick-player-methods','value'),
                  State('kalshi-context','data'),State('kalshi-category','value'),State('kalshi-snapshot','value'),
                  State('workspace-page','value'),State('market-url','hash'),
                  State(prompt_fields,'value'),State(prompt_fields,'id'),prevent_initial_call=True)
    def kalshi_quick_scan(clicks,probability,edge,methods,player_methods,game,category,selected,page,location_hash,prompt_values=None,prompt_ids=None):
        if not clicks or page!='kalshi' or not game or category!=FILTER:raise PreventUpdate
        if route(location_hash)!=(game['sport'],game['id']):raise PreventUpdate
        try:
            criteria=criteria_values(probability,edge)
            methods,player_methods=selected_methods(methods,player_methods)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope('kalshi',game['sport'],game['id']))
            game=deepcopy(game)
            def calculate_scan():
                snapshot,status=filter_snapshot(service,game,selected,current=True)
                ranked,errors=quick_calculate(game,snapshot,selected,methods,player_methods,criteria,game_forecast,player_forecast,overrides)
                if filtered_results is not None:
                    try:
                        filtered_results.save(snapshot,ranked,criteria,provider='Kalshi',game=game,
                            request=dict(selection=selected,methods=methods,player_methods=player_methods,prompt_methods=overrides,sequence=clicks))
                    except (DataSourceError,OSError,ValueError) as exc:
                        errors=[*errors,'Quick Filtering completed, but its report was not saved: '+str(exc)]
                return dict(snapshot=snapshot,game=game,ranked=ranked,errors=errors,status=status)
            context=identity(game,selected,methods,player_methods)
            key=request_key([context,criteria,overrides])
            jobs.tasks.read(key,calculate_scan,refresh=True,force=True)
            return dict(context=context,scope=quick_scope(game,selected,methods,player_methods),
                        criteria=criteria,prompt_methods=overrides,job=key,sequence=clicks), 'Quick Filtering is calculating in the background.'

        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return no_update,str(exc)+' Previous Quick Filtering results remain saved.'

    @scoped_callback(app,'kalshi',Output('kalshi-quick-results','children'),Input('kalshi-quick-scan','data'),Input('kalshi-context','data'),
                  Input('kalshi-quick-game-methods','value'),Input('kalshi-quick-player-methods','value'),
                  Input('kalshi-quick-probability','value'),Input('kalshi-quick-edge','value'),Input('kalshi-snapshot','value'),
                  Input('kalshi-category','value'),Input('workspace-page','value'),
                  Input(prompt_fields,'value'),State(prompt_fields,'id'),Input('kalshi-quick-poll','n_intervals'),prevent_initial_call=True)
    def kalshi_quick_results(scan,game,methods,player_methods,probability,edge,selected,category,page,prompt_values=None,prompt_ids=None,_tick=None):
        if page!='kalshi' or category!=FILTER:raise PreventUpdate
        prompt=html.P('Click Quick Filtering to compare all selected methods and see the top three edges.',className='saved-market-empty')
        def state(status,message):
            if not scan or not scan.get('job'):return
            set_props('kalshi-quick-state',{'data':dict(job=scan['job'],sequence=scan.get('sequence'),status=status)})
            set_props('kalshi-quick-status',{'children':message})
            set_props('kalshi-quick-poll',{'disabled':status!='pending'})
            set_props('kalshi-quick-filter',{'disabled':status=='pending'})
        def changed_selection(message='Quick Filtering settings changed. Click Quick Filtering to apply the current selections.'):
            state('changed' if scan else 'idle',message if scan else '')
            return prompt
        try:
            methods,player_methods=selected_methods(methods,player_methods)
            criteria=criteria_values(probability,edge)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope(
                'kalshi',(game or {}).get('sport'),(game or {}).get('id')))
        except ValueError as exc:return changed_selection(str(exc))
        scope=(scan or {}).get('scope')
        if scope is None and scan and scan.get('context'):
            context=scan['context']
            scope=[context[0],str(context[1]),*context[8:11]]
        if (not scan or not game or scope!=quick_scope(game,selected,methods,player_methods)
                or scan['criteria']!=criteria or scan.get('prompt_methods',{})!=overrides):return changed_selection()
        job_state=jobs.tasks.queue.state(scan['job']) if scan.get('job') else dict(pending=False,error=None)
        if job_state['pending']:
            state('pending','Quick Filtering is calculating in the background.')
            return indicator('Quick Filtering is calculating in the background...')
        # Read the value after observing completion: the worker publishes it
        # before clearing pending, so a just-finished scan cannot look expired.
        record=jobs.tasks.peek(scan['job']) if scan.get('job') else scan
        if job_state['error'] or record is None:
            message=('Quick Filtering failed: '+job_state['error']+'. Click Quick Filtering to retry.' if job_state['error'] else
                     'This result expired. Click Quick Filtering to load it again.')
            state('error',message)
            return html.P(message,className='mispricing-error',role='status')
        ranked=record['ranked']
        try:
            content=results({**record['snapshot'],'game':record.get('game') or game},ranked,criteria,dict(errors=record['errors']))
        except (DataSourceError,OSError,ValueError,TypeError,KeyError) as exc:
            message='Quick Filtering results could not be displayed: '+str(exc)+'. Click Quick Filtering to retry.'
            state('error',message)
            return html.P(message,className='mispricing-error',role='status')
        state('complete',record.get('status','')+f" Quick Filtering completed: {len(ranked['matches'])} qualifying selections; "
              f"{ranked['evaluated']} evaluated; {ranked['unavailable']} unavailable.")
        return content

    app.clientside_callback("""function(page,category,scan,game,state){const finished=state?.job===scan?.job&&
        state?.sequence===scan?.sequence&&['complete','error','changed'].includes(state?.status);
        const active=page==='kalshi'&&!finished&&
        category==='Mispricing Filter'&&scan?.job&&scan.context?.[0]===game?.sport&&String(scan.context?.[1])===String(game?.id);
        return [!active,!!active];} """,
        Output('kalshi-quick-poll','disabled'),Output('kalshi-quick-filter','disabled'),
        Input('workspace-page','value'),Input('kalshi-category','value'),Input('kalshi-quick-scan','data'),Input('kalshi-context','data'),
        Input('kalshi-quick-state','data'))
