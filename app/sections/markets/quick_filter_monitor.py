"""Dashboard-wide progress for Quick Filtering submissions in this browser."""
from secrets import token_bytes, token_hex

from dash import Input, Output, State, dcc, html, no_update
from flask import has_request_context, request, session


def owner():
    return session.get('quick_filter_owner') if has_request_context() else None


def empty():
    return html.P('No Quick Filtering runs yet.', className='backtest-monitor-empty')


def layout():
    return html.Aside([
        dcc.Store(id='quick-monitor-state'),
        dcc.Store(id='quick-monitor-completions'),
        dcc.Interval(id='quick-monitor-timer', interval=1000),
        html.Details([
            html.Summary([
                html.Span('Quick Filtering', className='backtest-monitor-heading'),
                html.Span('0', id='quick-monitor-count', className='backtest-monitor-count'),
                html.Span('⌄', className='backtest-monitor-chevron', **{'aria-hidden':'true'}),
            ], className='backtest-monitor-toggle', disable_n_clicks=True,
               **{'aria-controls':'quick-monitor-body'}),
            html.Div(empty(), id='quick-monitor-body', className='backtest-monitor-body'),
        ], id='quick-monitor-disclosure', open=True, disable_n_clicks=True),
    ], id='quick-monitor', className='backtest-monitor',
       **{'aria-label':'Quick Filtering job progress'})


def contents(jobs):
    if not jobs:
        return empty()
    cards = []
    for job in jobs:
        criteria = job['criteria']
        percent = job['percent']
        label = {'waiting':'Queued', 'running':f'{percent}%', 'complete':'Complete', 'failed':'Failed'}[job['status']]
        matchup = job['matchup']
        if job['provider'] in {'FanDuel','Kalshi'}:
            matchup = html.A(matchup, href=job['href'])
        cards.append(html.Li([
            html.Div([html.Strong(f"{job['sport']} · {job['provider']}"),
                      html.Span(label, className='backtest-job-percent')], className='backtest-job-heading'),
            html.Div(matchup, className='backtest-job-scope'),
            html.Small(f"Probability > {100*criteria['probability']:g}% · Edge > {100*criteria['edge']:g} pp",
                       className='backtest-job-detail'),
            html.Small(', '.join(job['methods']), className='backtest-job-detail'),
            html.Progress(value=percent, max=100, **{'aria-label':f"{job['matchup']} Quick Filtering progress"}),
            html.Small(job['message'], className='backtest-job-message'),
        ], className='backtest-job', **{'data-job-id':job['id']}))
    return html.Ul(cards, className='backtest-job-list')


def register(app, tasks):
    if not app.server.secret_key:
        app.server.secret_key = token_bytes(32)

    @app.server.before_request
    def identify_browser():
        if request.path.endswith(('/_dash-layout', '/_dash-update-component')) and not owner():
            session['quick_filter_owner'] = token_hex(16)

    @app.callback(Output('quick-monitor-body','children'), Output('quick-monitor-count','children'),
                  Output('quick-monitor-state','data'), Output('quick-monitor-completions','data'),
                  Input('quick-monitor-timer','n_intervals'),
                  State('quick-monitor-state','data'))
    def poll_quick_monitor(_tick, previous):
        jobs, active, completed = tasks.monitored(owner())
        state = dict(jobs=jobs, completed=completed)
        if state == previous:
            return no_update, no_update, no_update, no_update
        return contents(jobs), str(active), state, (
            no_update if previous and previous.get('completed') == completed else completed)
