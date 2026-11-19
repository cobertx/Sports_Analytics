"""Shared market activity dialog; opening it never changes the current game."""
import json
from dash import ALL, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from .display_time import format_eastern
from .market_structure import PROVIDERS, validate, flow_summary
from .presentation import PALETTE, chart_style


def layout():
    return html.Div([
        dcc.Store(id='structure-request'), dcc.Store(id='structure-data'),dcc.Store(id='structure-click'),
        dcc.Interval(id='structure-poll', interval=1500, disabled=True),
        html.Div([
            html.Button(id='structure-backdrop', className='structure-backdrop', n_clicks=0,
                        **{'aria-label':'Close Market Structure'}),
            html.Section([
                html.Div([html.Div([html.Small('ALL MARKET SOURCES'), html.H2('Market Structure', id='structure-title')]),
                    html.Button('Close ×', id='structure-close', n_clicks=0)], className='structure-heading'),
                html.P(id='structure-prompt'),
                html.Div([html.Button('Refresh activity', id='structure-refresh', n_clicks=0),
                    dcc.Dropdown(id='structure-window', options=[
                        dict(label='All available API history', value='all'),
                        dict(label='Last 24 hours', value='24h')], value='all', clearable=False,
                        className='structure-window', searchable=False),
                    dcc.Checklist(id='structure-live', options=[dict(label='Refresh every 30 seconds', value='live')], value=['live'])],
                    className='structure-controls'),
                html.P('Read executed flow, traded prices and resting liquidity separately. '
                       'Exchange executions count fills, not unique bettors or sportsbook tickets. '
                       'Related contracts with different settlement rules are labeled separately.', className='structure-note'),
                html.Div(id='structure-content'),
            ], className='structure-dialog', role='dialog', tabIndex=-1,
               **{'aria-modal':'true', 'aria-labelledby':'structure-title'}),
        ], id='structure-modal', className='structure-modal', style={'display':'none'}),
    ])


def amount(value, money=False):
    if value is None:
        return 'Unavailable'
    if money and 0<value<.01:return '<$0.01'
    return f'${value:,.2f}' if money else f'{value:,.2f}'.rstrip('0').rstrip('.')


def stamp(value):
    return format_eastern(value) if value else 'Time unavailable'


def table(labels, metrics):
    return html.Div(html.Table([
        html.Thead(html.Tr([html.Th('Measure', scope='col'), *[html.Th(labels[side], scope='col') for side in ('yes','no')]])),
        html.Tbody([html.Tr([html.Th(label, scope='row'), *[html.Td(amount(values.get(side), money)) for side in ('yes','no')]])
                    for label, values, money in metrics]),
    ]), className='structure-table-scroll', tabIndex=0, role='region',
        **{'aria-label':'Market activity measures by outcome'})


def price(value):
    return 'Unavailable' if value is None else f'{value*100:,.2f}'.rstrip('0').rstrip('.')+'¢'


def quotes(book, provider):
    sides = (book or {}).get('sides', {})
    bid = {s:max((r['price'] for r in sides.get(s, {}).get('levels', [])), default=None) for s in ('yes','no')}
    if provider == 'Kalshi':
        ask = {s:round(1-bid['no' if s == 'yes' else 'yes'], 10)
               if bid['no' if s == 'yes' else 'yes'] is not None else None for s in ('yes','no')}
    else:
        ask = {s:min((r['price'] for r in sides.get(s, {}).get('asks', [])), default=None) for s in ('yes','no')}
    return bid, ask


