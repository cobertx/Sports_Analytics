"""Sidebar overview and controls for running and paused backtests."""
from dash import dcc,html
from app.sections.backtesting.backtesting_view import find_model,model_name
from sports_analytics.backtesting_service import default_evaluation_games


def layout():
    return html.Aside([
        dcc.Store(id='backtest-monitor-jobs',data=[]),
        dcc.Interval(id='backtest-monitor-timer',interval=1000),
        html.Details([
            html.Summary([
                html.Span('Backtests',className='backtest-monitor-heading'),
                html.Span('0',id='backtest-monitor-count',className='backtest-monitor-count'),
                html.Span('⌄',className='backtest-monitor-chevron',**{'aria-hidden':'true'}),
            ],id='backtest-monitor-toggle',className='backtest-monitor-toggle',disable_n_clicks=True,
              **{'aria-controls':'backtest-monitor-body'}),
            html.Div(html.P('No backtests running.',className='backtest-monitor-empty'),
                     id='backtest-monitor-body',className='backtest-monitor-body'),
        ],id='backtest-monitor-disclosure',open=True,disable_n_clicks=True),
    ],id='backtest-monitor',className='backtest-monitor',disable_n_clicks=True,
      **{'aria-label':'Ongoing backtesting processes'})


def contents(jobs):
    if not jobs:return html.P('No backtests running.',className='backtest-monitor-empty')
    cards=[]
    for job in jobs:
        params=job['parameters'];sport=params['sport'];key=params['key']
        scope=job['display_label'];model=find_model(sport,(params['group'],key))
        label=model_name(model,sport) if model else key.upper()
        games=params.get('limit') or default_evaluation_games(sport,params.get('entity','__all__'))
        target=(params.get('market') or '').removeprefix('player_').replace('_',' ').title()
        percent=int(job['percent'])
        state=job['state']
        action='resume' if state=='paused' else 'pause'
        control='Pausing…' if state=='pausing' else action.capitalize()
        cards.append(html.Li([
            html.Div([html.Strong(f'{sport} · {key.upper()}'+(' · Research' if params.get('workspace')=='research' else ''),title=label),html.Span(f'{percent}%',className='backtest-job-percent')],
                className='backtest-job-heading'),
            html.Div(scope,className='backtest-job-scope',title=scope),
            html.Small(f'{games:,} games'+(' · '+target if target else ''),className='backtest-job-detail'),
            html.Progress(value=percent,max=100,**{'aria-label':f'{sport} {label}, {scope} progress'}),
            html.Small(job['message'],className='backtest-job-message'),
            html.Button(control,id=dict(type='backtest-control',job=job['id'],action=action),
                type='button',n_clicks=0,disabled=state=='pausing',className='export-button backtest-job-control',
                **{'aria-label':f'{control} {sport} {label}, {scope}'}),
        ],className='backtest-job',**{'data-job-id':job['id']}))
    return html.Ul(cards,className='backtest-job-list',**{'aria-label':'Active runs'})
