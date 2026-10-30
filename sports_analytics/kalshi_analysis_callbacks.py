"""Forecast only changed contract definitions; scan all categories on explicit clicks."""
from dash import ALL,Input,Output,State,ctx,html,no_update,set_props
from dash.exceptions import PreventUpdate

from .data import DataSourceError
from .display_time import format_eastern
from .kalshi import CATEGORIES
from .kalshi_analysis import FILTER,GAME_METHODS,PLAYER_METHODS,identity,calculate,decorate,rank,quick_calculate,prompt_targets
from . import quick_methods
from .market_view import probability_text
from .mispricing import criteria_values, selected_methods, method_details
from .kalshi_view import route,odds_text,comparison_rows,table as quote_table
from .prompt_history import button as history_button
from .market_structure_link import button as structure_button


def results(snapshot, ranked, criteria, models):
    consensus=ranked.get('consensus',False)
    def table(items,start):
        return html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label,scope='col') for label in
                ['Rank','Selection','Category','Method (probability / edge)' if consensus else 'Method','Market odds','Market implied',
                 'Lowest probability' if consensus else 'Calculated probability','Lowest edge (pp)' if consensus else 'Edge (pp)']])),
            html.Tbody([html.Tr([
                html.Td(start+i),html.Td([item['row']['title']+' — '+item['side'].title(),history_button(snapshot,item['row'],'Kalshi',item['side']),
                    structure_button(snapshot,item['row'],'Kalshi',item['side'],surface='quick-filter' if consensus else 'apply-criteria')]),
                html.Td(item['row']['category']),html.Td(method_details(item)),
                html.Td(odds_text(item['implied'])),
                html.Td(probability_text(item['implied'])),html.Td(probability_text(item['probability'])),
                html.Td(f"{100*item['edge']:+.2f}",className='mispricing-edge'),
            ]) for i,item in enumerate(items)] or [html.Tr(html.Td('No qualifying markets.',colSpan=8))]),
        ],className='mispricing-table'),className='mispricing-table-scroll')
    matches=ranked['matches']
    return html.Div([
        html.Div(f"{len(matches)} qualifying selections. Estimated probability > {100*criteria['probability']:g}% and "
                 f"edge > {100*criteria['edge']:g} pp. {ranked['evaluated']} evaluated; "
                 f"{ranked['unavailable']} unavailable or inactive selections excluded."
                 +(f" {ranked['excluded']} selections skipped by prompt selection." if ranked.get('excluded') else ''),className='criteria-evaluation',role='status'),
        html.P('Quote snapshot fetched '+format_eastern(snapshot['fetched_at'])+'. Click '+('Quick Filtering' if consensus else 'Apply Criteria')+' to update these results.',className='forecast-history'),
        *[html.P(message,className='ppp-comparison-note' if message.startswith('Game injuries:') else 'mispricing-error',role='status') for message in [*snapshot.get('notices',[]),*models.get('errors',[])]],
        html.Section([html.H3('Top 3 by edge'),table(matches[:3],1)],className='mispricing-top'),
        html.Section([html.H3('Remaining qualifying markets'),table(matches[3:],4)],className='mispricing-remaining'),
        html.P('Edge uses each side’s ask / payout, before trading fees. Calculated probability retains binary settlement equality outcomes. '
               'Unsupported rules, missing history and unavailable prices are excluded. '+
               ('Every method assigned to a prompt must pass both thresholds. Ranking uses the lowest edge across those methods; '
                'a missing estimate from any selected method excludes that selection.' if consensus else ''),className='ppp-comparison-note'),
    ],className='mispricing-results')


def filter_snapshot(service,game,selected,pull=False):
    if selected!='live':
        # Saved prices remain immutable; never mix them with live categories.
        found=next((category for category in CATEGORIES if selected in service.history(game,category)),None)
        if not found:raise ValueError('The selected saved snapshot is unavailable.')
        return service.saved(game,found,selected),'Applied to the selected saved category only. Select Live quotes to scan all categories.'
    snapshot=service.all_quotes(game,refresh=pull,save=pull)
    status='Applied to all linked game categories. Market edges are measured before fees.'
    if pull:status+=' JSON/CSV snapshots saved under data/kalshi_market; reopen them from the category View dropdown.'
    return snapshot,status