def price_history(trades, labels):
    rows = trades.get('price_history') or []
    if not rows:
        return html.P('Trade-price history unavailable for this snapshot.', className='structure-note')
    figure = dict(data=[dict(type='scatter',mode='markers',x=[r['time'] for r in rows],
        y=[r['yes_price']*100 for r in rows],marker=dict(color=PALETTE['probability'],size=5),
        customdata=[[stamp(r['time']),amount(r['contracts'])] for r in rows],
        hovertemplate='%{customdata[0]}<br>YES price: %{y:.2f}¢<br>%{customdata[1]} contracts<extra></extra>')],
        layout=chart_style(height=230,margin=dict(l=52,r=20,t=15,b=45),showlegend=False,
            xaxis=dict(title=dict(text='Trade time (UTC)'),type='date',gridcolor=PALETTE['line']),
            yaxis=dict(title=dict(text='YES price (¢)'),range=[0,100],gridcolor=PALETTE['line']),
            uirevision='structure-trade-prices'))
    return html.Div([
        html.H5('Executed YES prices · '+labels['yes']),
        html.P('Returned trade sample · first '+price(rows[0]['yes_price'])+' → latest '+price(rows[-1]['yes_price'])+
               '. Dots are individual executions, not continuous quotes or model probabilities.',className='structure-note'),
        dcc.Graph(figure=figure,responsive=True,style={'height':'260px'},
            config=dict(displayModeBar=False,scrollZoom=False),className='structure-price-chart'),
        html.Details([html.Summary('Trade-price table · latest 20 returned executions'),
            html.Div(html.Table([html.Thead(html.Tr([html.Th(x,scope='col') for x in ('Time','YES price','Contracts','Taker direction')])),
                html.Tbody([html.Tr([html.Td(stamp(r['time'])),html.Td(price(r['yes_price'])),
                    html.Td(amount(r['contracts'])),html.Td(r['side'].upper())]) for r in rows[-20:][::-1]])]),
                className='structure-table-scroll',tabIndex=0,role='region',**{'aria-label':'Returned trade prices'})])],className='structure-history')


def flow_bars(labels, sides, *, kalshi, complete):
    """Two absolute scales; partial samples never imply market-wide shares."""
    panels=[]
    for field,title,money in [('buys','Trade count' if kalshi else 'Purchase count',False),
                             ('buy_dollars','Traded value (USD)' if kalshi else 'Purchase value (USD)',True)]:
        maximum=max((sides[s][field] for s in ('yes','no') if sides[s][field] is not None),default=0)
        rows=[]
        for side in ('yes','no'):
            value=sides[side][field]
            width=100*value/maximum if value is not None and maximum else 0
            rows.append(html.Div([
                html.Div([html.Span(labels[side]),html.Strong(amount(value,money))],className='structure-bar-label'),
                html.Div(html.Span(style={'width':f'{width:.6f}%'},className='structure-bar-fill structure-bar-fill--'+side),
                    className='structure-bar-track',**{'aria-hidden':'true'}),
            ],className='structure-bar-row',**{'data-side':side,'data-value':str(value) if value is not None else 'unavailable'}))
        panels.append(html.Figure([
            html.Figcaption(title),html.Small('Complete API window' if complete else 'Returned sample only'),*rows,
            html.Div([html.Span(amount(0,money)),html.Span(amount(maximum,money))],className='structure-bar-scale',**{'aria-hidden':'true'})
                if any(sides[s][field] is not None for s in ('yes','no')) else html.P('No usable trade data',className='structure-note'),
        ],className='structure-flow-chart',role='group',**{'aria-label':title+' by outcome','data-metric':field}))
    return html.Div(panels,className='structure-flow-charts')


