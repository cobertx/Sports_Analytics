"""Upcoming game cards and FanDuel-style quote ladders for all providers."""
from collections import OrderedDict
import json
import math
from dash import dcc,html

from sports_analytics.display_time import format_eastern
from app.sections.markets.market_view import logo_badge,market_provider_heading,probability_text
from app.sections.markets.mispricing import GAME_METHODS,PLAYER_METHODS,QUICK_DEFAULT_PROBABILITY,QUICK_DEFAULT_EDGE
from app.sections.markets.prompt_history import button as history_button
from app.sections.market_structure.market_structure_link import button as structure_button
from sports_analytics.market_summary import PROVIDERS
from app.sections.markets.lazy_markets import markets as lazy_markets
from sports_analytics.nba_markets import NBA_MARKETS
from sports_analytics.nfl_markets import NFL_MARKETS,SPORTSBOOKS,draftkings_markets
from sports_analytics.ncaaf_markets import NCAAF_MARKETS
from app.components.static_markup import markup

SUMMARY_BATCH = 6
SUMMARY_STEP = 25
EAGER_SELECTIONS = 100
QUICK='Quick Filtering'
METRICS=('probability','odds','edge')


def provider_logo(provider):
    if provider=='DraftKings':
        return html.Span(provider,className='summary-source-name summary-source-draftkings')
    return html.Img(src='/assets/providers/'+provider.lower()+'.ico',alt=provider,className='summary-provider-logo',title=provider)


def quick_controls():
    return html.Section([
        dcc.Store(id='summary-quick-request'),dcc.Store(id='summary-quick-completed'),
        dcc.Interval(id='summary-quick-poll',interval=1000,n_intervals=0,disabled=True),
        html.H3(QUICK),html.P('Choose methods and thresholds, then run Quick Filtering. Each quote must pass both thresholds '
            'under every selected method for its market type. Qualifying game lines also appear in the probability charts.',
            className='ppp-comparison-note'),
        html.Div([
            field('Game Outcome methods',dcc.Dropdown(id='summary-quick-game-methods',options=list(GAME_METHODS),
                value=list(GAME_METHODS),multi=True)),
            field('Player Forecast methods',dcc.Dropdown(id='summary-quick-player-methods',options=list(PLAYER_METHODS),
                value=list(PLAYER_METHODS),multi=True)),
        ],className='forecast-methods'),
        html.Div([
            field('Calculated probability above (%)',dcc.Input(id='summary-quick-probability',type='number',
                value=QUICK_DEFAULT_PROBABILITY,min=0,max=100,step='any')),
            field('Edge above (percentage points)',dcc.Input(id='summary-quick-edge',type='number',
                value=QUICK_DEFAULT_EDGE,min=-100,max=100,step='any')),
            html.Button('Run Quick Filtering',id='summary-quick-run',n_clicks=0,className='apply-criteria-button'),
        ],className='criteria-controls'),
        html.Div(id='summary-quick-status',role='status',className='criteria-status'),
        html.Div(id='summary-quick-results'),
    ],id='summary-quick-panel',className='quick-filter-section',style={'display':'none'})


def chart_criteria_controls():
    return html.Section([
        dcc.Store(id='summary-chart-criteria'),
        html.H3('Apply Criteria'),
        html.Div([
            field('Calculated probability above (%)',dcc.Input(id='summary-chart-probability',type='number',value=75,min=0,max=100,step='any')),
            field('Edge above (percentage points)',dcc.Input(id='summary-chart-edge',type='number',value=5,min=-100,max=100,step='any')),
            html.Button('Apply Criteria',id='summary-chart-apply',n_clicks=0,className='apply-criteria-button'),
        ],className='criteria-controls'),
        html.Div(id='summary-chart-criteria-status',role='status',className='criteria-status'),
    ],className='market-criteria summary-chart-criteria')


def pull_estimate(sport,provider):
    if provider not in SPORTSBOOKS:return '0 Odds API credits · public market data'
    package={'NBA':NBA_MARKETS,'NFL':NFL_MARKETS,'NCAAF':NCAAF_MARKETS}.get(sport)
    if package and provider=='DraftKings':package=draftkings_markets(package)
    return f'Estimated up to {len(package)} API credits' if package else 'Choose a sport to see the credit estimate'


