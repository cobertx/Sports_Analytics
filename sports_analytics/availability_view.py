"""Reviewable evidence for the optional availability overlay."""
from dash import html


def panel(forecast):
    if forecast is None:return None
    info = getattr(forecast,'availability',None)
    if info is None:info = getattr(forecast,'diagnostics',{}).get('availability')
    if not isinstance(info,dict) or not info:return None
    applied = info.get('applied',[])
    rows = []
    for item in applied:
        target = item.get('player') or item.get('team')
        for evidence in item.get('players',[]):
            present, absent = evidence['playing'],evidence['not_playing']
            def averages(group):return ', '.join(f'{label}: {value:.2f}' for label,value in zip(item['targets'],group['target_means']))
            def win(group):return f"; wins {group['win_rate']:.1%}" if 'win_rate' in group else ''
            rows.append(html.Tr([
                html.Td(target),html.Td(evidence['player'].removeprefix('absent:')),
                html.Td(f"{present['games']} games; "+averages(present)+win(present)),
                html.Td(f"{absent['games']} games; "+averages(absent)+win(absent)),
                html.Td(f"{evidence['baseline_absence']:.0%}"),
            ]))
    return html.Details([
        html.Summary('Injury & roster adjustment · '+(f'{len(applied)} supported changes' if applied else 'baseline retained')),
        html.P(' '.join(item['description']+'.' for item in applied) if applied else info.get('reason','No supported change.'),role='status'),
        html.P('Status retrieved '+info['fetched_at']+'. Current availability can be newer than the saved odds; historical statistics keep the original forecast cutoff.') if info.get('fetched_at') else None,
        html.A('Structured status source',href=info['source_url'],target='_blank',rel='noopener noreferrer') if info.get('source_url') else None,
        html.Div(html.Table([
            html.Thead(html.Tr([html.Th(x) for x in ['Forecast','Absent player','When playing','When not playing','Recent absence share']])),
            html.Tbody(rows),
        ],className='mispricing-table'),className='mispricing-table-scroll') if rows else None,
        html.P('These are historical associations, not causal injury valuations. The adjustment controls for opponent, venue, season and time, shrinks small samples, '
               'and compares today’s absence with recent availability to reduce double-counting. It adds estimation uncertainty. '
               'Player workload adjustments are marginal forecasts, not a new jointly conserved team allocation. Historical baseline backtests do not validate this overlay.',className='ppp-comparison-note'),
        html.P('Uncertain statuses left at baseline: '+', '.join(info['uncertain'])) if info.get('uncertain') else None,
        html.Ul([html.Li(item) for item in info.get('skipped',[])]) if info.get('skipped') else None,
    ],className='panel availability-adjustment',open=bool(applied))