def activity(market, provider):
    labels = market['labels'];trades = flow_summary(market.get('trades'),provider);book = market.get('book');holders = market.get('holders')
    content = [html.Div([html.H4(market['title']),html.Span('Related contract' if market.get('match') == 'related' else
        'Equivalent contract' if market.get('match') == 'equivalent' else 'Selected contract',className='structure-badge')],className='structure-contract-heading'),
        html.Small(market['ticker'], className='structure-contract')]
    if market.get('match') == 'related':
        content.append(html.P('Related contract · settlement or refund rules differ, or exact equivalence is unverified. '
            'Its activity is shown separately and is not combined with the selected contract.', className='structure-note'))
    if market.get('rules'):
        content.append(html.Details([html.Summary('Settlement rules'), html.P(market['rules'],className='structure-note')]))
    is_kalshi = provider == 'Kalshi'
    content += [html.Div([html.Div([html.Small(label),html.Strong(amount(value,money)),html.Span(unit)])
        for label,value,money,unit in [
            ('24h volume',market.get('volume_24h'),not is_kalshi,'contracts' if is_kalshi else 'USD turnover'),
            ('Lifetime volume',market.get('volume'),not is_kalshi,'contracts' if is_kalshi else 'USD turnover'),
            ('Open interest',market.get('open_interest') if is_kalshi else (market.get('interest') or {}).get('dollars'),
             not is_kalshi,'contracts outstanding' if is_kalshi else 'USD · gross priced interest')]],className='structure-kpis'),
        html.P('Volume snapshot: '+stamp(market.get('quote_time'))+
            ('' if is_kalshi else ' · Open interest: '+stamp((market.get('interest') or {}).get('fetched_at'))),className='structure-time')]
    details = []
    count_label = 'Taker executions by direction' if is_kalshi else 'Purchase count (taker executions)'
    value_label = 'Direction-priced traded value' if is_kalshi else 'Amount paid for this outcome'
    contract_label = 'Executed contracts' if is_kalshi else 'Purchased shares'
    window = 'last 24 hours' if market.get('window') == '24h' else 'available API history'
    content.append(html.H5(('Executed directional flow' if is_kalshi else 'Purchases by outcome')+' · '+window))
    if trades:
        sides = trades['sides']
        def values(field):
            return {side:sides[side].get(field) for side in ('yes','no')}
        def percentages(field):
            if any(sides[side].get(field) is None for side in ('yes','no')):return dict(yes=None,no=None)
            total = sum(sides[side][field] for side in ('yes','no'))
            return {side:100*sides[side][field]/total if total else None for side in ('yes','no')}
        metrics = [(count_label, values('buys'), False),
                   (value_label, values('buy_dollars'), True),
                   (contract_label, values('buy_contracts'), False)]
        # A capped sample's percentages are not market-wide betting splits.
        if trades['complete'] and not (trades['invalid'] or trades['unclassified']):
            metrics.extend([('Share of taker executions (%)' if is_kalshi else 'Share of purchase count (%)', percentages('buys'), False),
                            ('Share of direction-priced value (%)' if is_kalshi else 'Share of amount paid (%)', percentages('buy_dollars'), False)])
        clean = trades['complete'] and not (trades['invalid'] or trades['unclassified'])
        content += [html.Div([html.Span('Complete API window' if clean else 'Partial sample',
                className='structure-badge '+('structure-badge--complete' if clean else 'structure-badge--partial')),
            html.Span(('Requested '+stamp(trades['start'])+' · ' if market.get('window') == '24h' else '')+
                      'Through '+stamp(trades['end']),className='structure-time')],className='structure-coverage'),
            html.P('Fetched '+stamp(trades.get('fetched_at')),className='structure-time'),
            html.Div([html.Div([html.Small(side.upper()+' DIRECTION' if is_kalshi else side.upper()+' PURCHASES'),
                html.Strong(labels[side]),html.Div([html.B(amount(sides[side]['buys'])),html.Span('Trade count (executions)' if is_kalshi else 'Purchase count (executions)')]),
                html.Div([html.B(amount(sides[side]['buy_dollars'],True)),html.Span(value_label)]),
                html.P(amount(sides[side]['buy_contracts'])+
                       (' contracts' if is_kalshi else ' shares'))],className='structure-outcome structure-outcome--'+side)
                for side in ('yes','no')],className='structure-outcomes'),
            flow_bars(labels,sides,kalshi=is_kalshi,complete=clean)]
        if not trades['classified']:
            content.append(html.P('No executions were returned for this contract in the requested window.' if clean else
                'No usable executions were returned. Side counts and traded values are unavailable.',className='structure-note'))
        if not clean:
            content.append(html.P('Counts and values are lower bounds from the returned sample; market-wide shares are unavailable. '
                'Reads are limited to 2,000 records across live and archived trades.',className='structure-note'))
        content.append(html.Details([html.Summary('Executed flow · full measures'),table(labels,metrics)]))
        if is_kalshi:
            content.append(html.P('Each trade is counted once by taker direction. YES exposure includes buying YES or selling NO; '
                'NO exposure includes buying NO or selling YES. Direction-priced value = contracts × outcome price '
                '(NO = $1 − YES price), before fees. It measures directional flow, not cash deposited, unique bets or holdings.',className='structure-note'))
            content.append(price_history(trades,labels))
        else:
            content.append(html.P('Polymarket counts taker BUY fills and their purchase value. SELL fills are excluded from these '
                'purchase totals and listed below. Purchases can include resales; this is not current money held.', className='structure-note'))
            details += [html.H5('Sell activity'), table(labels,[
                ('Taker SELL executions', values('sells'), False), ('Taker SELL turnover', values('sell_dollars'), True)])]
        if trades['invalid'] or trades['unclassified']:
            content.append(html.P(f"Excluded {trades['invalid']} invalid and {trades['unclassified']} unclassified records.", className='structure-note'))
        if trades.get('error'):
            content.append(html.P('Trade coverage: '+trades['error'], className='structure-error'))
    else:
        content += [html.P('Trade data unavailable. Refresh to retry.',className='structure-note'),table(labels,[
            (count_label,{},False),(value_label,{},True)])]
    if book:
        bid, ask = quotes(book,provider)
        content += [html.H5('Current bid / ask'),html.P('Order book fetched '+stamp(book['fetched_at']),className='structure-time'),
            html.Div([html.Div([html.Strong(side.upper()+' · '+labels[side]),html.P([
                html.Span('Bid '+price(bid[side])),html.Span('Ask '+price(ask[side])),
                html.Span('Spread '+price(round(ask[side]-bid[side],10)) if bid[side] is not None and ask[side] is not None else 'Spread unavailable')])],
                className='structure-quote') for side in ('yes','no')],className='structure-outcomes')]
        if is_kalshi:
            content.append(html.P('Asks are derived from opposite-side bids: YES ask = $1 − best NO bid. '
                'Displayed prices are unfilled orders; the trade chart shows past executions.',className='structure-note'))
        details += [html.H5('Resting buy liquidity'), html.P('Fetched '+stamp(book['fetched_at']), className='structure-time'),
            table(labels, [('Available contracts / shares', {s:book['sides'][s]['contracts'] for s in ('yes','no')}, False),
                           ('Unfilled bid value', {s:book['sides'][s]['dollars'] for s in ('yes','no')}, True)]),
            html.P('Outstanding bids can be canceled. This is available liquidity, not money already bet.', className='structure-note')]
        details.append(html.Details([html.Summary('Order-book depth · best 10 bid levels'),
            html.Div([html.Div([html.Strong(labels[side]), html.Table([
                html.Thead(html.Tr([html.Th('Price'), html.Th('Size'), html.Th('Bid value')])),
                html.Tbody([html.Tr([html.Td(f"${level['price']:.4f}"), html.Td(amount(level['size'])),
                    html.Td(amount(level['price']*level['size'], True))]) for level in book['sides'][side]['levels']])])])
                for side in ('yes','no')], className='structure-depth')]))
    else:
        content += [html.H5('Current bid / ask'),html.P('Order book unavailable. Prices and depth are not inferred from past trades.',className='structure-note')]
    if holders:
        details += [html.H5('Current Polymarket holdings · returned wallets'),
            html.P(('All returned holder pages fit in this snapshot' if holders['complete'] else 'Partial snapshot · first 100 holders per outcome')+
                ' · '+stamp(holders['fetched_at']), className='structure-time'),
            table(labels, [(label, {s:(holders['sides'][s] or {}).get(field) for s in ('yes','no')}, money)
                for label,field,money in [('Wallets returned','wallets',False), ('Shares held','shares',False),
                                         ('Reported entry cost','entry_cost',True), ('Current position value','current_value',True)]]),
            html.P('Wallets are not unique people. A wallet can hold both outcomes. These are gross positions for the '
                'returned holders; a partial holder snapshot is not a market-wide money split.', className='structure-note')]
    if details:
        content.append(html.Details([html.Summary('Resting liquidity, depth and holdings'), *details]))
    content.extend(html.P(error, className='structure-error') for error in market.get('errors', []))
    return html.Section(content, className='structure-contract-detail')


