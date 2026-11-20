"""Sportsbook matchup, forecast and filtering callbacks."""

from datetime import date
import hashlib
import json
from uuid import uuid4
from time import time
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from sports_analytics.data import DataSourceError
from sports_analytics.page_cache import forecast_revision
from sports_analytics.player_history_refresh import refresh_live_history
from sports_analytics.filter_jobs import request_key
from sports_analytics.nba_markets import NBA_CATEGORIES
from sports_analytics.ncaaf_forecast_service import NCAAFMarketForecastService
from models.season_weights import POLICY_VERSION
from app.sections.markets.market_view import CATEGORIES, game_header, latest_pull_text, market_sections
from app.sections.markets.market_navigation import market_game_id, market_game_sport, remembered_market
from app.shell.navigation import scoped_callback
from app.sections.markets.mispricing import (
    ALL_PROBABILITIES, FILTER_CATEGORY, consensus_rankings, criteria_values, filter_context,
    quick_progress, rank_markets, results as mispricing_results, run_forecasts, selected_methods,
)
from app.sections.markets import quick_methods
from sports_analytics.display_time import format_eastern
from sports_analytics.availability_adjustment import note as availability_note
from sports_analytics.game_information import ADDITIONAL_INFORMATION
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.epa_service import EPAMarketService
from sports_analytics.nba_ppp_service import NBAPPPMarketService
from sports_analytics.nba_epa_service import NBAEPAMarketService
from sports_analytics.elo_service import ELOMarketService, NBAELOMarketService
from sports_analytics.nba_pace_service import NBAPaceMarketService
from sports_analytics.nba_adjusted_efficiency_service import NBAAdjustedEfficiencyMarketService
from sports_analytics.nba_four_factors_service import NBAFourFactorsMarketService
from app.paths import PROJECT_DIRECTORY
from app.sections.parlay.callbacks import register as register_parlay