def source_pulls():
    return html.Div([
        html.Fieldset([
            html.Legend(provider),
            html.P('Select a game',id='summary-source-'+provider.lower()+'-status',className='summary-source-state',role='status'),
            html.Button('Pull '+provider,id='summary-pull-'+provider.lower(),n_clicks=0,className='pull-markets-button',
                title='Pull only '+provider+' quotes for this game'),
            html.Small(pull_estimate(None,provider),id='summary-source-'+provider.lower()+'-cost',className='summary-source-cost',
                title='The final charge depends on the markets returned.' if provider in SPORTSBOOKS else 'This source does not use The Odds API.'),
        ],id='summary-source-'+provider.lower(),disabled=True,className='summary-source-pull')
        for provider in PROVIDERS
    ],className='summary-source-pulls',role='group',**{'aria-label':'Pull individual market sources'})


def field(label,control):
    return html.Div([html.Label(label,htmlFor=control.id),control],className='field')


def layout():
    return html.Section([
        dcc.Store(id='summary-catalog'),dcc.Store(id='summary-quotes'),dcc.Store(id='summary-models'),dcc.Store(id='summary-rendered'),
        dcc.Store(id='summary-model-completed'),
        dcc.Interval(id='summary-cache-poll',interval=1500,n_intervals=0,disabled=True),
        market_provider_heading('DraftKings', 'https://sportsbook.draftkings.com/',
                                class_name='draftkings-market-heading'),
        html.Section([
            html.Div([html.Div('MARKET DATA',className='eyebrow accent'),
                      html.H2('Market Summary'),
                      html.P('Compare FanDuel, DraftKings, Kalshi and Polymarket quotes for one upcoming game.')],
                     className='market-heading summary-multi-source'),
            html.Div([
                field('Game window',dcc.Dropdown(id='summary-days',options=[dict(label='Next 7 days',value=7),dict(label='Next 14 days',value=14)],value=7,clearable=False)),
                html.Button('Refresh upcoming games',id='summary-refresh',n_clicks=0,className='export-button'),
            ],className='market-controls'),
            html.Div(id='summary-catalog-status',className='load-status',role='status'),
            field('Upcoming game',dcc.Dropdown(id='summary-game',options=[],placeholder='Select a game to compare its markets',clearable=True)),
        ],className='panel summary-heading'),
        html.Div(id='summary-game-cards'),
        html.Section([
            html.Div([
                html.Button('Back to upcoming games',id='summary-back',n_clicks=0,className='export-button'),
                html.Div([html.Button('Pull Live Markets',id='summary-pull',n_clicks=0,className='pull-markets-button'),
                          html.Small(id='summary-credit-note')],className='detail-pull-control summary-multi-source'),
            ],className='detail-page-actions'),
            html.Div(id='summary-game-header'),
            html.Div([
                field('Game Outcome Forecast Method',dcc.Dropdown(id='summary-game-method',options=list(GAME_METHODS),value='PPP',clearable=False)),
                field('Player Forecast Method',dcc.Dropdown(id='summary-player-method',options=list(PLAYER_METHODS),value='WHP',clearable=False)),
                html.Button('Recalculate estimates',id='summary-recalculate',n_clicks=0,className='export-button'),
            ],id='summary-forecast-controls',className='forecast-methods'),
            html.P('Saved sportsbook quotes load without spending API credits. Pull Live Markets refreshes all four sources. '
                   'FanDuel and DraftKings pulls use The Odds API credits. Odds use American format. Edge is before fees.',
                   className='ppp-comparison-note summary-multi-source'),
            html.P('Pull DraftKings saves a timestamped snapshot. Saved quotes load without spending API credits. '
                   'Odds use American format, and model probabilities exclude pushes.',
                   className='ppp-comparison-note summary-draftkings-only'),
            html.P('Compare each selection across source columns. Green highlights the best available price for the same outcome. '
                   'Equivalent integer thresholds share a row; hover over a quote for its original wording. '
                   'Lines with different push rules stay separate. A dash means no matching quote.',
                   className='summary-comparison-note summary-multi-source'),
            dcc.Loading(html.Div(id='summary-quote-status',className='load-status',role='status'),delay_show=500,color='#57dbc4',overlay_style={'visibility':'visible'}),
            source_pulls(),
            html.Div(field('DraftKings snapshot',dcc.Dropdown(id='summary-draftkings-view',
                options=[dict(label='Latest saved snapshot',value='latest')],value='latest',clearable=False)),
                className='summary-draftkings-only'),
            html.Div(html.Div(id='summary-model-status',className='forecast-history',role='status'),id='summary-forecast-status'),
            dcc.Tabs(id='summary-category',value='Game Lines',mobile_breakpoint=0,children=[dcc.Tab(label=value,value=value) for value in
                     ['Game Lines','Player Props',QUICK,'Other Markets']],className='market-category-tabs'),
            quick_controls(),
            html.Div(dcc.Loading(html.Div(id='summary-market-sections'),delay_show=500,color='#57dbc4',
                                 overlay_style={'visibility':'visible'}),id='summary-quote-content'),
            html.Details([
                html.Summary('Forecast vs. market probabilities'),
                chart_criteria_controls(),
                dcc.Loading(html.Div(id='summary-game-probability-charts',**{'data-scope':None}),
                            color='#57dbc4'),
            ],id='summary-game-probabilities',open=False,style={'display':'none'},
                className='sportsbook-market summary-game-probabilities'),
        ],id='summary-detail',style={'display':'none'}),
    ],id='market-summary-page',className='market-summary-page',style={'display':'none'})


