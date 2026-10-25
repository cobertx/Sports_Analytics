"""Persistent personal P&L tab; recording history never places an order."""
import base64
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
import sqlite3

from dash import ALL,Input,Output,State,ctx,dcc,html,dash_table,no_update
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go

from . import pnl
from .backtesting_results import field,figure


def dollars(cents):return f'${Decimal(cents)/100:,.2f}'


def local(value):return datetime.fromisoformat(value).astimezone(pnl.ZONE).strftime('%Y-%m-%d %H:%M:%S %Z') if value else ''


def table(identity,columns):
    return dash_table.DataTable(id=identity,data=[],columns=[dict(id=key,name=label) for key,label in columns],page_size=15,
        sort_action='native',filter_action='native',style_table={'overflowX':'auto'},
        style_cell={'backgroundColor':'#111d2e','color':'#c7d3e4','border':'1px solid #243249','padding':'10px','textAlign':'left',
                    'fontFamily':'Segoe UI','fontSize':'12px','minWidth':'95px','maxWidth':'300px','whiteSpace':'normal'},
        style_header={'backgroundColor':'#17253a','fontWeight':'600'},style_filter={'backgroundColor':'#17253a'})


def import_panel():
    return html.Details([
        html.Summary('Import sportsbook / exchange CSV statements'),
        html.P('Upload a bet-history or closed-position summary, map the columns, and preview it before saving. '
                   'Use one row per bet/position with total cash cost and total settlement return. '
                   'Stake/cost excludes separately mapped fees. If a cost already includes fees, leave those fees unmapped. '
                   'Individual trade fills need to be combined into positions first. Cash-transfer statements can be imported separately.'),
        dcc.Upload(html.Button('Choose statement CSV',className='export-button'),id='pnl-upload',accept='.csv,text/csv',multiple=False,max_size=5000000),
        html.P(id='pnl-import-file',className='chart-note'),
        html.Div([
            field('Default account (unless mapped)',dcc.Input(id='pnl-import-account',type='text',placeholder='Name of the statement account',maxLength=100)),
            field('Default record type',dcc.Dropdown(id='pnl-import-type',options=[dict(label=k.title(),value=k) for k in ['bet','opening','deposit','withdrawal','adjustment']],value='bet',clearable=False)),
            field('Default bet status (unless mapped)',dcc.Dropdown(id='pnl-import-status',options=[dict(label=k.replace('_',' ').title(),value=k) for k in pnl.STATUSES],value='open',clearable=False)),
            field('Return column contains',dcc.Dropdown(id='pnl-import-basis',options=[dict(label='Gross return, including returned stake',value='gross'),
                dict(label='Profit before fees (add stake)',value='profit'),dict(label='Profit after fees (add stake and fees)',value='net')],placeholder='Choose the meaning of the return column',clearable=False)),
            field('Date format',dcc.Dropdown(id='pnl-import-date',options=[dict(label=label,value=value) for value,label in [
                ('iso','ISO / YYYY-MM-DD (with optional time/offset)'),('us-time','MM/DD/YYYY HH:MM:SS (24-hour)'),('us-minute','MM/DD/YYYY HH:MM (24-hour)'),
                ('us-ampm','MM/DD/YYYY HH:MM AM/PM'),('us-date','MM/DD/YYYY')]],value='iso',clearable=False)),
            field('Mapped fee column is charged',dcc.Dropdown(id='pnl-import-fee-time',options=[dict(label='At settlement',value='settlement'),
                dict(label='When placed / filled',value='placement')],value='settlement',clearable=False)),
        ],className='backtest-controls'),
        dcc.Checklist(id='pnl-import-debits',options=[dict(label='Stake and fee columns show charges as negative amounts',value='debits')],value=[]),
        html.P('Stable external IDs and placement dates are required. Settled bets also need a settlement date. '
               'Money is USD only; dates without an offset use America/Indianapolis. Column suggestions must be checked against your statement.',className='chart-note'),
        html.Div(id='pnl-import-mapping',className='backtest-controls'),
        html.Button('Preview import',id='pnl-import-preview-button',n_clicks=0,className='export-button'),
        html.P(id='pnl-import-message',className='load-status',role='status'),
        table('pnl-import-table',[('external_id','External ID'),('account','Account'),('record_type','Type'),('placed_at','Placed at'),('description','Bet / selection'),
            ('stake','Stake ($)'),('status','Status'),('gross_return','Gross return ($)'),('fees','Settlement fees ($)'),('placement_fees','Placement fees ($)'),('amount','Cash amount ($)')]),
        html.Button('Save previewed statement',id='pnl-import-confirm',n_clicks=0,className='apply-criteria-button',disabled=True),
    ],open=True,className='panel pnl-form')


