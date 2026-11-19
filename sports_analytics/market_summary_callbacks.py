"""Isolated navigation, quote collection and forecast callbacks for Market Summary."""
from concurrent.futures import ThreadPoolExecutor
import json
from time import time
from uuid import uuid4

from .navigation import scoped_callback,register_visibility,summary_visibility

from dash import ALL,Input,Output,State,ctx,html,no_update
from dash.exceptions import PreventUpdate
from flask import has_request_context

from .data import DataSourceError,SOURCES
from .display_time import format_eastern
from .market_view import game_header
from .nba_markets import NBA_MARKETS
from .nfl_markets import NFL_MARKETS
from .ncaaf_markets import NCAAF_MARKETS
from .mispricing import GAME_METHODS,PLAYER_METHODS
from .filter_jobs import request_key
from .availability import revision as availability_revision
from .summary_quote_jobs import SummaryQuoteJobs
from .page_cache import PageTasks
from . import market_summary as analysis
from . import market_summary_view as view


def attempt(function):
    try:return function(),None
    except (DataSourceError,OSError,ValueError,TypeError) as exc:return None,str(exc)


def quote_version(sport,game,providers):
    """Forecast inputs, excluding refresh notices and other presentation state."""
    transient={'notices','updating','page_pending','saved'}
    return request_key([sport,game,[[p,r.get('game'),
        {k:v for k,v in r['snapshot'].items() if k not in transient} if r.get('snapshot') else None]
        for p,r in sorted(providers.items())]])


