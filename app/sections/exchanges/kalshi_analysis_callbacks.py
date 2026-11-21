"""Forecast only changed contract definitions; scan all categories on explicit clicks."""
from app.shell.navigation import scoped_callback
from app.components.presentation import signed_state
from copy import deepcopy

from dash import ALL,Input,Output,State,ctx,html,no_update,set_props
from dash.exceptions import PreventUpdate

from sports_analytics.data import DataSourceError
from sports_analytics.display_time import format_eastern
from sports_analytics.kalshi import CATEGORIES
from sports_analytics.kalshi_analysis import FILTER,identity,quick_scope,model_matches,definition,calculate,decorate,rank,quick_calculate,prompt_targets
from app.sections.markets import quick_methods
from app.sections.markets.market_view import probability_text
from app.sections.markets.mispricing import criteria_values, selected_methods, method_details, quick_finish_progress, quick_progress
from app.sections.exchanges.kalshi_view import href,route,odds_text,comparison_rows,prop_lines,prop_outcome,table as quote_table
from sports_analytics.kalshi_labels import selection,selection_rows
from app.sections.market_structure.market_structure_link import line_actions
from sports_analytics.filter_jobs import FilterJobs,request_key
from app.sections.markets.lazy_markets import markets as lazy_markets
from app.sections.markets import prop_probability, quick_filter_monitor, quick_visibility


def results(snapshot, ranked, criteria, models):
    consensus=ranked.get('consensus',False)
    matches=ranked['matches']
    view=panel=None
    player_lines=ranked.get('player_lines',())
    if player_lines:
        def render(key,selection):
            return prop_probability.chart(key, (line for line in prop_lines(player_lines,snapshot['sport'])
                                                 if line['method'] in selection['methods']))
        view,panel=prop_probability.mount(render)

    def table_row(item,rank):
        row,side=item['row'],item['side']
        label=selection(row,side,snapshot)[0]
        key=prop_probability.player_line(prop_outcome(row,snapshot['sport'],side)) if view else None
        methods=[value['method'] for value in item['estimates']] if 'estimates' in item else [item['method']]
        label,action=prop_probability.row_action(label,key,view,methods=methods)
        return html.Tr([
            html.Td(rank),html.Td([label,*line_actions(snapshot,row,'Kalshi',side,
                surface='quick-filter' if consensus else 'apply-criteria',
                grouped=consensus or row.get('category')=='Game Lines')]),
            html.Td(row['category']),html.Td(method_details(item)),html.Td(odds_text(item['implied'])),
            html.Td(probability_text(item['implied'])),html.Td(probability_text(item['probability'])),
            html.Td(f"{100*item['edge']:+.2f}",className='mispricing-edge',**{'data-sign':signed_state(item['edge'])}),
        ],**action)

    def table(items,start):
        return html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label,scope='col') for label in
                ['Rank','Selection','Category','Method (probability / edge)' if consensus else 'Method','Market odds','Market implied',
                 'Lowest probability' if consensus else 'Calculated probability','Lowest edge (pp)' if consensus else 'Edge (pp)']])),
            html.Tbody([table_row(item,i) for i,item in enumerate(items,start)] or [html.Tr(html.Td('No qualifying markets.',colSpan=8))]),
        ],className='mispricing-table'),className='mispricing-table-scroll',tabIndex=0,role='region',
            **{'aria-label':f'Filtered selections starting at rank {start}'})
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
                               max(0,len(matches)-3),size=50,text_more=True)],className='mispricing-remaining'),
        html.P('Edge uses each side’s ask / payout, before trading fees. Calculated probability retains binary settlement equality outcomes. '
               'Unsupported rules, missing history and unavailable prices are excluded.',className='ppp-comparison-note') if not consensus else None,
        panel,
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


