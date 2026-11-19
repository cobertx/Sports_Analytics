"""Sport performance summaries reuse compatible reports and queue only missing runs."""
import math
import json
import threading
from hashlib import sha256
from itertools import accumulate
from pathlib import Path

from .navigation import scoped_callback

from dash import Input,Output,State,ctx,dcc,html,no_update
from dash.exceptions import PreventUpdate
import numpy as np
import plotly.graph_objects as go

from .backtesting_view import SPORTS,GROUPS,PERFORMANCE_ROUTE,available_models,model_config,model_name,summary_href
from .backtesting_service import evaluation_bounds,current_player_cohort
from .backtesting_results import field,figure,range_accuracy,valid_prediction_interval
from .comparison import Comparisons,compare_reports
from .data import DataSourceError
from .display_time import eastern_datetime,format_eastern
from models.Player_Stats.Weighted_Historical_Performance.markets import NBA,FOOTBALL
from models.Player_Stats.Usage_Opportunity.markets import market_spec

GAME_MARKETS=[dict(label='Scoring margin / Spread',value='margin'),dict(label='Total points',value='total'),
              dict(label='Game winner',value='winner')]


def settings(context,market,team,limit,start=None,end=None,playoffs='exclude',line=None):
    if context.get('sport') not in SPORTS or context.get('group') not in GROUPS:raise ValueError('Choose a sport and market group.')
    player=context['group']=='player-stats'
    if market not in ((NBA if context['sport']=='NBA' else FOOTBALL) if player else {'margin','total','winner'}):
        raise ValueError('Choose a valid market.')
    if not isinstance(team,str) or not team:raise ValueError('Choose one team or all teams.')
    if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or int(limit)!=limit or not 1<=limit<=10000:
        raise ValueError('Choose 1–10,000 whole evaluation games.')
    if playoffs not in {'exclude','include'}:raise ValueError('Choose a postseason setting.')
    if line is not None and (isinstance(line,bool) or not isinstance(line,(int,float)) or not math.isfinite(line)):
        raise ValueError('Reference line must be finite or blank.')
    return dict(entity='__all__' if player else team,team_filter=team if player and team!='__all__' else None,
                market=market if player else None,limit=int(limit),start=start or None,end=end or None,
                include_playoffs=playoffs=='include',line=line if player else None,lookback=None)


