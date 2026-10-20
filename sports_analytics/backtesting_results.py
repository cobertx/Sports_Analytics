"""Controls, metric cards and plots for actual chronological replay records."""
import math
from dash import dcc,html,dash_table
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from .backtesting_service import diagnostics
from .display_time import format_eastern


def field(label,control,note=None):
    return html.Div([html.Label(label),control,*([html.Small(note)] if note else [])],className='field')


def layout():
    return html.Section([
        dcc.Store(id='backtest-context'),dcc.Store(id='backtest-result'),dcc.Download(id='backtest-download'),
        html.Div([
            html.H2('Backtesting results'),
            html.P('Replay completed games with the selected model. Forecasts use earlier available data; model settings stay fixed.'),
            html.Div([
                field(html.Span('Team',id='backtest-entity-label'),dcc.Dropdown(id='backtest-entity',options=[],clearable=False)),
                html.Div(field('Statistic',dcc.Dropdown(id='backtest-statistic',options=[],clearable=False)),id='backtest-statistic-field'),
                field('Evaluation dates (EDT)',dcc.DatePickerRange(id='backtest-dates',display_format='MM/DD/YYYY',
                    start_date_placeholder_text='Any start',end_date_placeholder_text='Any end',clearable=True)),
                field('Recent evaluation games',dcc.Dropdown(id='backtest-limit',value=20,clearable=False,
                    options=[dict(label=str(n),value=n) for n in [5,10,20,50,100]]),
                    'Earlier saved games still train the model.'),
                field('Error tolerance',dcc.Input(id='backtest-tolerance',type='number',value=5,min=0,step='any'),
                    'Accuracy within ± this many statistic units.'),
                html.Div(field('Reference line (optional)',dcc.Input(id='backtest-line',type='number',step='any'),
                    'Player probability metrics evaluate Over this fixed line; no historical price is assumed.'),id='backtest-line-field'),
            ],className='backtest-controls'),
            html.Div(id='backtest-input-status',className='backtest-status',role='status'),
            html.Div([html.Button('Run backtest',id='backtest-run',n_clicks=0,className='apply-criteria-button'),
                html.Button('Export results',id='backtest-export',n_clicks=0,disabled=True,className='export-button')],
                className='backtest-actions'),
            html.P(id='backtest-run-status',className='backtest-status',role='status',**{'aria-live':'polite'}),
            html.Div(field('Chart target',dcc.Dropdown(id='backtest-target',value='margin',clearable=False,
                options=[dict(label='Home score margin',value='margin'),dict(label='Total points',value='total')])),
                id='backtest-target-field',className='backtest-target'),
        ],className='panel backtest-setup'),
        dcc.Loading(html.Div(id='backtest-results'),color='#57dbc4'),
    ],id='backtest-evaluation-panel',style={'display':'none'})


def figure(title,x=None,y=None):
    fig=go.Figure()
    fig.update_layout(title=dict(text=title,font=dict(size=15)),template='plotly_dark',paper_bgcolor='#111d2e',
        plot_bgcolor='#111d2e',font=dict(color='#b8c8dc',family='Segoe UI'),height=365,
        margin=dict(l=55,r=20,t=60,b=60),legend=dict(orientation='h',y=-.22),
        xaxis_title=x,yaxis_title=y,hovermode='closest')
    fig.update_xaxes(gridcolor='#243249');fig.update_yaxes(gridcolor='#243249')
    return fig


def card(label,value,note):
    return html.Div([html.Span(label,className='eyebrow'),html.Strong(value),html.Small(note)],className='kpi')


def empty():
    chart=figure('Predicted vs. actual')
    chart.add_annotation(text='Run a backtest to display evaluated predictions.',showarrow=False,x=.5,y=.5,xref='paper',yref='paper')
    return html.Div([html.Div([card('EVALUATED GAMES','—','Completed out-of-sample predictions'),
        card('MAE','—','Mean absolute forecast error'),card('ACCURACY','—','Event classification or tolerance accuracy'),
        card('ERROR VARIANCE','—','Sample variance of prediction errors')],className='backtest-kpis'),
        dcc.Graph(figure=chart,config={'displaylogo':False})],className='panel backtest-empty')


def evaluated_records(result,target='margin'):
    records=[dict(r) for r in result['records']];context=result['context']
    game=context['group']=='game-outcome'
    if game:
        for row in records:
            row.update(actual=row['actual_'+target],predicted=row['predicted_'+target],baseline=row['baseline_'+target],
                lower=row['predicted_'+target]-1.64485362695*row[target+'_sd'],
                upper=row['predicted_'+target]+1.64485362695*row[target+'_sd'])
    return records


