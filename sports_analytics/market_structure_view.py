"""Shared market activity dialog; opening it never changes the current game."""
import json
from dash import ALL, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from .display_time import format_eastern
from .market_structure import PROVIDERS, validate


def layout():
    return html.Div([
        dcc.Store(id='structure-request'), dcc.Store(id='structure-data'),
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
                html.P('Compare the number of executed purchases and the amount paid for each outcome. '
                       'An exchange purchase is a matched fill, not a unique bettor or sportsbook ticket; one order may fill several times. '
                       'Sales, current holdings and unfilled orders are shown separately. '
                       'Equivalent contracts and related contracts with different settlement rules are labeled separately.', className='structure-note'),
                html.Div(id='structure-content'),
            ], className='structure-dialog', role='dialog', tabIndex=-1,
               **{'aria-modal':'true', 'aria-labelledby':'structure-title'}),
        ], id='structure-modal', className='structure-modal', style={'display':'none'}),
    ])


def amount(value, money=False):
    if value is None:
        return 'Unavailable'
    return f'${value:,.2f}' if money else f'{value:,.2f}'.rstrip('0').rstrip('.')


def stamp(value):
    return format_eastern(value) if value else 'Time unavailable'


def table(labels, metrics):
    return html.Div(html.Table([
        html.Thead(html.Tr([html.Th('Measure'), *[html.Th(labels[side]) for side in ('yes','no')]])),
        html.Tbody([html.Tr([html.Th(label), *[html.Td(amount(values.get(side), money)) for side in ('yes','no')]])
                    for label, values, money in metrics]),
    ]), className='structure-table-scroll', tabIndex=0, role='region',
        **{'aria-label':'Market activity measures by outcome'})


def activity(market, provider):
    labels = market['labels'];trades = market.get('trades');book = market.get('book');holders = market.get('holders')
    content = [html.H4(market['title']), html.Small(market['ticker'], className='structure-contract'),
        html.P('Quote data: '+stamp(market.get('quote_time')), className='structure-time')]
    if market.get('match') == 'related':
        content.append(html.P('Related contract · settlement or refund rules differ, or exact equivalence is unverified. '
            'Its activity is shown separately and is not combined with the selected contract.', className='structure-note'))
    if market.get('rules'):
        content.append(html.Details([html.Summary('Settlement rules'), html.P(market['rules'],className='structure-note')]))
    is_kalshi = provider == 'Kalshi'
    details = [html.P(
        'Lifetime volume: '+amount(market.get('volume'), not is_kalshi)+(' contracts' if is_kalshi else '')+
        ' · 24h volume: '+amount(market.get('volume_24h'), not is_kalshi)+(' contracts' if is_kalshi else '')+
        (' · Open interest: '+amount(market.get('open_interest'))+' contracts' if is_kalshi else
         ' · Priced gross open interest: '+amount((market.get('interest') or {}).get('dollars'), True)))]
    if trades:
        sides = trades['sides']
        def values(field):
            return {side:sides[side][field] for side in ('yes','no')}
        def percentages(field):
            total = sum(sides[side][field] for side in ('yes','no'))
            return {side:100*sides[side][field]/total if total else None for side in ('yes','no')}
        metrics = [('Purchase count (taker executions)', values('buys'), False),
                   ('Amount paid for this outcome', values('buy_dollars'), True),
                   ('Purchased contracts / shares', values('buy_contracts'), False)]
        # A capped sample's percentages are not market-wide betting splits.
        if trades['complete'] and not (trades['invalid'] or trades['unclassified']):
            metrics.extend([('Share of purchase count (%)', percentages('buys'), False),
                            ('Share of amount paid (%)', percentages('buy_dollars'), False)])
        window = 'last 24 hours' if market.get('window') == '24h' else 'available API history'
        content += [html.H5('Purchases by outcome · '+window),
            html.P(('Complete returned window' if trades['complete'] else 'Partial sample · up to 2,000 trade records; counts and amounts are lower bounds, not market totals')+
                (' · '+stamp(trades['start']) if market.get('window') == '24h' else '')+
                ' · Through '+stamp(trades['end']), className='structure-time'), table(labels, metrics)]
        if is_kalshi:
            content.append(html.P('Kalshi counts each taker execution once under its outcome. Amount paid uses that outcome’s price '
                '(No = $1 − Yes price), before fees. This is executed directional purchase value, not current holdings or new deposits.', className='structure-note'))
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
        content += [html.H5('Purchases by outcome'), table(labels,[
            ('Purchase count (taker executions)',{},False),('Amount paid for this outcome',{},True)])]
    if book:
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
    if holders:
        details += [html.H5('Current Polymarket holdings · returned wallets'),
            html.P(('All returned holder pages fit in this snapshot' if holders['complete'] else 'Partial snapshot · first 100 holders per outcome')+
                ' · '+stamp(holders['fetched_at']), className='structure-time'),
            table(labels, [(label, {s:(holders['sides'][s] or {}).get(field) for s in ('yes','no')}, money)
                for label,field,money in [('Wallets returned','wallets',False), ('Shares held','shares',False),
                                         ('Reported entry cost','entry_cost',True), ('Current position value','current_value',True)]]),
            html.P('Wallets are not unique people. A wallet can hold both outcomes. These are gross positions for the '
                'returned holders; a partial holder snapshot is not a market-wide money split.', className='structure-note')]
    content.append(html.Details([html.Summary('Volume, sell activity, holdings and order-book depth'), *details]))
    content.extend(html.P(error, className='structure-error') for error in market.get('errors', []))
    return html.Section(content, className='structure-contract-detail')


def render(data):
    cards = []
    for provider in PROVIDERS:
        state = data['providers'][provider];record = state.get('data') or {}
        content = [html.H3(provider), html.P('Updating…' if state.get('pending') else
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
        cards.append(html.Article(content, className='structure-source'))
    return html.Div(cards, className='structure-sources')


def register(app, service):
    buttons = dict(type='market-structure-open', context=ALL, surface=ALL)

    @app.callback(Output('structure-request','data'), Output('structure-modal','style'), Output('structure-prompt','children'),
        Input(buttons,'n_clicks'), Input('structure-close','n_clicks'), Input('structure-backdrop','n_clicks'),
        Input('market-url','hash'), Input('sport','value'), Input('workspace-page','value'), State(buttons,'id'),
        prevent_initial_call=True)
    def structure_open(clicks, close, backdrop, location, sport, page, identities):
        triggered = ctx.triggered_id
        if isinstance(triggered, str) and triggered in {'structure-close','structure-backdrop','market-url','sport','workspace-page'}:
            return None, {'display':'none'}, ''
        if not isinstance(triggered, dict):
            raise PreventUpdate
        index = next((i for i,identity in enumerate(identities) if identity == triggered), None)
        if index is None or not clicks[index]:
            raise PreventUpdate
        try:
            request = validate(json.loads(triggered['context']))
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
