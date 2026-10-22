"""Controls and comparable metrics/plots for research methodology comparisons."""
import numpy as np
from dash import dcc,html,dash_table
import plotly.graph_objects as go

from .backtesting_results import field,figure
from .comparison import methods,compare_reports


def layout():
    return html.Section([
        dcc.Store(id='comparison-context'),dcc.Store(id='comparison-state'),
        dcc.Store(id='comparison-rendered'),dcc.Store(id='comparison-detail-rendered'),
        dcc.Store(id='comparison-pages',data={},storage_type='session'),
        dcc.Interval(id='comparison-poll',interval=1500,disabled=True),dcc.Download(id='comparison-download'),
        html.Section([
            field('Methods to compare',dcc.Dropdown(id='comparison-methods',multi=True,clearable=True,options=[])),
            html.Button('Select all methods',id='comparison-all',n_clicks=0,className='export-button'),
            html.Div([
                field(html.Span('Team / player',id='comparison-entity-label'),dcc.Dropdown(id='comparison-entity',options=[],clearable=False)),
                html.Div(field('Evaluation team',dcc.Dropdown(id='comparison-team',options=[],value='__all__',clearable=False)),id='comparison-team-field'),
                html.Div(field('Statistic',dcc.Dropdown(id='comparison-statistic',options=[],clearable=False)),id='comparison-statistic-field'),
                field('Evaluation start (EDT)',dcc.DatePickerSingle(id='comparison-start',display_format='MM/DD/YYYY',clearable=True)),
                field('Evaluation end (EDT)',dcc.DatePickerSingle(id='comparison-end',display_format='MM/DD/YYYY',clearable=True)),
                field('Backtest Data Games',dcc.Input(id='comparison-limit',type='number',min=1,max=10000,step=1,value=100)),
                field('Postseason',dcc.Dropdown(id='comparison-playoffs',options=[dict(label='Regular season only',value='exclude'),
                    dict(label='Regular season + postseason',value='include')],value='exclude',clearable=False)),
                field('Training lookback',dcc.Input(id='comparison-lookback',type='text',value='Model Default',debounce=True),
                    'Model Default or a whole-number cap applied to every method.'),
                field('Error tolerance',dcc.Input(id='comparison-tolerance',type='number',value=5,min=0,step='any')),
                html.Div(field('Reference line (optional)',dcc.Input(id='comparison-line',type='number',step='any')),id='comparison-line-field'),
            ],className='backtest-controls comparison-controls'),
            html.P(id='comparison-input-status',className='backtest-status',role='status'),
            html.Div([html.Button('Run comparison',id='comparison-run',n_clicks=0,className='apply-criteria-button'),
                html.Button('Export comparison CSV',id='comparison-export',n_clicks=0,className='export-button',disabled=True)],className='backtest-actions'),
            html.P('Runs use each method’s sport-specific defaults and saved inputs. Model parameters can be customized on individual strategy pages; compare those reports through Previous simulations below.',className='backtest-status'),
        ],className='panel comparison-setup'),
        html.Details([
            html.Summary('Previous simulations and comparisons'),
            field('Saved comparison',dcc.Dropdown(id='comparison-history',options=[],placeholder='Choose a comparison')),
            html.Button('Open comparison',id='comparison-history-open',n_clicks=0,className='export-button'),
            html.H3('Compare previous simulations'),
            html.P('Choose one saved simulation for each selected method. Player reports must use the same statistic and reference line. Saved runs retain their original parameters and evaluation scopes.',className='backtest-status'),
            html.Div(id='comparison-saved-selectors',className='research-parameters'),
            html.Button('Compare selected simulations',id='comparison-saved-open',n_clicks=0,className='export-button'),
        ],id='comparison-history-panel',open=False,className='panel backtest-skipped comparison-history'),
        html.P(id='comparison-status',className='backtest-status',role='status',**{'aria-live':'polite'}),
        html.Div(id='comparison-progress',className='comparison-progress'),
        html.Div([
            field('Comparison sample',dcc.Dropdown(id='comparison-basis',options=[
                dict(label='Common evaluated games / appearances',value='common'),dict(label='All evaluated results per method',value='all')],
                value='common',clearable=False)),
            html.Div(field('Game target',dcc.Dropdown(id='comparison-target',options=[dict(label='Home margin',value='margin'),
                dict(label='Total points',value='total')],value='margin',clearable=False)),id='comparison-target-field'),
        ],className='research-scope-controls'),
        dcc.Loading(html.Div(id='comparison-results'),color='#57dbc4'),
        html.Section([
            field('Inspect full method report',dcc.Dropdown(id='comparison-detail-method',options=[],clearable=False)),
            html.Div(id='comparison-detail'),
        ],className='comparison-detail'),
    ],id='research-comparison',style={'display':'none'})