def render(data):
    cards = []
    selected = data.get('request',{}).get('provider','Kalshi')
    for provider in sorted(PROVIDERS,key=lambda p:p != selected):
        state = data['providers'][provider];record = state.get('data') or {}
        content = [html.Div([html.H3(provider),html.Span('Selected source' if provider == selected else 'Other source',className='structure-source-label')],className='structure-source-heading'), html.P(('Updating · saved snapshot shown' if record.get('fetched_at') else 'Loading activity…') if state.get('pending') else
            'Fetched '+stamp(record['fetched_at']) if record.get('fetched_at') else
            'Not supplied by the connected odds feed' if provider == 'FanDuel' else 'Data unavailable', className='structure-time')]
        if record.get('notice'):
            content.append(html.P(record['notice'], className='structure-note'))
        if state.get('error'):
            content.append(html.P(('Saved snapshot shown. ' if record else '')+state['error'], className='structure-error'))
        content.extend(activity(market, provider) for market in record.get('markets', []))
        content.extend(html.P(notice, className='structure-note') for notice in record.get('notices', []))
        if provider == 'FanDuel':
            content.extend([table(dict(yes='Selected outcome',no='Opposite outcome'),[
                ('Number of sportsbook tickets',{},False),('Total money wagered',{},True)]),
                html.P('No FanDuel ticket/handle source is connected. Odds and implied probability cannot determine '
                       'these totals; public percentages alone cannot recover absolute counts or dollars.',className='structure-note'),
                html.A('FanDuel Research', href='https://www.fanduel.com/research', target='_blank', rel='noopener noreferrer'),
                html.P('Additional sportsbook context (different sources; not FanDuel splits):'),
                html.A('VSiN · DraftKings / Circa', href='https://data.vsin.com/betting-splits/', target='_blank', rel='noopener noreferrer'),
                html.Span(' · '), html.A('Action Network · public betting', href='https://www.actionnetwork.com/public-betting',
                                        target='_blank', rel='noopener noreferrer')])
        else:
            content.append(html.A('Official data documentation', href='https://docs.kalshi.com/api-reference/market/get-trades'
                if provider == 'Kalshi' else 'https://docs.polymarket.com/api-reference/feeds/list-trades',
                target='_blank', rel='noopener noreferrer'))
        cards.append(html.Article(content, className='structure-source'+(' structure-source--primary' if provider == selected else '')))
    return html.Div(cards, className='structure-sources')


