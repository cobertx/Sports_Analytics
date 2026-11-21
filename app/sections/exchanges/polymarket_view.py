"""Polymarket navigation using the shared exchange presentation."""
import re
from dash import Input, Output, State, html
from dash.exceptions import PreventUpdate

from app.sections.exchanges import kalshi_view as exchange
from app.sections.market_summary import summary_probability
from app.sections.markets import game_distribution, prop_probability
from sports_analytics import market_summary as analysis
from sports_analytics.filter_jobs import request_key
from sports_analytics.polymarket import PolymarketService


def route(value):
    match=re.fullmatch(r'#(nba|nfl|ncaaf)-polymarket-(market|game-(\d+))',value or '')
    return (match[1].upper(),match[3]) if match else None


def href(sport, game_id=None):
    return f'#{sport.lower()}-polymarket-'+('game-'+game_id if game_id else 'market')


def layout():
    return exchange.layout('Polymarket')


def cards(catalog):
    return exchange.cards(catalog,'Polymarket',href)


def game_header(game):
    return exchange.game_header(game,'Polymarket',PolymarketService.url(game['slug']) if game else None)


def categories(game):
    return game.get('categories',[]) if game else []


def market_sections(snapshot):
    return exchange.market_sections(snapshot,'Polymarket')


def table(snapshot, include_rules=True):
    return exchange.table(snapshot,include_rules,'Polymarket')


def register_distributions(app, game_forecast, player_forecast, tasks):
    app.clientside_callback("function(state,page){return page!=='polymarket'||!state?.pending;}",
        Output('polymarket-model-poll','disabled'),Input('polymarket-distribution-state','data'),Input('workspace-page','value'))
    @app.callback(Output('polymarket-game-distributions','children'),
        Output('polymarket-prop-result','children'),Output('polymarket-distribution-state','data'),
        Input('polymarket-quotes','data'),Input('polymarket-context','data'),Input('polymarket-category','value'),
        Input('polymarket-snapshot','value'),Input('polymarket-prop-request','data-selection'),
        State('workspace-page','value'),State('market-url','hash'),Input('polymarket-model-poll','n_intervals'),
        State('polymarket-distribution-state','data'),prevent_initial_call=True)
    def polymarket_distributions(snapshot,game,category,selected,selection,page,location,_tick,previous):
        if page!='polymarket':raise PreventUpdate
        if not game or route(location)!=(game['sport'],game['id']):
            return [],None,None
        context=[game['sport'],game['id'],category]
        if not snapshot or [snapshot.get('sport'),snapshot.get('game_id'),snapshot.get('category')]!=context or (
                snapshot.get('selection',selected)!=selected):
            return [],None,None
        game_lines=category=='Game Lines'
        if not game_lines and (not selection or selection.get('context')!=context or selection.get('snapshot')!=selected):
            return [],None,None
        bundle=analysis.exchange_bundle('Polymarket',game,snapshot)
        if bundle is None:
            return ([html.P('Team identities are needed to compare spreads and totals.',className='chart-note')]
                    if game_lines else []),None,None

        method,player_method='PPP','WHP'
        rows,source=analysis.prepare_forecast(bundle)
        kind='game' if game_lines else 'player'
        key=None if game_lines else selection['key']
        if key:
            rows=[row for row in rows if prop_probability.matching_side(key,prop_probability.player_line(row['comparison']))]
        if source is not None:
            source['rows']=(source['rows'] if game_lines else [row['target'] for row in rows if row['target']])
        identity=context+[selected,key[:3] if key else None]
        previous=previous or {}
        cutoff=source['pulled_at'] if source else None
        if selected=='live' and previous.get('identity')==identity and previous.get('cutoff') and source:
            cutoff=source['pulled_at']=previous['cutoff']
        fitted=payloads=None
        pending=False
        if source and source['rows'] and bundle['oriented']:
            def compute():
                if not game_lines:
                    return player_forecast(source,player_method)[0],None
                forecast=game_forecast(source,method)[0]
                if forecast:
                    try:return forecast,game_distribution.distributions(forecast,source,method)[0]
                    except (AttributeError,ValueError,TypeError,OverflowError):pass
                return forecast,None
            job=request_key(['polymarket-distribution',identity,cutoff])
            result,pending,_=tasks.read(job,compute)
            if result is not None:fitted,payloads=result
        fingerprint=request_key([[(row['comparison'],row['implied']) for row in rows],
                                 selection.get('click') if key else None, bool(fitted),pending])
        state=dict(identity=identity,cutoff=cutoff,fingerprint=fingerprint,pending=pending)
        if state==previous:raise PreventUpdate
        if fitted:
            estimates=analysis.calculate(bundle,method,player_method,lambda *_:(fitted,None),
                lambda *_:(fitted,None,{}),kinds=(kind,),prepared=(rows,source),inline=True)
            rows=estimates['rows']
        if game_lines:
            return html.Section([html.H2('Game outcome distributions'),
                *summary_probability.panel(bundle,rows,method,pending,distributions=payloads)],
                className='game-distribution-market'),None,state
        content=prop_probability.chart(key,rows,waiting=pending,fits={player_method:fitted})
        return [],prop_probability.clicked(content,selection),state
