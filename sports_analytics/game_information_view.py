"""Additional Information tabs and independently updated NFL header conditions."""
import re
from .navigation import scoped_callback

from dash import Input,Output,State,ctx,dcc,html,no_update
from dash.exceptions import PreventUpdate
from .game_information import ADDITIONAL_INFORMATION,scope,roof,kickoff_weather
from .information import official_sources,time_text


def active(game,page,location):
    match=re.fullmatch(r'#(nba|nfl)-game-(\d+)',location or '')
    return bool(page=='market' and game and match and
                (game.get('sport'),str(game.get('id')))==(match[1].upper(),match[2]))


def layout():
    return html.Section([
        dcc.Store(id='game-information-data'),dcc.Interval(id='game-information-poll',interval=1500,disabled=True),
        html.Div([html.Div([html.Div('GAME CONTEXT',className='eyebrow accent'),html.H2(ADDITIONAL_INFORMATION)],className='panel-heading'),
                  html.Button('Refresh information',id='game-information-refresh',n_clicks=0,className='export-button')],className='game-information-heading'),
        html.P('Injury and roster reporting relevant to these two teams. Each report links to its publisher. '
               'News is current reporting and is independent of the selected saved odds snapshot.',className='ppp-comparison-note'),
        html.Div(id='game-information-content'),
    ],id='game-additional-information',className='panel game-additional-information',style={'display':'none'})


def conditions(packet,game):
    if not packet or packet.get('scope')!=scope(game):
        return html.P('Loading stadium and kickoff weather…',className='game-condition-status',role='status')
    venue=packet['venue'];weather=packet.get('weather') or {};saved=weather.get('cached') or {}
    forecast=saved.get('data') or {};hour=kickoff_weather(weather,game)
    metadata=(packet.get('metadata') or {}).get('cached') or {}
    venue_name=venue.get('fullName') or game.get('venue') or 'Venue not reported'
    source=(metadata.get('data') or {}).get('source_url')
    title=html.A(venue_name,href=source,target='_blank',rel='noopener noreferrer') if source else venue_name
    children=[html.Div([html.Strong(title),html.Span(roof(venue),className='game-roof-badge')],className='game-venue-condition')]
    if hour:
        temperature=(f"{hour['temperature']}°{hour.get('temperatureUnit') or ''}" if hour.get('temperature') is not None else 'Temperature not reported')
        wind=' '.join(str(hour[k]) for k in ('windDirection','windSpeed') if hour.get(k)) or 'Wind not reported'
        chance=(hour.get('probabilityOfPrecipitation') or {}).get('value')
        summary=[temperature,hour.get('shortForecast') or 'Conditions not reported','Wind '+wind]
        if chance is not None:summary.append(f'Precipitation {chance}%')
        children.append(html.Div([html.Strong('Kickoff weather'),html.Span(' · '.join(summary))],className='game-kickoff-weather'))
        point=packet.get('point') or {}
        children.append(html.Small([
            html.A('NWS · '+point.get('label','Forecast'),href=forecast.get('source_url'),target='_blank',rel='noopener noreferrer'),
            ' · Issued '+time_text(forecast.get('issued_at'))+' · Retrieved '+time_text(saved.get('fetched_at')),
            ' · Saved forecast; refresh overdue' if weather.get('stale') else '',
            ' · Outdoor conditions; indoor playing conditions can differ.' if venue.get('indoor') is True else '',
        ],className='game-condition-source'))
    else:
        country=str((venue.get('address') or {}).get('country') or '').upper()
        message=('Kickoff time is TBD; an hourly forecast cannot yet be matched.' if game.get('time_tbd') else
                 'Loading kickoff weather…' if packet.get('pending') else
                 'Kickoff weather is outside NWS U.S. coverage.' if country and country not in {'USA','US','UNITED STATES','UNITED STATES OF AMERICA'} else
                 'Kickoff weather is outside the available hourly forecast window. Refresh closer to kickoff.' if saved else
                 'Kickoff weather is unavailable for the reported venue. Refresh to retry.')
        children.append(html.P(message,className='game-condition-status',role='status'))
    if metadata.get('fetched_at'):
        children.append(html.Small('Venue metadata retrieved '+time_text(metadata['fetched_at']),className='game-condition-source'))
    children.extend(html.Small(error,className='game-condition-status') for error in packet.get('errors',[]) if error)
    return html.Div(children,className='game-conditions-card')