def register(app, service):
    buttons = dict(type='market-structure-open', context=ALL, surface=ALL)

    @app.callback(Output('structure-request','data'), Output('structure-modal','style'), Output('structure-prompt','children'),
        Input(buttons,'n_clicks'), Input('structure-close','n_clicks'), Input('structure-backdrop','n_clicks'),
        Input('market-url','hash'), Input('sport','value'), Input('workspace-page','value'), State(buttons,'id'),
        Input('structure-click','data'),
        prevent_initial_call=True)
    def structure_open(clicks, close, backdrop, location, sport, page, identities, selection=None):
        triggered = ctx.triggered_id
        if isinstance(triggered, str) and triggered in {'structure-close','structure-backdrop','market-url','sport','workspace-page'}:
            return None, {'display':'none'}, ''
        if triggered=='structure-click':
            if not selection or page!='market-summary':raise PreventUpdate
            target=selection.get('context')
        else:
            if not isinstance(triggered, dict):raise PreventUpdate
            index = next((i for i,identity in enumerate(identities) if identity == triggered), None)
            if index is None or not clicks[index]:raise PreventUpdate
            target=triggered['context']
        try:
            request = validate(json.loads(target))
        except (ValueError, TypeError, KeyError):
            return None, {'display':'none'}, 'This market could not be identified.'
        label = request['game']['away']['name']+' at '+request['game']['home']['name']+' · '+request['label']
        return request, {}, label

    app.clientside_callback('''function(request,data,live,window){
        if(!request)return true;
        if((live||[]).includes('live'))return false;
        const target=Object.assign({},request,{window:window||'all'});
        return !!(data&&JSON.stringify(data.request)===JSON.stringify(target)&&
            Object.values(data.providers).every(p=>!p.pending));
    }''', Output('structure-poll','disabled'), Input('structure-request','data'), Input('structure-data','data'), Input('structure-live','value'), Input('structure-window','value'))

    @app.callback(Output('structure-data','data'), Output('structure-content','children'),
        Input('structure-request','data'), Input('structure-refresh','n_clicks'), Input('structure-poll','n_intervals'), Input('structure-window','value'),
        State('structure-data','data'), State('structure-live','value'), prevent_initial_call=True)
    def structure_load(request, refresh, tick, window, previous, live):
        if not request:
            return None, ''
        request = {**request, 'window':window or 'all'}
        if (ctx.triggered_id == 'structure-poll' and 'live' not in (live or []) and previous
                and previous.get('request') == request and all(not p['pending'] for p in previous['providers'].values())):
            raise PreventUpdate
        data = service.read(request, refresh=ctx.triggered_id == 'structure-refresh')
        if data == previous:
            raise PreventUpdate
        return data, render(data)