class PerformanceSummaries(Comparisons):
    def __init__(self,backtesting,*,watch=True):
        super().__init__(backtesting,watch=watch)
        self.root=Path(backtesting.root)/'performance'

    def latest(self,context,market,team):
        """Read the newest nonempty report per method without starting jobs."""
        scope=settings(context,market,team,100)
        histories={model.key:[saved for saved in self.backtesting.history(context['sport'],context['group'],model.key)
                   if all(saved.get(k)==scope[k] for k in ('entity','team_filter','market'))]
                   for model in available_models(context['sport'],context['group'])}
        def newest(key,line=None,match_line=False):
            for saved in histories[key]:
                if match_line and saved.get('line')!=line:continue
                report=self.backtesting.saved(context['sport'],context['group'],key,saved['run_id'])
                if (report and report['records'] and current_player_cohort(report)
                        and all(report['context'].get(k)==scope[k] for k in ('entity','team_filter','market'))
                        and (not match_line or report['context'].get('line')==line)):
                    return report
        reports={key:report for key in histories if (report:=newest(key))}
        if not reports:return None
        anchor=max(reports.values(),key=lambda r:(r['created_at'],r['run_id']))
        # Event scores are comparable only at one shared player reference line.
        if context['group']=='player-stats':
            line=anchor['context'].get('line')
            reports={key:report for key in histories if (report:=newest(key,line,True))}
        request={key:anchor['context'].get(key,default) for key,default in scope.items()}
        entries={key:(dict(state='completed',run_id=reports[key]['run_id'],reused=True) if key in reports else
                      dict(state='not saved',message='No saved result for this selection'+
                           (' and shared reference line.' if context['group']=='player-stats' else '.')))
                 for key in histories}
        identity=sha256(json.dumps([context,entries],sort_keys=True).encode()).hexdigest()[:32]
        return dict(id=identity,context=dict(context),entries=entries,settings=request,
                    source='latest-saved',created_at=anchor['created_at'])

    def matching(self,context,model,request):
        defaults=model_config(model,context['sport'])
        for saved in self.backtesting.history(context['sport'],context['group'],model.key):
            if any(saved.get(key)!=value for key,value in request.items()):continue
            report=self.backtesting.saved(context['sport'],context['group'],model.key,saved['run_id'])
            if not report or not report['records'] or not current_player_cohort(report):continue
            if not self.full_team_request(report):continue
            original=report['context']
            if any(original.get(key)!=value for key,value in request.items()):continue
            if report.get('config')!=defaults:continue
            return report
        return None

    @staticmethod
    def full_team_request(report):
        context=report['context']
        if context['sport']!='NBA' or context['group']!='game-outcome' or context['entity']=='__all__':return True
        attempted=report.get('attempted',len(report['records'])+len(report.get('skipped',[])))
        return attempted>=context.get('limit',0)

    def ensure(self,context,request,team):
        evaluation_bounds(request['start'],request['end'],self.backtesting.now())
        with self.lock:
            models=available_models(context['sport'],context['group'])
            # Repeated clicks and revisits join the same in-flight simulations.
            for previous in self.history(context):
                if previous.get('settings')==request:
                    previous=self.refresh(context,previous['id'])
                    if any(e['state'] in {'queued','running'} for e in previous['entries'].values()):return previous
                    reports=self.reports(previous)
                    if (set(previous['entries'])=={m.key for m in models}
                            and all(e['state'] in {'completed','unsupported'} for e in previous['entries'].values())
                            and all(m.key in reports and reports[m.key]['records'] and current_player_cohort(reports[m.key]) and self.full_team_request(reports[m.key]) and
                                    reports[m.key].get('config')==model_config(m,context['sport'])
                                    for m in models if previous['entries'][m.key]['state']=='completed')):
                        return previous
            entries={}
            for model in models:
                if context['group']=='player-stats' and model.key!='whp' and market_spec(context['sport'],request['market']) is None:
                    entries[model.key]=dict(state='unsupported',message='This method does not support the selected statistic.')
                    continue
                report=self.matching(context,model,request)
                entries[model.key]=(dict(state='completed',run_id=report['run_id'],reused=True) if report else dict(state='queued'))
            manifest=self.new(context,entries,request,'performance')
            for key,entry in manifest['entries'].items():
                if entry['state']!='queued':continue
                try:
                    job=self.backtesting.start(sport=context['sport'],group=context['group'],key=key,tolerance=5,
                        display_label='Performance Summary · '+('All teams' if team=='__all__' else team),**request)
                    entry.update(state='running',job_id=job)
                except (ValueError,OSError,RuntimeError) as exc:entry.update(state='failed',message=str(exc))
                self.save(manifest)
            if self.watch and any(e['state']=='running' for e in entries.values()):
                threading.Thread(target=self.follow,args=(dict(context),manifest['id']),daemon=True,
                                 name='performance-'+manifest['id'][:8]).start()
            return manifest