def register(app,service,game_forecast,player_forecast,filtered_results=None,*,jobs=None,tasks=None,source_revision=None):
    jobs=jobs or FilterJobs()
    quick_methods.register_controls(app,'kalshi',single='kalshi-game-method',player_single='kalshi-player-method')
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

    @scoped_callback(app,'kalshi',Output('kalshi-models','data'),Input('kalshi-quotes','data'),Input('kalshi-filter-quotes','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input('workspace-page','value'),Input('kalshi-category','value'),State('kalshi-models','data'),State('market-url','hash'),
                  *([Input('kalshi-model-poll','n_intervals'),Input('kalshi-model-retry','n_clicks'),Input('history-update-poll','n_intervals')] if tasks is not None else []))
    def kalshi_models(snapshot,scan,game,method,player_method,selected,page,category,previous,location_hash=None,_model_tick=None,_retry=None,_history_tick=None):
        if page!='kalshi' or not game:raise PreventUpdate
        if location_hash is not None and route(location_hash)!=(game['sport'],game['id']):raise PreventUpdate
        if category==FILTER:
            if not scan or scan['selection']!=selected:raise PreventUpdate
            snapshot=scan['snapshot']
        if not snapshot or (snapshot['sport'],snapshot['game_id'])!=(game['sport'],game['id']):raise PreventUpdate
        if not snapshot['rows'] and snapshot.get('updating'):raise PreventUpdate
        if category!=FILTER and snapshot['category']!=category:raise PreventUpdate
        if method not in quick_methods.game_methods(game['sport']) or player_method not in quick_methods.player_methods(game['sport']):raise PreventUpdate
        before=previous
        context=identity(game,selected,method,player_method)
        if source_revision is not None:
            context[7]=[context[7],source_revision(game,method,player_method)]
        if (category==FILTER and previous and previous.get('context')==context
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
            return 'Waiting to calculate probabilities…'
        if models.get('pending'):
            return ('Updating calculated probabilities…' if models.get('keep_visible') else 'Calculating probabilities…')
        priced=sum(p.get('probability') is not None for p in models.get('predictions',{}).values())
        total=len(models.get('predictions',{}))
        status=(f'Calculated probabilities ready · {priced}/{total} contracts' if priced else
                'Calculated probabilities unavailable.')
        errors=models.get('errors',[])
        return html.Span(status,title='\n'.join(errors)) if errors else status

    rows=dict(type='kalshi-comparison-row',sport=ALL,game=ALL,ticker=ALL,side=ALL)
    @app.callback(Output('kalshi-game-distributions','children'),
                  Input('kalshi-models','data'),Input('kalshi-context','data'),Input('kalshi-game-method','value'),
                  Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),Input('kalshi-category','value'),
                  State('workspace-page','value'),State('market-url','hash'),prevent_initial_call=True)
    def kalshi_game_distributions(models,game,method,player_method,selected,category,page,location):
        # Retain the chart for a warm return. The market shell hides it while its route or quotes are stale.
        if page!='kalshi' or not game or route(location)!=(game['sport'],game['id']):raise PreventUpdate
        if category!='Game Lines':return []
        if not model_matches(models,game,selected,method,player_method):return []
        if models.get('pending') and not models.get('keep_visible'):return []
        from app.sections.markets.game_distribution import panel
        return panel(models,method,'|'.join([game['sport'],game['id'],selected]))

    app.clientside_callback('''function(models,selection,page){
        if(!selection||!models||models.pending||page!=='kalshi')return window.dash_clientside.no_update;
        return models.job||JSON.stringify(models.context);
    }''',Output('kalshi-prop-request','data-model-revision'),Input('kalshi-models','data'),
        State('kalshi-prop-request','data-selection'),State('workspace-page','value'),prevent_initial_call=True)

    @app.callback(Output('kalshi-prop-result','children'),Input('kalshi-prop-request','data-selection'),
                  Input('kalshi-prop-request','data-model-revision'),
                  Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),
                  Input('kalshi-snapshot','value'),Input('kalshi-category','value'),
                  State('kalshi-quotes','data'),State('kalshi-models','data'),
                  State('workspace-page','value'),State('market-url','hash'),prevent_initial_call=True)
    def kalshi_prop_probabilities(selection,_model_revision,game,method,player_method,selected,category,snapshot,models,page,location):
        if not selection:
            raise PreventUpdate
        if page!='kalshi' or not game or not snapshot or category==FILTER or (
                route(location)!=(game['sport'],game['id']) or selection.get('snapshot')!=selected):
            return None
        context=[game['sport'],game['id'],category]
        if selection.get('context')!=context or [snapshot['sport'],snapshot['game_id'],snapshot['category']]!=context:
            return None
        shown=decorate(snapshot,game,selected,method,player_method,models)
        waiting=not model_matches(models,game,selected,method,player_method) or models.get('pending')
        content=prop_probability.chart(selection['key'],prop_lines(shown['rows'],game['sport']),waiting=waiting)
        return prop_probability.clicked(content,selection)

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
            from app.sections.exchanges.kalshi_view import market_groups,market_rows,market_body
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
        location=href(game['sport'],game['id'])
        try:
            criteria=criteria_values(probability,edge)
            pull=ctx.triggered_id=='kalshi-pull'
            if pull:selected='live'
            snapshot,status=filter_snapshot(service,game,selected,pull)
            return dict(snapshot=snapshot,selection=selected,sequence=[applies,pulls]),criteria,quick_visibility.status(status,location,selected)
        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return no_update,no_update,quick_visibility.status(str(exc)+' Previous criteria and results remain applied.',location,selected)

    @scoped_callback(app,'kalshi',Output('kalshi-filter-results','children'),
                  Input('kalshi-filter-quotes','data'),Input('kalshi-applied-criteria','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input('kalshi-category','value'),Input('workspace-page','value'),Input('kalshi-models','data'),prevent_initial_call=True)
    def kalshi_filter_results(scan,criteria,game,method,player_method,selected,category,page,models):
        if page!='kalshi' or category!=FILTER:raise PreventUpdate
        if not scan or not criteria or not game:return None
        snapshot=scan['snapshot']
        if (snapshot['sport'],snapshot['game_id'],scan['selection'])!=(game['sport'],game['id'],selected):return None
        if method not in quick_methods.game_methods(game['sport']) or player_method not in quick_methods.player_methods(game['sport']):return None
        def in_scope(content):return quick_visibility.scoped(content,href(game['sport'],game['id']),selected)
        if not models or models.get('pending') or not model_matches(models,game,selected,method,player_method):
            return in_scope(html.P('Calculating the selected forecast methods…',className='saved-market-empty'))
        displayed={**decorate(snapshot,game,selected,method,player_method,models,criteria),'game':game}
        return in_scope(results(displayed,rank(displayed,criteria),criteria,models))

    app.clientside_callback("""function(clicks,category,selected){
        const hash=window.location.hash,route=window.dash_clientside.market_transition.route(hash);
        if(!clicks||category!=='Mispricing Filter'||route?.provider!=='kalshi'||!route.game){
            return [window.dash_clientside.no_update,window.dash_clientside.no_update];
        }
        const selection=selected??'live';
        return [{sequence:clicks,location:hash,selection},JSON.stringify([hash,String(selection)])];
    }""",Output('kalshi-quick-request','data'),Output('kalshi-starting-quick-progress','data-quick-scope'),
        Input('kalshi-quick-filter','n_clicks'),State('kalshi-category','value'),State('kalshi-snapshot','value'),
        prevent_initial_call=True)

    @app.callback(Output('kalshi-quick-scan','data'),Output('kalshi-quick-status','children'),
                  Input('kalshi-quick-request','data'),State('kalshi-quick-probability','value'),State('kalshi-quick-edge','value'),
                  State('kalshi-quick-game-methods','value'),State('kalshi-quick-player-methods','value'),
                  State('kalshi-context','data'),State('kalshi-games','data'),
                  State(prompt_fields,'value'),State(prompt_fields,'id'),prevent_initial_call=True,
                  running=[(Output('kalshi-starting-quick-progress','hidden'),False,True)])
    def kalshi_quick_scan(request,probability,edge,methods,player_methods,game,catalog=None,prompt_values=None,prompt_ids=None):
        if not request:raise PreventUpdate
        clicks,location_hash,selected=request['sequence'],request['location'],request['selection']
        target=route(location_hash)
        def submission(content):
            return quick_visibility.scoped(html.Div(content,className='kalshi-quick-submission'),location_hash,selected,clicks)

        try:
            if not target or not target[1]:raise ValueError('Choose a Kalshi game to start Quick Filtering.')
            sport,game_id=target
            if not game or (game.get('sport'),game.get('id'))!=target:
                game=next((candidate for candidate in (catalog or {}).get('games',[])
                           if (candidate.get('sport'),candidate.get('id'))==target),None)
            if not game:raise ValueError('This game is still loading. Try Quick Filtering again when the game opens.')
            criteria=criteria_values(probability,edge)
            methods,player_methods=selected_methods(methods,player_methods,sport)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope('kalshi',sport,game_id),ignore_stale=True)
            game=deepcopy(game)
            def calculate_scan(report):
                report(5,'Loading market quotes')
                snapshot,status=filter_snapshot(service,game,selected,current=True)
                report(10,'Preparing forecast methods')
                ranked,errors=quick_calculate(game,snapshot,selected,methods,player_methods,criteria,game_forecast,player_forecast,overrides,report)
                if filtered_results is not None:
                    report(95,'Saving filtered selections')
                    try:
                        filtered_results.save(snapshot,ranked,criteria,provider='Kalshi',game=game,
                            request=dict(selection=selected,methods=methods,player_methods=player_methods,prompt_methods=overrides,sequence=clicks))
                    except (DataSourceError,OSError,ValueError) as exc:
                        errors=[*errors,'Quick Filtering completed, but its report was not saved: '+str(exc)]
                return dict(snapshot=snapshot,game=game,ranked=ranked,errors=errors,status=status)
            context=identity(game,selected,methods,player_methods)
            key=request_key([context,criteria,overrides])
            _,_,error=jobs.tasks.scan(key,calculate_scan,refresh=True,force=True)
            if error:raise DataSourceError(error)
            scan = dict(context=context,scope=quick_scope(game,selected,methods,player_methods),
                        criteria=criteria,prompt_methods=overrides,job=key,sequence=clicks)
            jobs.tasks.track(quick_filter_monitor.owner(),key,('kalshi',location_hash,selected),scan,
                             provider='Kalshi',sport=sport,matchup=game.get('title') or game_id,
                             methods=[*methods,*player_methods])
            return scan, submission(quick_progress(jobs.tasks.progress(key),'kalshi-submitted'))

        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return dict(request,error=str(exc)),submission(str(exc))

    @scoped_callback(app,'kalshi',Output('kalshi-quick-results','children'),Input('kalshi-quick-scan','data'),Input('kalshi-context','data'),
                  Input('kalshi-quick-game-methods','value'),Input('kalshi-quick-player-methods','value'),
                  Input('kalshi-quick-probability','value'),Input('kalshi-quick-edge','value'),Input('kalshi-snapshot','value'),
                  Input('kalshi-category','value'),Input('workspace-page','value'),
                  Input(prompt_fields,'value'),State(prompt_fields,'id'),Input('kalshi-quick-poll','n_intervals'),
                  State('kalshi-quick-state','data'),State('kalshi-quick-request','data'),
                  Input('market-url','hash'),Input('quick-monitor-completions','data'),prevent_initial_call=True)
    def kalshi_quick_results(scan,game,methods,player_methods,probability,edge,selected,category,page,prompt_values=None,prompt_ids=None,_tick=None,previous=None,request=None,location_hash=None,_completed=None):
        if page!='kalshi' or category!=FILTER:raise PreventUpdate
        selected=selected if selected is not None else 'live'
        location=location_hash if location_hash is not None else href(game['sport'],game['id']) if game else None
        target=route(location)
        submitted = (request or {}).get('location')==location and (request or {}).get('selection')==selected
        restored = None
        if target and not submitted and (scan or {}).get('scope',[])[:3]!=[target[0],target[1],selected]:
            restored = jobs.tasks.latest(quick_filter_monitor.owner(),('kalshi',location,selected))
            if restored:
                scan = restored
        if _completed is not None and set(ctx.triggered_prop_ids)=={'quick-monitor-completions.data'} and (
                not restored or not any(key==restored['job'] for key,_ in (_completed or []))):
            raise PreventUpdate
        sequence=(scan or {}).get('sequence')
        def in_scope(content):return quick_visibility.scoped(content,location,selected,sequence)
        prompt=in_scope(html.P(quick_visibility.PROMPTS['kalshi'],className='saved-market-empty'))
        scope=(scan or {}).get('scope')
        if scope is None and scan and scan.get('context'):
            context=scan['context']
            scope=[context[0],str(context[1]),*context[8:11]]
        if scan and scan.get('error'):return prompt
        if submitted and (not scan or scan.get('sequence')!=request.get('sequence')):
            raise PreventUpdate
        def state(status,message,progress=None):
            if not scan or not scan.get('job'):return
            record=dict(job=scan['job'],sequence=scan.get('sequence'),status=status)
            if progress is not None:record['progress']=progress
            set_props('kalshi-quick-state',{'data':record})
            scan_location=href(scope[0],scope[1]) if scope else None
            set_props('kalshi-quick-status',{'children':quick_visibility.status(message,scan_location,scope[2] if scope else None,sequence)})
        def changed_selection(message='Quick Filtering settings changed. Click Quick Filtering to apply the current selections.'):
            state('changed' if scan else 'idle',message if scan else '')
            return prompt
        if not scan or not target or not target[1] or not scope or tuple(scope[:2])!=target:return prompt
        sport,game_id=target
        criteria = scan['criteria']
        if not restored:
            try:
                methods,player_methods=selected_methods(methods,player_methods,sport)
                criteria=criteria_values(probability,edge)
                overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope('kalshi',sport,game_id),ignore_stale=True)
            except ValueError as exc:return changed_selection(str(exc))
            if (scope!=[sport,game_id,selected,methods,player_methods]
                    or scan['criteria']!=criteria or scan.get('prompt_methods',{})!=overrides):return changed_selection()
        def pending_view(progress):
            state('pending','',progress['percent'])
            return in_scope(quick_progress(progress,'kalshi'))

        job_state=jobs.tasks.queue.state(scan['job']) if scan.get('job') else dict(pending=False,error=None)
        if job_state['pending']:
            return pending_view(jobs.tasks.progress(scan['job']))
        # Read the value after observing completion: the worker publishes it
        # before clearing pending, so a just-finished scan cannot look expired.
        record=jobs.tasks.peek(scan['job']) if scan.get('job') else scan
        if job_state['error'] or record is None:
            message=('Quick Filtering failed: '+job_state['error']+'. Click Quick Filtering to retry.' if job_state['error'] else
                     'This result expired. Click Quick Filtering to load it again.')
            state('error',message)
            return in_scope(html.P(message,className='mispricing-error',role='status'))
        if (not restored and previous and previous.get('status')=='pending' and previous.get('job')==scan.get('job')
                and previous.get('sequence')==scan.get('sequence')):
            checkpoint=quick_finish_progress(jobs.tasks,scan['job'],previous.get('progress'))
            if checkpoint:return pending_view(checkpoint)
        ranked=record['ranked']
        try:
            content=results({**record['snapshot'],'game':record.get('game') or game},ranked,criteria,dict(errors=record['errors']))
        except (DataSourceError,OSError,ValueError,TypeError,KeyError) as exc:
            message='Quick Filtering results could not be displayed: '+str(exc)+'. Click Quick Filtering to retry.'
            state('error',message)
            return in_scope(html.P(message,className='mispricing-error',role='status'))
        state('complete',record.get('status','')+f" Quick Filtering completed: {len(ranked['matches'])} qualifying selections; "
              f"{ranked['evaluated']} evaluated; {ranked['unavailable']} unavailable.")
        return in_scope(content)

    app.clientside_callback("""function(page,category,scan,game,state,request,starting,hash,selected){
        const visible=page==='kalshi'&&category==='Mispricing Filter';
        const current=request?.location===hash&&request?.selection===(selected??'live')?request:null;
        const sending=visible&&!!current&&starting===false;
        const finished=state?.job===scan?.job&&
        state?.sequence===scan?.sequence&&['complete','error','changed'].includes(state?.status);
        const scanHash=scan?.context?.[0]&&'#'+scan.context[0].toLowerCase()+'-kalshi-game-'+scan.context[1];
        const matches=hash===undefined?scan?.context?.[0]===game?.sport&&String(scan?.context?.[1])===String(game?.id):hash===scanHash;
        const active=visible&&!finished&&scan?.job&&(!current||scan.sequence===current.sequence)&&
          matches&&(selected===undefined||(scan.scope?.[2]??scan.context?.[8])===(selected??'live'));
        return [!active,sending||!!active];} """,
        Output('kalshi-quick-poll','disabled'),Output('kalshi-quick-filter','disabled'),
        Input('workspace-page','value'),Input('kalshi-category','value'),Input('kalshi-quick-scan','data'),Input('kalshi-context','data'),
        Input('kalshi-quick-state','data'),Input('kalshi-quick-request','data'),
        Input('kalshi-starting-quick-progress','hidden'),Input('market-url','hash'),Input('kalshi-snapshot','value'))
