"""Controls, metric cards and plots for actual chronological replay records."""
import math,json
from dash import dcc,html,dash_table
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from .presentation import chart_style, table_cell, table_header
from .backtesting_service import diagnostics,PREDICTIVE_INTERVAL,evaluated_records
from .display_time import format_eastern,EASTERN


def field(label,control,note=None):
    return html.Div([html.Label(label),control,*([html.Small(note)] if note is not None else [])],className='field')


def layout():
    return html.Section([
        dcc.Store(id='backtest-context'),dcc.Store(id='backtest-result'),dcc.Store(id='backtest-job'),
        dcc.Store(id='backtest-pages',data={},storage_type='session'),
        dcc.Interval(id='backtest-poll',interval=750,disabled=True),dcc.Download(id='backtest-download'),
        html.Details([
            html.Summary('Previous simulations'),
            html.P('Reopen completed simulations for this sport and method, including their original settings and results.',className='backtest-status'),
            html.Div([
                field('Saved simulation',dcc.Dropdown(id='backtest-history',options=[],value=None,
                    placeholder='Choose a previous simulation',clearable=True)),
                html.Button('Open simulation',id='backtest-history-open',n_clicks=0,disabled=True,className='export-button'),
            ],className='backtest-history-controls'),
            html.P(id='backtest-history-status',className='backtest-status',role='status'),
            html.P(['Saved files: ',html.Code(id='backtest-history-location')],className='backtest-status'),
            html.P('JSON reports retain the full simulation. CSV exports open in Excel and are available through Export results.',className='backtest-status'),
        ],id='backtest-history-panel',open=False,className='panel backtest-history'),
        html.Div([
            html.H2('Backtesting results'),
            html.P('Replay completed games using only earlier available data. Choose the evaluation sample and estimation history separately.'),
            html.Div([
                field(html.Span('Team',id='backtest-entity-label'),dcc.Dropdown(id='backtest-entity',options=[],clearable=False)),
                html.Div(field('Statistic',dcc.Dropdown(id='backtest-statistic',options=[],clearable=False)),id='backtest-statistic-field'),
                html.Div([
                    field('Evaluation start (EDT)',dcc.DatePickerSingle(id='backtest-start',display_format='MM/DD/YYYY',
                        placeholder='MM/DD/YYYY',clearable=True,first_day_of_week=0)),
                    field('Evaluation end (EDT)',dcc.DatePickerSingle(id='backtest-end',display_format='MM/DD/YYYY',
                        placeholder='MM/DD/YYYY',clearable=True,first_day_of_week=0)),
                    html.Small('Optional; leave blank for any date.',className='backtest-date-note'),
                ],className='backtest-date-fields'),
                field('Backtest Data Games',dcc.Input(id='backtest-limit',type='number',value=1000,min=1,max=10000,step=1),
                    html.Span('Checking available games…',id='backtest-available')),
                field('Include playoff / postseason games',dcc.Dropdown(id='backtest-playoffs',value='exclude',clearable=False,
                    options=[dict(label='No — regular season only',value='exclude'),dict(label='Yes — regular season + postseason',value='include')]),
                    'Applies to evaluation and estimation history. College postseason includes bowls.'),
                field('Lookback Training Data Games (optional)',dcc.Input(id='backtest-lookback',type='text',debounce=True,
                    value='All available (Model Default)',placeholder='Model Default or whole number'),
                    html.Span(id='backtest-lookback-note')),
                field('Error tolerance',dcc.Input(id='backtest-tolerance',type='number',value=5,min=0,step='any'),
                    'Accuracy within ± this many statistic units.'),
                html.Div(field('Reference line (optional)',dcc.Input(id='backtest-line',type='number',step='any'),
                    'Player probability metrics evaluate Over this fixed line; no historical price is assumed.'),id='backtest-line-field'),
            ],className='backtest-controls'),
            html.Div(id='backtest-input-status',className='backtest-status',role='status'),
            html.Div([html.Button('Run backtest',id='backtest-run',n_clicks=0,className='apply-criteria-button'),
                html.Button('Export results',id='backtest-export',n_clicks=0,disabled=True,className='export-button'),
                html.Div([html.Progress(id='backtest-progress',value=0,max=100),
                    html.Span('Ready',id='backtest-progress-label',role='status',**{'aria-live':'polite'})],
                    className='backtest-progress')],
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
    fig.update_layout(**chart_style(),title=dict(text=title,font=dict(size=15)),height=365,
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


def per_game_axis(fig,data):
    """Keep every evaluated game distinct, including games on the same date."""
    x=np.arange(1,len(data)+1)
    ticks=np.unique(np.linspace(0,len(data)-1,min(7,len(data)),dtype=int))
    fig.update_xaxes(tickmode='array',tickvals=x[ticks],
        ticktext=[format_eastern(data.iloc[i].date)[:10]+f'<br>#{i+1}' for i in ticks],
        title='Evaluated game (date)')
    return x,[[format_eastern(r.date),r.label,str(r.game_id)] for r in data.itertuples()]


def range_accuracy(records,level=PREDICTIVE_INTERVAL):
    """Inclusive interval coverage, using only valid ranges at the requested level."""
    hits=[]
    for row in records:
        recorded_level=row.get('interval_level')
        values=[row.get(key) for key in ('actual','lower','upper')]
        if (not isinstance(recorded_level,(int,float)) or not math.isclose(recorded_level,level,rel_tol=1e-5)
                or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in values)):
            continue
        actual,lower,upper=values
        if lower<=upper:hits.append(lower<=actual<=upper)
    return dict(range_accuracy=sum(hits)/len(hits) if hits else None,range_samples=f'{sum(hits)} / {len(hits)}')


def error_summary(errors):
    """Exact statistics of signed forecast errors, rather than histogram bins."""
    values=np.asarray(errors,dtype=float).ravel()
    if not np.isfinite(values).all():raise ValueError('Forecast errors must be finite.')
    if not len(values):return dict(samples=0)
    q05,q25,q75,q95=np.percentile(values,[5,25,75,95])
    return dict(samples=len(values),mean=float(values.mean()),median=float(np.median(values)),
        sample_sd=float(values.std(ddof=1)) if len(values)>1 else None,
        mae=float(np.abs(values).mean()),rmse=float(np.sqrt(np.mean(values**2))),
        minimum=float(values.min()),maximum=float(values.max()),
        p05=float(q05),q25=float(q25),q75=float(q75),p95=float(q95))


def error_distribution(fig,errors):
    """Exact histogram counts with a Gaussian-kernel descriptive overlay."""
    errors=np.asarray(errors,float).ravel()
    summary=error_summary(errors)
    if not len(errors):
        fig.add_annotation(text='No evaluated forecast errors.',showarrow=False,x=.5,y=.5,xref='paper',yref='paper')
        return
    count=max(20,min(100,math.ceil(3*math.sqrt(len(errors)))))
    counts,edges=np.histogram(errors,bins=count)
    centers=(edges[:-1]+edges[1:])/2
    fig.add_trace(go.Bar(x=centers,y=counts,width=np.diff(edges)*.92,name='Forecast error',marker_color='#8197ff',
        customdata=np.column_stack([edges[:-1],edges[1:]]),
        hovertemplate='Error bin %{customdata[0]:.2f} to %{customdata[1]:.2f}<br>Games: %{y}<extra></extra>'))
    width=float(edges[1]-edges[0])
    sd=float(errors.std(ddof=1)) if len(errors)>1 else 0.
    iqr=float(np.subtract(*np.percentile(errors,[75,25])))/1.349
    scale=min(sd,iqr) if iqr>0 else sd
    bandwidth=max(2*width,.9*scale*len(errors)**(-.2))
    grid=np.linspace(edges[0]-3*bandwidth,edges[-1]+3*bandwidth,384)
    kernel=np.zeros_like(grid)
    # Chunking bounds memory even for 10,000-game backtests. Multiplying
    # density by N * bin width keeps the curve on the histogram count scale.
    for offset in range(0,len(errors),512):
        z=(grid[:,None]-errors[None,offset:offset+512])/bandwidth
        kernel+=np.exp(-.5*z*z).sum(axis=1)
    smooth=kernel*width/(bandwidth*math.sqrt(2*math.pi))
    fig.add_trace(go.Scatter(x=grid,y=smooth,mode='lines',name='Smoothed distribution',
        line=dict(color='#57dbc4',width=2.5),
        hovertemplate='Error: %{x:.2f}<br>Smoothed games per bin: %{y:.2f}<extra></extra>'))
    fig.add_vline(x=0,line_dash='dash',line_color='#f6c667')
    fig.update_yaxes(rangemode='tozero',dtick=1 if max(counts)<10 else None)
    # Reserve space above the plot so the statistics cannot obscure histogram
    # bars or tails. Three short columns remain readable in narrow chart panels.
    fig.update_layout(height=460,margin=dict(t=150),title=dict(y=.98,yanchor='top'),
        meta={**(fig.layout.meta or {}),'error_distribution':summary})
    stats=[('Mean (bias)',f'{summary["mean"]:+.2f}','Average signed error. Positive means overprediction; negative means underprediction.'),
           ('Median',f'{summary["median"]:+.2f}','Middle signed error; less sensitive to extreme errors than the mean.'),
           ('Sample SD',f'{summary["sample_sd"]:.2f}' if summary['sample_sd'] is not None else 'N/A',
            'Sample standard deviation of signed errors (n − 1 denominator). Requires at least two observations.'),
           ('Samples',f'{summary["samples"]:,}','Evaluated games or player appearances in this distribution.'),
           ('MAE',f'{summary["mae"]:.2f}','Mean absolute error, in the selected statistic’s units.'),
           ('RMSE',f'{summary["rmse"]:.2f}','Root mean squared error; larger misses receive more weight.')]
    range_text=(f'<br>Min / max: {summary["minimum"]:.2f} / {summary["maximum"]:.2f}'
                f'<br>25th / 75th percentile: {summary["q25"]:.2f} / {summary["q75"]:.2f}'
                f'<br>5th / 95th percentile: {summary["p05"]:.2f} / {summary["p95"]:.2f}')
    for index,(label,value,help_text) in enumerate(stats):
        column=index%3
        fig.add_annotation(x=[0,.5,1][column],y=1.29 if index<3 else 1.07,xref='paper',yref='paper',
            xanchor=['left','center','right'][column],yanchor='bottom',showarrow=False,
            text=f'<span style="font-size:10px">{label}</span><br><b>{value}</b>',
            hovertext=help_text+range_text,align='center',font=dict(size=12,color='#c7d3e4'),
            bgcolor='#17253a',bordercolor='#243249',borderpad=5)


def historical_accuracy(fig,data,context):
    """Daily and trailing-game coverage of the actual central 80% intervals."""
    ordered=data.copy()
    level=ordered['interval_level'] if 'interval_level' in ordered else pd.Series(PREDICTIVE_INTERVAL,index=ordered.index)
    valid=np.isclose(level,PREDICTIVE_INTERVAL)&np.isfinite(ordered[['actual','lower','upper']].to_numpy(float)).all(axis=1)
    ordered=ordered[valid].copy()
    if ordered.empty:
        fig.add_annotation(text='Rerun this backtest to calculate<br>80% predictive interval accuracy.',showarrow=False,
            x=.5,y=.5,xref='paper',yref='paper')
        fig.update_yaxes(range=[-.05,1.05],tickformat='.0%',title='80% interval accuracy')
        return
    ordered['event_time']=pd.to_datetime(ordered.date,utc=True)
    ordered=ordered.sort_values('event_time',kind='stable').reset_index(drop=True)
    ordered['success']=ordered.actual.ge(ordered.lower)&ordered.actual.le(ordered.upper)
    ordered['day']=ordered.event_time.dt.tz_convert(EASTERN).dt.strftime('%Y-%m-%d')
    x,hover=per_game_axis(fig,ordered)
    ordered['position']=x
    daily=ordered.groupby('day',sort=True).agg(accuracy=('success','mean'),games=('success','size'),position=('position','max'))
    fig.add_trace(go.Scatter(x=daily.position,y=daily.accuracy,mode='lines+markers',name='Daily average',
        line=dict(color='#57dbc4',width=2),marker=dict(size=4),
        customdata=[[pd.Timestamp(day).strftime('%m/%d/%Y'),int(row.games)] for day,row in daily.iterrows()],
        hovertemplate='%{customdata[0]} EDT<br>Games on date: %{customdata[1]}<br>Accuracy: %{y:.1%}<extra>Daily average</extra>'))
    for window,color in [(10,'#8197ff'),(20,'#f6c667'),(50,'#ef96d0')]:
        sample=ordered.success.rolling(window,min_periods=1)
        fig.add_trace(go.Scatter(x=x,y=sample.mean(),name=f'Last {window} games',mode='lines',line=dict(color=color,width=2),
            customdata=[[*h,int(n)] for h,n in zip(hover,sample.count())],
            hovertemplate='%{customdata[0]}<br>%{customdata[1]}<br>Games in window: %{customdata[3]}<br>Accuracy: %{y:.1%}<extra>%{fullData.name}</extra>'))
    fig.update_layout(margin=dict(t=85))
    fig.add_annotation(text='Success: inside 80% interval (inclusive)<br>Early windows use available games',showarrow=False,
        x=0,y=1.1,xref='paper',yref='paper',xanchor='left',font=dict(size=11))
    fig.update_yaxes(range=[-.05,1.05],tickformat='.0%',title='Accuracy')


def render(result,target='margin'):
    records=evaluated_records(result,target);context=result['context'];game=context['group']=='game-outcome'
    metrics=diagnostics(records,context['tolerance']);data=pd.DataFrame(records)
    data['error']=data.predicted-data.actual
    interval_label=f'{100*records[0]["interval_level"]:.0f}%'
    coverage=range_accuracy(records,records[0]['interval_level'])
    metrics['interval_coverage']=coverage['range_accuracy']
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
        card(interval_label+' INTERVAL COVERAGE',percent('interval_coverage'),'Actual results inside the model’s predictive interval.'),
        card('BRIER SCORE',values('brier'),'Event probability error; 0 is ideal. Pushes excluded.'),
        card('LOG LOSS',values('log_loss'),'Penalizes confident incorrect probabilities; lower is better.'),
        card('BASELINE MAE',values('baseline_mae'),'Prior league home margin/total mean.' if game else 'Prior five-appearance mean on the same evaluated games.'),
        card('PUSHES / TIES',str(metrics['pushes']),'Excluded from binary event accuracy and probability scoring.')]
    unit=('home margin (points)' if target=='margin' else 'total points') if game else context['market'].removeprefix('player_').replace('_',' ')
    timeline=figure('Predicted vs. actual over time','Game date (EDT)',unit)
    timeline_x,timeline_hover=per_game_axis(timeline,data)
    data['display_date']=[row[0] for row in timeline_hover]
    timeline.add_trace(go.Scatter(x=timeline_x,y=data.lower,mode='lines',line=dict(width=0),showlegend=False,hoverinfo='skip'))
    timeline.add_trace(go.Scatter(x=timeline_x,y=data.upper,mode='lines',line=dict(width=0),fill='tonexty',
        fillcolor='rgba(87,219,196,.13)',name=interval_label+' predictive interval',hoverinfo='skip'))
    timeline.add_trace(go.Scatter(x=timeline_x,y=data.predicted,name='Predicted',mode='lines+markers',line=dict(color='#57dbc4'),
        marker=dict(size=3),text=data.label,customdata=timeline_hover,
        hovertemplate='%{customdata[0]}<br>%{customdata[1]}<br>Game %{customdata[2]}<br>Predicted: %{y:.2f}<extra>Predicted</extra>'))
    inside=data.actual.ge(data.lower)&data.actual.le(data.upper)
    for mask,color,symbol,label in [(inside,'#8197ff','circle','Actual — inside interval'),
                                    (~inside,'#ff7b87','x','Actual — outside interval')]:
        timeline.add_trace(go.Scatter(x=timeline_x[mask.to_numpy()],y=data.loc[mask,'actual'],name=label,mode='markers',
            marker=dict(color=color,size=4,symbol=symbol),text=data.loc[mask,'label'],
            customdata=data.loc[mask,['display_date','lower','upper']],
            hovertemplate='%{text}<br>%{customdata[0]}<br>Actual: %{y:.2f}<br>'+interval_label+
                ' interval: [%{customdata[1]:.2f}, %{customdata[2]:.2f}]<extra>%{fullData.name}</extra>'))
    timeline.update_xaxes(type='linear')
    parity=figure('Prediction agreement','Actual '+unit,'Predicted '+unit)
    parity.add_trace(go.Scatter(x=data.actual,y=data.predicted,mode='markers',name='Evaluated game',
        marker=dict(color=data.error,colorscale='RdBu',showscale=True,colorbar=dict(title='Error')),text=data.label))
    extent=[float(min(data.actual.min(),data.predicted.min())),float(max(data.actual.max(),data.predicted.max()))]
    parity.add_trace(go.Scatter(x=extent,y=extent,mode='lines',name='Perfect forecast',line=dict(color='#a7b9cd',dash='dash')))
    residual=figure('Forecast error distribution','Predicted − actual','Games')
    error_distribution(residual,data.error)
    rolling=figure('Rolling absolute error','Game date','Mean absolute error (up to 5 games)')
    game_x,hover=per_game_axis(rolling,data)
    rolling.add_trace(go.Scatter(x=game_x,y=data.error.abs().rolling(5,min_periods=1).mean(),
        name='Model',mode='lines+markers',line=dict(color='#57dbc4',shape='spline',smoothing=.5),marker=dict(size=4),
        customdata=hover,hovertemplate='%{customdata[0]}<br>%{customdata[1]}<br>Game %{customdata[2]}<br>MAE: %{y:.2f}<extra>Model</extra>'))
    baseline=(data.baseline-data.actual).abs().rolling(5,min_periods=1).mean()
    rolling.add_trace(go.Scatter(x=game_x,y=baseline,name='Prior-data baseline',mode='lines',
        line=dict(color='#f6c667',shape='spline',smoothing=.5),customdata=hover,
        hovertemplate='%{customdata[0]}<br>%{customdata[1]}<br>MAE: %{y:.2f}<extra>Prior-data baseline</extra>'))
    rolling.update_yaxes(rangemode='tozero')
    calibration=figure('Probability calibration','Predicted event probability','Observed event frequency')
    calibration.add_trace(go.Scatter(x=[0,1],y=[0,1],mode='lines',name='Perfect calibration',line=dict(color='#a7b9cd',dash='dash')))
    bins=metrics.get('reliability',[])
    if bins:
        calibration.add_trace(go.Scatter(x=[r['predicted'] for r in bins],y=[r['observed'] for r in bins],
            mode='lines+markers',name='Calibration (2% bins)',
            text=[f'{r["lower"]:.0%}–{r["upper"]:.0%}; n={r["n"]}' for r in bins],
            hovertemplate='%{text}<br>Mean forecast: %{x:.1%}<br>Observed: %{y:.1%}<extra></extra>',
            marker=dict(size=[7+min(15,math.sqrt(r['n'])) for r in bins],color='#57dbc4')))
    else:calibration.add_annotation(text='Enter a player reference line and rerun for probability calibration.',showarrow=False,x=.5,y=.5,xref='paper',yref='paper')
    calibration.update_xaxes(range=[0,1],tickformat='.0%');calibration.update_yaxes(range=[0,1],tickformat='.0%')
    accuracy=figure('Historical accuracy by game','Game date','Accuracy')
    historical_accuracy(accuracy,data,context)
    chart_notes={id(timeline):'Evaluated games or player appearances are spaced evenly. Dates without evaluated results, including the offseason, are omitted. Hover for the actual game date.',
        id(residual):'Error = predicted − actual. Mean and median describe bias; sample SD describes spread. MAE and RMSE measure miss size. Hover a statistic for its definition, range and percentiles.',
        id(rolling):'Each point averages absolute errors over the last up to 5 evaluated observations, including individual player appearances in cohort runs. Lower is better. Compare the model with the prior-data baseline on the same observations; rising values indicate worse recent point forecasts.',
        id(calibration):'Predictions are grouped in 2-percentage-point bins. The diagonal means forecast probability matches observed frequency; below it means the event happened less often than predicted. Hover for each bin’s sample count. '+
            ('This checks home-team win probabilities, even when the error charts show total points. It does not check every alternate spread or total line.' if game else
             'This checks Over the fixed reference line used in this run; pushes are excluded. It does not check every alternate prop line.')}
    table=[]
    for r in records:
        table.append(dict(date=format_eastern(r['date']),game=r['label'],actual=round(r['actual'],3),
            predicted=round(r['predicted'],3),error=round(r['predicted']-r['actual'],3),
            lower=round(r['lower'],3) if r.get('lower') is not None else None,
            upper=round(r['upper'],3) if r.get('upper') is not None else None,
            probability=round(100*r['probability'],2) if r.get('probability') is not None else None,
            training_through=r.get('latest_training')))
    return html.Div([
        html.Details([html.Summary('Parameters used for this simulation'),
            html.Pre(json.dumps(result.get('config',{}),indent=2),className='research-used-parameters')],className='panel research-applied-parameters'),
        html.P(('Research experiment: ' if context.get('workspace')=='research' else 'Saved run: ')+f'{format_eastern(result["created_at"])} · {context["sport"]} · '
            f'{context.get("entity_label",context["entity"])} · {records[0]["date"][:10]} to {records[-1]["date"][:10]} · '
            f'Last {context["limit"]} eligible games · '
            + ('Playoff setting: not recorded (older run)' if 'include_playoffs' not in context else
               'Regular season + postseason' if context['include_playoffs'] else 'Regular season only')
            + f' · Estimation history: {str(context["lookback"])+" prior games maximum per team/player" if context.get("lookback") is not None else "model defaults"}. '
            + 'Run again to apply changed filters.',className='backtest-applied'),
        html.Div(cards,className='backtest-kpis'),
        html.P('Event metrics evaluate the home winner.' if game else
            ('Event metrics evaluate Over '+str(context['line'])+'.' if context['line'] is not None else 'Event metrics require an optional reference line.'),
            className='backtest-status'),
        html.P('Probabilities are evaluated against outcomes, without assumed historical sportsbook prices. '
               'Availability lags and revised source files limit historical as-of fidelity. '
               'Accuracy intervals are descriptive; repeated players and teams can be correlated.',className='backtest-status'),
        *([html.P('This saved player run contains 90% intervals. Run backtest again to calculate the new 80% intervals.',
            className='backtest-status')] if not game and records[0]['interval_level']!=PREDICTIVE_INTERVAL else []),
        html.Div([
            html.Div([dcc.Graph(figure=timeline,config={'displaylogo':False}),
                html.Aside([html.Strong(percent('interval_coverage')),html.Span('Inside '+interval_label+' interval'),
                    html.Small(coverage['range_samples'].replace(' / ',' of ')+' actuals with valid ranges')],className='backtest-coverage')],
                className='panel backtest-chart backtest-timeline',role='group',**{'aria-label':'Predicted versus actual over time and interval coverage'}),
            *[html.Div([dcc.Graph(figure=fig,config={'displaylogo':False}),
                *([html.P(chart_notes[id(fig)],className='chart-note backtest-chart-note')] if id(fig) in chart_notes else [])],
                className='panel backtest-chart',role='group',**{'aria-label':fig.layout.title.text})
                for fig in [parity,residual,rolling,calibration,accuracy]],
        ],className='backtest-chart-grid'),
        html.Section([html.Div(html.H3('Evaluated predictions'),className='panel-heading'),
            dash_table.DataTable(data=table,columns=[dict(name=label,id=key) for key,label in [
                ('date','Game time (EDT)'),('game','Matchup / player'),('actual','Actual'),('predicted','Predicted'),
                ('error','Error'),('lower',interval_label+' interval low'),('upper',interval_label+' interval high'),('probability','Event probability (%)'),
                ('training_through','Latest training game')]],page_size=12,sort_action='native',filter_action='native',
                style_table={'overflowX':'auto'},style_cell=table_cell(),
                style_header=table_header())],className='panel backtest-table'),
        html.Details([html.Summary(f'Skipped games and reasons ({len(result["skipped"])})'),
            html.Ul([html.Li(f'{r["date"][:10]} · {r["game_id"]}: {r["reason"]}') for r in result['skipped']])],
            className='panel backtest-skipped'),
    ])