def layout():
    return html.Section([
        dcc.Store(id='performance-context'),dcc.Store(id='performance-state'),dcc.Store(id='performance-rendered'),
        dcc.Store(id='performance-pages',data={},storage_type='session'),
        dcc.Interval(id='performance-poll',interval=1500,disabled=True),
        html.Section([
            html.Div('BACKTESTING',className='eyebrow accent'),html.H2('Performance Summary'),
            html.P('The latest saved backtests for the selected category, market and team load automatically. '
                   'Use the simulation controls and Load performance / Run missing simulations to evaluate a different scope.'),
            html.Div([
                field('Market group',dcc.Dropdown(id='performance-group',options=[dict(label=v,value=k) for k,v in GROUPS.items()],value='game-outcome',clearable=False)),
                field('Market type',dcc.Dropdown(id='performance-market',clearable=False)),
                field('Evaluation team',dcc.Dropdown(id='performance-team',options=[dict(label='All teams',value='__all__')],value='__all__',clearable=False)),
                field('Evaluation games',dcc.Input(id='performance-limit',type='number',min=1,max=10000,step=1,value=100),
                      'Latest eligible games; player markets include appearances from those games.'),
                field('Evaluation start (EDT)',dcc.DatePickerSingle(id='performance-start',display_format='MM/DD/YYYY',
                    placeholder='',clearable=True)),
                field('Evaluation end (EDT)',dcc.DatePickerSingle(id='performance-end',display_format='MM/DD/YYYY',
                    placeholder='',clearable=True)),
                field('Postseason',dcc.Dropdown(id='performance-playoffs',options=[dict(label='Regular season only',value='exclude'),dict(label='Include postseason',value='include')],value='exclude',clearable=False)),
                html.Div(field('Player reference line (optional)',dcc.Input(id='performance-line',type='number',step='any'),
                    'Required for Over accuracy, Brier score and log loss.'),id='performance-line-field',style={'display':'none'}),
                field('Error tolerance',dcc.Input(id='performance-tolerance',type='number',min=0,step='any',value=5)),
                field('Comparison sample',dcc.Dropdown(id='performance-basis',options=[dict(label='All evaluated observations per model',value='all'),
                    dict(label='Common evaluated observations',value='common')],value='all',clearable=False),
                    'The default matches each saved backtest. Common observations compares only games evaluated by every completed model.'),
            ],className='backtest-controls'),
            html.P(id='performance-input-status',className='backtest-status'),
            html.Button('Load performance / Run missing simulations',id='performance-load',n_clicks=0,className='apply-criteria-button'),
        ],className='panel comparison-setup'),
        html.P(id='performance-status',className='backtest-status',role='status',**{'aria-live':'polite'}),
        html.Div(id='performance-results'),
    ],id='performance-summary-page',className='performance-summary-page',style={'display':'none'})


def rolling_coverage(rows,intervals,window):
    """Summarize the full sample before reducing the number of displayed points."""
    samples=list(accumulate((bounds is not None for bounds in intervals),initial=0))
    hits=list(accumulate((bounds is not None and bounds[0]<=row['actual']<=bounds[1]
                          for row,bounds in zip(rows,intervals)),initial=0))
    step=max(1,math.ceil(len(rows)/150))
    endpoints={eastern_datetime(rows[end-1]['date']).date().isoformat():end
               for end in [*range(step,len(rows),step),len(rows)]}
    for day,end in endpoints.items():
        start=max(0,end-window)
        count=samples[end]-samples[start]
        covered=hits[end]-hits[start]
        yield day,covered/count if count else None,[covered,count,end-start]


def prediction_groups(rows):
    predicted=np.asarray([row['predicted'] for row in rows])
    actual=np.asarray([row['actual'] for row in rows])
    # Quantile boundaries keep dense regions readable without splitting tied forecasts.
    edges=np.unique(np.quantile(predicted,np.linspace(0,1,min(10,len(rows))+1)))
    bins=np.searchsorted(edges[1:-1],predicted,side='right')
    for index in np.unique(bins):
        mask=bins==index
        yield float(predicted[mask].mean()),float(actual[mask].mean()),int(mask.sum())


