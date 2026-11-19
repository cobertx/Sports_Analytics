"""Explicit submission, bounded background work and scoped result rendering."""
from time import time
from uuid import uuid4

from dash import Input,Output,State,html,no_update
from dash.exceptions import PreventUpdate

from .navigation import scoped_callback
from .mispricing import criteria_values,selected_methods
from .filter_jobs import request_key
from .summary_filtering import calculate_filter
from .availability import revision as availability_revision
from . import market_summary_view as view
from .market_summary import POLICY_VERSION


def register(app,quotes,jobs,game_forecast,player_forecast,source_revision):
    app._summary_quick_jobs=jobs
    def revision(sport,methods,players):
        return request_key([source_revision(dict(sport=sport),method,player) for method,player in
            [(m,players[0]) for m in methods]+[(methods[0],p) for p in players[1:]]]) if source_revision else ''

    def ranked_result(request,sport):
        ranked=jobs.peek(request['job'])
        current=[revision(sport,request['methods'],request['players']),availability_revision(sport)]
        if ranked and ranked.get('input_revision',[request.get('source_revision',''),request['availability']])!=current:
            return None,'changed'
        return ranked,'complete' if ranked is not None else 'error'

    def chart_result(request,completed,sport):
        if completed and completed.get('job')==request['job'] and completed.get('status')=='complete':
            return ranked_result(request,sport)[0]
        return None

    app.clientside_callback('''function(category){const quick=category==='Quick Filtering';
        return [quick?{}:{display:'none'},quick?{display:'none'}:{},quick?{display:'none'}:{},quick?{display:'none'}:{}];}''',
        Output('summary-quick-panel','style'),Output('summary-market-sections','style'),Output('summary-forecast-controls','style'),Output('summary-model-status','style'),
        Input('summary-category','value'))

    app.clientside_callback('''function(page,category,request,done,bundle,selected){
        const ready=page==='market-summary'&&(category==='Quick Filtering'||category==='Game Lines')&&
            bundle&&String(bundle.game?.id)===selected;
        const pending=ready&&request?.job&&request.bundle_id===bundle.id&&done?.job!==request.job;
        return [!pending,!!pending||!ready||category!=='Quick Filtering'];
    }''',Output('summary-quick-poll','disabled'),Output('summary-quick-run','disabled'),
        Input('workspace-page','value'),Input('summary-category','value'),Input('summary-quick-request','data'),
        Input('summary-quick-completed','data'),Input('summary-quotes','data'),Input('summary-game','value'))

    @app.callback(Output('summary-quick-request','data'),Input('summary-quick-run','n_clicks'),
        State('summary-quick-game-methods','value'),State('summary-quick-player-methods','value'),
        State('summary-quick-probability','value'),State('summary-quick-edge','value'),State('summary-quotes','data'),
        State('summary-game','value'),State('summary-category','value'),State('workspace-page','value'),State('sport','value'),prevent_initial_call=True)
    def summary_quick_submit(clicks,methods,players,probability,edge,bundle,selected,category,page,sport):
        if not clicks or page!='market-summary' or category!=view.QUICK:raise PreventUpdate
        bundle=quotes.resolve(bundle)
        token=uuid4().hex
        try:
            if not bundle or bundle['sport']!=sport or str(bundle['game']['id'])!=selected:
                raise ValueError('Select a game and wait for its quotes before running Quick Filtering.')
            if not any((r.get('snapshot') or {}).get('rows') for r in bundle['providers'].values()):
                raise ValueError('No source quotes are available yet. Load a source before running Quick Filtering.')
            methods,players=selected_methods(methods,players);criteria=criteria_values(probability,edge)
            source=revision(sport,methods,players)
            key=request_key(['summary-quick-v1',bundle['id'],methods,players,criteria,POLICY_VERSION,availability_revision(sport),source,token])
            def compute():
                # The first forecast may prepare newly completed games. Refit
                # once against that new revision so all methods share it.
                for _ in range(2):
                    before=[revision(sport,methods,players),availability_revision(sport)]
                    ranked=calculate_filter(bundle,methods,players,criteria,game_forecast,player_forecast)
                    after=[revision(sport,methods,players),availability_revision(sport)]
                    if before==after:return {**ranked,'input_revision':after}
                raise ValueError('Historical inputs changed during the scan. Run Quick Filtering again.')
            jobs.read(key,compute)
            return dict(job=key,bundle_id=bundle['id'],game_id=selected,sport=sport,methods=methods,players=players,applied_at=time(),
                criteria=criteria,policy=POLICY_VERSION,availability=availability_revision(sport),source_revision=source,partial=bool(bundle.get('updating')))
        except (ValueError,TypeError) as exc:return dict(error=str(exc),token=token)

    @scoped_callback(app,'market-summary',Output('summary-quick-results','children'),Output('summary-quick-status','children'),
        Output('summary-quick-completed','data'),Input('summary-quick-request','data'),Input('summary-quotes','data'),
        Input('summary-category','value'),Input('summary-game','value'),Input('workspace-page','value'),Input('sport','value'),
        Input('summary-quick-poll','n_intervals'),State('summary-quick-completed','data'),Input('history-update-poll','n_intervals'),prevent_initial_call=True)
    def summary_quick_results(request,bundle,category,selected,page,sport,tick,completed,history_tick=None):
        if page!='market-summary' or category not in {view.QUICK,'Game Lines'}:raise PreventUpdate
        if category=='Game Lines' and (not request or not request.get('job') or not bundle or
                                      request['bundle_id']!=bundle['id']):raise PreventUpdate
        prompt=html.P('Choose methods and thresholds, then click Run Quick Filtering.',className='saved-market-empty')
        if not request:return prompt,'',no_update
        if request.get('error'):return prompt,request['error'],dict(token=request['token'],status='error')
        if (not bundle or (request['sport'],request['game_id'],request['bundle_id'])!=(sport,selected,bundle['id']) or
                request['policy']!=POLICY_VERSION):
            return prompt,'Quotes or historical inputs changed. Run Quick Filtering again to use the current data.',dict(job=request['job'],status='changed')
        bundle=quotes.resolve(bundle)
        if bundle is None:return prompt,'This quote view expired. Select the game again.',dict(job=request['job'],status='expired')
        state=jobs.queue.state(request['job'])
        if state['pending']:
            if category!='Quick Filtering':raise PreventUpdate
            return html.P('Calculating the selected methods in the background…',role='status'),\
                'Quick Filtering is running. You can continue browsing game lines and player props.',no_update
        ranked,status=ranked_result(request,sport)
        if status=='changed':
            return prompt,'Historical inputs changed. Run Quick Filtering again to use the current data.',dict(job=request['job'],status='changed')
        event=dict(job=request['job'],status='error' if state['error'] else status)
        if event==completed:raise PreventUpdate
        if event['status']=='error':return prompt,'Quick Filtering failed. '+(state['error'] or 'This result expired.')+' Click Run Quick Filtering to retry.',event
        return view.quick_results(bundle,ranked,request['criteria']),\
            ('Partial source coverage. ' if request.get('partial') else '')+f"Quick Filtering completed: {len(ranked['matches'])} qualifying quotes.",event

    return chart_result