def render(result,target='margin'):
    records=evaluated_records(result,target);context=result['context'];game=context['group']=='game-outcome'
    metrics=diagnostics(records,context['tolerance']);data=pd.DataFrame(records)
    data['error']=data.predicted-data.actual
    values=lambda key,suffix='':f'{metrics[key]:.2f}{suffix}' if metrics.get(key) is not None else '—'
    percent=lambda key:f'{100*metrics[key]:.1f}%' if metrics.get(key) is not None else '—'
    accuracy_note=('Home-winner classification; tied results excluded.' if game else
                   'Over/Under at the applied reference line; pushes excluded.')
    m=metrics['probability_samples']
    if m:
        rate=metrics['accuracy'];z=1.95996398454
        center=(rate+z*z/(2*m))/(1+z*z/m)
        half=z*math.sqrt(rate*(1-rate)/m+z*z/(4*m*m))/(1+z*z/m)
        accuracy_note+=f' 95% Wilson interval {100*(center-half):.1f}–{100*(center+half):.1f}% (n={m}).'
    else:accuracy_note='Enter a reference line and rerun to evaluate event accuracy.'
    cards=[card('EVALUATED GAMES',f'{len(records):,}',f'{result["attempted"]} attempted; {len(result["skipped"])} skipped'),
        card('EVENT ACCURACY',percent('accuracy'),accuracy_note),
        card('MAE',values('mae'),'Average absolute error; lower is better.'),
        card('RMSE',values('rmse'),'Larger errors receive more weight.'),
        card('BIAS',values('bias'),'Predicted − actual; positive means overprediction.'),
        card('ERROR VARIANCE',values('error_variance'),f'Sample variance (squared units); error SD {values("error_sd")}.'),
        card('WITHIN TOLERANCE',percent('within_tolerance'),f'Absolute error ≤ {context["tolerance"]:g} statistic units.'),
        card('90% INTERVAL COVERAGE',percent('coverage_90'),'Actual results inside the model’s predictive interval.'),
        card('BRIER SCORE',values('brier'),'Event probability error; 0 is ideal. Pushes excluded.'),
        card('LOG LOSS',values('log_loss'),'Penalizes confident incorrect probabilities; lower is better.'),
        card('BASELINE MAE',values('baseline_mae'),'Prior league home margin/total mean.' if game else 'Prior five-appearance mean on the same evaluated games.'),
        card('PUSHES / TIES',str(metrics['pushes']),'Excluded from binary event accuracy and probability scoring.')]
    unit=('home margin (points)' if target=='margin' else 'total points') if game else context['market'].removeprefix('player_').replace('_',' ')
    timeline=figure('Predicted vs. actual over time','Game date',unit)
    timeline.add_trace(go.Scatter(x=data.date,y=data.lower,mode='lines',line=dict(width=0),showlegend=False,hoverinfo='skip'))
    timeline.add_trace(go.Scatter(x=data.date,y=data.upper,mode='lines',line=dict(width=0),fill='tonexty',
        fillcolor='rgba(87,219,196,.13)',name='90% predictive interval',hoverinfo='skip'))
    timeline.add_trace(go.Scatter(x=data.date,y=data.predicted,name='Predicted',mode='lines+markers',line=dict(color='#57dbc4'),text=data.label))
    timeline.add_trace(go.Scatter(x=data.date,y=data.actual,name='Actual',mode='lines+markers',line=dict(color='#8197ff'),text=data.label))
    timeline.update_xaxes(tickformat='%m/%d/%Y')
    parity=figure('Prediction agreement','Actual '+unit,'Predicted '+unit)
    parity.add_trace(go.Scatter(x=data.actual,y=data.predicted,mode='markers',name='Evaluated game',
        marker=dict(color=data.error,colorscale='RdBu',showscale=True,colorbar=dict(title='Error')),text=data.label))
    extent=[float(min(data.actual.min(),data.predicted.min())),float(max(data.actual.max(),data.predicted.max()))]
    parity.add_trace(go.Scatter(x=extent,y=extent,mode='lines',name='Perfect forecast',line=dict(color='#a7b9cd',dash='dash')))
    residual=figure('Forecast error distribution','Predicted − actual','Games')
    residual.add_trace(go.Histogram(x=data.error,name='Forecast error',marker_color='#8197ff',nbinsx=min(25,max(5,len(data)//2))))
    residual.add_vline(x=0,line_dash='dash',line_color='#57dbc4')
    rolling=figure('Rolling absolute error','Game date','Mean absolute error (up to 5 games)')
    rolling.add_trace(go.Scatter(x=data.date,y=data.error.abs().rolling(5,min_periods=1).mean(),
        name='Model',mode='lines+markers',line=dict(color='#57dbc4')))
    baseline=(data.baseline-data.actual).abs().rolling(5,min_periods=1).mean()
    rolling.add_trace(go.Scatter(x=data.date,y=baseline,name='Prior-data baseline',mode='lines',line=dict(color='#f6c667')))
    rolling.update_xaxes(tickformat='%m/%d/%Y')
    calibration=figure('Probability calibration','Predicted event probability','Observed event frequency')
    calibration.add_trace(go.Scatter(x=[0,1],y=[0,1],mode='lines',name='Perfect calibration',line=dict(color='#a7b9cd',dash='dash')))
    bins=metrics.get('reliability',[])
    if bins:
        calibration.add_trace(go.Scatter(x=[r['predicted'] for r in bins],y=[r['observed'] for r in bins],
            mode='lines+markers',name='Evaluated forecasts',text=[f'n={r["n"]}' for r in bins],
            marker=dict(size=[7+min(15,math.sqrt(r['n'])) for r in bins],color='#57dbc4')))
    else:calibration.add_annotation(text='Enter a player reference line and rerun for probability calibration.',showarrow=False,x=.5,y=.5,xref='paper',yref='paper')
    calibration.update_xaxes(range=[0,1],tickformat='.0%');calibration.update_yaxes(range=[0,1],tickformat='.0%')
    table=[]
    for r in records:
        table.append(dict(date=format_eastern(r['date']),game=r['label'],actual=round(r['actual'],3),
            predicted=round(r['predicted'],3),error=round(r['predicted']-r['actual'],3),
            lower=round(r['lower'],3),upper=round(r['upper'],3),
            probability=round(100*r['probability'],2) if r.get('probability') is not None else None,
            training_through=r.get('latest_training')))
    return html.Div([
        html.P(f'Saved run: {format_eastern(result["created_at"])} · {context["sport"]} · '
            f'{context.get("entity_label",context["entity"])} · {records[0]["date"][:10]} to {records[-1]["date"][:10]} · '
            f'Last {context["limit"]} eligible games. Run again to apply changed filters.',className='backtest-applied'),
        html.Div(cards,className='backtest-kpis'),
        html.P('Event metrics evaluate the home winner.' if game else
            ('Event metrics evaluate Over '+str(context['line'])+'.' if context['line'] is not None else 'Event metrics require an optional reference line.'),
            className='backtest-status'),
        html.P('Probabilities are evaluated against outcomes, without assumed historical sportsbook prices. '
               'Availability lags and revised source files limit historical as-of fidelity. '
               'Accuracy intervals are descriptive; repeated players and teams can be correlated.',className='backtest-status'),
        html.Div([html.Div(dcc.Graph(figure=fig,config={'displaylogo':False}),className='panel backtest-chart')
                  for fig in [timeline,parity,residual,rolling,calibration]],className='backtest-chart-grid'),
        html.Section([html.Div(html.H3('Evaluated predictions'),className='panel-heading'),
            dash_table.DataTable(data=table,columns=[dict(name=label,id=key) for key,label in [
                ('date','Game time (EDT)'),('game','Matchup / player'),('actual','Actual'),('predicted','Predicted'),
                ('error','Error'),('lower','90% interval low'),('upper','90% interval high'),('probability','Event probability (%)'),
                ('training_through','Latest training game')]],page_size=12,sort_action='native',filter_action='native',
                style_table={'overflowX':'auto'},style_cell={'backgroundColor':'#111d2e','color':'#c7d3e4','border':'1px solid #243249',
                    'textAlign':'left','padding':'10px','fontFamily':'Segoe UI','fontSize':'12px'},
                style_header={'backgroundColor':'#17253a','fontWeight':'600'})],className='panel backtest-table'),
        html.Details([html.Summary(f'Skipped games and reasons ({len(result["skipped"])})'),
            html.Ul([html.Li(f'{r["date"][:10]} · {r["game_id"]}: {r["reason"]}') for r in result['skipped']])],
            className='panel backtest-skipped'),
    ])