def register(app,service,game_forecast,player_forecast,filtered_results=None):
    prompt_fields=dict(type='kalshi-quick-prompt-method',scope=ALL,prompt=ALL)

    @app.callback(Output('kalshi-quick-prompts','children'),Input('kalshi-quick-prompts-panel','open'),
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

    @app.callback(Output('kalshi-models','data'),Input('kalshi-quotes','data'),Input('kalshi-filter-quotes','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input('workspace-page','value'),Input('kalshi-category','value'),State('kalshi-models','data'),State('market-url','hash'))
    def kalshi_models(snapshot,scan,game,method,player_method,selected,page,category,previous,location_hash=None):
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
        if (category==FILTER and previous and previous.get('context')==identity(game,selected,method,player_method)
                and previous.get('scan_sequence')!=scan.get('sequence')):
            # Deliberate rescans may retry missing history; routine price polls do not refit.
            previous={**previous,'predictions':{key:value for key,value in previous.get('predictions',{}).items()
                                              if value.get('probability') is not None},'errors':[]}
        record=calculate(game,snapshot,selected,method,player_method,game_forecast,player_forecast,previous)
        if category==FILTER:record={**record,'scan_sequence':scan.get('sequence')}
        return no_update if record==before else record

    @app.callback(Output('kalshi-model-status','children'),Input('kalshi-models','data'),Input('kalshi-context','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'))
    def kalshi_model_status(models,game,method,player_method,selected):
        if not game:return ''
        if not models or models.get('context')!=identity(game,selected,method,player_method):
            return 'Calculated probabilities: waiting for the selected category and methods.'
        history=models.get('history',{})
        return [html.Span(f"{method} latest historical game: {history.get('game') or 'unavailable'} · "
                          f"{player_method} latest player history: {history.get('player') or 'unavailable'}"),
                *[html.P(error,className='ppp-comparison-note' if error.startswith('Game injuries:') else 'mispricing-error') for error in models.get('errors',[])]]

    rows=dict(type='kalshi-comparison-row',sport=ALL,game=ALL,ticker=ALL,side=ALL)
    @app.callback(Output(rows,'children'),Output(rows,'className'),
                  Input('kalshi-models','data'),Input('kalshi-applied-criteria','data'),
                  Input('kalshi-game-method','value'),Input('kalshi-player-method','value'),Input('kalshi-snapshot','value'),
                  Input(rows,'id'),State('kalshi-quotes','data'),State('kalshi-context','data'),
                  State('kalshi-category','value'),State('workspace-page','value'),State('market-url','hash'),
                  State('kalshi-detail-table','open'),prevent_initial_call=False)
    def kalshi_model_rows(models,criteria,method,player_method,selected,identities,snapshot,game,category,page,location_hash,table_open):
        # Update only existing rows after forecasts finish, preserving accordions.
        if (page!='kalshi' or not game or not snapshot or category==FILTER or not identities
                or route(location_hash)!=(game['sport'],game['id'])
                or (snapshot['sport'],snapshot['game_id'],snapshot['category'])!=(game['sport'],game['id'],category)):
            raise PreventUpdate
        shown={**decorate(snapshot,game,selected,method,player_method,models,criteria),'game':game}
        rendered={}
        for row in shown['rows']:
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

    @app.callback(Output('kalshi-filter-results','children'),
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
        if not models or models.get('context')!=identity(game,selected,method,player_method):
            return html.P('Calculating the selected forecast methods…',className='saved-market-empty')
        displayed={**decorate(snapshot,game,selected,method,player_method,models,criteria),'game':game}
        return results(displayed,rank(displayed,criteria),criteria,models)

    @app.callback(Output('kalshi-quick-scan','data'),Output('kalshi-quick-status','children'),
                  Input('kalshi-quick-filter','n_clicks'),State('kalshi-quick-probability','value'),State('kalshi-quick-edge','value'),
                  State('kalshi-quick-game-methods','value'),State('kalshi-quick-player-methods','value'),
                  State('kalshi-context','data'),State('kalshi-category','value'),State('kalshi-snapshot','value'),
                  State('workspace-page','value'),State('market-url','hash'),
                  State(prompt_fields,'value'),State(prompt_fields,'id'),prevent_initial_call=True,
                  running=[(Output('kalshi-quick-filter','disabled'),True,False)])
    def kalshi_quick_scan(clicks,probability,edge,methods,player_methods,game,category,selected,page,location_hash,prompt_values=None,prompt_ids=None):
        if not clicks or page!='kalshi' or not game or category!=FILTER:raise PreventUpdate
        if route(location_hash)!=(game['sport'],game['id']):raise PreventUpdate
        try:
            criteria=criteria_values(probability,edge)
            methods,player_methods=selected_methods(methods,player_methods)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope('kalshi',game['sport'],game['id']))
            snapshot,status=filter_snapshot(service,game,selected)
            ranked,errors=quick_calculate(game,snapshot,selected,methods,player_methods,criteria,game_forecast,player_forecast,overrides)
            if filtered_results is not None:
                try:
                    filtered_results.save(snapshot,ranked,criteria,provider='Kalshi',game=game,
                        request=dict(selection=selected,methods=methods,player_methods=player_methods,prompt_methods=overrides,sequence=clicks))
                except (DataSourceError,OSError,ValueError) as exc:
                    errors=[*errors,'Quick Filtering completed, but its report was not saved: '+str(exc)]
            return dict(context=identity(game,selected,methods,player_methods),criteria=criteria,
                        snapshot=snapshot,ranked=ranked,errors=errors,prompt_methods=overrides),status+' Every method assigned to a prompt must pass both thresholds.'
        except (DataSourceError,OSError,ValueError,TypeError) as exc:
            return no_update,str(exc)+' Previous Quick Filtering results remain saved.'

    @app.callback(Output('kalshi-quick-results','children'),Input('kalshi-quick-scan','data'),Input('kalshi-context','data'),
                  Input('kalshi-quick-game-methods','value'),Input('kalshi-quick-player-methods','value'),
                  Input('kalshi-quick-probability','value'),Input('kalshi-quick-edge','value'),Input('kalshi-snapshot','value'),
                  Input('kalshi-category','value'),Input('workspace-page','value'),
                  Input(prompt_fields,'value'),State(prompt_fields,'id'),prevent_initial_call=True)
    def kalshi_quick_results(scan,game,methods,player_methods,probability,edge,selected,category,page,prompt_values=None,prompt_ids=None):
        if page!='kalshi' or category!=FILTER:raise PreventUpdate
        prompt=html.P('Click Quick Filtering to compare all selected methods and see the top three edges.',className='saved-market-empty')
        try:
            methods,player_methods=selected_methods(methods,player_methods)
            criteria=criteria_values(probability,edge)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope(
                'kalshi',(game or {}).get('sport'),(game or {}).get('id')))
        except ValueError:return prompt
        if (not scan or not game or scan['context']!=identity(game,selected,methods,player_methods)
                or scan['criteria']!=criteria or scan.get('prompt_methods',{})!=overrides):return prompt
        return results({**scan['snapshot'],'game':game},scan['ranked'],criteria,dict(errors=scan['errors']))
