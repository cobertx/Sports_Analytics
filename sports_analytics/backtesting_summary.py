"""Browse and reopen completed sport simulations directly from their saved reports."""
import json
from hashlib import sha256

import pandas as pd
from dash import Input,Output,State,ctx,dash_table,dcc,html,no_update
from dash.exceptions import PreventUpdate

from .backtesting_view import GROUPS,SPORTS,available_models,find_model,model_name,summary_route
from .backtesting_results import render
from .backtesting_service import evaluated_records,export_filename
from .display_time import eastern_datetime,format_eastern


def layout():
    columns=[('open','Results'),('created','Completed (EDT)'),('group','Category'),('method','Method'),
             ('entity','Team / player'),('market','Statistic'),('evaluated','Evaluated games'),
             ('limit','Requested games'),('lookback','Training lookback'),('period','Evaluation dates'),
             ('phase','Season scope'),('workspace','Workspace'),('run','Run ID')]
    return html.Section([
        dcc.Store(id='backtest-summary-rendered'),
        html.Div([html.Div('SAVED SIMULATIONS',className='eyebrow accent'),html.H2('Backtest Summary'),
                  html.P('Reopen completed simulations for this sport across all Game Outcome and Player Stats methods. '
                         'Results load from saved reports with their original settings; opening a result does not run a model.')],
                 className='panel backtesting-heading'),
        html.Section([
            html.Button('Refresh saved simulations',id='backtest-summary-refresh',n_clicks=0,className='export-button'),
            html.P(id='backtest-summary-count',className='backtest-status',role='status'),
            html.P('Newest first. Use the column filters to find a method, team, player or statistic, then click Open results.',className='backtest-status'),
            dcc.Loading(dash_table.DataTable(id='backtest-summary-table',data=[],
                columns=[dict(id=key,name=label,**({'presentation':'markdown'} if key=='open' else
                    {'type':'numeric'} if key in {'evaluated','limit'} else {})) for key,label in columns],
                markdown_options={'link_target':'_self'},cell_selectable=False,page_size=15,sort_action='native',filter_action='native',
                style_table={'overflowX':'auto'},
                style_cell={'backgroundColor':'#111d2e','color':'#c7d3e4','border':'1px solid #243249',
                            'padding':'10px','textAlign':'left','fontFamily':'Segoe UI','fontSize':'12px',
                            'minWidth':'115px','maxWidth':'300px','whiteSpace':'normal','height':'auto'},
                style_header={'backgroundColor':'#17253a','fontWeight':'600'}),color='#57dbc4',delay_show=500),
        ],id='backtest-summary-list',className='panel backtest-summary-list'),
        html.Section([
            html.A('← All saved simulations',href='#backtesting-summary',className='back-to-games'),
            html.Div([
                html.Div([html.Label('Result target',htmlFor='backtest-summary-target'),dcc.Dropdown(id='backtest-summary-target',
                    options=[dict(label='Scoring margin',value='margin'),dict(label='Total points',value='total')],
                    value='margin',clearable=False)],id='backtest-summary-target-field',className='field'),
                html.Button('Export results',id='backtest-summary-export',n_clicks=0,disabled=True,className='export-button'),
            ],className='backtest-summary-actions'),
            html.P(id='backtest-summary-status',className='backtest-status',role='status'),
            html.Div(id='backtest-summary-settings'),
            dcc.Loading(html.Div(id='backtest-summary-results'),color='#57dbc4',delay_show=500),
        ],id='backtest-summary-detail',style={'display':'none'}),
        dcc.Download(id='backtest-summary-download'),
    ],id='backtest-summary-page',style={'display':'none'})


def catalog(service,sport):
    rows=[]
    for model in available_models(sport):
        for report in service.history(sport,model.group,model.key):
            href=f'#backtesting-summary-{sport.lower()}-{model.group}-{model.key}-{report["run_id"]}'
            rows.append(dict(open=f'[Open results]({href})',created_at=report['created_at'],
                created=eastern_datetime(report['created_at']).strftime('%Y-%m-%d %H:%M:%S'),
                group=GROUPS[model.group],method=model_name(model,sport),entity=report['entity_label'],
                market=(report.get('market') or 'Margin / total').removeprefix('player_').replace('_',' ').title(),
                evaluated=report['evaluated'],limit=report['limit'],lookback=report.get('lookback') or 'Model default',
                period=(report.get('start') or 'Any start')+' → '+(report.get('end') or 'Any end'),
                phase='Regular + playoffs' if report.get('include_playoffs') else 'Regular season',
                workspace='Research' if report.get('workspace')=='research' else 'Backtesting',run=report['run_id'][:12]))
    return sorted(rows,key=lambda row:(row['created_at'],row['open']),reverse=True)


