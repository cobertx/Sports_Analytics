"""Information Variable source directory, attributed reporting and NFL weather."""
from dash import Input,Output,State,ctx,dcc,html,no_update
from dash.exceptions import PreventUpdate

from . import information as data
from .backtesting_results import field


def link(label,url):
    return html.A(label,href=url,target='_blank',rel='noopener noreferrer')


def navigation():
    return html.Details([html.Summary('Information Variable'),html.Div(id='information-navigation',className='market-provider-pages')],
                        id='information-menu',open=True,className='information-menu',style={'display':'none'})


def layout():
    return html.Section([
        dcc.Store(id='information-context'),dcc.Store(id='information-data'),
        dcc.Interval(id='information-timer',interval=300000,disabled=True),
        html.Section([
            html.Div('INFORMATION VARIABLE',className='eyebrow accent'),html.H2(id='information-title'),
            html.P('League reports are the primary record. News coverage supplies context; every headline links to its publisher. '
                   'Publication and retrieval times are shown separately.'),
            html.Div(id='information-sources',className='information-sources'),
            html.Button('Refresh information',id='information-refresh',n_clicks=0,className='export-button'),
            html.P(id='information-status',className='load-status',role='status',**{'aria-live':'polite'}),
        ],className='panel information-heading'),
        html.Section([
            html.Div([
                field('News type',dcc.Dropdown(id='information-kind',options=[dict(label='Injuries and roster moves',value='all'),
                    dict(label='Injuries',value='injuries'),dict(label='Roster moves',value='roster')],value='all',clearable=False)),
                field('Team mentioned by publisher',dcc.Dropdown(id='information-team',options=[dict(label='All teams',value='__all__')],value='__all__',clearable=False)),
                field('Search headlines',dcc.Input(id='information-search',type='text',debounce=True,placeholder='Player or keyword')),
            ],className='backtest-controls'),
            html.P('ESPN reporting, filtered by headline keywords. This is a limited news feed, not a complete injury or roster database. '
                   'Team tags come from the publisher. A headline alone does not establish official playing status.',className='information-note'),
            html.Div(id='information-headlines'),
        ],id='information-news',className='panel information-news'),
        html.Section([
            html.H3('NFL city weather and alerts'),
            html.Div([
                field('NFL city / area',dcc.Dropdown(id='information-location',options=[dict(label=k,value=k) for k in sorted(data.LOCATIONS)]+[dict(label='Custom U.S. point',value='custom')],
                      value='Indianapolis, IN',clearable=False)),
                field('Latitude',dcc.Input(id='information-latitude',type='number',min=-90,max=90,step='any',value=39.7684,debounce=True)),
                field('Longitude',dcc.Input(id='information-longitude',type='number',min=-180,max=180,step='any',value=-86.1581,debounce=True)),
            ],className='backtest-controls'),
            html.P('Presets use approximate city centers. For venue-specific conditions, enter the actual venue coordinates. '
                   'Match the forecast hour to kickoff; forecasts apply outdoors and do not establish stadium roof status. '
                   'Times below use America/Indianapolis. NWS coverage is U.S. only.',className='information-note'),
            html.Div(id='information-weather-results'),
        ],id='information-weather',className='panel information-weather',style={'display':'none'}),
    ],id='information-page',className='information-page',style={'display':'none'})


def sources(context,service):
    if context['kind']=='weather':
        records=[dict(title='National Weather Service',url='https://www.weather.gov/',note='Official U.S. forecasts, watches and warnings.'),
                 dict(title='NWS public data service',url='https://www.weather.gov/documentation/services-web-api',note='Free forecast and alert data; no API key is needed.'),
                 dict(title='UK Met Office',url='https://weather.metoffice.gov.uk/forecast/gcpvj0v07',note='Official London forecast for U.K. fixtures; not covered by the U.S. feed.')]
    else:
        records=data.official_sources(context['sport'],service.now())
        if context['kind'] in {'injuries','roster'}:records=[r for r in records if r['kind']==context['kind']]
        records+=[dict(title='ESPN '+context['sport']+' reporting',url=f'https://www.espn.com/{context["sport"].lower()}/',note='Secondary reporting and interviews. Confirm participation and roster changes against the league records.')]
    return [html.Article([link(record['title'],record['url']),html.P(record['note'])],className='information-source') for record in records]