def layout():
    statuses=[dict(label=s.replace('_',' ').title(),value=s) for s in pnl.STATUSES]
    return html.Section([
        dcc.Store(id='pnl-token'),dcc.Store(id='pnl-revision'),dcc.Store(id='pnl-statement'),dcc.Store(id='pnl-import-preview'),dcc.Download(id='pnl-download'),
        html.Section([
            html.H2('P&L History'),
            html.P('Your recorded bets, available cash and realized profit/loss in USD. Add an opening balance for each account, '
                   'then record actual bets and cash transfers or import a statement using the CSV template. This page tracks local records; it is not connected to sportsbook or exchange balances.'),
            html.Div([field('Account',dcc.Dropdown(id='pnl-account',options=[dict(label='All accounts',value='__all__')],value='__all__',clearable=False)),
                      html.Button('Refresh history',id='pnl-refresh',n_clicks=0,className='export-button'),
                      html.Button('Export ledger CSV',id='pnl-export',n_clicks=0,className='export-button'),
                      html.Button('Download CSV template',id='pnl-template',n_clicks=0,className='export-button')],className='pnl-toolbar'),
            html.P(id='pnl-status',className='load-status',role='status',**{'aria-live':'polite'}),
        ],className='panel pnl-heading'),
        import_panel(),
        html.Div(id='pnl-kpis',className='pnl-kpis'),html.P(id='pnl-empty',className='chart-note'),
        html.Div([html.Section(dcc.Graph(id='pnl-balance-chart',figure=figure('Recorded balance history'),config={'displaylogo':False}),className='panel'),
                  html.Section(dcc.Graph(id='pnl-profit-chart',figure=figure('Realized P&L history'),config={'displaylogo':False}),className='panel')],className='backtest-chart-grid'),
        html.P('Cash balance subtracts stakes when bets are placed and credits gross returns minus fees at settlement. '
               'Recorded bankroll adds open stakes at cost to cash; open bets are not valued at current market prices. '
               'Deposits, withdrawals and balance adjustments do not count as betting P&L. ROI is closed-bet net P&L divided by settled stakes; '
               'win rate counts only Won and Lost bets. Paid placement fees reduce cash and realized P&L immediately; settled ROI uses closed bets only. All times use America/Indianapolis.',className='chart-note'),
        html.Details([
            html.Summary('Record a bet'),
            html.Div([
                field('Account name',dcc.Input(id='pnl-bet-account',type='text',value='FanDuel',maxLength=100)),
                field('Sport',dcc.Dropdown(id='pnl-sport',options=['NBA','NFL','NCAAF','Other'],value='NBA',clearable=False)),
                field('Bet / selection',dcc.Input(id='pnl-description',type='text',placeholder='Event and selection',maxLength=500)),
                field('Market (optional)',dcc.Input(id='pnl-market',type='text',maxLength=200)),
                field('Cash stake ($)',dcc.Input(id='pnl-stake',type='number',min=.01,step=.01)),
                field('American odds (optional)',dcc.Input(id='pnl-odds',type='number',step='any')),
                field('Placed at (optional)',dcc.Input(id='pnl-placed-at',type='text',placeholder='YYYY-MM-DDTHH:MM; blank = now')),
                field('Status',dcc.Dropdown(id='pnl-add-status',options=statuses,value='open',clearable=False)),
                field('Settled at (optional)',dcc.Input(id='pnl-add-settled-at',type='text',placeholder='Blank = now for settled bets')),
                field('Actual gross return ($, optional)',dcc.Input(id='pnl-add-return',type='number',min=0,step=.01)),
                field('Settlement fees ($)',dcc.Input(id='pnl-add-fees',type='number',min=0,step=.01,value=0)),
                field('Placement / fill fees ($)',dcc.Input(id='pnl-placement-fees',type='number',min=0,step=.01,value=0)),
            ],className='backtest-controls'),
            html.P('For Won, the default return comes from the entered odds; an actual gross return overrides it. '
                   'For exchange positions, use cash cost excluding separately entered fees as stake and actual total settlement receipts as gross return. '
                   'Gross return includes returned stake. Enter placement fees separately from settlement fees. Open bets have no return or settlement fees.',className='chart-note'),
            html.Button('Save bet',id='pnl-add',n_clicks=0,className='apply-criteria-button'),
        ],className='panel pnl-form'),
        html.Details([
            html.Summary('Settle or correct a recorded bet'),
            html.Div([
                field('Recorded bet',dcc.Dropdown(id='pnl-bet-select',options=[],placeholder='Choose a bet')),
                field('Outcome',dcc.Dropdown(id='pnl-settle-status',options=statuses,value='won',clearable=False)),
                field('Settlement date/time',dcc.Input(id='pnl-settled-at',type='text',placeholder='Blank = now')),
                field('Actual gross return ($, optional)',dcc.Input(id='pnl-return',type='number',min=0,step=.01)),
                field('Settlement fees ($)',dcc.Input(id='pnl-fees',type='number',min=0,step=.01,value=0)),
            ],className='backtest-controls'),
            html.P('Saved settlements can be corrected. Previous versions remain in the local audit trail. '
                   'A correction replaces the recorded settlement rather than crediting a second payout.',className='chart-note'),
            html.Button('Save settlement',id='pnl-settle',n_clicks=0,className='apply-criteria-button'),
        ],className='panel pnl-form'),
        html.Details([
            html.Summary('Opening balances, deposits and withdrawals'),
            html.Div([
                field('Account name',dcc.Input(id='pnl-cash-account',type='text',value='FanDuel',maxLength=100)),
                field('Transaction',dcc.Dropdown(id='pnl-cash-kind',options=[dict(label=k.title(),value=k) for k in ['opening','deposit','withdrawal','adjustment']],value='opening',clearable=False)),
                field('Amount ($)',dcc.Input(id='pnl-cash-amount',type='number',step=.01)),
                field('Date/time (optional)',dcc.Input(id='pnl-cash-at',type='text',placeholder='YYYY-MM-DDTHH:MM; blank = now')),
                field('Notes',dcc.Input(id='pnl-cash-notes',type='text',maxLength=500)),
            ],className='backtest-controls'),
            html.P('Use positive amounts for opening balances, deposits and withdrawals. Adjustments may be positive or negative. '
                   'Each account can have one opening balance.',className='chart-note'),
            html.Button('Save cash transaction',id='pnl-cash-save',n_clicks=0,className='apply-criteria-button'),
        ],className='panel pnl-form'),
        html.Section([html.H3('Bet history'),table('pnl-bets',[('placed_at','Placed at'),('account','Account'),('sport','Sport'),('description','Bet / selection'),
            ('market','Market'),('stake','Stake ($)'),('american_odds','American odds'),('status','Status'),('settled_at','Settled at'),('gross_return','Gross return ($)'),('fees','Settlement fees ($)'),('placement_fees','Placement fees ($)'),('pnl','Net P&L ($)')])],className='panel pnl-table'),
        html.Section([html.H3('Balance / P&L ledger'),table('pnl-events',[('at','Date/time'),('account','Account'),('type','Transaction'),('description','Description'),
            ('cash','Cash change ($)'),('pnl','Realized P&L change ($)'),('cash_balance','Cash balance ($)'),('open_stake','Open stake ($)'),('bankroll','Bankroll at cost ($)'),('cumulative_pnl','Cumulative P&L ($)')])],className='panel pnl-table'),
    ],id='pnl-page',className='pnl-page',style={'display':'none'})