def register(app,service):
    def active(page,sport,location_hash):
        return page=='backtesting' and sport in SPORTS and summary_route(location_hash)

    def saved(page,sport,location_hash):
        route=active(page,sport,location_hash)
        if not route or route.get('sport')!=sport or not route.get('run_id'):return None
        return service.saved(sport,route['group'],route['model'],route['run_id'])

    @app.callback(Output('backtest-summary-page','style'),Output('backtest-summary-list','style'),
                  Output('backtest-summary-detail','style'),Output('backtest-summary-table','data'),
                  Output('backtest-summary-count','children'),Input('workspace-page','value'),Input('sport','value'),
                  Input('market-url','hash'),Input('backtest-summary-refresh','n_clicks'),
                  Input('backtest-result','modified_timestamp'))
    def backtest_summary_list(page,sport,location_hash,_refresh,_updated):
        route=active(page,sport,location_hash);hidden={'display':'none'}
        if not route:return hidden,hidden,hidden,no_update,no_update
        detail=bool(route.get('run_id') and route.get('sport')==sport)
        rows=catalog(service,sport)
        message=(f'{len(rows):,} saved {sport} simulations.' if rows else
                 f'No completed {sport} simulations saved yet. Run a backtest from a method page to add one here.')
        return {},hidden if detail else {},{} if detail else hidden,rows,message

    @app.callback(Output('backtest-summary-results','children'),Output('backtest-summary-settings','children'),
                  Output('backtest-summary-status','children'),Output('backtest-summary-export','disabled'),
                  Output('backtest-summary-target-field','style'),Output('backtest-summary-rendered','data'),
                  Input('workspace-page','value'),Input('sport','value'),
                  Input('market-url','hash'),Input('backtest-summary-target','value'),State('backtest-summary-rendered','data'))
    def backtest_summary_result(page,sport,location_hash,target,previous=None):
        route=active(page,sport,location_hash)
        if not route:raise PreventUpdate
        if not route.get('run_id') or route.get('sport')!=sport:
            return no_update,no_update,no_update,True,{'display':'none'},no_update
        result=saved(page,sport,location_hash)
        if not result or not result['records']:
            return [],[],'This saved simulation is unavailable. Return to the summary and refresh the saved list.',True,{'display':'none'},None
        model=find_model(sport,(route['group'],route['model']))
        context=result['context'];target=target if target in {'margin','total'} else 'margin'
        status=f'{sport} · {model_name(model,sport)} · '+(context.get('entity_label') or context.get('entity',''))
        status+=' · Completed '+format_eastern(result['created_at'])+' · Saved run '+result['run_id'][:12]
        signature=[sport,result['run_id'],target,sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()]
        style={} if route['group']=='game-outcome' else {'display':'none'}
        if signature==previous:return no_update,no_update,status,False,style,no_update
        settings=html.Details([html.Summary('Original simulation settings'),
            html.Pre(json.dumps(dict(evaluation=context,model_parameters=result.get('config',{})),indent=2,ensure_ascii=False),
                     className='research-used-parameters')],className='panel research-applied-parameters')
        return render(result,target),settings,status,False,style,signature

    @app.callback(Output('backtest-summary-download','data'),Input('backtest-summary-export','n_clicks'),
                  State('workspace-page','value'),State('sport','value'),State('market-url','hash'),
                  State('backtest-summary-target','value'),prevent_initial_call=True)
    def backtest_summary_export(clicks,page,sport,location_hash,target):
        if not clicks or ctx.triggered_id!='backtest-summary-export':raise PreventUpdate
        result=saved(page,sport,location_hash)
        if not result or not result['records']:raise PreventUpdate
        target=target if target in {'margin','total'} else 'margin'
        return dcc.send_data_frame(pd.DataFrame(evaluated_records(result,target)).to_csv,export_filename(result,target),index=False)
