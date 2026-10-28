"""Local, nonblocking feedback for the callback output being updated."""
from dash import Input,Output,dcc,html


def indicator(message):
    return html.Span([html.Span(className='action-spinner',**{'aria-hidden':'true'}),message],
                     className='action-loading-message',role='status',**{'aria-live':'polite'})


def loading(children,message='Loading…'):
    identity=getattr(children,'id',None)
    return dcc.Loading(children,show_initially=False,delay_show=150,delay_hide=100,
                       overlay_style={'visibility':'visible'},parent_className='action-loading',
                       custom_spinner=indicator(message),**({'id':identity+'-loading'} if isinstance(identity,str) else {}))


# These outputs belong to the work itself, including the first stage of
# callbacks that subsequently render a table from a Store.
MESSAGES={
    'load-status':'Loading historical data…',
    'market-detail-pull-status':'Pulling live markets…',
    'market-criteria-status':'Applying criteria…',
    'market-quick-status':'Preparing Quick Filtering…',
    'kalshi-quick-status':'Calculating and filtering markets…',
    'kalshi-criteria-status':'Applying criteria…',
    'kalshi-quote-status':'Loading market quotes…',
    'polymarket-quote-status':'Loading market quotes…',
    'kalshi-model-status':'Calculating probabilities…',
    'summary-catalog-status':'Loading upcoming games…',
    'summary-model-status':'Calculating probabilities…',
    'performance-status':'Loading saved backtests…',
    'performance-results':'Comparing model performance…',
    'backtest-run-status':'Starting simulation…',
    'backtest-history-status':'Loading saved simulation…',
    'backtest-input-status':'Loading simulation inputs…',
    'performance-input-status':'Loading saved categories…',
    'comparison-status':'Loading comparison…',
    'information-status':'Loading information…',
    'game-information-content':'Loading game information…',
    'structure-content':'Loading market activity…',
    'filtered-results-status':'Loading saved filter results…',
    'pnl-status':'Saving ledger changes…',
    'pnl-import-message':'Preparing statement preview…',
}


def configure(layout):
    """Keep existing IDs/callbacks; place feedback at the top of each result."""
    def visit(node,inside=False):
        if isinstance(node,(list,tuple)):return [visit(child,inside) for child in node]
        if not hasattr(node,'to_plotly_json'):return node
        is_loading=isinstance(node,dcc.Loading)
        identity=getattr(node,'id',None)
        message=MESSAGES.get(identity) if isinstance(identity,str) else None
        if is_loading:
            node.show_initially=False
            node.delay_show=150
            node.delay_hide=100
            node.overlay_style={'visibility':'visible'}
            node.parent_className='action-loading'
            node.custom_spinner=indicator('Loading…')
        if hasattr(node,'children'):node.children=visit(node.children,inside or is_loading)
        if message and not inside and not is_loading:
            return loading(node,message)
        return node
    return visit(layout)


def register(app):
    # A background source can outlive the HTTP callback that queued it. Keep
    # feedback visible until its pending state clears, without another request.
    for output,store,page,condition in [
        ('load-status','historical-cache-pending','historical','Boolean(state)'),
        ('information-status','information-data','information','state&&state.pending'),
        ('game-information-content','game-information-data','market','state&&state.pending'),
        ('kalshi-quote-status','kalshi-quotes','kalshi','state&&state.page_pending&&!(state.rows||[]).length'),
        ('polymarket-quote-status','polymarket-quotes','polymarket','state&&state.page_pending&&!(state.rows||[]).length'),
    ]:
        app.clientside_callback(f"function(state,page){{return page==={page!r}&&({condition})?'show':'auto';}}",
            Output(output+'-loading','display'),Input(store,'data'),Input('workspace-page','value'))