def register(app,ledger):
    @app.callback(Output('pnl-page','style'),Input('sport','value'))
    def pnl_visibility(tab):return {} if tab=='PNL' else {'display':'none'}

    @app.callback(Output('pnl-token','data'),Input('sport','value'),State('pnl-token','data'))
    def pnl_token(tab,previous):
        if tab!='PNL' or previous:raise PreventUpdate
        return uuid4().hex

    @app.callback(Output('pnl-revision','data'),Output('pnl-status','children'),Output('pnl-token','data',allow_duplicate=True),
                  Input('pnl-add','n_clicks'),Input('pnl-settle','n_clicks'),Input('pnl-cash-save','n_clicks'),Input('pnl-import-confirm','n_clicks'),
                  State('sport','value'),State('pnl-token','data'),
                  *[State(identity,'value') for identity in ['pnl-bet-account','pnl-sport','pnl-description','pnl-market','pnl-stake','pnl-odds','pnl-placed-at',
                    'pnl-add-status','pnl-add-settled-at','pnl-add-return','pnl-add-fees','pnl-bet-select','pnl-settle-status','pnl-settled-at','pnl-return','pnl-fees',
                    'pnl-cash-account','pnl-cash-kind','pnl-cash-amount','pnl-cash-at','pnl-cash-notes']],State('pnl-import-preview','data'),State('pnl-placement-fees','value'),prevent_initial_call=True,
                  running=[(Output(identity,'disabled'),True,False) for identity in ['pnl-add','pnl-settle','pnl-cash-save','pnl-upload']])
    def pnl_save(add,settle,cash,import_clicks,tab,token,account,sport,description,market,stake,american,placed,status,settled,gross,fees,
                 bet_id,outcome,settled_at,returned,fee,cash_account,cash_kind,amount,at,notes,preview,placement_fees):
        if tab!='PNL':raise PreventUpdate
        try:
            action=ctx.triggered_id
            if action=='pnl-add' and add:
                operation='add';values=dict(record_type='bet',account=account,sport=sport,description=description,market=market,stake=stake,
                    american_odds=american,placed_at=placed,status=status,settled_at=settled,gross_return=gross,fees=fees,placement_fees=placement_fees)
            elif action=='pnl-settle' and settle:
                operation='settle';values=dict(id=bet_id,status=outcome,settled_at=settled_at,gross_return=returned,fees=fee)
            elif action=='pnl-cash-save' and cash:
                operation='add';values=dict(record_type=cash_kind,account=cash_account,amount=amount,placed_at=at,notes=notes)
            elif action=='pnl-import-confirm' and import_clicks:
                if not preview:raise ValueError('Preview and validate the statement before saving.')
                operation='import';values=preview
            else:raise PreventUpdate
            result=ledger.write(operation,values,token+':'+action if token else token)
            return uuid4().hex,result['message'],uuid4().hex
        except (ValueError,sqlite3.Error) as exc:return no_update,str(exc),no_update

    @app.callback(Output('pnl-account','options'),Output('pnl-account','value'),Output('pnl-bet-select','options'),
                  Input('sport','value'),Input('pnl-revision','data'),Input('pnl-refresh','n_clicks'),State('pnl-account','value'))
    def pnl_options(tab,revision,refresh,account):
        if tab!='PNL':raise PreventUpdate
        records=ledger.records();accounts={r['account_key']:r['account'] for r in records}
        return [dict(label='All accounts',value='__all__'),*[dict(label=accounts[k],value=k) for k in sorted(accounts)]],account if account in accounts else '__all__',[
            dict(label=f"{r['account']} · {r['description']} · {r['status']} · {dollars(r['stake'])} · {r['id'][:8]}",value=r['id']) for r in reversed(records) if r['record_type']=='bet']

    @app.callback(Output('pnl-kpis','children'),Output('pnl-balance-chart','figure'),Output('pnl-profit-chart','figure'),Output('pnl-bets','data'),Output('pnl-events','data'),Output('pnl-empty','children'),
                  Input('sport','value'),Input('pnl-revision','data'),Input('pnl-account','value'),Input('pnl-refresh','n_clicks'))
    def pnl_history(tab,revision,account,refresh):
        if tab!='PNL':raise PreventUpdate
        result=pnl.history(ledger.records(),account);entries=result['timeline'];balance=figure('Recorded balance history','Date/time (America/Indianapolis)','USD');profit=figure('Realized P&L history','Date/time (America/Indianapolis)','USD')
        if entries:
            for key,label in [('cash_balance','Available cash'),('bankroll','Cash + open stakes at cost')]:
                balance.add_trace(go.Scatter(x=[r['at'] for r in entries],y=[r[key]/100 for r in entries],name=label,mode='lines+markers',line_shape='hv',
                    customdata=[local(r['at']) for r in entries],hovertemplate='%{customdata}<br>$%{y:,.2f}<extra>%{fullData.name}</extra>'))
            profit.add_trace(go.Scatter(x=[r['at'] for r in entries],y=[r['cumulative_pnl']/100 for r in entries],name='Realized P&L',mode='lines+markers',line_shape='hv',
                customdata=[local(r['at']) for r in entries],hovertemplate='%{customdata}<br>$%{y:,.2f}<extra>%{fullData.name}</extra>'))
            ticks=sorted({round(i*(len(entries)-1)/min(6,len(entries)-1)) for i in range(min(6,len(entries)-1)+1)}) if len(entries)>1 else [0]
            for chart in [balance,profit]:chart.update_xaxes(tickmode='array',tickvals=[entries[i]['at'] for i in ticks],ticktext=[local(entries[i]['at']) for i in ticks])
        else:
            for chart in [balance,profit]:chart.add_annotation(text='Add or import your actual history to see this chart.',x=.5,y=.5,xref='paper',yref='paper',showarrow=False)
        metrics=[('RECORDED CASH',dollars(result['cash'])),('OPEN STAKES',dollars(result['open_stake'])),('BANKROLL AT COST',dollars(result['bankroll'])),
                 ('REALIZED P&L',dollars(result['pnl'])),('SETTLED ROI',f"{result['roi']:.2%}" if result['roi'] is not None else '—'),('WIN RATE',f"{result['win_rate']:.1%}" if result['win_rate'] is not None else '—')]
        kpis=[html.Div([html.Small(label),html.Strong(value if result['records'] else '—')],className='kpi') for label,value in metrics]
        bets=[]
        for r in reversed(result['bets']):
            row={k:r.get(k) for k in ['account','sport','description','market','american_odds','status']}
            row.update(placed_at=local(r['placed_at']),settled_at=local(r['settled_at']),stake=r['stake']/100,gross_return=r['gross_return']/100 if r['gross_return'] is not None else None,
                       fees=r['fees']/100,placement_fees=r.get('placement_fees',0)/100,
                       pnl=(r['gross_return']-r['fees']-r['stake']-r.get('placement_fees',0))/100 if r['status']!='open' else -r.get('placement_fees',0)/100 if r.get('placement_fees',0) else None)
            bets.append(row)
        events=[{**{k:r[k]/100 for k in ['cash','pnl','cash_balance','open_stake','bankroll','cumulative_pnl']},'at':local(r['at']),
                 **{k:r[k] for k in ['account','type','description']}} for r in reversed(entries)]
        return kpis,balance,profit,bets,events,('' if entries else 'No history recorded yet. Add an opening balance and actual bets, or import your ledger CSV.')

    @app.callback(Output('pnl-download','data'),Input('pnl-export','n_clicks'),Input('pnl-template','n_clicks'),State('sport','value'),State('pnl-account','value'),prevent_initial_call=True)
    def pnl_export(export,template,tab,account):
        if tab!='PNL':raise PreventUpdate
        if ctx.triggered_id=='pnl-template' and template:return dcc.send_string(pnl.csv_text(),'pnl_import_template.csv')
        if ctx.triggered_id!='pnl-export' or not export:raise PreventUpdate
        return dcc.send_string(pnl.csv_text(pnl.history(ledger.records(),account)['records']),'pnl_history.csv')

    @app.callback(Output('pnl-statement','data'),Output('pnl-import-mapping','children'),Output('pnl-import-file','children'),
                  Input('pnl-upload','contents'),State('pnl-upload','filename'),State('sport','value'),prevent_initial_call=True)
    def pnl_statement(contents,filename,tab):
        if tab!='PNL' or not contents:raise PreventUpdate
        try:
            if len(contents)>7000000:raise ValueError('Import files up to 5 MB.')
            try:decoded=base64.b64decode(contents.split(',',1)[1],validate=True).decode('utf-8-sig')
            except (ValueError,IndexError,UnicodeDecodeError):raise ValueError('Choose a UTF-8 CSV statement.') from None
            columns,rows=pnl.statement_rows(decoded);mapping=pnl.column_mapping(columns)
            controls=[field(name.replace('_',' ').title(),dcc.Dropdown(id=dict(type='pnl-import-map',field=name),options=[dict(label=c,value=c) for c in columns],
                       value=mapping[name],placeholder='Use default / leave blank',clearable=True)) for name in pnl.FIELDS]
            return dict(rows=rows,columns=columns),controls,f'{filename or "Statement"}: {len(rows)} rows. Check the mapping and choose the return-column meaning.'
        except ValueError as exc:return None,[],str(exc)

    @app.callback(Output('pnl-import-preview','data'),Output('pnl-import-table','data'),Output('pnl-import-message','children'),Output('pnl-import-confirm','disabled'),
                  Input('pnl-import-preview-button','n_clicks'),Input('pnl-statement','data'),Input(dict(type='pnl-import-map',field=ALL),'value'),
                  Input('pnl-import-account','value'),Input('pnl-import-type','value'),Input('pnl-import-status','value'),Input('pnl-import-basis','value'),
                  Input('pnl-import-date','value'),Input('pnl-import-debits','value'),State(dict(type='pnl-import-map',field=ALL),'id'),State('sport','value'),Input('pnl-import-fee-time','value'))
    def pnl_preview(clicks,statement,columns,account,kind,status,basis,date_format,debits,identities,tab,fees_at='settlement'):
        if tab!='PNL':raise PreventUpdate
        if ctx.triggered_id!='pnl-import-preview-button' or not clicks:return None,[],'Preview the current mapping before saving.',True
        try:
            if not statement:raise ValueError('Choose a CSV statement first.')
            mapping={item['field']:column for item,column in zip(identities,columns)}
            rows=pnl.mapped_statement(statement['rows'],mapping,account,kind,status,basis,date_format,'debits' in (debits or []),fees_at)
            return rows,rows[:20],f'{len(rows)} valid rows ready. Showing the first 20. Nothing is saved until you click Save previewed statement.',False
        except ValueError as exc:return None,[],str(exc),True