def news(packet,context,kind,team,search):
    if not packet or packet.get('scope',{}).get('sport')!=context['sport'] or packet.get('scope',{}).get('kind')!='news':return []
    saved=packet.get('cached')
    if not saved:return html.P('Loading attributed news headlines…' if packet.get('pending') else 'No saved headlines available. Use the official sources above or retry Refresh information.')
    selected=context['kind'] if context['kind'] in {'injuries','roster'} else kind
    items=[item for item in saved['data']['items'] if (selected=='all' or selected in item['kinds'])
           and (team=='__all__' or team in item['teams']) and (not search or search.casefold() in item['headline'].casefold())]
    return [html.Article([
        html.Div([html.Span('ESPN · '+', '.join(item['kinds']).replace('injuries','Injury reporting').replace('roster','Roster reporting')),
                  html.Time(data.time_text(item['published']),dateTime=item['published']) if item['published'] else html.Span('Publication time unavailable')],className='information-news-meta'),
        html.H3(link(item['headline'],item['url'])),html.Small(', '.join(item['teams']) or 'Team tags not supplied'),
    ],className='information-article') for item in items] or html.P('No matching headlines in the current saved feed. Use the league reports above for complete official coverage.')


def weather(packet,latitude,longitude,now):
    expected=dict(kind='weather',sport='NFL',latitude=round(latitude,4),longitude=round(longitude,4)) if isinstance(latitude,(int,float)) and isinstance(longitude,(int,float)) else None
    if not packet or packet.get('scope')!=expected:return []
    saved=packet.get('cached')
    if not saved:return html.P('Loading NWS forecast and alerts…' if packet.get('pending') else 'No saved forecast is available for this point. See the source links above or refresh to retry.')
    forecast=saved['data']
    periods=[p for p in forecast['hours'] if data.timestamp(p['endTime'])>now]
    alerts=[a for a in forecast['alerts'] if not data.timestamp(a['ends']) or data.timestamp(a['ends'])>now]
    notice=forecast.get('notice')
    return [
        html.P([html.Strong(forecast['location'] or 'Selected point'),f' · {latitude:.4f}, {longitude:.4f} · Forecast issued {data.time_text(forecast["issued_at"])} · ',
                link('Open official point forecast',forecast['source_url'])]),
        html.H3('Weather alerts'),html.P(notice,className='information-note') if notice else None,
        *[html.Article([link(a['headline'] or a['event'],a['url']),html.P(f"{a['severity']} · Sent {data.time_text(a['sent'])} · Ends {data.time_text(a['ends'])}")],
                       className='information-alert') for a in alerts],
        html.P('No active alerts were returned when this snapshot was retrieved.') if not alerts and not notice else None,
        html.H3('Hourly outlook'),
        html.Div(html.Table([html.Thead(html.Tr([html.Th(v,scope='col') for v in ['Forecast hour','Temperature','Wind','Precipitation chance','Conditions']])),
            html.Tbody([html.Tr([html.Th(data.time_text(p['startTime']),scope='row'),
                html.Td(f"{p['temperature']}°{p['temperatureUnit']}" if p['temperature'] is not None else 'Unavailable'),
                html.Td(' '.join(str(p[k]) for k in ['windDirection','windSpeed'] if p[k] is not None) or 'Unavailable'),
                html.Td(f"{p['probabilityOfPrecipitation']['value']}%" if isinstance(p.get('probabilityOfPrecipitation'),dict) and p['probabilityOfPrecipitation'].get('value') is not None else 'Unavailable'),
                html.Td(p['shortForecast'])]) for p in periods[:48]])],className='mispricing-table'),className='mispricing-table-scroll') if periods else
        html.P('The saved forecast has no future hours remaining. Refresh information for a new forecast.'),
    ]