def register(app, market_services, game_services, player_services, *, ncaaf_model_data,
             availability_service, market_page_tasks, filter_jobs, filtered_results_service):
    app.clientside_callback('''function(page,history,hash,snapshot,category){
        return page!=='market'||category==='Mispricing'||category==='Additional Information'||!history||!history.pending||
            JSON.stringify(history.context)!==JSON.stringify([hash,snapshot]);
    }''',Output('market-model-poll','disabled'),Input('workspace-page','value'),Input('market-forecast-history','data'),
        Input('market-url','hash'),Input('market-snapshot','value'),Input('market-category','value'))

    app.clientside_callback('function(hash,sport,page){return window.dash_clientside.market_transition.select(hash,sport,page);}',
        Output('sport','value'),Output('workspace-page','value'),Input('market-url','hash'),State('sport','value'),State('workspace-page','value'))

    app.clientside_callback('''function(hash){const m=/^#(nba|nfl|ncaaf)-game-[0-9]+$/.exec(hash||''),sport=m?.[1]||'nfl';
        return ['#'+sport+'-games','FanDuel only ? Up to '+({nba:13,nfl:17,ncaaf:17}[sport])+' credits per new pull'];}''',
        Output('market-back-link','href'),Output('market-detail-credit','children'),Input('market-url','hash'))

    quick_methods.register_controls(app,'market')
    app.clientside_callback('''function(hash,previous,saved){const no=window.dash_clientside.no_update;
        const route=window.dash_clientside.market_navigation.route(hash),entry=route&&window.dash_clientside.market_navigation.entry(saved,route.sport);
        if(!route)return [no,no];
        const options=route.sport==='NBA'?${nba}:${other};
        const allowed=value=>options.some(option=>option.value===value);
        const desired=entry?.route===hash&&allowed(entry.method)?entry.method:(allowed(previous)?previous:'PPP');
        return [options,desired===previous?no:desired];}'''.replace('${nba}',json.dumps(quick_methods.game_options('NBA')))
            .replace('${other}',json.dumps(quick_methods.game_options())),
        Output('market-forecast-method','options'),Output('market-forecast-method','value'),Input('market-url','hash'),
        State('market-forecast-method','value'),State('market-return','data'))

    @scoped_callback(app,'market',Output("market-list", "style"), Output("market-game-detail", "style"),
                  Output("market-game-header", "children"), Output("market-snapshot", "options"), Output("market-snapshot", "value"),
                  Output("market-detail-context", "data"),
                  Input("market-url", "hash"), Input("sport", "value"), Input("workspace-page", "value"),
                  Input("market-schedule", "data"), Input("market-refresh-signal", "data"),
                  State("market-snapshot", "value"), State("market-detail-context", "data"),State('market-return','data'))
    def open_game(location_hash, sport, page, games, refreshed, previous, previous_context,market_return=None):
        game_id = market_game_id(location_hash)
        if page!='market' or sport not in market_services:raise PreventUpdate
        if not game_id or market_game_sport(location_hash)!=sport:
            return {"display":"block"},{"display":"none"},no_update,no_update,no_update,no_update
        if (previous_context and previous_context.get('sport')==sport and str(previous_context.get('id'))==game_id and previous
                and set(ctx.triggered_prop_ids)<={'market-url.hash','workspace-page.value','nav-market.data'}):
            return (no_update,)*6
        snapshots = market_services[sport].history(game_id)
        game = next((game for game in (games or []) if str(game["id"]) == game_id), None)
        if not game and snapshots:
            saved = snapshots[0]
            game = saved.get("game") or {"id": game_id, "start": saved["commence_time"],
                "home": {"name": saved["home_team"], "abbreviation": saved.get("home_abbreviation", "HOME")},
                "away": {"name": saved["away_team"], "abbreviation": saved.get("away_abbreviation", "AWAY")}, "time_tbd": False}
        context = {**game, "sport": sport, "token": previous_context["token"] if previous_context and previous_context.get("sport", "NFL") == sport and str(previous_context.get("id")) == game_id
                   else uuid4().hex} if game else None
        options = [{"label": format_eastern(item["pulled_at"]),
                    "value": item.get("csv_file", item["pulled_at"])} for item in snapshots]
        available={option['value'] for option in options}
        remembered=remembered_market(market_return,sport,location_hash)
        restored=remembered.get('snapshot') if remembered else None
        chosen = previous if previous in available else restored if restored in available else options[0]['value'] if options else None
        if ("market-refresh-signal.data" in ctx.triggered_prop_ids and refreshed and
                refreshed.get("sport", "NFL") == sport and str(refreshed.get("game_id")) == game_id and refreshed.get("snapshot") in {item["value"] for item in options}):
            chosen = refreshed["snapshot"]
        return {"display": "none"}, {"display": "block"}, game_header(game, snapshots[0] if snapshots else None, sport), options, chosen, context

    @app.callback(Output("market-refresh-signal", "data"),
                  Input("market-detail-pull", "n_clicks"), State("market-detail-context", "data"),
                  State("market-url", "hash"), State("sport", "value"), State("workspace-page", "value"),
                  prevent_initial_call=True, running=[(Output("market-detail-pull", "disabled"), True, False)])
    def pull_game_detail(clicks, game, location_hash, sport, page):
        game_id = market_game_id(location_hash)
        if not clicks or sport not in market_services or market_game_sport(location_hash) != sport or page != "market" or not game_id:
            raise PreventUpdate
        if not game or game.get("sport", "NFL") != sport or str(game.get("id")) != game_id:
            return {"sport": sport, "game_id": game_id, "error": "Game information is not loaded yet. Refresh the upcoming schedule before pulling."}
        try:
            snapshot = market_services[sport].pull(game, f"detail:{game_id}:{game['token']}:{clicks}")
        except DataSourceError as exc:
            return {"sport": sport, "game_id": game_id, "error": str(exc), "attempt": clicks}
        return {"sport": sport, "game_id": game_id, "snapshot": snapshot.get("csv_file", snapshot["pulled_at"]), "pulled_at": snapshot["pulled_at"]}

    @scoped_callback(app,'market',Output("market-detail-pull-status", "children"), Input("market-refresh-signal", "data"), Input("market-url", "hash"))
    def detail_pull_status(result, location_hash):
        if not result or result.get("sport", "NFL") != market_game_sport(location_hash) or str(result.get("game_id")) != market_game_id(location_hash):
            return []
        if result.get("error"):
            return html.P(result["error"], className="market-pull-error")
        return html.P("Saved new snapshot · " + format_eastern(result["pulled_at"]), className="detail-pull-success")

    def saved_snapshot(location_hash, selection):
        game_id = market_game_id(location_hash)
        if not game_id or not selection:
            return None
        return next((item for item in market_services[market_game_sport(location_hash)].history(game_id)
                     if item.get("csv_file", item["pulled_at"]) == selection), None)

    @scoped_callback(app,'market',Output("market-category", "children"), Output("market-category", "value"), Output("market-snapshot-status", "children"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), State("market-category", "value"),State('market-return','data'))
    def snapshot_categories(selection, location_hash, previous,market_return=None):
        if not market_game_id(location_hash):raise PreventUpdate
        sport=market_game_sport(location_hash)
        information_tab=(dcc.Tab(label=ADDITIONAL_INFORMATION,value=ADDITIONAL_INFORMATION,className='market-category-tab',
                                selected_className='market-category-tab selected') if sport in {'NBA','NFL'} else None)
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return ([information_tab],ADDITIONAL_INFORMATION,"No saved markets yet. Additional Information is available without pulling odds.") if information_tab is not None else (
                [], None, "No saved markets yet. Use Pull Live Markets above to download a snapshot.")
        available = {row["group"] for row in snapshot["rows"]}
        available.add(FILTER_CATEGORY)
        if information_tab is not None:available.add(ADDITIONAL_INFORMATION)
        categories = NBA_CATEGORIES if market_game_sport(location_hash) == "NBA" else CATEGORIES
        tabs = [dcc.Tab(label=label, value=key, className="market-category-tab", selected_className="market-category-tab selected")
                for key, label in categories if key in available]
        if information_tab is not None:tabs.append(information_tab)
        tabs.append(dcc.Tab(label='Mispricing Filter',value=FILTER_CATEGORY,className='market-category-tab',
                            selected_className='market-category-tab selected'))
        remembered=remembered_market(market_return,market_game_sport(location_hash),location_hash)
        restored=remembered.get('category') if remembered and remembered.get('snapshot')==selection else None
        value = restored if restored in available else previous if previous in available else tabs[0].value if tabs else None
        cost = f"{snapshot['cost']} credits" if snapshot["cost"] is not None else "Cost not reported"
        return tabs, value, [html.Span(latest_pull_text(snapshot) + f" · {cost}"),
                             html.Small("Saved in data/market_data", title=snapshot.get("csv_file", "Saved locally"))]

    @app.callback(Output("market-applied-criteria", "data"), Output("market-criteria-status", "children"),
                  Output("market-category", "value", allow_duplicate=True),
                  Input("market-apply-criteria", "n_clicks"), State("market-probability-threshold", "value"),
                  State("market-edge-threshold", "value"), State("market-category", "value"), prevent_initial_call=True)
    def apply_market_criteria(clicks, probability, edge, category):
        if not clicks:raise PreventUpdate
        try:criteria=criteria_values(probability,edge)
        except ValueError as exc:return no_update,str(exc),no_update
        return criteria, (
            f"Applied to all calculated selections: estimated probability > {probability:g}% and Edge > {edge:g} percentage points. Player tabs and Game Lines use their selected forecast methods."), category or FILTER_CATEGORY

    prompt_fields=dict(type='market-quick-prompt-method',scope=ALL,prompt=ALL)

    @scoped_callback(app,'market',Output('market-quick-prompts','children'),Input('market-quick-prompts-panel','open'),
                  Input('market-url','hash'),Input('market-snapshot','value'),Input('market-category','value'),
                  Input('workspace-page','value'))
    def quick_prompt_methods(opened,location_hash,selection,category,page):
        if ctx.triggered_id=='market-quick-prompts-panel' and not opened:return no_update
        if not opened or page!='market' or category!=FILTER_CATEGORY or not market_game_id(location_hash):return []
        snapshot=saved_snapshot(location_hash,selection)
        return quick_methods.controls('market',(snapshot or {}).get('rows',[]),
            quick_methods.scope('market',market_game_sport(location_hash),market_game_id(location_hash)))

    @app.callback(Output('mispricing-request','data'),Output('market-quick-status','children'),
                  Input('market-quick-filter','n_clicks'),State('market-quick-probability','value'),State('market-quick-edge','value'),
                  State('market-quick-game-methods','value'),State('market-quick-player-methods','value'),
                  State('market-url','hash'),State('market-snapshot','value'),
                  State(prompt_fields,'value'),State(prompt_fields,'id'),prevent_initial_call=True)
    def request_quick_filter(clicks,probability,edge,methods,player_methods,location_hash,selection,prompt_values=None,prompt_ids=None):
        if not clicks:raise PreventUpdate
        try:
            criteria=criteria_values(probability,edge)
            methods,player_methods=selected_methods(methods,player_methods,market_game_sport(location_hash))
            if not market_game_id(location_hash) or not selection:
                raise ValueError('Choose a saved game snapshot before using Quick Filtering.')
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope(
                'market',market_game_sport(location_hash),market_game_id(location_hash)))
            request={**filter_context(location_hash,selection,methods,player_methods,criteria),'sequence':clicks}
            if overrides:request['prompt_methods']=overrides
            return request, ''
        except ValueError as exc:return no_update,str(exc)

    @app.callback(Output('mispricing-filter-panel','style'),Output('market-quote-content','style'),Input('market-category','value'))
    def mispricing_visibility(category):
        selected=category==FILTER_CATEGORY
        return {'display':'block' if selected else 'none'},{'display':'none' if selected or category==ADDITIONAL_INFORMATION else 'block'}

    @scoped_callback(app,'market',Output("player-forecast-method", "options"), Output("player-forecast-method", "value"),
                  Input("market-url", "hash"), State("player-forecast-method", "value"),State('market-return','data'))
    def player_forecast_methods(location_hash, previous,market_return=None):
        sport=market_game_sport(location_hash)
        if not sport:return no_update,no_update
        supported=quick_methods.player_methods(sport)
        saved=remembered_market(market_return,sport,location_hash)
        if saved and saved.get('player_method') in supported:previous=saved['player_method']
        return quick_methods.player_options(sport,detailed=True),previous if previous in supported else 'WHP'

    @scoped_callback(app,'market',Output("game-forecast-history", "children"), Output("player-forecast-history", "children"),
                  Input("market-forecast-history", "data"), Input("market-url", "hash"),
                  Input("market-snapshot", "value"), Input("market-forecast-method", "value"),
                  Input("player-forecast-method", "value"),Input('mispricing-forecast-history','data'))
    def forecast_history(history, location_hash, selection, game_method, player_method,quick_history=None):
        current = history if history and history.get("context") == [location_hash, selection] else {}
        quick=quick_history if quick_history and quick_history.get('context')==[location_hash,selection] else {}
        current={**current,'game':{**quick.get('game',{}),**current.get('game',{})},
                 'player':{**quick.get('player',{}),**current.get('player',{})}}
        def label(kind, method, prefix):
            if not selection:
                return prefix + "No saved snapshot selected"
            if current.get('pending') and ((current.get('view_key') or [None])[0]=='Game')==(kind=='game'):
                return prefix + 'Calculating…'
            record = current.get(kind, {}).get(method)
            if record is None:
                return prefix + ("Calculating…" if current.get('pending') else "Not calculated yet")
            try:
                last = date.fromisoformat(record["latest_date"]).strftime("%m/%d/%Y")
            except (ValueError, TypeError, KeyError):
                return prefix + "Unavailable"
            if kind == "player":
                last += " · " + record["category"] + ("" if record["category"]=='All player props' else " markets")
            return prefix + last
        return (label("game", game_method or "PPP", "Latest historical game used: "),
                label("player", player_method or "WHP", "Latest historical performance used: "))

    model_sources=sorted((PROJECT_DIRECTORY / 'models').rglob('*.py'))
    def forecast_environment(sport):
        revisions=[]
        for path in model_sources:
            stat=path.stat();revisions.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
        if sport in {'NBA','NFL'}:revisions.append(availability_service.revision(sport))
        if sport=='NCAAF':revisions+=forecast_revision(ncaaf_model_data,sport)
        return revisions,int(time()//300)

    def has_game_lines(snapshot):
        return any(row['market_key'] in {'alternate_spreads','alternate_totals','team_totals'} for row in snapshot['rows'])

    def forecast_snapshot_key(snapshot,game=False):
        if game:
            # Native game engines fit team history and ignore the individual
            # quoted lines. Retain both timestamps for snapshot diagnostics.
            return request_key([*(snapshot.get(key) for key in ('sport','game_id','home_team','away_team',
                    'pulled_at','commence_time')), snapshot.get('game'), has_game_lines(snapshot)])
        return request_key({key:value for key,value in snapshot.items() if key!='raw_response'})

    def forecast_identity(snapshot,method,category=None,*,environment=None,snapshot_keys=None):
        sport=snapshot['sport']
        if method in player_services:
            source=player_services[method]
        else:
            source=game_services[sport][method]
        revisions=forecast_revision(source,sport)
        common,epoch=forecast_environment(sport) if environment is None else environment
        game=type(source) in (PPPMarketService,EPAMarketService,ELOMarketService,
                              NBAPPPMarketService,NBAEPAMarketService,NBAELOMarketService,NBAPaceMarketService,
                              NBAAdjustedEfficiencyMarketService,NBAFourFactorsMarketService,NCAAFMarketForecastService)
        scope=forecast_snapshot_key(snapshot,game) if snapshot_keys is None else snapshot_keys[game]
        parameters=[repr(getattr(source,'config',None)),repr(getattr(source,'scenarios',None)),
                    repr(getattr(getattr(source,'model',None),'config',None))]
        return request_key([method,category,POLICY_VERSION,revisions,common,parameters,epoch,scope])

    def forecast_player_markets(snapshot,player_method,category=None):
        if player_method not in quick_methods.player_methods(snapshot['sport']):
            return None,'Choose a player forecast method available for this sport.',{}
        scope=category if player_method=='WHP' else None
        return filter_jobs.forecast(forecast_identity(snapshot,player_method,scope),
                                    lambda:calculate_player_markets(snapshot,player_method,scope))

    def calculate_player_markets(snapshot,player_method,category=None):
        forecast,error=None,None
        rows=[row for row in snapshot['rows'] if row['market_key'].startswith('player_') and
              (category is None or row['group']==category)]
        if not rows:return None,None,{'latest_date':None,'category':'All player props' if category is None else category}
        try:
            player_source=player_services[player_method]
            refresh_live_history(snapshot,player_source)
            if snapshot['sport']=='NCAAF':
                names=[snapshot['home_team'],snapshot['away_team']]
                ncaaf_model_data.prepare(names,snapshot['commence_time'])
                if player_method!='WHP':ncaaf_model_data.prepare_player_inputs(names,snapshot['commence_time'])
            if player_method!='WHP':
                # One joint game forecast preserves the same simulated volumes
                # and player correlations across tabs and the cross-market filter.
                forecast=player_source.forecast(snapshot)
            else:
                if snapshot['sport']=='NCAAF':ncaaf_model_data.prepare_players(snapshot,rows)
                forecast=player_source.forecast({**snapshot,'rows':rows})
            if player_method not in {'PTR','ILA'}:
                forecast=availability_service.apply(snapshot,forecast,player_method)
        except (DataSourceError,OSError,ValueError) as exc:
            error=str(exc)
            if 'No saved player history.' in error:
                error='Player estimates are unavailable because no saved historical player data is available.'
        dates=[item.diagnostics.get('last_game_date') for item in forecast.forecasts.values()] if forecast else []
        dates=[value for value in dates if value]
        return forecast,error,dict(latest_date=max(dates) if dates else None,category='All player props' if category is None else category)

    def forecast_game_markets(snapshot,method):
        return filter_jobs.forecast(forecast_identity(snapshot,method),lambda:calculate_game_markets(snapshot,method))

    def calculate_game_markets(snapshot,method):
        if not has_game_lines(snapshot):return None,None
        try:
            service=game_services[snapshot['sport']][method]
            return availability_service.apply(snapshot,service.forecast(snapshot),method),None
        except (DataSourceError,OSError,ValueError) as exc:return None,str(exc)

    @scoped_callback(app,'market',Output('mispricing-results','children'),Output('mispricing-forecast-history','data'),
                  Input('mispricing-request','data'),Input('market-category','value'),Input('market-url','hash'),
                  Input('market-snapshot','value'),Input('market-quick-game-methods','value'),Input('market-quick-player-methods','value'),
                  Input('market-quick-probability','value'),Input('market-quick-edge','value'),
                  State('mispricing-forecast-history','data'),State('workspace-page','value'),
                  Input(prompt_fields,'value'),State(prompt_fields,'id'),Input('market-quick-poll','n_intervals'),prevent_initial_call=True)
    def quick_filter_markets(request,category,location_hash,selection,methods,player_methods,probability,edge,previous_history,page=None,prompt_values=None,prompt_ids=None,_quick_tick=None):
        if (page is not None and page!='market') or not market_game_id(location_hash):raise PreventUpdate
        prompt=html.P('Click Quick Filtering to show the top three selections and all remaining matches.',className='saved-market-empty')
        def changed_selection():
            if page is None:return prompt,no_update
            previous=previous_history if previous_history and previous_history.get('context')==[location_hash,selection] else {}
            return prompt,dict(previous,context=[location_hash,selection],pending=False)
        if category!=FILTER_CATEGORY:return prompt,no_update
        # Mounting or editing the per-prompt selectors only changes controls.
        # Fitting starts when the button publishes a new request.
        if page is not None and isinstance(ctx.triggered_id,dict):return changed_selection()
        try:
            methods,player_methods=selected_methods(methods,player_methods,market_game_sport(location_hash))
            criteria=criteria_values(probability,edge)
            overrides=quick_methods.choices(prompt_values,prompt_ids,quick_methods.scope(
                'market',market_game_sport(location_hash),market_game_id(location_hash)))
        except ValueError:return changed_selection()
        expected=filter_context(location_hash,selection,methods,player_methods,criteria)
        if overrides:expected['prompt_methods']=overrides
        elif request and request.get('prompt_methods'):return changed_selection()
        if not request or any(request.get(key)!=value for key,value in expected.items()):return changed_selection()
        if (page is not None and ctx.triggered_id in {'market-url','nav-market'} and previous_history
                and previous_history.get('context')==[location_hash,selection] and previous_history.get('filter_key')==request):
            return no_update,no_update
        if page is not None and ctx.triggered_id=='market-quick-poll' and previous_history and previous_history.get('filter_key')==request:
            job=previous_history.get('job')
            if job:
                state=filter_jobs.tasks.queue.state(job)
                if state['pending']:return quick_progress(filter_jobs.tasks,job,'market'),no_update
                if state['error']:return html.P(state['error'],className='mispricing-error'),dict(previous_history,pending=False)
                result=filter_jobs.tasks.peek(job)
                if result is not None:return result[0],{**result[1],'filter_key':request}
                return html.P('This result expired. Run Quick Filtering again.',className='mispricing-error'),dict(previous_history,pending=False)
        snapshot=saved_snapshot(location_hash,selection)
        if not snapshot:return html.P('That saved snapshot is unavailable. Choose a snapshot and run Quick Filtering again.',className='saved-market-empty'),no_update
        snapshot={**snapshot,'sport':market_game_sport(location_hash)}
        def calculate_filter(report=None):
            if report:report(5,'Preparing saved quotes and forecast methods')
            assignments=quick_methods.plan(snapshot['rows'],methods,player_methods,overrides)
            needed={method for choices in assignments.values() for method in choices}
            game_methods=[method for method in quick_methods.game_methods(snapshot['sport']) if method in needed]
            chosen_players=[method for method in quick_methods.player_methods(snapshot['sport']) if method in needed]
            tasks={method:lambda method=method:forecast_game_markets(snapshot,method) for method in game_methods}
            tasks.update({method:lambda method=method:forecast_player_markets(snapshot,method) for method in chosen_players})
            forecasts=run_forecasts(tasks,snapshot['sport'],report)
            if report:report(85,'Applying thresholds and ranking selections')
            game_rows={**snapshot,'rows':[row for row in snapshot['rows'] if not row['market_key'].startswith('player_')]}
            player_rows={**snapshot,'rows':[row for row in snapshot['rows'] if row['market_key'].startswith('player_')]}
            rankings={method:rank_markets(game_rows,forecasts[method][0],None,ALL_PROBABILITIES,method) for method in game_methods}
            rankings.update({method:rank_markets(player_rows,None,forecasts[method][0],ALL_PROBABILITIES,player_method=method) for method in chosen_players})
            ranked=quick_methods.consensus(rankings,assignments,snapshot['rows'],criteria,
                lambda item:(item['row']['market_key'],item['row']['subject'],item['row']['selection'],str(item['row']['line'])),consensus_rankings)
            context=[location_hash,selection]
            previous=previous_history if previous_history and previous_history.get('context')==context else {}
            history=dict(context=context,filter_key=request,game=dict(previous.get('game',{})),player=dict(previous.get('player',{})),weighting_policy=POLICY_VERSION)
            for method in game_methods:
                forecast=forecasts[method][0]
                history['game'][method]={'latest_date':forecast.diagnostics.get('last_game_date') if forecast else None}
            for method in chosen_players:history['player'][method]=forecasts[method][2]
            errors=[f'{method} forecast: {record[1]}' for method,record in forecasts.items() if record[1]]
            injuries=availability_note(*(record[0] for record in forecasts.values()))
            if injuries:errors.append(injuries)
            if report:report(95,'Saving filtered selections')
            try:
                filtered_results_service.save(snapshot,ranked,criteria,request=request)
            except (DataSourceError,OSError,ValueError) as exc:
                errors.append('Quick Filtering completed, but its report was not saved: '+str(exc))
            return mispricing_results(snapshot,ranked,criteria,errors),history
        if page is None:return calculate_filter()
        environment=forecast_environment(snapshot['sport'])
        snapshot_keys={game:forecast_snapshot_key(snapshot,game) for game in (False,True)}
        identities=[forecast_identity(snapshot,method,environment=environment,snapshot_keys=snapshot_keys)
                    for method in [*methods,*player_methods]]
        job=request_key([{key:value for key,value in request.items() if key!='sequence'},identities])
        explicit=ctx.triggered_id=='mispricing-request'
        result,pending,error=filter_jobs.tasks.scan(job,calculate_filter,refresh=explicit,force=explicit)
        if result is not None and not pending and not error:return result[0],{**result[1],'filter_key':request}
        history=dict(context=[location_hash,selection],filter_key=request,pending=pending,job=job)
        return (html.P(error,className='mispricing-error') if error else quick_progress(filter_jobs.tasks,job,'market')),history

    app.clientside_callback("""function(page,category,history,location,selection){
        const pending=page==='market'&&category==='Mispricing'&&history?.pending&&
            history.context?.[0]===location&&history.context?.[1]===selection;
        return [!pending,!!pending];} """,
        Output('market-quick-poll','disabled'),Output('market-quick-filter','disabled'),
        Input('workspace-page','value'),Input('market-category','value'),Input('mispricing-forecast-history','data'),
        Input('market-url','hash'),Input('market-snapshot','value'))


    @scoped_callback(app,'market',Output("saved-market-sections", "children"), Output("market-forecast-history", "data"),
                  Input("market-category", "value"),
                  Input("market-snapshot", "value"), Input("market-url", "hash"), Input("market-applied-criteria", "data"),
                  Input("market-forecast-method", "value"), Input("player-forecast-method", "value"),
                  State("market-forecast-history", "data"),State('workspace-page','value'),Input('market-model-poll','n_intervals'),Input('history-update-poll','n_intervals'))
    def show_saved_markets(category, selection, location_hash, criteria, method, player_method, previous_history,page=None,_model_tick=None,_history_tick=None):
        if (page is not None and page!='market') or not market_game_id(location_hash):raise PreventUpdate
        if category is None:
            raise PreventUpdate
        if category in {FILTER_CATEGORY,ADDITIONAL_INFORMATION}:return [],no_update
        method = method or "PPP"
        if method not in quick_methods.game_methods(market_game_sport(location_hash)):
            raise PreventUpdate
        context = [location_hash, selection]
        previous = previous_history if previous_history and previous_history.get("context") == context else {}
        view_key=[category,method,player_method,criteria,POLICY_VERSION]
        if (page is not None and ctx.triggered_id=='market-model-poll' and previous.get('view_key')==view_key
                and previous.get('pending') and previous.get('job')
                and market_page_tasks.queue.state(previous['job'])['pending']):
            # Polling a running fit needs no snapshot reads, filesystem scans,
            # hashes or quote/accordion reconstruction.
            return no_update,no_update
        if page is not None and ctx.triggered_id in {'market-url','nav-market'} and previous.get('view_key')==view_key and not previous.get('pending'):
            return no_update,no_update
        history = {"context": context, "view_key":view_key,"game": dict(previous.get("game", {})), "player": dict(previous.get("player", {}))}
        snapshot = saved_snapshot(location_hash, selection)
        if not snapshot:
            return html.P("Use Pull Live Markets above to download this game's first snapshot.", className="saved-market-empty"), history
        if not snapshot["rows"]:
            return html.P("The saved response contains no FanDuel selections.", className="saved-market-empty"), history
        snapshot = {**snapshot, "sport": market_game_sport(location_hash)}
        player_method=player_method or 'WHP'
        if category!='Game' and player_method not in quick_methods.player_methods(snapshot['sport']):raise PreventUpdate
        def calculate_view():
            if category!='Game':return forecast_player_markets(snapshot,player_method,category)
            forecast,error=forecast_game_markets(snapshot,method)
            return forecast,error,{'latest_date':forecast.diagnostics.get('last_game_date') if forecast else None}
        if page is not None:
            # Highlight thresholds and the other market type's method do not
            # change a forecast. Reuse the exact saved-snapshot calculation.
            forecast_scope=['game',method] if category=='Game' else ['player',player_method,category if player_method=='WHP' else 'all']
            if category=='Game':
                source=game_services[snapshot['sport']][method]
            else:
                source = player_services[player_method]
            revision=forecast_revision(source,snapshot['sport'])
            if snapshot['sport'] in {'NBA','NFL'}:revision.append(availability_service.revision(snapshot['sport']))
            if snapshot['sport']=='NCAAF' and category!='Game':revision+=forecast_revision(ncaaf_model_data,'NCAAF')
            key=hashlib.sha256(json.dumps([context,forecast_scope,POLICY_VERSION,revision,snapshot],sort_keys=True).encode()).hexdigest()
            # Preparing a cold forecast can itself create input files. Finish
            # that job rather than submitting another one on every poll.
            if ctx.triggered_id=='market-model-poll' and previous.get('pending') and previous.get('job') and previous.get('view_key')==view_key:
                key=previous['job']
            resume=previous.get('pending') and previous.get('job')==key
            calculated,pending,error=market_page_tasks.read(key,calculate_view)
            history.update(pending=pending,job=key,error=error)
            if pending and previous.get('view_key')==view_key and previous.get('rendered'):
                history.update(rendered=True,keep_visible=True)
                return no_update,history
            if pending and resume and ctx.triggered_id=='market-model-poll':return no_update,no_update
            if pending or calculated is None:
                return market_sections(snapshot,category,model_error=error,method=player_method if category!='Game' else method,
                    criteria=criteria,loading=pending),history
            forecast,model_error,record=calculated
        else:forecast,model_error,record=calculate_view()
        if category != "Game":
            record={**record,'category':category}
            history['player'][player_method]=record
            history['rendered']=True
            return market_sections(snapshot, category, forecast, model_error, criteria, player_method), history
        history['game'][method]=record
        history['rendered']=True
        return market_sections(snapshot, category, forecast, model_error, criteria, method), history

    def exchange_source_revision(game,method,player_method):
        sport=game['sport']
        return [forecast_revision(game_services[sport][method],sport),
                forecast_revision(player_services[player_method],sport)]

    register_parlay(app, saved_snapshot, forecast_game_markets, forecast_player_markets)
    return forecast_game_markets, forecast_player_markets, exchange_source_revision