def comparison_charts(data,metrics,basis):
    summary=list(metrics.values());records=data.get('records',{})
    if not any(row.get('compared') for row in summary):
        return html.P('No shared evaluated observations to plot. Choose All evaluated observations per model to inspect each saved sample.',className='backtest-status')
    labels=[row['method'] for row in summary]
    rates=[row['range_accuracy'] for row in summary]
    hover=[[row['label'],row['range_samples'],row['compared']] for row in summary]
    accuracy=figure('Coverage of 80% prediction intervals','Model','Actual outcomes inside interval')
    accuracy.add_trace(go.Bar(x=labels,y=rates,
        marker_color=['#57dbc4' if rate is not None and rate>=.8 else '#ffa3af' for rate in rates],
        texttemplate='%{y:.1%}',textposition='outside',customdata=hover,
        hovertemplate='%{customdata[0]}<br>Coverage: %{y:.1%}<br>Hits / valid intervals: %{customdata[1]}<br>Compared: %{customdata[2]}<extra></extra>'))
    accuracy.update_layout(bargap=.42)
    accuracy.update_yaxes(range=[0,1.12],tickformat='.0%')

    window=max(100,math.ceil(max((len(rows) for rows in records.values()),default=0)/100))
    trend=figure('Rolling 80% interval coverage','Evaluation date (EDT)',f'Trailing {window:,} observations')
    parity=figure('Predicted vs. actual','Mean prediction','Mean actual')
    widths=figure('80% prediction interval widths','Model','Width in statistic units')
    colors=('#57dbc4','#8197ff','#f6c667','#ef96d0')
    dashes=('solid','dash','dot','dashdot')
    trend_values=[]
    parity_values=[]
    for index,(key,rows) in enumerate(records.items()):
        if not rows:continue
        color=colors[index%len(colors)]
        intervals=[valid_prediction_interval(row) for row in rows]
        days,coverage,counts=zip(*rolling_coverage(rows,intervals,window))
        valid_coverage=[value for value in coverage if value is not None]
        trend_values.extend(valid_coverage)
        if valid_coverage:
            trend.add_trace(go.Scatter(x=days,y=coverage,name=key.upper(),
                mode='lines' if len(valid_coverage)>1 else 'markers',connectgaps=False,
                marker=dict(color=color,size=7),line=dict(color=color,width=2,dash=dashes[index%len(dashes)]),
                customdata=counts,hovertemplate='%{x|%b %d, %Y}<br>Coverage: %{y:.1%}<br>Hits / valid intervals: '
                '%{customdata[0]} / %{customdata[1]}<br>Observations in window: %{customdata[2]}<extra>%{fullData.name}</extra>'))

        predictions,actuals,counts=zip(*prediction_groups(rows))
        parity_values.extend([*predictions,*actuals])
        parity.add_trace(go.Scatter(x=predictions,y=actuals,name=key.upper(),mode='lines+markers',
            line=dict(color=color,dash=dashes[index%len(dashes)]),marker=dict(size=7),customdata=counts,
            hovertemplate='Mean prediction: %{x:.2f}<br>Mean actual: %{y:.2f}<br>Observations: %{customdata}<extra>%{fullData.name}</extra>'))

        span=[bounds[1]-bounds[0] for bounds in intervals if bounds is not None]
        if span:
            p05,q1,median,q3,p95=np.percentile(span,[5,25,50,75,95]).tolist()
            widths.add_trace(go.Box(x=[key.upper()],name=key.upper(),q1=[q1],median=[median],q3=[q3],
                lowerfence=[p05],upperfence=[p95],boxpoints=False,marker_color=color,showlegend=False))

    for chart in (accuracy,trend):
        chart.add_hline(y=.8,line_dash='dash',line_color='#f6c667',annotation_text='80% target')
    for chart,empty in ((accuracy,all(rate is None for rate in rates)),(trend,not trend_values),(widths,not widths.data)):
        if empty:
            chart.add_annotation(text='No valid 80% intervals in these saved results.',showarrow=False,x=.5,y=.5,xref='paper',yref='paper')
    trend.update_xaxes(type='date',nticks=7)
    trend.update_yaxes(range=[max(0,min([.7,*trend_values])-.04),min(1.04,max([.9,*trend_values])+.04)],tickformat='.0%')
    if parity_values:
        low,high=min(parity_values),max(parity_values)
        pad=max(.5,(high-low)*.06)
        parity.add_shape(type='line',x0=low-pad,x1=high+pad,y0=low-pad,y1=high+pad,
                         line=dict(color='#a8b8cc',dash='dash',width=1),layer='below')
        parity.update_xaxes(range=[low-pad,high+pad])
        parity.update_yaxes(range=[low-pad,high+pad])
    widths.update_yaxes(rangemode='tozero')
    charts=[
        (accuracy,'Teal bars meet or exceed 80% coverage. Coral bars fall below it. Wider prediction intervals can increase coverage.',True),
        (trend,f'Coverage over up to {window:,} trailing evaluated observations. Hover for counts of hits and valid intervals. '+
         ('Models use the same shared observations.' if basis=='common' else 'Each model uses its own evaluated observations.'),True),
        (parity,'Points group similar predictions. Above the diagonal, the model tended to underpredict. Below it, the model tended to overpredict.',False),
        (widths,'Boxes show the middle 50% of interval widths. Whiskers span the 5th to 95th percentiles. Read narrower intervals alongside coverage.',False),
    ]
    return html.Div([html.Div([dcc.Graph(figure=chart,config={'displaylogo':False}),
                              html.P(note,className='backtest-status backtest-chart-note')],
                              className='panel backtest-chart'+(' performance-chart-wide' if wide else ''))
                     for chart,note,wide in charts],className='performance-chart-grid')