def cards(games,sport,provider='Market Summary'):
    return html.Div([html.Article([
        html.Button('',id={'type':'summary-game-choice','game':str(game['id'])},n_clicks=0,className='game-card-hit',
                    **{'aria-label':'Compare markets for '+game['away']['name']+' at '+game['home']['name']}),
        html.Div(sport+' - '+provider.upper(),className='game-sport'),
        *[html.Div([logo_badge(game[side]['name'],game[side].get('abbreviation',''),sport),
                    html.Div([html.Strong(game[side]['name']),html.Small(side.title())])],className='game-team') for side in ('away','home')],
        html.Div(format_eastern(game['start']),className='game-card-footer'),
    ],className='panel game-card') for game in games],className='game-grid')


def odds_text(value,probability=None):
    if value is not None:return f'{value:+.0f}'
    return '+∞' if probability==0 else '−∞' if probability==1 else '—'


def model_value(row,metric):
    if metric=='probability':return [probability_text(row['probability']),html.Small(row['method'] or '')],row['detail']
    if metric=='odds':return odds_text(row['model_odds'],row['probability']),''
    return f"{100*row['edge']:+.2f} pp" if row['edge'] is not None else '—',''


def model_values(row):
    values=[]
    for metric,label in [('probability','Calculated probability'),('odds','Model fair odds'),('edge','Edge')]:
        value,title=model_value(row,metric)
        values.append(html.Div([html.Dt(label),html.Dd(html.Span(value,title=title))]))
    return values


def metric_values(row,metric):
    value,title=model_value(row,metric)
    if metric=='edge':
        return html.Span([html.Span(value,className='summary-edge '+('positive' if row['edge'] is not None and row['edge']>0 else
                                                        'negative' if row['edge'] is not None and row['edge']<0 else '')),
                          html.Small(row['edge_provider']) if row.get('edge_provider') else None],
                         title='Calculated probability minus this provider’s implied probability, before fees.')
    return html.Span(value,title=title)


def comparison_order(item):
    """Keep each team's spread ladder and each Over/Under ladder together."""
    outcome=item['outcome'];identity=outcome['key'];subject='';direction=0
    if len(identity)==6 and identity[0]==outcome['category']:
        subject=identity[2]
        direction=0 if identity[4] in {'gt','ge'} else 1
    return subject,direction,outcome['line'] if outcome['line'] is not None else 0,outcome['label']


def comparison_rows(rows,category):
    groups=OrderedDict()
    for row in rows:
        outcome=row.get('comparison') or dict(key=[row['provider'],row['index'],row['side']],category=row['category'],
            group=row['group'],label=row['selection'],line=row['line'],note='Provider-specific contract')
        if category!='All Markets' and outcome['category']!=category:continue
        markets=groups.setdefault((outcome['category'],outcome['group']),OrderedDict())
        matches=markets.setdefault(tuple(outcome['key']),[])
        # Retain every contract if a source lists more than one for an outcome.
        match=next((item for item in matches if row['provider'] not in item['quotes']),None)
        if match is None:
            match=dict(outcome=outcome,quotes={});matches.append(match)
        match['quotes'][row['provider']]=row
    return {key:sorted([item for matches in markets.values() for item in matches],
                       key=comparison_order)
            for key,markets in groups.items()}


def visible_comparisons(items,count):
    """Keep every priced source represented when alternate ladders differ.

    Preserve team/side ladder order within the visible window. Reserve up to two
    rows per source, preferring shared comparisons, then fill from the full
    ladder. Expanding the window eventually returns every original row.
    """
    if count<=0:return []
    if count>=len(items):return items
    priced=[{p for p,row in item['quotes'].items() if row.get('implied') is not None} for item in items]
    required={p:min(2,sum(p in providers for providers in priced)) for p in PROVIDERS}
    covered=dict.fromkeys(PROVIDERS,0);chosen=set()
    for index in sorted(range(len(items)),key=lambda i:(-len(priced[i]),i)):
        if not any(covered[p]<required[p] for p in priced[index]):continue
        chosen.add(index)
        for provider in priced[index]:covered[provider]+=1
        if len(chosen)>=count or all(covered[p]>=required[p] for p in PROVIDERS):break
    for index in range(len(items)):
        if len(chosen)>=count:break
        chosen.add(index)
    return [items[index] for index in sorted(chosen)]