def contents(packet,game,now):
    sources=[html.A(record['title'],href=record['url'],target='_blank',rel='noopener noreferrer',className='export-button')
             for record in official_sources(game['sport'],now)]
    children=[html.Div(sources,className='game-information-sources')]
    if not packet or packet.get('scope')!=scope(game):
        return [*children,html.P('Loading reports for this matchup…',role='status')]
    packets=packet.get('news',[])
    if not packets:
        return [*children,html.P('Loading reports for this matchup…',role='status')]
    stamps=[(p.get('cached') or {}).get('fetched_at') for p in packets]
    stamps=[stamp for stamp in stamps if stamp]
    children.append(html.P(
        ('Reports retrieved '+time_text(min(stamps))+'. ' if stamps else 'No saved reports yet. ')+
        ('Updating in the background. ' if packet.get('pending') else '')+
        ('Some saved reports are due for refresh. ' if any(p.get('stale') and p.get('cached') for p in packets) else ''),
        className='load-status',role='status'))
    children.extend(html.P(error,className='information-note') for error in packet.get('errors',[]))
    for kind,title in [('injuries','Injury News'),('roster','Roster News')]:
        items=[item for item in packet.get('items',[]) if kind in item['kinds']]
        children.append(html.Section([html.H3(title),*[
            html.Article([
                html.Div([html.Span('ESPN · '+', '.join(item['matched_teams'])),
                          html.Time(time_text(item['published']),dateTime=item['published']) if item.get('published') else html.Span('Publication time unavailable')],
                         className='information-news-meta'),
                html.H4(html.A(item['headline'],href=item['url'],target='_blank',rel='noopener noreferrer')),
            ],className='information-article') for item in items],
            html.P('No matching reports in the loaded feeds.' if not packet.get('pending') else 'Loading matching reports…') if not items else None,
        ],className='game-news-section'))
    children.append(html.P('Includes all matching reports returned by the league and both team feeds, up to 100 articles per feed before filtering. '
                          'This is a limited publisher feed, not a complete injury-status or transaction database. '
                          'Use the official reports above to confirm availability and roster moves.',className='ppp-comparison-note'))
    return children


def register(app,service):
    @scoped_callback(app,'market',Output('game-additional-information','style'),Input('market-category','value'),
                  Input('market-detail-context','data'),Input('workspace-page','value'),Input('market-url','hash'))
    def additional_information_visibility(category,game,page,location):
        return {'display':'block' if category==ADDITIONAL_INFORMATION and active(game,page,location) else 'none'}

    app.clientside_callback('''function(game,page,hash,category,packet){
        const m=/^#(nba|nfl)-game-(\\d+)$/.exec(hash||'');
        const active=page==='market'&&game&&m&&game.sport===m[1].toUpperCase()&&String(game.id)===m[2]&&
            (game.sport==='NFL'||category==='Additional Information');
        return [!active,packet&&packet.pending?1500:300000];
    }''',Output('game-information-poll','disabled'),Output('game-information-poll','interval'),
        Input('market-detail-context','data'),Input('workspace-page','value'),Input('market-url','hash'),
        Input('market-category','value'),Input('game-information-data','data'))

    @scoped_callback(app,'market',Output('game-information-data','data'),Input('market-detail-context','data'),Input('workspace-page','value'),
                  Input('market-url','hash'),Input('market-category','value'),Input('game-information-refresh','n_clicks'),
                  Input('game-information-poll','n_intervals'),State('game-information-data','data'))
    def load_game_information(game,page,location,category,clicks,tick,previous):
        if not active(game,page,location) or (game['sport']!='NFL' and category!=ADDITIONAL_INFORMATION):raise PreventUpdate
        packet=service.load(game,news=category==ADDITIONAL_INFORMATION,
                            refresh=ctx.triggered_id=='game-information-refresh' and bool(clicks))
        return no_update if packet==previous else packet

    @scoped_callback(app,'market',Output('market-game-conditions','children'),Input('game-information-data','data'),
                  Input('market-detail-context','data'),Input('workspace-page','value'),Input('market-url','hash'))
    def game_header_conditions(packet,game,page,location):
        if not active(game,page,location) or game['sport']!='NFL':return []
        return conditions(packet,game)

    @scoped_callback(app,'market',Output('game-information-content','children'),Input('game-information-data','data'),
                  Input('market-detail-context','data'),Input('market-category','value'),Input('workspace-page','value'),Input('market-url','hash'))
    def game_additional_information(packet,game,category,page,location):
        if not active(game,page,location) or category!=ADDITIONAL_INFORMATION:raise PreventUpdate
        return contents(packet,game,service.information.now())
