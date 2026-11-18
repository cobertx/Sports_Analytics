"""Isolated navigation, quote collection and forecast callbacks for Market Summary."""
from concurrent.futures import ThreadPoolExecutor
from time import time
from uuid import uuid4

from .navigation import scoped_callback,register_visibility,summary_visibility

from dash import ALL,Input,Output,State,ctx,html,no_update
from dash.exceptions import PreventUpdate

from .data import DataSourceError,SOURCES
from .display_time import format_eastern
from .market_view import game_header
from .nba_markets import NBA_MARKETS
from .nfl_markets import NFL_MARKETS
from .ncaaf_markets import NCAAF_MARKETS
from .mispricing import GAME_METHODS,PLAYER_METHODS
from .filter_jobs import request_key
from .availability import revision as availability_revision
from . import market_summary as analysis
from . import market_summary_view as view


def attempt(function):
    try:return function(),None
    except (DataSourceError,OSError,ValueError,TypeError) as exc:return None,str(exc)


def register(app,schedule,markets,kalshi,polymarket,game_forecast,player_forecast,pages=None,tasks=None,source_revision=None):
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

    @app.callback(Output('summary-game','options'),Output('summary-game','value'),
                  Input('summary-catalog','data'),Input({'type':'summary-game-choice','game':ALL},'n_clicks'),
                  Input('summary-back','n_clicks'),State('summary-game','value'),prevent_initial_call=True)
    def summary_choose(catalog,clicks,back,previous):
        games=(catalog or {}).get('games',[])
        options=[dict(label=f"{format_eastern(game['start'])} · {game['away']['name']} at {game['home']['name']}",value=str(game['id'])) for game in games]
        identities={o['value'] for o in options};clicked=ctx.triggered_id
        chosen=(clicked['game'] if isinstance(clicked,dict) and any(clicks or []) and clicked.get('game') in identities else
                None if clicked=='summary-back' else previous if previous in identities else None)
        return options,chosen

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
                  Input('summary-cache-poll','n_intervals'),prevent_initial_call=True,
                  running=[(Output('summary-pull','disabled'),True,False)])
    def summary_quotes(selected,catalog,clicks,page,sport,previous,_cache_tick=None):
        if page!='market-summary' or not catalog or catalog.get('sport')!=sport:raise PreventUpdate
        game=next((g for g in catalog['games'] if str(g['id'])==selected),None)
        if not game:raise PreventUpdate
        linked={provider:analysis.matching_game(game,sport,catalog.get(provider)) for provider in ('Kalshi','Polymarket')}
        scope=[sport,game,linked];pull=ctx.triggered_id=='summary-pull' and bool(clicks)
        polling=ctx.triggered_id=='summary-cache-poll'
        if polling and not (previous or {}).get('updating'):raise PreventUpdate
        if not pull and not polling and previous and previous.get('scope')==scope:raise PreventUpdate
        def fanduel():
            if pull:
                token=(previous or {}).get('id') or uuid4().hex
                snapshot=markets[sport].pull({**game,'sport':sport},f'summary:{sport}:{selected}:{token}:{clicks}')
            else:snapshot=markets[sport].latest([selected]).get(selected)
            if not snapshot:raise DataSourceError('No saved snapshot. Pull Live Markets to load FanDuel quotes.')
            return {**snapshot,'sport':sport}
        tasks={'FanDuel':fanduel}
        for provider,service in [('Kalshi',kalshi),('Polymarket',polymarket)]:
            if linked[provider]:
                def exchange_quotes(service=service,provider=provider):
                    cache=(pages or {}).get(provider)
                    if cache is not None and not pull:return cache.read(linked[provider],'All Markets')
                    snapshot=service.all_quotes(linked[provider],refresh=pull,save=pull)
                    if cache is not None:cache.remember(linked[provider],'All Markets',snapshot)
                    return snapshot
                tasks[provider]=exchange_quotes
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='summary-quotes') as pool:
            pending={provider:pool.submit(attempt,function) for provider,function in tasks.items()}
            loaded={provider:task.result() for provider,task in pending.items()}
        providers={};status=[]
        for provider in analysis.PROVIDERS:
            snapshot,error=loaded.get(provider,(None,'No unambiguous matching event in the provider catalog.'))
            provider_game=game if provider=='FanDuel' else linked[provider]
            if snapshot and (snapshot.get('sport')!=sport or str(snapshot.get('game_id'))!=str(provider_game['id'])):
                snapshot,error=None,'The provider returned quotes for another game.'
            providers[provider]=dict(snapshot=snapshot,game=provider_game)
            stamp=(snapshot or {}).get('pulled_at') or (snapshot or {}).get('fetched_at') or (snapshot or {}).get('metadata_fetched_at')
            status.append(html.P(provider+': '+(error if error else f"{len(snapshot['rows'])} markets · {format_eastern(stamp) if stamp else 'Quote time unavailable'}")))
            status.extend(html.P(provider+': '+notice) for notice in (snapshot or {}).get('notices',[]))
        if not pull and previous and previous.get('scope')==scope and previous.get('providers')==providers:
            return no_update,status
        return dict(id=uuid4().hex,sport=sport,game=game,scope=scope,providers=providers,
                    updating=any((item['snapshot'] or {}).get('updating') for item in providers.values())),status

    @scoped_callback(app,'market-summary',Output('summary-models','data'),Output('summary-model-status','children'),
                  Input('summary-quotes','data'),Input('summary-game-method','value'),Input('summary-player-method','value'),
                  Input('summary-recalculate','n_clicks'),Input('workspace-page','value'),Input('sport','value'),
                  State('summary-game','value'),State('summary-models','data'),
                  *([Input('summary-model-completed','data'),Input('history-update-poll','n_intervals')] if tasks is not None else []),prevent_initial_call=True,
                  running=[(Output('summary-recalculate','disabled'),True,False)])
    def summary_models(bundle,method,player_method,clicks,page,sport,selected,previous,_model_tick=None,_history_tick=None):
        if page!='market-summary' or not bundle or bundle.get('sport')!=sport or str(bundle['game']['id'])!=selected:raise PreventUpdate
        if any((record.get('snapshot') or {}).get('page_pending') and not (record.get('snapshot') or {}).get('rows')
               for record in bundle.get('providers',{}).values()):raise PreventUpdate
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
        if page!='market-summary':raise PreventUpdate
        if not bundle or bundle.get('sport')!=sport or str(bundle['game']['id'])!=selected:
            return html.P('Select a game to load its provider quotes.',className='saved-market-empty'),None
        models=stored_models(models)
        ready=analysis.current_models(models,bundle,method,player_method)
        signature=[bundle['id'],method,player_method,category,analysis.POLICY_VERSION]
        if signature==previous:raise PreventUpdate
        rows=models['rows'] if ready else analysis.clean(analysis.entries(bundle))
        return [html.P('Sportsbook probabilities exclude pushes. Exchange probabilities keep each binary contract’s equality outcome. '
                       'Different settlement rules can produce different estimates for similar-looking lines. Unsupported targets remain visible without estimates.',
                       className='ppp-comparison-note'),*view.sections(bundle,rows,category)],signature

    cells=dict(type='summary-model-cell',bundle=ALL,provider=ALL,index=ALL,side=ALL)
    @scoped_callback(app,'market-summary',Output(cells,'children'),Input('summary-models','data'),
                  Input('summary-game-method','value'),Input('summary-player-method','value'),Input(cells,'id'),
                  Input('workspace-page','value'),Input('sport','value'),State('summary-quotes','data'),State('summary-game','value'),
                  prevent_initial_call=False)
    def summary_model_cells(models,method,player_method,identities,page,sport,bundle,selected):
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
        lookup={(row['provider'],row['index'],row['side'] or ''):row for row in rows}
        values=[]
        for identity in identities:
            row=lookup.get((identity['provider'],identity['index'],identity['side'])) if identity['bundle']==bundle['id'] else None
            values.append(view.model_values(row) if row else no_update)
        return values