def shared_estimate(quotes):
    """Use one coherent, source-specific estimate and its corresponding edge."""
    ranked=[(p,row) for p,row in quotes.items() if row.get('edge') is not None and math.isfinite(row['edge'])]
    if ranked:
        provider,row=max(ranked,key=lambda item:(item[1]['edge'],-PROVIDERS.index(item[0])))
        return {**row,'edge_provider':provider}
    modeled=[row for row in quotes.values() if row.get('probability') is not None]
    available=modeled or list(quotes.values())
    row=min(available,key=lambda row:row['implied'] if row.get('implied') is not None else math.inf)
    return {**row,'edge_provider':None}


def shared_cells(bundle,quotes):
    row=shared_estimate(quotes)
    return [html.Td(metric_values(row,metric),className='summary-model-values summary-shared-cell'+
        (' summary-model-start' if metric=='probability' else ''),**{'data-metric':metric}) for metric in METRICS]


def quote_cell(bundle,row,snapshot,best):
    if row is None:
        return [html.Td(html.Span('—',title='No matching quote from this source'),colSpan=2,className='summary-missing')]
    original=snapshot['rows'][row['index']]
    price=html.Td(html.Div([
        html.Div([html.Strong(odds_text(row['odds'],row['implied']),className='summary-price'),
                  html.Span('Best price',className='summary-best-label') if best else None],className='summary-price-line'),
        html.Small(f"{100*row['ask']:.2f}¢ ask" if row['ask'] is not None else row['status'],className='summary-quote-detail'),
        html.Details([html.Summary('Details',**{'aria-label':row['provider']+' history and market structure'}),
            history_button(snapshot,original,row['provider'],row['side'] or 'yes'),
            structure_button(snapshot,original,row['provider'],row['side'] or 'yes',surface='summary'),
        ],className='summary-quote-tools'),
    ],title=row['selection'],className='summary-quote'+(' summary-best' if best else '')),className='summary-source-cell summary-provider-start',
        **{'data-provider':row['provider']})
    return [price,html.Td(probability_text(row['implied']),className='summary-source-cell summary-implied-cell',**{'data-provider':row['provider']})]


def selection_table(bundle,items,title,snapshots,count):
    content=[];anchors=[]
    providers=('DraftKings',) if bundle.get('mode')=='draftkings' else PROVIDERS
    for item in visible_comparisons(items,count):
        outcome=item['outcome'];quotes=item['quotes']
        anchor=next(quotes[p] for p in providers if p in quotes)
        anchors.append([anchor['provider'],anchor['index'],anchor['side'] or ''])
        priced=[row['implied'] for row in quotes.values() if row['implied'] is not None]
        best=min(priced) if len(priced)>1 else None
        content.append(html.Tr([
            html.Th([html.Span(outcome['label']),html.Small(outcome['note']) if outcome['note'] else None],scope='row',className='summary-selection'),
            *[cell for provider in providers for cell in quote_cell(bundle,quotes.get(provider),snapshots.get(provider),best is not None and
                         quotes.get(provider,{}).get('implied')==best)],
            *shared_cells(bundle,quotes),
        ],**{'data-summary-row':json.dumps(anchors[-1],separators=(',',':'))}))
    content=[html.P('Scroll across the table to see market odds and model estimates.',className='summary-scroll-hint'),html.Div(html.Table([
        html.Caption(title+' — Market odds by source',className='summary-table-caption'),
        html.Thead([
            html.Tr([html.Th('Market / Selection',scope='col',rowSpan=2),*[html.Th(provider_logo(provider),
                colSpan=2,scope='colgroup',className='summary-provider-head summary-source-'+provider.lower()) for provider in providers],
                html.Th('Model estimates',colSpan=3,scope='colgroup',className='summary-shared-head summary-model-start')]),
            html.Tr([*[html.Th(label,scope='col') for provider in providers for label in ['Market odds','Implied probability']],
                     *[html.Th(label,scope='col',className='summary-shared-head'+(' summary-model-start' if i==0 else '')) for i,label in
                       enumerate(['Calculated probability','Calculated odds','Best edge (pp)'])]]),
        ]),
        html.Tbody(content),
    ],className='summary-table'+(' summary-table--single-source' if len(providers)==1 else '')),
        className='summary-table-scroll',tabIndex=0,role='region',**{'aria-label':title+' source comparison'}),
        html.P(f'Showing {min(count,len(items))} of {len(items)} selections'+
            (' · Includes prices from each available source. Show more to see additional lines.' if count<len(items) else ''),className='summary-row-count')]
    # Native markup has no per-cell Redux subscriptions. Dash owns this one
    # table boundary; read-only quote rows and delegated controls remain HTML.
    return html.Div(
        id=dict(type='summary-model-table',bundle=bundle['id'],category=items[0]['outcome']['category'],group=title),
        **{'data-rows':json.dumps(anchors,separators=(',',':')),'data-models':'','data-markup':markup(content)},className='summary-native-table')


