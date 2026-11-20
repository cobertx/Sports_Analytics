"""Historical data, performance and career callbacks."""

from collections import OrderedDict
import math
from time import time
from threading import Lock
from dash import Input, Output, State, ctx, dcc, html, no_update, set_props
from dash.exceptions import PreventUpdate, MissingCallbackContextException
import pandas as pd
import plotly.graph_objects as go
from sports_analytics.data import DataSourceError, PLAYER_SPORTS, Query, SOURCES
from sports_analytics.career import career_statistic
from sports_analytics.forecast import recorded_games, rolling_baseline
from sports_analytics.filter_jobs import request_key
from app.sections.historical.historical_view import historical_route, statistics_kind
from sports_analytics.historical_links import history_target,matches_dataset,target_entity,target_metric,market_metrics,NBA_COMBINATIONS
from app.sections.markets.prompt_history import prior_history,outcomes as prompt_outcomes,annotate as annotate_prompt
from sports_analytics.metrics import metric_label, preferred_metric, relevant_metrics, statistic_categories, statistic_category
from sports_analytics.history_scope import analysis_scope, career_scope
from app.shell.navigation import scoped_callback
from sports_analytics.display_time import format_eastern
from app.components.charts import style_figure
from app.components.charts import empty_figure
from app.components.controls import kpi


ALL_TEAMS = "__all__"


def supported_dataset(dataset):
    return bool(dataset) and (dataset["kind"] == "team" or dataset["sport"] in PLAYER_SPORTS or dataset.get('saved_players'))

def filter_team(frame, kind, team):
    if kind == "player" and team and team != ALL_TEAMS:
        return frame[frame["team"] == team]
    return frame

def game_labels(history):
    labels = []
    for row in history.itertuples():
        if pd.notna(row.event_date):
            played = row.event_date.strftime("%Y-%m-%d")
        elif pd.notna(row.week):
            played = f"{row.season} · Week {int(row.week)}"
        else:
            played = f"{row.season} · Date unavailable"
        matchup = getattr(row, "matchup", None)
        if pd.isna(matchup) or not matchup:
            matchup = f"{row.team} vs {row.opponent} (venue unavailable)"
        labels.append((played, matchup))
    return labels

def performance_figure(history, metric, window, prediction, *, season_history=False):
    sport = history.iloc[0]["sport"]
    label = metric_label(metric, sport)
    x = list(range(1, len(history) + 1))
    details = game_labels(history)
    figure = go.Figure()
    hover = "%{customdata[0]}<br>%{customdata[1]}<br>%{y:.2f}<extra>%{fullData.name}</extra>"
    components=NBA_COMBINATIONS.get(metric,()) if sport=='NBA' else ()
    if components and set(components)<=set(history):
        colors={'pts':'#57dbc4','reb':'#8197ff','ast':'#f6c667'}
        for component in components:
            figure.add_trace(go.Bar(x=x,y=history[component],name=metric_label(component,'NBA'),width=.65,
                marker={'color':colors[component]},customdata=[(*detail,float(total)) for detail,total in zip(details,history[metric])],
                hovertemplate=hover.replace('<extra>','<br>Combined total: %{customdata[2]:.2f}<extra>')))
    else:
        components=()
        figure.add_trace(go.Bar(x=x, y=history[metric], name=label, width=.65,
                               marker={"color": "#57dbc4"}, customdata=details, hovertemplate=hover))
    if season_history:
        windows = (5, 10, 20) if sport == "NBA" else (3, 5, 7)
        styles = [("#a3caff", "solid"), ("#d0b7ff", "dash"), ("#ffb7d0", "dot")]
        for span, (color, dash) in zip(windows, styles):
            average = history[metric].rolling(span, min_periods=span).mean()
            figure.add_trace(go.Scatter(x=x, y=average, mode="lines+markers", name=f"{span}-game average",
                line=dict(color=color, dash=dash, width=2), marker=dict(size=4),
                customdata=details, hovertemplate=hover))
    else:
        figure.add_trace(go.Scatter(x=x, y=history["baseline"], mode="lines", name=f"Prior {window}-game mean",
                                   line={"color": "#8197ff", "dash": "dot"}, customdata=details, hovertemplate=hover))
    step = max(1, math.ceil(len(history) / 10))
    indices = sorted(set(range(0, len(history), step)) | {len(history) - 1})
    ticks = [x[i] for i in indices]
    labels = [details[i][0] if season_history else f"{details[i][0]}<br>{details[i][1]}" for i in indices]
    if not season_history and prediction is not None:
        figure.add_trace(go.Bar(x=[len(history) + 1], y=[prediction], name="Next appearance baseline", width=.65,
                               marker={"color": "#f6c667"},
                               hovertemplate="Next appearance<br>Date and matchup not yet known<br>%{y:.2f}<extra>Baseline</extra>"))
        ticks.append(len(history) + 1)
        labels.append("Next appearance<br>Matchup TBD")
    figure = style_figure(figure)
    figure.update_layout(height=440, barmode="stack" if components else "overlay",
                         meta={'stat_components':list(components)},margin={"l": 55, "r": 25, "t": 35, "b": 115})
    figure.update_xaxes(title="Game date" if season_history else "Game date · Matchup (away @ home)", tickmode="array", tickvals=ticks,
                        ticktext=labels, tickangle=-35, automargin=True)
    figure.update_yaxes(title=label)
    return figure