def register(app,schedule,markets,kalshi,polymarket,game_forecast,player_forecast,pages=None,tasks=None,source_revision=None):
    quote_jobs=SummaryQuoteJobs()
    quick_jobs=PageTasks(capacity=8)
    from .summary_filter_callbacks import register as register_quick
    register_quick(app,quote_jobs,quick_jobs,game_forecast,player_forecast,source_revision)
    costs={sport:view.pull_estimate(sport,'FanDuel') for sport in SOURCES}
    app.clientside_callback('''function(page,sport,selected,catalog,bundle){
        const costs='''+json.dumps(costs)+''';
        const valid=page==='market-summary'&&catalog?.sport===sport&&
            (catalog.games||[]).some(game=>String(game.id)===selected);
        const current=valid&&bundle?.sport===sport&&String(bundle.game?.id)===selected;
        const disabled=[],labels=[];
        for(const provider of ['FanDuel','Kalshi','Polymarket']){
            const record=current?bundle.providers?.[provider]:null,snapshot=record?.snapshot;
            const count=snapshot?.row_count??(snapshot?.rows||[]).length,markets=count===1?'market':'markets';
            const matched=provider==='FanDuel'||!!record?.game;
            disabled.push(!valid||!matched);
            labels.push(!valid?'Select a game':!current?'Checking source availability…':!matched?
                'No matching event · refresh upcoming games':record?.error?
                (count?'Pull failed · saved quotes shown':'Unavailable · ready to retry'):!snapshot?'No quotes loaded':
                snapshot.updating?(count?`${count} saved ${markets} · updating`:'Loading source quotes…'):
                count?`${count} ${markets} loaded`:'No markets returned');
        }
        return [...disabled,...labels,costs[sport]||'Choose a sport to see the credit estimate'];
    }''',*[Output('summary-source-'+p.lower(),'disabled') for p in analysis.PROVIDERS],
        *[Output('summary-source-'+p.lower()+'-status','children') for p in analysis.PROVIDERS],
        Output('summary-source-fanduel-cost','children'),Input('workspace-page','value'),Input('sport','value'),
        Input('summary-game','value'),Input('summary-catalog','data'),Input('summary-quotes','data'))

    def stored_models(models):
        if not models:return None
        if 'rows' in models:return models
        cached=tasks.peek(models.get('job')) if tasks is not None and models.get('job') else None
        if not isinstance(cached,dict) or 'rows' not in cached:return None
        if any(cached.get(key)!=models.get(key) for key in ('bundle_id','method','player_method','weighting_policy','availability_revision')):
            return None
        return {**models,'rows':cached['rows']}

    if tasks is not None:
        @scoped_callback(app,'market-summary',Output('summary-model-completed','data'),
            Input('summary-model-poll','n_intervals'),State('summary-models','data'),State('summary-game','value'),
            State('workspace-page','value'),State('sport','value'),State('summary-model-completed','data'),prevent_initial_call=True)
        def summary_model_poll(tick,models,selected,page,sport,previous):
            # Poll only the small job reference. Quote bundles and forecast rows
            # must never cross the browser boundary on each timer tick.
            if (page!='market-summary' or not models or not models.get('pending') or not models.get('job')
                    or models.get('sport')!=sport or models.get('game_id')!=selected):raise PreventUpdate
            job=models['job'];cached=tasks.peek(job);state=tasks.queue.state(job)
            if cached is None and state['pending']:raise PreventUpdate
            event=dict(job=job,status='complete' if cached is not None else 'error' if state['error'] else 'missing')
            if event==previous:raise PreventUpdate
            return event

    app.clientside_callback('''function(page,selected,catalog,quotes){
        return page!=='market-summary'||!((catalog&&catalog.updating)||(selected&&quotes&&quotes.updating));
    }''',Output('summary-cache-poll','disabled'),Input('workspace-page','value'),Input('summary-game','value'),
        Input('summary-catalog','data'),Input('summary-quotes','data'))
    register_visibility(app,summary_visibility,Output('market-summary-page','style'),Output('market-data-navigation','style'),
                  Output('market-data-navigation','className'),Output('market-summary-page-link','href'),
                  Output('market-summary-page-link','className'),Output('summary-credit-note','children'))

    @scoped_callback(app,'market-summary',Output('summary-catalog','data'),Output('summary-catalog-status','children'),
                  Input('workspace-page','value'),Input('sport','value'),Input('summary-days','value'),Input('summary-refresh','n_clicks'),
                  State('summary-catalog','data'),Input('summary-cache-poll','n_intervals'),prevent_initial_call=True,
                  running=[(Output('summary-refresh','disabled'),True,False)])
    def summary_catalog(page,sport,days,clicks,previous,_cache_tick=None):
        if page!='market-summary' or sport not in SOURCES or days not in {7,14}:raise PreventUpdate
        refresh=ctx.triggered_id=='summary-refresh'
        polling=ctx.triggered_id=='summary-cache-poll'
        if polling and not (previous or {}).get('updating'):raise PreventUpdate
        if (not refresh and not polling and previous and [previous.get('sport'),previous.get('days')]==[sport,days]
                and 0<=time()-previous.get('checked_at',0)<300):raise PreventUpdate
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='summary-games') as pool:
            tasks=[pool.submit(attempt,function) for function in
                   [lambda:schedule.load(sport,days,refresh=refresh),lambda:kalshi.games(sport,days,refresh=refresh),
                    lambda:polymarket.games(sport,days,refresh=refresh)]]
            (slate,error),(k_games,k_error),(p_games,p_error)=[task.result() for task in tasks]
        errors=[name+': '+message for name,message in [('ESPN schedule',error),('Kalshi',k_error),('Polymarket',p_error)] if message]
        games=slate.games if slate else []
        if slate and (slate.failed_days or slate.stale_days):errors.append('Some schedule dates have missing or saved coverage. Refresh the game list to retry.')
        updating=bool(getattr(slate,'updating',False) or (k_games or {}).get('updating') or (p_games or {}).get('updating'))
        record=dict(sport=sport,days=days,games=games,Kalshi=k_games,Polymarket=p_games,checked_at=time(),updating=updating)
        return record,[html.Span(f'{len(games)} upcoming games. '),*[html.Span(message+' ') for message in errors]]

    @app.callback(Output('summary-game','options'),Input('summary-catalog','data'),prevent_initial_call=True)
    def summary_options(catalog):
        games=(catalog or {}).get('games',[])
        return [dict(label=f"{format_eastern(game['start'])} · {game['away']['name']} at {game['home']['name']}",value=str(game['id'])) for game in games]

    # Select immediately in the browser. A catalog poll must not supersede an
    # in-flight server response containing the user's newer game-card click.
    app.clientside_callback('''function(catalog,clicks,back,previous){
        const identities=new Set((catalog?.games||[]).map(game=>String(game.id)));
        const clicked=window.dash_clientside.callback_context.triggered_id;
        if(clicked?.type==='summary-game-choice'&&(clicks||[]).some(Boolean)&&identities.has(clicked.game))return clicked.game;
        if(clicked==='summary-back'||previous!=null&&!identities.has(previous))return null;
        return window.dash_clientside.no_update;
    }''',Output('summary-game','value'),Input('summary-catalog','data'),
        Input({'type':'summary-game-choice','game':ALL},'n_clicks'),Input('summary-back','n_clicks'),
        State('summary-game','value'),prevent_initial_call=True)

    @scoped_callback(app,'market-summary',Output('summary-game-cards','children'),Output('summary-game-cards','style'),
                  Output('summary-detail','style'),Output('summary-game-header','children'),
                  Input('summary-catalog','data'),Input('summary-game','value'),Input('workspace-page','value'),Input('sport','value'))
    def summary_selection(catalog,selected,page,sport):
        if page!='market-summary':raise PreventUpdate
        games=catalog.get('games',[]) if catalog and catalog.get('sport')==sport else []
        game=next((g for g in games if str(g['id'])==selected),None)
        if game:return no_update,{'display':'none'},{'display':'block'},game_header(game,sport=sport)
        return view.cards(games,sport),{}, {'display':'none'},''

    @scoped_callback(app,'market-summary',Output('summary-quotes','data'),Output('summary-quote-status','children'),
                  Input('summary-game','value'),Input('summary-catalog','data'),Input('summary-pull','n_clicks'),
                  Input('workspace-page','value'),Input('sport','value'),State('summary-quotes','data'),
                  Input('summary-cache-poll','n_intervals'),
                  *[Input('summary-pull-'+p.lower(),'n_clicks') for p in analysis.PROVIDERS],prevent_initial_call=True,
                  running=[(Output(identity,'disabled'),True,False) for identity in
                           ['summary-pull',*['summary-pull-'+p.lower() for p in analysis.PROVIDERS]]])
    def summary_quotes(selected,catalog,clicks,page,sport,previous,_cache_tick=None,fd_clicks=0,k_clicks=0,p_clicks=0):
        if page!='market-summary' or not catalog or catalog.get('sport')!=sport:raise PreventUpdate
        game=next((g for g in catalog['games'] if str(g['id'])==selected),None)
        if not game:raise PreventUpdate
        incoming=previous
        previous=quote_jobs.resolve(previous)
        linked={provider:analysis.matching_game(game,sport,catalog.get(provider)) for provider in ('Kalshi','Polymarket')}
        source_clicks=dict(zip(analysis.PROVIDERS,(fd_clicks,k_clicks,p_clicks)))
        targeted=next((p for p in analysis.PROVIDERS if ctx.triggered_id=='summary-pull-'+p.lower() and source_clicks[p]),None)
        if targeted and incoming and previous is None:
            # An expired server reference must never turn a single-source pull
            # into an empty replacement for the other sources, or spend credits.
            return no_update,[html.P('The saved market view expired. Reselect the game before pulling '+targeted+'. No pull was made.')]
        scope=[sport,game,linked];pull=ctx.triggered_id=='summary-pull' and bool(clicks)
        requested=set(analysis.PROVIDERS) if pull else {targeted} if targeted else set()
        same_game=bool(previous and previous.get('sport')==sport and str(previous.get('game',{}).get('id'))==selected)
        polling=ctx.triggered_id=='summary-cache-poll' and previous is not None
        if polling and not (previous or {}).get('updating'):raise PreventUpdate
        if not requested and not polling and previous and previous.get('scope')==scope:raise PreventUpdate
        def fanduel():
            if 'FanDuel' in requested:
                token=(previous or {}).get('id') or uuid4().hex
                action='FanDuel' if targeted else 'all'
                count=source_clicks['FanDuel'] if targeted else clicks
                snapshot=markets[sport].pull({**game,'sport':sport},f'summary:{sport}:{selected}:{token}:{action}:{count}')
            else:snapshot=markets[sport].latest([selected]).get(selected)
            if not snapshot:raise DataSourceError('No saved snapshot. Use Pull FanDuel to load this source.')
            return {**snapshot,'sport':sport}
        retained={p for p in analysis.PROVIDERS if targeted and p!=targeted}
        if polling and same_game:
            retained.update(p for p,record in previous.get('providers',{}).items()
                            if not (record.get('snapshot') or {}).get('updating') or record.get('error'))
        work={'FanDuel':fanduel} if 'FanDuel' not in retained else {}
        for provider,service in [('Kalshi',kalshi),('Polymarket',polymarket)]:
            if provider in retained:continue
            if linked[provider]:
                def exchange_quotes(service=service,provider=provider):
                    refresh=provider in requested
                    cache=(pages or {}).get(provider)
                    if cache is not None and not refresh:return cache.read(linked[provider],'All Markets',poll=polling)
                    snapshot=service.all_quotes(linked[provider],refresh=refresh,save=refresh)
                    if cache is not None:cache.remember(linked[provider],'All Markets',snapshot)
                    return snapshot
                work[provider]=exchange_quotes
        jobs=dict(previous.get('source_jobs',{})) if same_game else {}
        # A disabled Dash interval can initially be None. Detect the actual web
        # request as well, so its first click can never take the synchronous
        # compatibility path used by direct analytical callers.
        if (has_request_context() or _cache_tick is not None) and not requested:
            loaded={}
            for provider,function in work.items():
                job=jobs.get(provider) if polling else None
                if not job:
                    job=dict(key=uuid4().hex,started_at=time());jobs[provider]=job
                prior=(previous or {}).get('providers',{}).get(provider,{}) if same_game else {}
                result,pending,error=quote_jobs.read(job['key'],lambda f=function:attempt(f),started_at=job['started_at'],
                    repeat=bool(polling and (prior.get('snapshot') or {}).get('updating')))
                snapshot,source_error=result or (None,None)
                error=error or source_error
                if error and not snapshot and (prior.get('snapshot') or {}).get('rows'):
                    snapshot=prior['snapshot']
                if not snapshot and not error:
                    provider_game=game if provider=='FanDuel' else linked[provider]
                    snapshot=dict(sport=sport,game_id=str(provider_game['id']),rows=[],notices=[],fetched_at=None)
                if snapshot and (pending or error):
                    snapshot={**snapshot,'updating':bool((pending or snapshot.get('updating')) and not error),
                              'page_pending':bool((pending or snapshot.get('page_pending')) and not error)}
                loaded[provider]=(snapshot,error)
        else:
            with ThreadPoolExecutor(max_workers=3,thread_name_prefix='summary-quotes') as pool:
                pending={provider:pool.submit(attempt,function) for provider,function in work.items()}
                loaded={provider:task.result() for provider,task in pending.items()}
        providers={};status=[]
        for provider in analysis.PROVIDERS:
            prior=previous.get('providers',{}).get(provider,{}) if same_game else {}
            if provider in retained:
                snapshot,error=prior.get('snapshot'),prior.get('error')
                provider_game=prior.get('game') or (game if provider=='FanDuel' else linked[provider])
            else:
                snapshot,error=loaded.get(provider,(None,'No unambiguous matching event in the provider catalog.'))
                provider_game=game if provider=='FanDuel' else linked[provider]
            if snapshot and (snapshot.get('sport')!=sport or str(snapshot.get('game_id'))!=str(provider_game['id'])):
                snapshot,error=None,'The provider returned quotes for another game.'
            if targeted==provider and error and prior.get('snapshot'):
                snapshot=prior['snapshot'];provider_game=prior['game']
                error='Saved quotes kept. '+error
            if provider=='FanDuel' and provider not in requested and prior.get('error') and snapshot==prior.get('snapshot'):
                error=prior['error']
            providers[provider]=dict(snapshot=snapshot,game=provider_game,error=error)
            stamp=(snapshot or {}).get('pulled_at') or (snapshot or {}).get('fetched_at') or (snapshot or {}).get('metadata_fetched_at')
            message=error or (f"{len(snapshot['rows'])} markets · {format_eastern(stamp) if stamp else 'Quote time unavailable'}"
                              if snapshot else 'No quotes loaded. Use Pull '+provider+'.')
            status.append(html.P(provider+': '+message))
            status.extend(html.P(provider+': '+notice) for notice in (snapshot or {}).get('notices',[]))
        if not requested and previous and previous.get('scope')==scope and previous.get('providers')==providers:
            return no_update,status
        version=quote_version(sport,game,providers)
        prior_version=(previous.get('version') or quote_version(sport,game,previous['providers'])) if same_game else None
        session=(previous.get('session_id') or previous['id']) if same_game else uuid4().hex
        # Overlapping completion polls can all carry the same pending browser
        # State. Identical finished prices must receive the same ID, otherwise
        # each response restarts forecasts and invalidates the rendered table.
        identity=previous['id'] if version==prior_version else request_key([session,version])
        bundle=dict(id=identity,session_id=session,version=version,sport=sport,game=game,scope=scope,providers=providers,source_jobs=jobs,
                    updating=any((item['snapshot'] or {}).get('updating') and not item.get('error') for item in providers.values()))
        return quote_jobs.wire(bundle) if has_request_context() else bundle,status

    @scoped_callback(app,'market-summary',Output('summary-models','data'),Output('summary-model-status','children'),
                  Input('summary-quotes','data'),Input('summary-game-method','value'),Input('summary-player-method','value'),
                  Input('summary-recalculate','n_clicks'),Input('workspace-page','value'),Input('sport','value'),
                  State('summary-game','value'),State('summary-models','data'),
                  *([Input('summary-model-completed','data'),Input('history-update-poll','n_intervals')] if tasks is not None else []),prevent_initial_call=True,
                  running=[(Output('summary-recalculate','disabled'),True,False)])
    def summary_models(bundle,method,player_method,clicks,page,sport,selected,previous,_model_tick=None,_history_tick=None):
        bundle=quote_jobs.resolve(bundle)
        if page!='market-summary' or not bundle or bundle.get('sport')!=sport or str(bundle['game']['id'])!=selected:raise PreventUpdate
        if not any((record.get('snapshot') or {}).get('rows') for record in bundle.get('providers',{}).values()) and any(
                (record.get('snapshot') or {}).get('page_pending') for record in bundle.get('providers',{}).values()):raise PreventUpdate
        if method not in GAME_METHODS or player_method not in PLAYER_METHODS:raise PreventUpdate
        revision=source_revision(dict(sport=sport),method,player_method) if source_revision else []
        if (analysis.current_models(previous,bundle,method,player_method) and stored_models(previous) is not None
                and (previous or {}).get('source_revision',[])==revision
                and ctx.triggered_id!='summary-recalculate'):raise PreventUpdate
        def compute():
            return analysis.calculate(bundle,method,player_method,game_forecast,player_forecast)
        background=tasks is not None and (_model_tick is not None or _history_tick is not None)
        if background:
            key=request_key(['summary-model-v2',bundle['id'],method,player_method,analysis.POLICY_VERSION,availability_revision(sport),revision,clicks])
            models,pending,error=tasks.read(key,compute)
            if pending:
                clear=bool(previous and (previous.get('status')=='complete' or previous.get('job')==key and previous.get('clear')))
                record=dict(job=key,pending=True,status='pending',bundle_id=bundle['id'],sport=sport,game_id=selected,
                    method=method,player_method=player_method,clear=clear)
                if record==previous:return no_update,no_update
                return record,'Quotes available · Calculating probabilities in the background…'
            if error or models is None:
                return dict(job=key,pending=False,status='error',bundle_id=bundle['id'],sport=sport,game_id=selected,
                    method=method,player_method=player_method,clear=True,errors=[error or 'Calculation unavailable.']),\
                    'Calculation failed. '+(error or '')+' Use Recalculate estimates to retry.'
            models={**models,'job':key,'pending':False,'status':'complete'}
        else:models=compute()
        models['source_revision']=revision
        # Overlapping polls for one completed job must publish the same value;
        # a new UUID on every response needlessly retriggers every model cell.
        models['render_id']=models.get('job') or uuid4().hex
        status=(f'{method} game / {player_method} player estimates. Historical cutoff: '+format_eastern(models['cutoff'])+'. '
                'All providers use the earliest quote timestamp, capped at game start, to avoid using later history for older saved prices.' if models['cutoff'] else '')
        # PageTasks already owns the full result. Send a scoped reference rather
        # than retransmitting thousands of model rows on each poll and cell POST.
        wire={key:value for key,value in models.items() if key!='rows'} if background else models
        return wire,[html.Span(status),*[html.P(error,className='ppp-comparison-note' if error.startswith('Game injuries:') else 'mispricing-error') for error in models['errors']]]

    @scoped_callback(app,'market-summary',Output('summary-market-sections','children'),Output('summary-rendered','data'),
                  Input('summary-quotes','data'),State('summary-models','data'),Input('summary-category','value'),
                  Input('summary-game-method','value'),Input('summary-player-method','value'),Input('summary-game','value'),
                  Input('workspace-page','value'),Input('sport','value'),State('summary-rendered','data'),prevent_initial_call=True)
    def summary_sections(bundle,models,category,method,player_method,selected,page,sport,previous=None):
        reference=bundle
        bundle=quote_jobs.resolve(bundle)
        if page!='market-summary':raise PreventUpdate
        if category==view.QUICK:return [],None
        if reference and reference.get('quote_ref') and bundle is None:
            return html.P('This saved market view expired. Select the game again to reload it.',className='saved-market-empty'),\
                [reference['id'],method,player_method,category,analysis.POLICY_VERSION]
        if not bundle or bundle.get('sport')!=sport or str(bundle['game']['id'])!=selected:
            return html.P('Select a game to load its provider quotes.',className='saved-market-empty'),None
        models=stored_models(models)
        ready=analysis.current_models(models,bundle,method,player_method)
        signature=[bundle['id'],method,player_method,category,analysis.POLICY_VERSION]
        if signature==previous:raise PreventUpdate
        rows=models['rows'] if ready else analysis.clean(analysis.entries(bundle))
        return [html.P('Sportsbook probabilities exclude pushes. Exchange probabilities keep each binary contract’s equality outcome. '
                       'Different settlement rules can produce different estimates for similar-looking lines. The right-hand estimates use the quote '
                       'with the highest calculated edge; its provider is named below the edge. Unsupported targets remain visible without estimates.',
                       className='ppp-comparison-note'),*view.sections(bundle,rows,category)],signature

    cells=dict(type='summary-model-table',bundle=ALL,category=ALL,group=ALL)
    @scoped_callback(app,'market-summary',Output(cells,'data-models'),Input('summary-models','data'),
                  Input('summary-game-method','value'),Input('summary-player-method','value'),Input(cells,'data-rows'),
                  Input('workspace-page','value'),Input('sport','value'),State('summary-quotes','data'),State('summary-game','value'),
                  State(cells,'id'),
                  prevent_initial_call=False)
    def summary_model_cells(models,method,player_method,visible,page,sport,bundle,selected,identities):
        bundle=quote_jobs.resolve(bundle)
        # Forecast completion updates cells independently of the immediate quote view.
        if page!='market-summary' or not identities or not bundle or bundle.get('sport')!=sport or str(bundle['game']['id'])!=selected:
            raise PreventUpdate
        same_scope=bool(models and models.get('bundle_id')==bundle['id']
                        and models.get('method')==method and models.get('player_method')==player_method)
        # A completed result can become invalid after a policy or availability
        # change. Clear its displayed values instead of retaining stale numbers.
        clear=bool(same_scope and (models.get('clear') or not models.get('pending')
                   and not analysis.current_models(models,bundle,method,player_method)))
        models=stored_models(models)
        ready=analysis.current_models(models,bundle,method,player_method)
        # The quote view already has pending placeholders. Do not dispatch
        # redundant writes to every mounted number while the forecast runs.
        if not ready and not clear:raise PreventUpdate
        rows=models['rows'] if ready else analysis.clean(analysis.entries(bundle))
        lookup={}
        for items in view.comparison_rows(rows,'All Markets').values():
            for item in items:
                shared=view.shared_estimate(item['quotes'])
                for row in item['quotes'].values():lookup[row['provider'],row['index'],row['side'] or '']=shared
        values=[]
        # One small update per native table. No per-cell Dash outputs/subscribers.
        for identity,anchors in zip(identities,visible):
            if identity['bundle']!=bundle['id']:
                values.append(no_update);continue
            values_by_row={}
            for provider,source_index,side in json.loads(anchors):
                row=lookup.get((provider,source_index,side))
                if row:
                    values_by_row[json.dumps([provider,source_index,side],separators=(',',':'))]=[
                        view.markup(view.metric_values(row,metric)) for metric in view.METRICS]
            values.append(json.dumps(values_by_row,separators=(',',':')))
        return values