def sections(bundle,rows,category):
    groups=comparison_rows(rows,category);categories=OrderedDict();index=0
    snapshots={name:{**record['snapshot'],'game':record.get('game')} for name,record in bundle['providers'].items() if record.get('snapshot')}
    # Keep all comparisons and forecasts. Bound only the mounted quote view:
    # native collapsed details still mount every child unless we defer them.
    lazy=sum(len(items) for items in groups.values())>EAGER_SELECTIONS
    scope=lazy_markets.view() if lazy else None
    for (group,title),items in groups.items():
        def render(count,items=items,title=title):
            return selection_table(bundle,items,title,snapshots,count)
        body=(lazy_markets.panel(scope,index,render,len(items),initial=index<2,size=SUMMARY_BATCH,step=SUMMARY_STEP,
                                 loading=False,text_more=True)
              if lazy else render(len(items)))
        table=html.Details([
            html.Summary([html.Strong(title),html.Span(f'{len(items)} selections',className='market-chevron')]),
            body,
        ],open=index<2,key=group+'-'+title,className='sportsbook-market summary-market')
        categories.setdefault(group,[]).append(table);index+=1
    return [html.Section([html.H3(group,className='summary-category-heading'),*categories[group]],className='summary-category')
            for group in ('Game Lines','Player Props','Other Markets') if group in categories] or html.P(
                'Loading source quotes. Each source appears here as soon as it is ready.' if bundle.get('updating') else
                'No quoted selections are available in this category.',className='saved-market-empty')


def quick_results(bundle,ranked,criteria):
    snapshots={p:{**r['snapshot'],'game':r.get('game')} for p,r in bundle['providers'].items() if r.get('snapshot')}
    def table(items,start):
        content=[]
        for rank,row in enumerate(items,start):
            snapshot=snapshots[row['provider']];original=snapshot['rows'][row['index']]
            details='; '.join(f"{v['method']}: {probability_text(v['probability'])}, {v['edge']*100:+.2f} pp" for v in row['estimates'])
            content.append(html.Tr([html.Td(rank),html.Th([row['selection'],html.Div([
                history_button(snapshot,original,row['provider'],row['side'] or 'yes'),
                structure_button(snapshot,original,row['provider'],row['side'] or 'yes',surface='summary')])],scope='row'),
                html.Td(provider_logo(row['provider'])),html.Td(row['method'],title=details),
                html.Td(odds_text(row['odds'],row['implied'])),html.Td(probability_text(row['implied'])),
                html.Td(probability_text(row['probability'])),html.Td(metric_values(row,'odds')),html.Td(metric_values(row,'edge'))]))
        table=html.Div(html.Table([html.Thead(html.Tr([html.Th(label,scope='col') for label in
            ['Rank','Selection','Source','Methods','Market odds','Implied probability','Lowest calculated probability','Calculated odds','Lowest edge (pp)']])),
            html.Tbody(content or html.Tr(html.Td('No qualifying markets.',colSpan=9)))],className='summary-quick-table'),
            className='summary-table-scroll',tabIndex=0,role='region',**{'aria-label':'Quick Filtering rankings'})
        return html.Div(className='summary-native-table',**{'data-markup':markup(table)})
    matches=ranked['matches']
    return html.Div([
        html.P(f"{len(matches)} qualifying quotes · {ranked['evaluated']} evaluated · {ranked['unavailable']} unavailable. "
               f"Probability > {criteria['probability']*100:g}% and edge > {criteria['edge']*100:g} pp under every selected method.",
               className='criteria-evaluation',role='status'),
        *[html.P(error,className='mispricing-error',role='status') for error in ranked['errors']],
        html.H3('Top 3 by edge'),table(matches[:3],1),
        html.H3('Remaining qualifying quotes'),lazy_markets.panel(lazy_markets.view(),0,
            lambda count:table(matches[3:3+count],4),max(0,len(matches)-3),size=25,text_more=True),
    ],className='summary-quick-ranking')