def career_figure(career, sport, metric, name):
    if career.frame.empty:
        return empty_figure("No career season summaries available for this player and statistic")
    history = career.frame.set_index("season")
    years = list(range(int(history.index.min()), int(history.index.max()) + 1))
    history = history.reindex(years)
    # Missing seasons remain gaps rather than being plotted as zero or joined across.
    values = history["value"].astype(object).where(history["value"].notna(), None).tolist()
    teams = history["team"].fillna("No recorded data").tolist()
    seasons = [f"{year}–{str(year + 1)[-2:]}" if sport == "NBA" else str(year) for year in years]
    statistic = career_statistic(metric, sport)
    games = history.get("games", pd.Series(index=history.index, dtype=float))
    details = [(season, team, f"<br>{played:g} games played" if pd.notna(played) else "")
               for season, team, played in zip(seasons, teams, games)]
    figure = go.Figure(go.Scatter(x=years, y=values, mode="lines+markers", connectgaps=False,
                                 line={"color": "#8197ff", "width": 2}, marker={"size": 7},
                                 name=name, customdata=details,
                                 hovertemplate="%{customdata[0]} · %{customdata[1]}%{customdata[2]}<br>"
                                               + statistic + " %{y:.2f}<extra>%{fullData.name}</extra>"))
    figure = style_figure(figure)
    figure.update_xaxes(title="Career season", tickmode="array", tickvals=years, ticktext=seasons,
                        tickangle=-35, automargin=True)
    figure.update_yaxes(title=statistic)
    return figure


