"""Sport-scoped saved Quick Filtering selections, sorted by probability then edge."""
from .presentation import table_cell, table_header
import sqlite3
from .navigation import scoped_callback,register_visibility,filtered_visibility

from dash import Input,Output,dash_table,dcc,html,no_update
from dash.exceptions import PreventUpdate

from .display_time import format_eastern
from .filtered_results import SPORTS


def layout():
    columns = [('open','Game markets'),('matchup','Game'),('start','Scheduled start'),('provider','Source'),
        ('selection','Selection'),('category','Category'),('market','Market'),('method','Methods'),
        ('probability','Calculated probability'),('edge','Edge (pp)'),('odds','Market odds'),
        ('implied','Market implied'),('weighting','Weighting'),('criteria','Applied criteria'),('quote_time','Quote snapshot'),('saved_at','Filtered at')]
    return html.Section([
        html.Div([html.Div('MARKET DATA',className='eyebrow accent'),html.H2('Filtered Results'),
            html.P('Qualifying selections saved by Quick Filtering across upcoming games. '
                   'Sorted by calculated probability, then edge, highest first.')],className='panel market-heading'),
        html.Section([
            html.Div([
                html.Div([html.Label('Saved runs',htmlFor='filtered-results-scope'),dcc.Dropdown(id='filtered-results-scope',
                    options=[dict(label='Latest run per game and source',value='latest'),dict(label='All saved runs (including earlier weighting)',value='all')],
                    value='latest',clearable=False)],className='field'),
                html.Button('Refresh saved results',id='filtered-results-refresh',n_clicks=0,className='export-button'),
            ],className='market-controls'),
            html.P(id='filtered-results-status',className='load-status',role='status'),
            html.P('Results retain the quotes, criteria and methods used when filtering. '
                   'The latest-run view shows current-season weighting only. Earlier estimates remain labeled in All saved runs; rerun Quick Filtering to update them. '
                   'For multiple methods, probability and edge are the lowest values across the selected methods. '
                   'Games disappear from this view at their scheduled start. Opening this page reads local results only.',className='ppp-comparison-note'),
            dash_table.DataTable(id='filtered-results-table',data=[],columns=[dict(id=key,name=label,
                **({'presentation':'markdown'} if key=='open' else {'type':'numeric','format':{'specifier':'.2%'}}
                   if key in {'probability','implied'} else {'type':'numeric','format':{'specifier':'+.2f'}}
                   if key=='edge' else {'type':'numeric','format':{'specifier':'+.0f'}} if key=='odds' else {})) for key,label in columns],
                markdown_options={'link_target':'_self'},cell_selectable=False,page_size=25,filter_action='native',
                sort_action='native',sort_mode='multi',sort_by=[dict(column_id='probability',direction='desc'),dict(column_id='edge',direction='desc')],
                tooltip_duration=None,style_table={'overflowX':'auto'},
                style_cell=table_cell(minWidth='110px', maxWidth='290px', whiteSpace='normal', height='auto'),style_header=table_header()),
        ],className='panel'),
        dcc.Interval(id='filtered-results-clock',interval=30000,disabled=True),
    ],id='filtered-results-page',style={'display':'none'})


def rows(records):
    data=[];tooltips=[]
    for row in records:
        criteria=row['criteria']
        data.append({**{key:row[key] for key in ('id','matchup','provider','selection','category','market','method','probability','implied','odds')},
            'open':f'[Open game]({row["href"]})','edge':100*row['edge'],
            'start':format_eastern(row['start']),'quote_time':format_eastern(row['quote_time']) if row['quote_time'] else 'Unavailable',
            'saved_at':format_eastern(row['saved_at']),
            'weighting':row.get('weighting','Earlier weighting; rerun Quick Filtering'),
            'criteria':f"Probability > {100*criteria['probability']:g}%; edge > {100*criteria['edge']:g} pp"})
        tooltips.append({'method':dict(type='text',value='\n'.join(
            f"{value['method']}: {100*value['probability']:.2f}% / {100*value['edge']:+.2f} pp" for value in row['estimates']))})
    return data,tooltips


def register(app,service):
    register_visibility(app,filtered_visibility,Output('filtered-results-page','style'),Output('filtered-results-page-link','href'),
        Output('filtered-results-page-link','className'),Output('filtered-results-clock','disabled'))

    @scoped_callback(app,'filtered-results',Output('filtered-results-table','data'),Output('filtered-results-table','tooltip_data'),
        Output('filtered-results-status','children'),Input('workspace-page','value'),Input('sport','value'),
        Input('filtered-results-scope','value'),Input('filtered-results-refresh','n_clicks'),Input('filtered-results-clock','n_intervals'))
    def show_filtered_results(page,sport,scope,refresh,tick):
        if page!='filtered-results' or sport not in SPORTS:raise PreventUpdate
        try:
            records=service.upcoming(sport,scope)
            data,tooltips=rows(records)
        except (OSError,ValueError,sqlite3.Error):
            return no_update,no_update,'Saved results could not be read. Refresh to retry.'
        status=(f'{len(data):,} qualifying selections from upcoming {sport} games.' if data else
                f'No saved qualifying selections for upcoming {sport} games. Run Quick Filtering in a game’s Mispricing Filter to add results.')
        return data,tooltips,status