def render(manager,manifest,market,tolerance,basis='all'):
    context=manifest['context'];reports=manager.reports(manifest)
    target='total' if market=='total' else 'margin'
    if basis not in {'common','all'}:raise ValueError('Choose a comparison sample.')
    data=compare_reports(context,reports,target,basis,tolerance) if reports else dict(summary=[],common=0,conflicting_actuals=0)
    metrics={r['method'].lower():r for r in data['summary']}
    for key,metric in metrics.items():metric.update(range_accuracy(data['records'][key]))
    probability=context['group']=='player-stats' or market!='total'
    columns=[('range_accuracy','Accuracy (80% range)'),('range_samples','80% range hits / samples'),
             ('compared','Compared'),('evaluated','Evaluated'),('skipped','Skipped'),('mae','MAE'),('rmse','RMSE'),('bias','Bias'),
             ('within_tolerance','Within tolerance'),('baseline_mae','Baseline MAE')]
    if probability:columns += [('accuracy','Over accuracy' if context['group']=='player-stats' else 'Winner accuracy'),
                              ('probability_samples','Probability samples'),('brier','Brier score'),('log_loss','Log loss')]
    rows=[]
    for model in available_models(context['sport'],context['group']):
        entry=manifest['entries'].get(model.key,{})
        metric=metrics.get(model.key,{})
        report=reports.get(model.key)
        stamp=report.get('created_at') if report else None
        status=('Saved result' if entry.get('reused') else 'Completed') if entry.get('state')=='completed' else entry.get('state','').capitalize()
        if report and not report['records']:status='No evaluated observations'
        cells=[]
        for key,_ in columns:
            value=metric.get(key)
            if value is not None:value=f'{value:.1%}' if key in {'range_accuracy','accuracy','within_tolerance'} else f'{value:.3f}' if isinstance(value,float) else str(value)
            cells.append(html.Td(value if value is not None else '—'))
        reason=entry.get('message','')
        if report and not report['records']:reason='; '.join(dict.fromkeys(r.get('reason','') for r in report.get('skipped',[])))[:500]
        saved_scope=[]
        if report:
            original=report['context']
            saved_scope=[html.Small(f"{original.get('limit',0)} requested games · {original.get('start') or 'Any start'} to {original.get('end') or 'Any end'} · "+
                ('Including postseason' if original.get('include_playoffs') else 'Regular season')+
                f" · Lookback: {original.get('lookback') or 'Model Default'}",className='backtest-status')]
        rows.append(html.Tr([html.Th(model_name(model,context['sport']),scope='row'),
            html.Td([html.Span(status),html.Small(reason,className='backtest-status') if reason else None]),*cells,
            html.Td([html.A('Open results',href=summary_href(context,model.key,report['run_id'],target,basis,reports,tolerance)),
                     html.Small(format_eastern(stamp),className='backtest-status'),*saved_scope] if stamp else '—')]))
    request=manifest['settings'];team=request.get('team_filter') or request['entity']
    note=(f"{context['sport']} · {market.replace('player_','').replace('_',' ').title()} · {'All teams' if team=='__all__' else team} · "+
          ("Latest saved result per model; original evaluation settings are shown in each row." if manifest.get('source')=='latest-saved' else
           f"last {request['limit']} eligible games · {request['start'] or 'Any start'} to {request['end'] or 'Any end'} · "
           f"{'Including postseason' if request['include_playoffs'] else 'Regular season'}."))
    if context['group']=='player-stats':note+=f" Shared reference line: {request.get('line') if request.get('line') is not None else 'None'}."
    return html.Section([
        html.H3('Model accuracy'),html.P(note,className='backtest-applied'),
        html.P((f"{data['common']:,} identical evaluated games / player appearances shared by the completed models. "
                'Metrics update as methods finish; the shared sample can change. Open results preserves this exact comparison cohort.' if basis=='common' else
                'Each model uses its own evaluated sample, matching its saved backtest results. Compare the sample counts; differences in coverage can affect accuracy.'),className='backtest-status'),
        html.P('Accuracy (80% range) is the percentage of actual outcomes inside the model’s 80% prediction range, including both endpoints. '
               'Hits / samples counts covered outcomes and observations with a valid 80% range in the selected comparison sample; player statistics count player-game appearances. '
               'Missing or invalid ranges are excluded. Older saved player runs with only 90% ranges must be rerun to show this accuracy. '
               'For Game winner and Spread, the range measures scoring margin; Total points uses the total. '
               'A well-calibrated 80% range should cover about 80% of outcomes; wider ranges can raise coverage without improving point forecasts.',className='backtest-status'),
        html.P('Lower MAE, RMSE, Brier score and log loss are better. Bias is predicted minus actual. '
               f'Within tolerance means an error of at most ±{tolerance:g} statistic units. '
               'Spread accuracy uses scoring-margin errors. '+('Event scores measure the home winner.' if probability and context['group']=='game-outcome' else
               'Event scores measure Over the shared reference line; blank lines have no event scores.' if probability else
               'Total-point metrics do not use the saved home-winner probabilities.'),className='backtest-status'),
        html.P(f"{data['conflicting_actuals']} conflicting actual outcomes excluded from the common sample.",className='backtest-status') if data['conflicting_actuals'] else None,
        html.Div(html.Table([html.Thead(html.Tr([html.Th(label,scope='col') for label in
            ['Model','Status',*[label for _,label in columns],'Saved simulation']])),html.Tbody(rows)],className='mispricing-table'),className='mispricing-table-scroll'),
        comparison_charts(data,metrics,basis),
    ],className='panel comparison-summary')