def register(app, service, career_service, career_tasks, ncaaf_model_data, backtesting_service):
    def historical_target(location_hash):
        target=history_target(location_hash)
        if target and target['sport']=='NCAAF' and target['kind']=='team':
            try:target={**target,'name':ncaaf_model_data.resolve_school(target['name'])}
            except (DataSourceError,OSError,ValueError):pass
        return target

    app.clientside_callback("function(page,job,sport){return page!=='historical'||!['NBA','NFL'].includes(sport)||!job?.pending;}",
        Output('career-poll','disabled'),Input('workspace-page','value'),Input('career-job','data'),Input('sport','value'))

    app.clientside_callback('function(figure,...args){return window.dash_clientside.market_transition.history_state(figure,null,...args);}',
        Output('historical-render-scope','data'),Input('trend','figure'),
        Input('dataset','data'),Input('entity','value'),Input('metric','value'),Input('window','value'),
        Input('player-team','value'),Input('market-url','hash'),Input('sport','value'),Input('entity-type','value'),
        Input('season','value'),Input('season-type','value'),Input('week','value'),Input('college-team','value'),
        Input('historical-cache-pending','data'),State('historical-render-scope','data'))

    @scoped_callback(app,'historical',Output("college-fields", "style"), Output("week-fields", "style"), Input("sport", "value"),Input('entity-type','value'))
    def source_details(sport,kind='team'):
        if sport not in SOURCES:
            raise PreventUpdate
        return ({"display": "block" if sport == "NCAAF" and kind!='player' else "none"},
                {"display": "block" if sport != "NBA" else "none"})

    @scoped_callback(app,'historical',Output("entity-type", "value"),Input("sport", "value"),
                  Input('market-url','hash'),State("entity-type", "value"))
    def statistics_modes(sport,location_hash,kind):
        if sport not in SOURCES:
            raise PreventUpdate
        target=history_target(location_hash)
        if target and target['sport']==sport:return target['kind']
        return statistics_kind(sport,historical_route(location_hash) or kind)

    @scoped_callback(app,'historical',Output('season','value'),Output('season-type','value'),Output('college-team','value'),
                  Output('week','value'),Input('market-url','hash'))
    def historical_market_scope(location_hash):
        target=historical_target(location_hash)
        if not target:raise PreventUpdate
        return (target.get('season',no_update),'regular',
                target['name'] if target['sport']=='NCAAF' and target['kind']=='team' else no_update,None)

    def college_player_history(season,phase,week):
        frame=backtesting_service.player_inputs('NCAAF')
        frame=frame[pd.to_numeric(frame.season,errors='coerce').eq(int(season)) & frame.season_type.eq(phase)].copy()
        if week is not None:
            if 'week' not in frame:raise DataSourceError('Week metadata is unavailable in saved college player history. Clear the week filter.')
            frame=frame[pd.to_numeric(frame.week,errors='coerce').eq(int(week))]
        if frame.empty:raise DataSourceError('No saved college player history matches this season and phase. Prepare college player history or choose another season.')
        if 'matchup' not in frame:frame['matchup']=frame.team+' vs '+frame.opponent
        return frame.assign(sport='NCAAF',entity_type='player')

    app.clientside_callback("function(page,pending){return page!=='historical'||!pending;}",
        Output('historical-cache-poll','disabled'),Input('workspace-page','value'),Input('historical-cache-pending','data'))
    app.clientside_callback('''function(cachePending, job, page, hash, sport, kind){
        if (page !== 'historical') return 'hide';
        const scope = job?.scope;
        const careerPending = job?.pending && kind === 'player' && scope?.kind === kind &&
            scope.sport === sport && scope.route === (hash || '');
        return cachePending || careerPending ? 'show' : 'auto';
    }''', Output('historical-loading','display'),
        Input('historical-cache-pending','data'), Input('career-job','data'),
        Input('workspace-page','value'), Input('market-url','hash'), Input('sport','value'), Input('entity-type','value'))

    @scoped_callback(app,'historical',Output("dataset", "data"), Output("load-status", "children"),
                  Output("dataset-summary", "children"), Output("export", "disabled"),
                  Output('historical-cache-pending','data'),
                  Input("load", "n_clicks"), Input("refresh", "n_clicks"), Input("sport", "value"),
                  Input("season", "value"), Input("entity-type", "value"), Input("season-type", "value"),
                  Input("college-team", "value"), Input("week", "value"), Input("workspace-page", "value"), State('dataset','data'),
                  Input('market-url','hash'),
                  Input('historical-cache-poll','n_intervals'),
                  Input('history-update-poll','n_intervals'),
                  running=[(Output("load", "disabled"), True, False), (Output("refresh", "disabled"), True, False)])
    def load_data(_load, _refresh, sport, season, kind, phase, team, week, page, previous=None,location_hash=None,_cache_tick=None,_history_tick=None):
        if page != "historical" or sport not in SOURCES:
            raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        target=historical_target(location_hash)
        if target and ctx.triggered_id in {'market-url','workspace-page','sport','entity-type','nav-historical'}:
            # Resolve the incoming link before the visible controls finish updating.
            # Explicit changes to season/week/phase remain normal historical filters.
            if ctx.triggered_id in {'market-url','workspace-page','nav-historical'} or sport==target['sport']:
                sport,kind=target['sport'],target['kind']
                season=target.get('season',season);phase='regular';week=None
                if sport=='NCAAF' and kind=='team':team=target['name']
        elif target and sport==target['sport']:kind=target['kind']
        if sport not in PLAYER_SPORTS and sport!='NCAAF':
            kind = "team"
        try:
            if season is None or int(season) != float(season):
                raise ValueError("Choose a whole season year.")
            if sport != "NBA" and week is not None and int(week) != float(week):
                raise ValueError("Choose a whole week number.")
            if sport=='NCAAF' and kind=='player':
                frame=college_player_history(season,phase,week)
                return (dict(key='saved-college-players',sport=sport,season=season,kind=kind,phase=phase,week=week,saved_players=True),
                        'Saved CollegeFootballData player boxes. This view reads prepared history; it does not download data.',
                        f'{sport} · {season} · {phase} · {len(frame):,} appearances · {frame.entity_id.nunique():,} players',False,False)
            query = Query(sport, int(season), kind, phase, (team or "").strip() if sport == "NCAAF" else "",
                          int(week) if week is not None and sport != "NBA" else None)
            result = service.load(query, refresh=ctx.triggered_id == "refresh",local_first=True)
        except (DataSourceError, ValueError, TypeError) as exc:
            return None, str(exc), "No dataset loaded", True,False
        if result.status=='loading cache':
            name=target['name'] if target else f'{sport} {kind} statistics'
            return None,f'Loading historical data for {name}…','',True,True
        summary = (f"{sport} · {season} · {phase} · {len(result.frame):,} appearances · "
                   f"{result.frame['entity_id'].nunique():,} {kind}s")
        message = f"{result.status.capitalize()} · Retrieved {format_eastern(result.fetched_at)}."
        source=getattr(result,'source',None)
        if isinstance(source,str) and source:message+=' '+source+'.'
        if result.notice:
            message += f" Showing previously saved data. {result.notice}"
        dataset={"key": result.key, "sport": sport, "season": season, "kind": kind, "phase": phase,
                 'week':query.week,'team':query.team}
        revision=getattr(result,'revision',None)
        if isinstance(revision,str) and revision:dataset['revision']=revision
        else:dataset['fetched_at']=result.fetched_at
        # Still check source freshness above, but do not rebuild every dependent
        # selector and chart just because an unchanged page was reopened.
        unchanged=previous==dataset and bool(dataset.get('revision'))
        return no_update if unchanged else dataset, message, summary, False,result.status=='updating cache'

    historical_frames=OrderedDict()
    historical_frames_lock=Lock()
    def historical_frame(dataset):
        saved=None if dataset.get('saved_players') else service.read(dataset['key'])
        frame=college_player_history(dataset['season'],dataset['phase'],dataset.get('week')) if saved is None else saved.frame
        revision=getattr(saved,'revision',None) if saved is not None else None
        if not isinstance(revision,str) or not revision:revision=id(frame)
        key=(dataset['key'],dataset['sport'],dataset['kind'])
        with historical_frames_lock:
            cached=historical_frames.get(key)
            if cached is None or cached[0]!=revision:
                cached=(revision,market_metrics(frame,dataset['sport'],dataset['kind']))
                historical_frames[key]=cached
            historical_frames.move_to_end(key)
            while len(historical_frames)>4:historical_frames.popitem(last=False)
            return cached[1]

    def linked_metric(dataset,entity,metrics,location_hash):
        target=historical_target(location_hash)
        if matches_dataset(target,dataset):
            frame=historical_frame(dataset)
            if target_entity(frame,target)==entity:
                return target_metric(target,metrics,frame[frame.entity_id.eq(entity)]),True
        return None,False

    @scoped_callback(app,'historical',Output('historical-link-status','children'),Input('dataset','data'),Input('entity','value'),
                  Input('metric','value'),Input('market-url','hash'),Input('load-status','children'))
    def historical_market_status(dataset,entity,metric,location_hash,load_status=None):
        target=historical_target(location_hash)
        if not target:return ''
        if not dataset and load_status:
            if isinstance(load_status,str) and load_status.startswith('Loading historical data'):return ''
            return f"Historical data for {target['name']} could not be loaded. {load_status}"
        if not matches_dataset(target,dataset):return ''
        try:
            resolved=target_entity(historical_frame(dataset),target)
        except DataSourceError:return 'Reload historical data to open this market’s history.'
        if resolved is None:
            return f"No unique match for {target['name']} in this season’s saved data. Choose another season or select a name manually."
        if entity!=resolved:return ''
        if metric is None:return f"No recorded statistic for this market is available for {target['name']} in this scope. Choose another season or statistic."
        if target.get('note'):return target['note']
        if target['market'] in {'player_1st_td','player_last_td'}:
            return 'Touchdown counts are shown. These box scores do not record scorer order.'
        return ''

    @app.callback(Output('ncaaf-history-status','children'),Input('dataset','data'),Input('entity','value'),
                  Input('refresh','n_clicks'),State('workspace-page','value'))
    def prepare_college_selection(dataset,entity,_refresh,page):
        if page!='historical' or not dataset or dataset.get('sport')!='NCAAF' or dataset['kind']!='team':return ''
        if not entity:return 'Choose a college school to prepare reusable team/player forecasting CSVs.'
        try:
            frame=service.read(dataset['key']).frame
            selected=frame[frame.entity_id.eq(str(entity))]
            if selected.empty:return ''
            school=str(selected.iloc[-1].entity_name)
            return ncaaf_model_data.prepare_team(school,f"{int(dataset['season'])}-12-31T00:00:00+00:00",
                                                 refresh=ctx.triggered_id=='refresh')
        except (DataSourceError,OSError,ValueError) as exc:
            return f'Historical statistics remain available; forecast preparation needs attention: {exc}'

    @scoped_callback(app,'historical',Output("entity", "options"), Output("entity", "value"),
                  Output("entity", "disabled"), Output("entity", "placeholder"),
                  Input("dataset", "data"), Input("player-team", "value"), State("entity", "value"),Input('market-url','hash'))
    def select_options(dataset, team, previous_entity,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "No data available — see source status"
        try:
            frame = historical_frame(dataset)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        if matches_dataset(historical_target(location_hash),dataset):team=ALL_TEAMS
        frame = filter_team(frame, dataset["kind"], team)
        entities = frame.reindex(columns=["entity_id", "entity_name", "team", "position"]).drop_duplicates("entity_id").sort_values("entity_name")
        options = [{"label": (f"{r.entity_name} ({r.team}" + (f" · {r.position}" if pd.notna(r.position) and r.position else "") + ")")
                    if dataset["kind"] == "player" else r.entity_name,
                    "value": r.entity_id} for r in entities.itertuples()]
        if not options:
            return [], None, True, "No matching observations"
        selected_entity = previous_entity if previous_entity in set(entities["entity_id"]) else options[0]["value"]
        target=historical_target(location_hash)
        placeholder='Choose a team or player'
        if matches_dataset(target,dataset):
            selected_entity=target_entity(frame,target)
            if selected_entity is None:placeholder=f"No unique match for {target['name']} — select manually"
        return options, selected_entity, False, placeholder

    @scoped_callback(app,'historical',Output("player-team-field", "style"), Output("identity-filters", "className"),
                  Output("entity-label", "children"), Input("entity-type", "value"), Input("sport", "value"))
    def show_player_team(kind, sport):
        if kind == "player" and sport in {*PLAYER_SPORTS,'NCAAF'}:
            return {"display": "block"}, "analysis-filters identity-filters", "Player"
        return {"display": "none"}, "analysis-filters identity-filters single-entity", "Team"

    @scoped_callback(app,'historical',Output("player-team", "options"), Output("player-team", "value"),
                  Output("player-team", "disabled"), Output("player-team", "placeholder"),
                  Input("dataset", "data"), State("player-team", "value"),Input('market-url','hash'))
    def select_player_teams(dataset, previous_team,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset) or dataset["kind"] != "player":
            return [], None, True, "Choose player statistics to filter by team"
        try:
            frame = historical_frame(dataset)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        teams = sorted(team for team in frame["team"].dropna().unique() if str(team).strip())
        if not teams:
            return [], None, True, "No team data available"
        options = [{"label": "All teams", "value": ALL_TEAMS}] + [{"label": team, "value": team} for team in teams]
        selected = previous_team if previous_team == ALL_TEAMS or previous_team in teams else ALL_TEAMS
        if matches_dataset(history_target(location_hash),dataset):selected=ALL_TEAMS
        return options, selected, False, "Choose a team"

    def available_metrics(dataset, entity, team):
        if not supported_dataset(dataset) or entity is None:
            return []
        frame = historical_frame(dataset)
        frame = filter_team(frame, dataset["kind"], team)
        selected = frame[frame["entity_id"] == entity]
        return relevant_metrics(selected, dataset["sport"], dataset["kind"])

    @scoped_callback(app,'historical',Output("metric-group", "options"), Output("metric-group", "value"),
                  Output("metric-group", "disabled"), Output("metric-group", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  State("metric-group", "value"), State("metric", "value"),Input('market-url','hash'))
    def select_categories(dataset, entity, team, previous_category, previous_metric,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "Load historical data — see source status above"
        if entity is None:
            return [], None, True, "Choose a team or player first"
        try:
            metrics = available_metrics(dataset, entity, team)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        if not metrics:
            return [], None, True, "No relevant statistic types available"
        options = statistic_categories(metrics, dataset["sport"])
        categories = {option["value"] for option in options}
        preferred = previous_metric if previous_metric in metrics else preferred_metric(metrics)
        value = previous_category if previous_category in categories else statistic_category(preferred, dataset["sport"])
        requested,linked=linked_metric(dataset,entity,metrics,location_hash)
        if linked:value=statistic_category(requested,dataset['sport']) if requested else None
        return options, value, False, "Choose a statistic type"

    @scoped_callback(app,'historical',Output("metric", "options"), Output("metric", "value"),
                  Output("metric", "disabled"), Output("metric", "placeholder"),
                  Input("dataset", "data"), Input("entity", "value"), Input("player-team", "value"),
                  Input("metric-group", "value"), State("metric", "value"),Input('market-url','hash'))
    def select_metrics(dataset, entity, team, category, previous_metric,location_hash=None):
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if not supported_dataset(dataset):
            return [], None, True, "Load historical data — see source status above"
        if entity is None:
            return [], None, True, "Choose a team or player first"
        if category is None:
            return [], None, True, "Choose a statistic type first"
        try:
            metrics = available_metrics(dataset, entity, team)
        except DataSourceError:
            return [], None, True, "Reload the historical data"
        metrics = [metric for metric in metrics if statistic_category(metric, dataset["sport"]) == category]
        if not metrics:
            return [], None, True, "Choose an available statistic type"
        value = previous_metric if previous_metric in metrics else preferred_metric(metrics)
        requested,linked=linked_metric(dataset,entity,metrics,location_hash)
        if linked and requested in metrics:value=requested
        return [{"label": metric_label(c, dataset["sport"]), "value": c} for c in metrics], value, False, "Choose a statistic"

    @scoped_callback(app,'historical',Output("trend", "figure"), Output("forecast-summary", "children"),
                  Output("game-log", "data"), Output("game-log", "columns"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"), Input("window", "value"),
                  Input("player-team", "value"),Input('market-url','hash'),Input('workspace-page','value'))
    def analyze(dataset, entity, metric, window, team,location_hash=None,page=None):
        if page is not None and page!='historical':raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        def rendered(figure,cards,records,columns):
            # Bind the visible result to its route; an earlier player's HTTP
            # response must not be presented under a newly selected player.
            figure.update_layout(meta={**(figure.layout.meta or {}),'navigation_scope':{
                'route':location_hash or '', 'sport':(dataset or {}).get('sport'),'kind':(dataset or {}).get('kind')},
                'analysis_scope':analysis_scope(dataset,entity,metric,window,team,location_hash),
                'link_ready':not historical_target(location_hash) or bool(target)})
            return figure,cards,records,columns
        target=historical_target(location_hash)
        blank = [kpi("OBSERVED AVERAGE", "—", "Selected statistic per appearance"),
                 kpi("NEXT APPEARANCE", "—", "Rolling-average baseline"),
                 kpi("BACKTEST MAE", "—", "Mean absolute error on past games")]
        if not supported_dataset(dataset):
            return rendered(empty_figure(), blank, [], [])
        frame = historical_frame(dataset)
        try:trigger=ctx.triggered_id
        except MissingCallbackContextException:trigger=None
        if matches_dataset(target,dataset) and trigger in {'dataset','market-url','entity','nav-historical'}:
            resolved=target_entity(frame,target)
            if trigger!='entity' or entity==resolved:
                entity=resolved;team=ALL_TEAMS
                own=frame[frame.entity_id.eq(entity)]
                metric=target_metric(target,relevant_metrics(own,dataset['sport'],dataset['kind']),own)
        if entity is None or metric is None:
            return rendered(empty_figure(),blank,[],[])
        frame = filter_team(frame, dataset["kind"], team)
        if metric not in frame:
            return rendered(empty_figure(), blank, [], [])
        selected = frame[frame["entity_id"] == entity]
        if not matches_dataset(target,dataset) or target_entity(frame,target)!=entity:target=None
        if selected.empty or metric not in relevant_metrics(selected, dataset["sport"], dataset["kind"]):
            return rendered(empty_figure(), blank, [], [])
        season_history = bool(target and (target['market'] or target.get('season')))
        scoped_history = prior_history(selected,target)
        history, prediction, mae, evaluated = rolling_baseline(scoped_history, metric, window)
        chart_history = recorded_games(selected, metric) if season_history else history
        if chart_history.empty:
            message = 'No recorded appearances for this statistic in the loaded scope'
            return rendered(empty_figure(message), blank, [], [])
        figure = performance_figure(chart_history, metric, window, prediction, season_history=season_history)
        cards = [kpi("OBSERVED AVERAGE", f"{history[metric].mean():,.2f}" if len(history) else "—",
                     f"{len(history):,} recorded appearances"),
                 kpi("NEXT APPEARANCE", f"{prediction:,.2f}" if prediction is not None else "—",
                     f"Mean of the last {window} appearances" if prediction is not None else f"Requires {window} recorded appearances"),
                 kpi("BACKTEST MAE", f"{mae:,.2f}" if mae is not None else "—",
                     f"{evaluated} past predictions evaluated" if evaluated else f"Requires more than {window} appearances")]
        fields = ["event_date"] + ([] if dataset["sport"] == "NBA" else ["week"])
        components=list((figure.layout.meta or {}).get('stat_components',[]))
        table = history.reindex(columns=fields + ["team", "opponent", "matchup",*components,metric,"baseline"]).copy()
        table["event_date"] = table["event_date"].dt.strftime("%Y-%m-%d")
        table = table.sort_index(ascending=False).round(3)
        # JSON nulls rather than NaN/NaT if schedule metadata is unavailable.
        records = table.astype(object).where(pd.notna(table), None).to_dict("records")
        columns = [{"name": metric_label(c, dataset["sport"]) if c == metric or c in components else c.replace("_", " ").title(), "id": c}
                   for c in table]
        chart_result=prompt_outcomes(chart_history,metric,target)
        if chart_result is not None:
            annotate_prompt(figure,chart_history,metric,target,chart_result)
        result=prompt_outcomes(history,metric,target) if season_history and target.get('as_of') else chart_result
        if result is not None:
            hits,count,pushes=int(result.eq('Hit').sum()),int(result.notna().sum()),int(result.eq('Push').sum())
            scope=' pre-game' if target.get('as_of') else ''
            detail=f'{hits} hits / {count}{scope} appearances · {pushes} pushes'
            if target.get('origin'):
                detail=[detail+'. ',html.A('Back to market',href=target['origin'],className='market-history-button')]
            cards.append(kpi('PROMPT HIT RATE',f'{100*hits/count:.1f}%' if count else '—',detail))
            for record,index in zip(records,table.index):record['prompt_result']=result.loc[index]
            columns.append({'name':'Prompt result','id':'prompt_result'})
        figure.update_layout(uirevision=f"{dataset['sport']}:{dataset['kind']}:{dataset['season']}:{dataset.get('phase')}:{entity}:{metric}:{location_hash or ''}")
        return rendered(figure, cards, records, columns)

    @scoped_callback(app,'historical',Output("career-panel", "style"), Input("entity-type", "value"), Input("sport", "value"))
    def show_career(kind, sport):
        return {"display": "block" if kind == "player" and sport in PLAYER_SPORTS else "none"}

    @scoped_callback(app,'historical',Output("career-trend", "figure"), Output("career-status", "children"),
                  Input("dataset", "data"), Input("entity", "value"), Input("metric", "value"),
                  Input('workspace-page','value'),State('market-url','hash'),State('sport','value'),
                  Input('career-poll','n_intervals'),State('career-job','data'),Input('career-retry','n_clicks'),Input('history-update-poll','n_intervals'))
    def analyze_career(dataset, entity, metric,page=None,location_hash=None,sport=None,_career_tick=None,previous_job=None,_career_retry=None,_history_tick=None):
        if page is not None and page!='historical':raise PreventUpdate
        if location_hash and not historical_route(location_hash):raise PreventUpdate
        if dataset and sport is not None and dataset.get('sport')!=sport:raise PreventUpdate
        def rendered(figure,status):
            scope=career_scope(dataset,entity,metric,location_hash)
            figure.update_layout(meta={**(figure.layout.meta or {}),'career_scope':scope},
                uirevision=f"career:{scope['sport']}:{scope['phase']}:{entity}:{metric}")
            return figure,status
        if _career_tick is not None and (not supported_dataset(dataset) or dataset['kind']!='player' or entity is None or metric is None):
            set_props('career-job',{'data':None})
        if not supported_dataset(dataset) or dataset["kind"] != "player" or entity is None or metric is None:
            return rendered(empty_figure("Choose a player and statistic to load career history"), "")
        try:
            frame = historical_frame(dataset)
            selected = frame[frame["entity_id"] == entity]
            if selected.empty or metric not in relevant_metrics(selected, dataset["sport"], "player"):
                if _career_tick is not None:set_props('career-job',{'data':None})
                return rendered(empty_figure("Choose a statistic relevant to this player"), "")
            name = str(selected.iloc[0]["entity_name"])
            def load_career():
                return career_service.load(dataset['sport'],entity,name,metric,dataset.get('phase','regular')),time()
            # Dash always supplies its poll counter. Direct analytical callers
            # may omit it and retain the synchronous calculation contract.
            if _career_tick is None:
                career,_=load_career()
            else:
                scope=career_scope(dataset,entity,metric,location_hash)
                key=request_key(['career-v1',scope,dataset.get('revision') or dataset.get('fetched_at'),_career_retry])
                cached=career_tasks.peek(key)
                result,pending,error=career_tasks.read(key,load_career,refresh=bool(cached and time()-cached[1]>=300))
                job=dict(job=key,scope=scope,pending=pending,status='pending' if pending else 'error' if error else 'complete',error=error)
                if job!=previous_job:set_props('career-job',{'data':job})
                if pending:
                    if previous_job==job:return no_update,no_update
                    figure=empty_figure('Loading the selected player’s career…')
                    figure.update_layout(meta={'pending':True})
                    return rendered(figure,'Loading career history in the background…')
                if error:raise DataSourceError(error)
                if result is None:raise DataSourceError('Career history is unavailable. Change selection or retry loading history.')
                career=result[0]
        except DataSourceError as exc:
            if _career_tick is not None:
                set_props('career-job',{'data':dict(scope=career_scope(dataset,entity,metric,location_hash),pending=False,status='error',error=str(exc))})
            return rendered(empty_figure("Career history could not be loaded"), str(exc))
        phase_label = "Regular season" if career.phase == "regular" else "Postseason"
        statistic = career_statistic(metric, dataset['sport'])
        status = (f"{name} · {statistic} · {len(career.frame)} recorded seasons · {phase_label} · "
                  f"{career.source}. Career history covers all available seasons and teams.")
        if career.searched:
            status += f" Source coverage checked: {min(career.searched)}–{max(career.searched)}."
        if career.failed:
            status += " Incomplete coverage: could not load " + ", ".join(map(str, career.failed)) + "."
        return rendered(career_figure(career, dataset["sport"], metric, name), status)

    @app.callback(Output("download", "data"), Input("export", "n_clicks"), State("dataset", "data"), prevent_initial_call=True)
    def export_data(_clicks, dataset):
        if not supported_dataset(dataset):
            return no_update
        frame = historical_frame(dataset)
        return dcc.send_data_frame(frame.to_csv, f"{dataset['sport']}_{dataset['season']}_{dataset['kind']}_stats.csv", index=False)
