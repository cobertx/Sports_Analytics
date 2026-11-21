"""Explicit submission, bounded background work and scoped result rendering."""
from time import time
from uuid import uuid4

from dash import Input,Output,State,ctx,html,no_update
from dash.exceptions import PreventUpdate

from app.shell.navigation import scoped_callback
from app.sections.markets.mispricing import criteria_values,quick_finish_progress,quick_progress,selected_methods
from app.sections.markets import quick_filter_monitor, quick_visibility
from sports_analytics.filter_jobs import QuickScanTasks,request_key
from sports_analytics.summary_filtering import calculate_filter
from sports_analytics.availability import revision as availability_revision
from app.sections.market_summary import market_summary_view as view
from sports_analytics.market_summary import POLICY_VERSION


def register(app,quotes,game_forecast,player_forecast,source_revision,jobs=None):
    jobs=jobs if jobs is not None else QuickScanTasks(capacity=8)
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

    def chart_result(request,completed,sport,scope=None,selection=None):
        if scope and selection:
            tracked = jobs.latest(quick_filter_monitor.owner(),scope)
            if tracked and (tracked['job'],tracked['bundle_id'])==(selection['job'],selection['bundle']):
                state=jobs.queue.state(tracked['job'])
                if not state['pending'] and not state['error']:
                    return jobs.peek(tracked['job'])
            if (not request or request.get('location',scope[1])!=scope[1] or
                    (request.get('job'),request.get('bundle_id'))!=(selection['job'],selection['bundle'])):
                return None
        if completed and completed.get('job')==request['job'] and completed.get('status')=='complete':
            return ranked_result(request,sport)[0]
        return None

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
        location=quick_visibility.summary_location(bundle,sport)
        bundle=quotes.resolve(bundle)
        token=uuid4().hex
        try:
            if not bundle or bundle['sport']!=sport or str(bundle['game']['id'])!=selected:
                raise ValueError('Select a game and wait for its quotes before running Quick Filtering.')
            if not any((r.get('snapshot') or {}).get('rows') for r in bundle['providers'].values()):
                raise ValueError('No source quotes are available yet. Load a source before running Quick Filtering.')
            methods,players=selected_methods(methods,players,sport);criteria=criteria_values(probability,edge)
            source=revision(sport,methods,players)
            key=request_key(['summary-quick-v1',bundle['id'],methods,players,criteria,POLICY_VERSION,availability_revision(sport),source,token])
            def compute(report):
                report(5,'Preparing source quotes and forecast methods')
                # The first forecast may prepare newly completed games. Refit
                # once against that new revision so all methods share it.
                def report_retry(percent,message):
                    report(86+percent//10,'Updated history · '+message)

                for progress in (report,report_retry):
                    before=[revision(sport,methods,players),availability_revision(sport)]
                    ranked=calculate_filter(bundle,methods,players,criteria,game_forecast,player_forecast,progress)
                    after=[revision(sport,methods,players),availability_revision(sport)]
                    if before==after:
                        report(97,'Publishing filtered selections')
                        return {**ranked,'input_revision':after,'quick_bundle':bundle}
                raise ValueError('Historical inputs changed during the scan. Run Quick Filtering again.')
            _,_,error=jobs.scan(key,compute)
            if error:raise ValueError(error)
            packet = dict(job=key,bundle_id=bundle['id'],game_id=selected,sport=sport,methods=methods,players=players,applied_at=time(),
                criteria=criteria,policy=POLICY_VERSION,availability=availability_revision(sport),source_revision=source,
                partial=bool(bundle.get('updating')),sequence=clicks,location=location)
            game = bundle['game']
            matchup = game['away']['name']+' at '+game['home']['name']
            jobs.track(quick_filter_monitor.owner(),key,('summary',location,selected),packet,
                       provider='DraftKings' if bundle.get('mode')=='draftkings' else 'Market Summary',
                       sport=sport,matchup=matchup,methods=[*methods,*players])
            return packet
        except (ValueError,TypeError) as exc:return dict(error=str(exc),token=token,location=location,game_id=selected,sequence=clicks)

    @scoped_callback(app,'market-summary',Output('summary-quick-results','children'),Output('summary-quick-status','children'),
        Output('summary-quick-completed','data'),Output('summary-quick-progress-state','data'),
        Input('summary-quick-request','data'),Input('summary-quotes','data'),
        Input('summary-category','value'),Input('summary-game','value'),Input('workspace-page','value'),Input('sport','value'),
        Input('summary-quick-poll','n_intervals'),State('summary-quick-completed','data'),Input('history-update-poll','n_intervals'),
        State('summary-quick-progress-state','data'),Input('quick-monitor-completions','data'),prevent_initial_call=True)
    def summary_quick_results(request,bundle,category,selected,page,sport,tick,completed,history_tick=None,previous=None,_completed=None):
        if page!='market-summary' or category not in {view.QUICK,'Game Lines'}:raise PreventUpdate
        location=quick_visibility.summary_location(bundle,sport)
        restored = None
        if category==view.QUICK and (bundle or {}).get('game',{}).get('id') is not None and str(bundle['game']['id'])==selected:
            if (not request or request.get('game_id')!=selected or request.get('location',location)!=location
                    or request.get('job') and request.get('bundle_id')!=bundle['id']):
                restored = jobs.latest(quick_filter_monitor.owner(),('summary',location,selected))
                if restored:
                    request = restored
        if _completed is not None and set(ctx.triggered_prop_ids)=={'quick-monitor-completions.data'} and (
                not restored or not any(key==restored['job'] for key,_ in _completed)):
            raise PreventUpdate
        if category=='Game Lines' and (not request or not request.get('job') or not bundle or
                                      request['bundle_id']!=bundle['id']):raise PreventUpdate
        sequence=(request or {}).get('sequence')
        def publish(content,message,event,progress=no_update):
            return (quick_visibility.scoped(content,location,selected,sequence),
                    quick_visibility.status(message,location,selected,sequence),event,progress)
        prompt=html.P(quick_visibility.PROMPTS['summary'],className='saved-market-empty')
        if not request:return publish(prompt,'',no_update)
        if request.get('error'):
            if (request.get('location'),request.get('game_id'))!=(location,selected):return publish(prompt,'',no_update)
            return publish(prompt,request['error'],dict(token=request['token'],status='error'))
        changed=dict(job=request['job'],status='changed')
        if (request['sport'],request['game_id'],request.get('location',location))!=(sport,selected,location):
            return publish(prompt,'',changed)
        if not bundle or (not restored and request['bundle_id']!=bundle['id']) or request['policy']!=POLICY_VERSION:
            return publish(prompt,'Quotes or historical inputs changed. Run Quick Filtering again to use the current data.',changed)
        def pending_view(progress):
            display=dict(job=request['job'],percent=progress['percent'])
            return publish(quick_progress(progress,'summary'),'',no_update,no_update if display==previous else display)

        state=jobs.queue.state(request['job'])
        if state['pending']:
            if category!='Quick Filtering':raise PreventUpdate
            return pending_view(jobs.progress(request['job']))
        if restored:
            ranked=jobs.peek(request['job'])
            status='complete' if ranked is not None else 'error'
        else:
            ranked,status=ranked_result(request,sport)
        if status=='changed':
            return publish(prompt,'Historical inputs changed. Run Quick Filtering again to use the current data.',changed)
        event=dict(job=request['job'],status='error' if state['error'] else status)
        if event==completed and (not restored or ctx.triggered_id in {'history-update-poll','summary-quick-poll'}):
            raise PreventUpdate
        if event['status']=='error':
            return publish(prompt,'Quick Filtering failed. '+(state['error'] or 'This result expired.')+' Click Run Quick Filtering to retry.',event)
        if not restored and category==view.QUICK and previous and previous.get('job')==request['job']:
            checkpoint=quick_finish_progress(jobs,request['job'],previous.get('percent'))
            if checkpoint:return pending_view(checkpoint)
        submitted_bundle = ranked['quick_bundle'] if restored else quotes.resolve(bundle)
        if submitted_bundle is None:return publish(prompt,'This quote view expired. Select the game again.',dict(job=request['job'],status='expired'))
        return publish(view.quick_results(submitted_bundle,ranked,request['criteria'],request['job']),
            ('Using the quotes and settings from this submitted scan. ' if restored else '')+
            ('Partial source coverage. ' if request.get('partial') else '')+
            f"Quick Filtering completed: {len(ranked['matches'])} qualifying quotes.",event,None)

    return chart_result