def register(app,backtesting):
    manager=PerformanceSummaries(backtesting)

    @scoped_callback(app,'backtesting',Output('performance-summary-page','style'),Output('performance-context','data'),Output('performance-line-field','style'),
                  Input('workspace-page','value'),Input('sport','value'),Input('market-url','hash'),Input('performance-group','value'))
    def performance_context(page,sport,location_hash,group):
        active=page=='backtesting' and sport in SPORTS and location_hash==PERFORMANCE_ROUTE and group in GROUPS
        return ({} if active else {'display':'none'},dict(sport=sport,group=group) if active else None,
                {} if group=='player-stats' else {'display':'none'})

    @app.callback(Output('performance-market','options'),Output('performance-market','value'),Output('performance-team','options'),
                  Output('performance-team','value'),Output('performance-input-status','children'),Input('performance-context','data'),
                  State('performance-market','value'),State('performance-team','value'))
    def performance_options(context,market,team):
        if not context:raise PreventUpdate
        player=context['group']=='player-stats';teams={};errors=[]
        for model in available_models(context['sport'],context['group']):
            for saved in backtesting.history(context['sport'],context['group'],model.key):
                name=saved.get('team_filter') if player else saved.get('entity')
                if name and name!='__all__':teams[name]=dict(label=name,value=name)
            if player and model.key!='whp':continue
            try:
                entities,_,frame=backtesting.options(context['sport'],context['group'],model.key,'__all__')
                for name in (frame.team.dropna().astype(str).unique() if player else [o['value'] for o in entities if o['value']!='__all__']):
                    teams[name]=dict(label=name,value=name)
            except (DataSourceError,OSError,ValueError,TypeError) as exc:errors.append(model.key.upper()+': '+str(exc))
        markets=([dict(label=m.removeprefix('player_').replace('_',' ').title(),value=m) for m in (NBA if context['sport']=='NBA' else FOOTBALL)] if player else GAME_MARKETS)
        options=[dict(label='All teams',value='__all__'),*[teams[k] for k in sorted(teams)]]
        return markets,market if market in {m['value'] for m in markets} else markets[0]['value'],options,team if team in teams else '__all__',(
            'Sport defaults and Model Default lookback. '+('NBA game runs download missing older history automatically. '
                if context['sport']=='NBA' and context['group']=='game-outcome' else 'Saved inputs only. ')+(' · '.join(errors) if errors else
            'NFL player markets evaluate regular contributors using strictly pregame workload from the prior 3 team games.'
            if player and context['sport']=='NFL' else 'Player markets evaluate all eligible players on the selected team(s).'))

    @app.callback(Output('performance-state','data'),Output('performance-status','children'),Output('performance-pages','data'),
                  Output('performance-limit','value'),Output('performance-start','date'),Output('performance-end','date'),
                  Output('performance-playoffs','value'),Output('performance-line','value'),
                  Input('performance-load','n_clicks'),Input('performance-context','data'),Input('performance-poll','n_intervals'),
                  Input('performance-market','value'),Input('performance-team','value'),State('performance-limit','value'),
                  State('performance-start','date'),State('performance-end','date'),State('performance-playoffs','value'),State('performance-line','value'),
                  State('performance-state','data'),State('performance-pages','data'),prevent_initial_call=True)
    def performance_load(clicks,context,tick,market,team,limit,start,end,playoffs,line,current,pages):
        unchanged=(no_update,)*5
        if not context:return no_update,no_update,no_update,*unchanged
        pages=dict(pages or {});page=context['sport']+'/'+context['group']
        try:
            restore=False
            if ctx.triggered_id=='performance-load':
                if not clicks:raise PreventUpdate
                request=settings(context,market,team,limit,start,end,playoffs,line)
                manifest=manager.ensure(context,request,team)
            elif ctx.triggered_id=='performance-poll':
                identity=pages.get(page)
                if not identity:raise PreventUpdate
                manifest=manager.refresh(context,identity)
                if not manifest:return None,'Saved performance summary is unavailable. Load performance to rebuild it.',no_update,*unchanged
            else:
                scope=settings(context,market,team,100)
                previous=manager.refresh(context,pages[page]) if pages.get(page) else None
                running=previous and any(e['state'] in {'queued','running'} for e in previous['entries'].values())
                manifest=(previous if running and all(previous['settings'].get(k)==scope[k] for k in ('entity','team_filter','market'))
                          else manager.latest(context,market,team))
                if not manifest:
                    return None,'No saved backtests for this category, market and team. Use Load performance / Run missing simulations to create them.',no_update,*unchanged
                restore=True
            if manifest.get('source')!='latest-saved':pages[page]=manifest['id']
            running=sum(e['state'] in {'queued','running'} for e in manifest['entries'].values())
            complete=sum(e['state']=='completed' for e in manifest['entries'].values())
            status=(f'Latest saved backtests · {complete} models · Newest completed {format_eastern(manifest["created_at"])}. '
                    'Original parameters are retained; models without saved results are listed below.' if manifest.get('source')=='latest-saved' else
                    f'{complete} models completed · {running} running. '+('See the progress panel; jobs continue while you visit another page.' if running else 'Results are saved locally. Failed or unsupported methods are listed below.'))
            if ctx.triggered_id=='performance-poll' and current==manifest:raise PreventUpdate
            request=manifest['settings']
            controls=(request['limit'],request['start'],request['end'],'include' if request['include_playoffs'] else 'exclude',request['line']) if restore else unchanged
            return manifest,status,pages,*controls
        except (ValueError,TypeError,OSError,DataSourceError) as exc:return no_update,str(exc),no_update,*unchanged

    app.clientside_callback('''function(context,state){return !context||!state||state.context.sport!==context.sport||
        state.context.group!==context.group||!Object.values(state.entries).some(e=>e.state==='running'||e.state==='queued');}''',
        Output('performance-poll','disabled'),Input('performance-context','data'),Input('performance-state','data'))

    @scoped_callback(app,'backtesting',Output('performance-results','children'),Output('performance-rendered','data'),Input('performance-state','data'),
                  Input('performance-context','data'),Input('performance-market','value'),Input('performance-team','value'),Input('performance-limit','value'),
                  Input('performance-start','date'),Input('performance-end','date'),Input('performance-playoffs','value'),Input('performance-line','value'),
                  Input('performance-tolerance','value'),State('performance-rendered','data'),Input('performance-basis','value'))
    def performance_results(manifest,context,market,team,limit,start,end,playoffs,line,tolerance,previous,basis='all'):
        if not context:raise PreventUpdate
        try:
            request=settings(context,market,team,limit,start,end,playoffs,line)
            if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<0:raise ValueError('Choose a finite, nonnegative error tolerance.')
            # Latest history can retain a custom lookback from an individual
            # strategy page, while this page's new-run controls use defaults.
            applied={**manifest['settings'],'lookback':None} if manifest and manifest.get('source')=='latest-saved' else manifest['settings'] if manifest else None
            if not manifest or manifest['context']!=context or applied!=request:
                return html.P('Load performance to see results for these filters.',className='backtest-status'),None
            signature=[manifest['id'],manifest['entries'],market,tolerance,basis]
            if signature==previous:raise PreventUpdate
            return render(manager,manifest,market,tolerance,basis),signature
        except (ValueError,TypeError,OSError,DataSourceError) as exc:return html.P(str(exc),className='backtest-status'),None