def register(app,service):
    @app.callback(Output('information-menu','style'),Output('information-menu','className'),Output('information-navigation','children'),
                  Output('information-page','style'),Output('information-context','data'),Output('information-title','children'),
                  Output('information-sources','children'),Output('information-news','style'),Output('information-weather','style'),
                  Input('sport','value'),Input('workspace-page','value'),Input('market-url','hash'))
    def information_visibility(sport,page,location_hash):
        supported=sport in data.SPORTS;active=supported and page=='information';hidden={'display':'none'}
        found=data.route(location_hash);kind=found['kind'] if found and found['sport']==sport else data.PAGES[sport][0][0] if supported else None
        context=dict(sport=sport,kind=kind) if active else None
        links=[html.A(title,href=f'#{sport.lower()}-information-{key}',className='sport-page-link'+(' active' if active and kind==key else ''),
                      **{'aria-current':'page' if active and kind==key else 'false'}) for key,title in data.PAGES.get(sport,[])]
        return ({} if supported else hidden,'information-menu'+(' active' if active else ''),links,{} if active else hidden,context,
                sport+' · '+dict(data.PAGES[sport])[kind] if active else '',sources(context,service) if active else [],
                hidden if kind=='weather' else {},{} if kind=='weather' else hidden)

    @app.callback(Output('information-latitude','value'),Output('information-longitude','value'),Input('information-location','value'))
    def information_location(location):
        return data.LOCATIONS.get(location,(no_update,no_update))

    @app.callback(Output('information-data','data'),Output('information-status','children'),Input('information-context','data'),
                  Input('information-refresh','n_clicks'),Input('information-timer','n_intervals'),
                  Input('information-latitude','value'),Input('information-longitude','value'),State('information-data','data'))
    def information_load(context,clicks,tick,latitude,longitude,previous):
        if not context:raise PreventUpdate
        try:
            packet=service.load('weather' if context['kind']=='weather' else 'news',sport=context['sport'],latitude=latitude,longitude=longitude,
                                refresh=ctx.triggered_id=='information-refresh')
            if packet==previous:raise PreventUpdate
            stamp=(packet.get('cached') or {}).get('fetched_at')
            status=('Saved data retrieved '+data.time_text(stamp)+'. ' if stamp else 'No saved data yet. ')
            status+=('Updating in the background. ' if packet['pending'] else '')+(packet['error'] or '')
            return packet,status
        except (ValueError,DataSourceError) as exc:return None,str(exc)

    app.clientside_callback('''function(context,packet){return [!context,packet&&packet.pending?1500:300000];}''',
        Output('information-timer','disabled'),Output('information-timer','interval'),Input('information-context','data'),Input('information-data','data'))

    @app.callback(Output('information-team','options'),Output('information-team','value'),Input('information-data','data'),State('information-team','value'))
    def information_teams(packet,previous):
        saved=(packet or {}).get('cached') or {};teams=sorted({team for item in saved.get('data',{}).get('items',[]) for team in item['teams']})
        return [dict(label='All teams',value='__all__'),*[dict(label=t,value=t) for t in teams]],previous if previous in teams else '__all__'

    @app.callback(Output('information-headlines','children'),Output('information-weather-results','children'),
                  Input('information-context','data'),Input('information-data','data'),Input('information-kind','value'),Input('information-team','value'),
                  Input('information-search','value'),Input('information-latitude','value'),Input('information-longitude','value'))
    def information_contents(context,packet,kind,team,search,latitude,longitude):
        if not context:raise PreventUpdate
        if context['kind']=='weather':return no_update,weather(packet,latitude,longitude,service.now())
        return news(packet,context,kind,team,search),no_update
