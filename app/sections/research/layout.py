"""Research experiment setup controls."""

from dash import dcc, html
from app.components.controls import field


def research_setup():
    return html.Section([
        dcc.Store(id='research-editor-context'),html.H3('Research experiment'),
        html.Div([
            field('Sport',dcc.Dropdown(id='research-sport',options=['NBA','NFL','NCAAF'],value='NBA',clearable=False)),
            html.Div(field('Evaluation team',dcc.Dropdown(id='research-team',options=[dict(label='All teams',value='__all__')],value='__all__',clearable=False)),id='research-team-field'),
        ],className='research-scope-controls'),
        html.Div([
        html.Div([html.H3('Model parameters'),html.Button('Reset sport defaults',id='research-reset',n_clicks=0,className='export-button')],className='research-parameter-heading'),
        html.P('Every configuration field is shown below. Values are validated together before a run starts. Model sport follows the sport selector. Saved results retain the exact effective parameters.',className='backtest-status'),
        html.Div(id='research-parameters',className='research-parameters'),
        html.P(id='research-parameter-note',className='backtest-status'),
        ],id='research-model-settings'),
    ],id='research-setup',className='panel research-setup',style={'display':'none'})
