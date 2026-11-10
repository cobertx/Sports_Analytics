"""Independent presentation guards: optional work cannot hold the shell closed."""
from dash import Input, Output, State, dcc, html

from .loading import indicator


def configure(layout):
    """Keep component IDs mounted; place scope guards around result sections."""
    def visit(node):
        if isinstance(node, (list, tuple)):
            return [visit(child) for child in node]
        if not hasattr(node, 'to_plotly_json'):
            return node
        if hasattr(node, 'children'):
            node.children = visit(node.children)
        identity = getattr(node, 'id', None)
        if isinstance(identity, str) and identity in {'market-page', 'kalshi-page', 'polymarket-page'}:
            node.children = [html.Div([
                html.Div(indicator('Loading the selected game…'), className='section-page-loading'),
                html.P('Game unavailable. Refresh the game list or choose another game.', className='section-page-unavailable'),
            ], className='section-page-status', role='status'), *node.children]
        if getattr(node, 'className', None) == 'historical-results':
            node.id = 'historical-results'
        if identity == 'career-panel':
            node.children.append(html.Button('Retry career history',id='career-retry',n_clicks=0,
                className='export-button',style={'display':'none'}))
        if identity == 'kalshi-model-status':
            return html.Div([node,html.Button('Retry calculation',id='kalshi-model-retry',n_clicks=0,
                className='export-button',style={'display':'none'})])
        return node
    layout=visit(layout)
    layout.children=[dcc.Interval(id=page+'-model-poll',interval=500,n_intervals=0,disabled=True)
                     for page in ('kalshi','summary')]+layout.children
    return layout


def register(app):
    for prefix,page in [('kalshi','kalshi'),('summary','market-summary')]:
        app.clientside_callback('''function(page,models,sport){return page!=='''+repr(page)+'''||
            !['NBA','NFL','NCAAF'].includes(sport)||!models?.pending;}''',
            Output(prefix+'-model-poll','disabled'),Input('workspace-page','value'),Input(prefix+'-models','data'),Input('sport','value'))
    for button,store in [('career-retry','career-job'),('kalshi-model-retry','kalshi-models')]:
        app.clientside_callback("function(result){return result?.status==='error'?{}:{display:'none'};}",
            Output(button,'style'),Input(store,'data'))
    # Shell visibility has no dependency on any data/model completion. In
    # particular, Dash must not schedule it behind an optional career request.
    app.clientside_callback(
        'function(hash,sport,page){return window.dash_clientside.market_transition.shell(hash,sport,page);}',
        Output('app-shell', 'className'), Input('market-url', 'hash'),
        Input('sport', 'value'), Input('workspace-page', 'value'))

    for provider in ('market', 'kalshi', 'polymarket'):
        context = 'market-detail-context' if provider == 'market' else provider+'-context'
        catalog = 'market-schedule-scope' if provider == 'market' else provider+'-games'
        app.clientside_callback(
            'function(hash,sport,context,catalog,schedule){return window.dash_clientside.market_transition.page_guard('
            +repr(provider)+',hash,sport,context,catalog,schedule);}',
            Output(provider+'-page', 'className'), Input('market-url', 'hash'), Input('sport', 'value'),
            Input(context, 'data'), Input(catalog, 'data'),
            *([State('market-schedule', 'data')] if provider == 'market' else []))

    # These guards depend on their own render metadata, never another section.
    app.clientside_callback('''function(scope,hash,sport){
        if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
        const ready=scope?.ready&&(scope.route||'')===(hash||'')&&scope.sport===sport;
        return 'historical-results'+(!ready?' history-view-pending':'')+(ready&&scope?.updating?' history-view-updating':'');
    }''', Output('historical-results', 'className'), Input('historical-render-scope', 'data'),
        Input('market-url','hash'),Input('sport','value'))
    app.clientside_callback('''function(figure,dataset,entity,metric,hash,sport,kind){
        return window.dash_clientside.market_transition.career_guard(figure,dataset,entity,metric,hash,sport,kind);
    }''', Output('career-panel', 'className'), Input('career-trend', 'figure'), Input('dataset', 'data'),
        Input('entity', 'value'), Input('metric', 'value'), Input('market-url', 'hash'),
        Input('sport', 'value'), Input('entity-type', 'value'))

    app.clientside_callback('''function(hash,selection,category,method,player,history){
        return window.dash_clientside.market_transition.market_guard(hash,selection,category,method,player,history);
    }''', Output('market-game-detail', 'className'), Input('market-url', 'hash'),
        Input('market-snapshot', 'value'), Input('market-category', 'value'),
        Input('market-forecast-method', 'value'), Input('player-forecast-method', 'value'),
        Input('market-forecast-history', 'data'))
    for provider in ('kalshi', 'polymarket'):
        app.clientside_callback('''function(hash,selection,category,view,quotes){
            return window.dash_clientside.market_transition.quote_guard('''+repr(provider)+''',hash,selection,category,view,quotes);
        }''', Output(provider+'-detail', 'className'), Input('market-url', 'hash'),
            Input(provider+'-snapshot', 'value'), Input(provider+'-category', 'value'),
            Input(provider+'-rules-signature', 'data'), Input(provider+'-quotes', 'data'))
    app.clientside_callback('''function(bundle,rendered,selected,sport,category){
        const ready=bundle?.sport===sport&&String(bundle?.game?.id)===selected&&rendered?.[0]===bundle?.id&&rendered?.[3]===category;
        return ready?'':'summary-quotes-pending';
    }''',Output('summary-market-sections','className'),Input('summary-quotes','data'),
        Input('summary-rendered','data'),Input('summary-game','value'),Input('sport','value'),Input('summary-category','value'))
    # Model guards have a separate DOM owner, so quote rendering never waits on
    # a forecast result (including when both are downstream of a route change).
    app.clientside_callback('''function(hash,selection,method,player,models){
        return window.dash_clientside.market_transition.model_guard(hash,selection,method,player,models);
    }''', Output('kalshi-market-sections', 'className'), Input('market-url', 'hash'),
        Input('kalshi-snapshot', 'value'), Input('kalshi-game-method', 'value'),
        Input('kalshi-player-method', 'value'), Input('kalshi-models', 'data'))

    app.clientside_callback('''function(hash,classes,previous){
        return window.dash_clientside.market_transition.feedback(hash,classes,previous);
    }''', Output('navigation-feedback', 'children'), Input('market-url', 'hash'),
        Input('app-shell', 'className'), State('navigation-feedback', 'children'))