def render(context,reports,target,basis,tolerance):
    data=compare_reports(context,reports,target,basis,tolerance)
    summary=data['summary'];rows=data['records']
    columns=[('method','Method'),('evaluated','Evaluated'),('compared','Compared'),('excluded','Outside sample'),('skipped','Skipped'),('mae','MAE'),
        ('rmse','RMSE'),('bias','Bias'),('within_tolerance','Within tolerance'),('interval_level','Interval level'),
        ('interval_coverage','Interval coverage'),('accuracy','Event accuracy'),('brier','Brier'),('log_loss','Log loss'),
        ('baseline_mae','Baseline MAE'),('probability_samples','Probability samples')]
    table=[]
    for record in summary:
        formatted={key:(round(record[key],4) if isinstance(record.get(key),float) else record.get(key)) for key,_ in columns}
        for key in ['within_tolerance','interval_level','interval_coverage','accuracy']:
            formatted[key]=f'{record[key]:.1%}' if record.get(key) is not None else None
        table.append(formatted)
    comparison_table=dash_table.DataTable(data=table,columns=[dict(id=k,name=label) for k,label in columns],
        sort_action='native',style_table={'overflowX':'auto'},
        style_cell={'backgroundColor':'#111d2e','color':'#c7d3e4','border':'1px solid #243249','padding':'10px','textAlign':'left','fontFamily':'Segoe UI','fontSize':'12px'},
        style_header={'backgroundColor':'#17253a','fontWeight':'600'})
    errors=figure('Forecast errors by method','Method','Error in statistic units')
    for metric,label in [('mae','MAE'),('rmse','RMSE')]:
        errors.add_trace(go.Bar(x=[r['method'] for r in summary],y=[r.get(metric) for r in summary],name=label))
    errors.update_layout(barmode='group')
    timeline=figure('Rolling absolute error on the comparison sample','Evaluated game / appearance','Trailing 10-observation MAE')
    for key,records in rows.items():
        absolute=np.array([abs(r['predicted']-r['actual']) for r in records])
        if not len(absolute):continue
        cumulative=np.r_[0,np.cumsum(absolute)];indices=np.arange(len(absolute));starts=np.maximum(0,indices-9)
        rolling=(cumulative[indices+1]-cumulative[starts])/(indices-starts+1)
        timeline.add_trace(go.Scatter(x=indices+1,y=rolling,mode='lines',name=key.upper(),
            customdata=[[r['date'][:10],r['label']] for r in records],
            hovertemplate='%{customdata[0]}<br>%{customdata[1]}<br>MAE %{y:.3f}<extra>%{fullData.name}</extra>'))
    notes=[f"{data['common']:,} identical evaluated games / player appearances shared by all {len(reports)} completed methods.",
        'Lower MAE, RMSE, Brier score and log loss are better. Bias is predicted minus actual. Coverage must be read alongside each report’s interval level.',
        f'Within-tolerance metrics are recalculated at ±{tolerance:g} units for every report. '+
        ('Event probabilities evaluate the home winner, including when the point-error target is total points.' if context['group']=='game-outcome' else
         'Event probabilities evaluate Over the saved shared reference line; missing lines have no event scores.'),
        'Individual full reports below retain their original sample, parameters and tolerance.']
    if basis=='all':notes.insert(0,'Each method uses its own evaluated sample. Differences in coverage can affect these rankings; observation positions need not refer to the same games.')
    if data['conflicting_actuals']:notes.insert(0,f"{data['conflicting_actuals']} common identities have inconsistent actual outcomes across saved reports and are excluded from the common sample.")
    if basis=='common' and not data['common']:notes.insert(0,'No common evaluated observations. Adjust the dates/scope, load compatible saved runs, or choose All evaluated results per method.')
    return html.Div([
        html.Section([html.H3('Methodology results'),*[html.P(note,className='backtest-status') for note in notes],comparison_table],className='panel comparison-summary'),
        html.Div([html.Div(dcc.Graph(figure=chart,config={'displaylogo':False}),className='panel backtest-chart') for chart in [errors,timeline]],className='backtest-chart-grid'),
    ]),data
