"""Parlay drawer callbacks."""

from dash import ALL, Input, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate
from sports_analytics.data import DataSourceError
from sports_analytics.parlay import build_leg,clean_state,quote_key,toggle_leg
from app.sections.parlay.parlay_view import slip_contents, odds as parlay_odds
from app.sections.markets.market_view import _market_probability, selection_text
from app.sections.markets.market_navigation import market_game_sport


def register(app, saved_snapshot, forecast_game_markets, forecast_player_markets):
    @app.callback(Output('parlay-store','data'),Output('parlay-notice','children'),
                  Input({'type':'parlay-add','key':ALL},'n_clicks'),
                  Input({'type':'parlay-remove','key':ALL},'n_clicks'),
                  Input('parlay-clear','n_clicks'),Input('parlay-toggle','n_clicks'),
                  State({'type':'parlay-add','key':ALL},'id'),State({'type':'parlay-remove','key':ALL},'id'),
                  State('market-url','hash'),State('market-snapshot','value'),State('parlay-store','data'),
                  prevent_initial_call=True)
    def update_parlay(add_clicks,remove_clicks,clear_clicks,toggle_clicks,add_ids,remove_ids,
                      location_hash,selection,stored):
        triggered=ctx.triggered_id
        state=clean_state(stored)
        if triggered=='parlay-clear' and clear_clicks:
            return dict(version=1,legs=[],collapsed=False),'Selections cleared.'
        if triggered=='parlay-toggle' and toggle_clicks:
            state['collapsed']=not state['collapsed']
            return state,no_update
        if not isinstance(triggered,dict):raise PreventUpdate
        kind,key=triggered.get('type'),triggered.get('key')
        ids,counts=(add_ids,add_clicks) if kind=='parlay-add' else (remove_ids,remove_clicks)
        if not any(identity==triggered and count for identity,count in zip(ids or [],counts or [])):
            raise PreventUpdate
        if kind=='parlay-remove':
            state['legs']=[leg for leg in state['legs'] if leg['key']!=key]
            return state,'Selection removed.'
        if kind!='parlay-add':raise PreventUpdate
        snapshot=saved_snapshot(location_hash,selection)
        if not snapshot:return no_update,'That saved snapshot is no longer selected. Click the displayed quote again.'
        snapshot={**snapshot,'sport':market_game_sport(location_hash)}
        match=next(((index,row) for index,row in enumerate(snapshot['rows']) if quote_key(snapshot,row,index)==key),None)
        if match is None:return no_update,'The displayed snapshot changed. Click a quote in the current snapshot.'
        index,row=match
        # Exact same quote toggles without fetching/refitting its default model.
        previous=next((leg for leg in state['legs'] if leg['quote_key']==key),None)
        if previous:return toggle_leg(state,previous)
        estimate,error,latest=None,None,None
        try:
            if row['market_key'].startswith('player_'):
                forecast,error,history=forecast_player_markets({**snapshot,'rows':[row]},'WHP')
                if forecast:
                    estimate=forecast.selection(row)
                    if estimate is None:error=forecast.detail(row)
                    latest=history.get('latest_date')
            elif row['market_key'] in {'alternate_spreads','alternate_totals'} and snapshot['sport'] in {'NBA','NFL','NCAAF'}:
                forecast,error=forecast_game_markets(snapshot,'PPP')
                estimate=_market_probability(row,forecast)
                if forecast:latest=forecast.diagnostics.get('last_game_date')
            else:error='PPP is unavailable for this sport/market.'
        except (DataSourceError,OSError,ValueError) as exc:
            error=str(exc)
        if estimate is None and not error:error='Default-model estimate unavailable for this selection.'
        try:
            leg=build_leg(snapshot,row,index,selection_text(row),estimate,error,latest)
        except (TypeError,ValueError) as exc:return no_update,str(exc)
        return toggle_leg(state,leg)

    @app.callback(Output('parlay-body','children'),Output('parlay-drawer','className'),Output('parlay-drawer','style'),
                  Output('parlay-count','children'),Output('parlay-mini','children'),Output('parlay-toggle','aria-expanded'),
                  Input('parlay-store','data'),Input('workspace-page','value'),Input('sport','value'))
    def render_parlay(stored,page,sport):
        state=clean_state(stored)
        body,summary=slip_contents(state['legs'])
        count=summary['legs']
        return (body,'parlay-drawer'+(' is-collapsed' if state['collapsed'] else '')+(' is-empty' if not count else ''),
                {'display':'block' if count and page=='market' and sport!='ODDS_API' else 'none'},f'{count} '+('leg' if count==1 else 'legs'),
                ('Est. ' if summary.get('same_game') else '')+parlay_odds(summary.get('market_odds')),
                'false' if state['collapsed'] else 'true')

    @app.callback(Output({'type':'parlay-add','key':ALL},'className'),
                  Output({'type':'parlay-add','key':ALL},'aria-pressed'),
                  Input('parlay-store','data'),Input({'type':'parlay-add','key':ALL},'id'))
    def selected_parlay_prices(stored,identities):
        selected={leg['quote_key'] for leg in clean_state(stored)['legs']}
        flags=[identity['key'] in selected for identity in identities]
        return (['market-price market-price-button'+(' is-selected' if flag else '') for flag in flags],
                ['true' if flag else 'false' for flag in flags])
