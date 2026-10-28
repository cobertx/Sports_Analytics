"""Game-scoped public news and venue weather; no odds or model requests."""
import re
from .data import DataSourceError
from .information import LOCATIONS,SPORTS,timestamp

ADDITIONAL_INFORMATION='Additional Information'


def scope(game):
    return [game['sport'],str(game['id']),game.get('start'),
            game['away']['name'],game['home']['name']]


def name(value):
    text=re.sub(r'[^a-z0-9]+',' ',str(value).casefold()).strip()
    return re.sub(r'^la ','los angeles ',text)


def relevant_news(packets,game):
    """Union both team feeds and the league feed, with exact team matching."""
    sides=[game['away'],game['home']];found={}
    for packet in packets:
        if packet.get('scope',{}).get('sport')!=game['sport']:continue
        cached=packet.get('cached') or {}
        for item in cached.get('data',{}).get('items',[]):
            tags={name(team) for team in item.get('teams',[])}
            ids=set(map(str,item.get('team_ids',[])))
            headline=' '+name(item['headline'])+' '
            matched=[side['name'] for side in sides if
                name(side['name']) in tags or (side.get('id') and str(side['id']) in ids)
                or ' '+name(side['name'])+' ' in headline]
            if matched:
                previous=found.get(item['url'])
                found[item['url']]={**item,'matched_teams':sorted(set(matched+(previous or {}).get('matched_teams',[])))}
    return sorted(found.values(),key=lambda item:item.get('published') or '',reverse=True)


def roof(venue):
    value=venue.get('roof_type')
    if value=='retractable':return 'Retractable roof · game roof position not reported'
    if value in {'dome','fixed'}:return 'Domed / covered stadium'
    if value in {'outdoors','open'}:return 'Not domed · open-air stadium'
    if venue.get('indoor') is True:return 'Domed / covered stadium (ESPN indoor classification) · roof position not reported'
    if venue.get('indoor') is False:return 'Not domed · open-air stadium (ESPN classification)'
    return 'Dome / roof classification not reported'


def weather_point(venue):
    """Use supplied venue coordinates, otherwise label an area forecast."""
    address=venue.get('address') or {}
    country=str(address.get('country') or '').upper()
    if country not in {'USA','US','UNITED STATES','UNITED STATES OF AMERICA'}:return None
    lat,lon=venue.get('latitude'),venue.get('longitude')
    if (isinstance(lat,(int,float)) and not isinstance(lat,bool) and -90<=lat<=90 and
            isinstance(lon,(int,float)) and not isinstance(lon,bool) and -180<=lon<=180):
        return dict(latitude=lat,longitude=lon,label='Venue point forecast')
    city=address.get('city');state=address.get('state')
    # Orchard Park hosts Buffalo's stadium; this fallback is explicitly regional.
    city={'Orchard Park':'Buffalo'}.get(city,city)
    label=f'{city}, {state}'
    if label in LOCATIONS:
        lat,lon=LOCATIONS[label]
        return dict(latitude=lat,longitude=lon,label=label+' area forecast')
    return None


def kickoff_weather(packet,game):
    cached=(packet or {}).get('cached') or {};data=cached.get('data') or {}
    start=timestamp(game.get('start'))
    if not start or game.get('time_tbd'):return None
    for hour in data.get('hours',[]):
        left,right=timestamp(hour.get('startTime')),timestamp(hour.get('endTime'))
        if left and right and left<=start<right:return hour
    return None


class GameInformationService:
    def __init__(self,information):
        self.information=information

    def event(self,game,refresh=False):
        sport=game['sport'];game_id=str(game['id'])
        if sport not in SPORTS or not re.fullmatch(r'\d{1,12}',game_id):raise ValueError('Choose an NBA or NFL game.')
        def fetch():
            data=self.information.client.json(f'https://site.api.espn.com/apis/site/v2/sports/{SPORTS[sport]}/summary',params={'event':game_id})
            header=data.get('header') or {}
            if str(header.get('id'))!=game_id:raise DataSourceError('ESPN returned venue information for another game.')
            competitions=header.get('competitions') or []
            competition=competitions[0] if competitions else {}
            sides={c.get('homeAway'):c.get('team') or {} for c in competition.get('competitors',[])}
            for side in ('home','away'):
                actual=sides.get(side) or {};expected=game[side]
                if (expected.get('id') and str(actual.get('id'))!=str(expected['id']) or
                        not actual or name(actual.get('displayName'))!=name(expected['name'])):
                    raise DataSourceError('ESPN game teams do not match this matchup.')
            venue={**(competition.get('venue') or {}),**((data.get('gameInfo') or {}).get('venue') or {})}
            venue={k:v for k,v in venue.items() if k in {'id','fullName','address','indoor','latitude','longitude'}}
            venue_id=str(venue.get('id') or '')
            if sport=='NFL' and re.fullmatch(r'\d{1,10}',venue_id) and 'indoor' not in venue:
                try:
                    details=self.information.client.json(f'https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/venues/{venue_id}')
                    if str(details.get('id'))==venue_id:
                        venue={**venue,**{k:v for k,v in details.items() if k in {'indoor','latitude','longitude'}}}
                except DataSourceError:
                    pass  # An unknown roof stays unknown; retain the event venue.
            return dict(venue=venue,teams={side:{'name':game[side]['name'],'id':str(sides[side]['id'])}
                                          for side in sides if sides[side].get('id')},
                        source_url=f'https://www.espn.com/{sport.lower()}/game/_/gameId/{game_id}')
        return self.information.request(dict(kind='game',sport=sport,id=game_id,teams=[game['away']['name'],game['home']['name']]),fetch,3600,refresh)

    def load(self,game,*,news=False,refresh=False):
        metadata=self.event(game,refresh)
        saved=metadata.get('cached') or {};event=saved.get('data') or {}
        resolved={**game}
        for side in ('away','home'):
            resolved[side]={**game[side],**event.get('teams',{}).get(side,{})}
        # Event metadata owns the venue; a changed stadium must not inherit the
        # previous schedule venue's roof flag or coordinates.
        venue=event.get('venue') or game.get('venue_details') or {}
        packets=[]
        if news:
            packets.append(self.information.load('news',sport=game['sport'],refresh=refresh))
            for side in ('away','home'):
                team=resolved[side].get('id')
                if team:packets.append(self.information.load('news',sport=game['sport'],team=team,refresh=refresh))
        point=weather_point(venue) if game['sport']=='NFL' else None
        weather=(self.information.load('weather',sport='NFL',latitude=point['latitude'],longitude=point['longitude'],refresh=refresh)
                 if point else None)
        all_packets=[metadata,*packets,*([weather] if weather else [])]
        return dict(scope=scope(game),venue=venue,metadata=metadata,weather=weather,point=point,
                    news=packets,items=relevant_news(packets,resolved),
                    pending=any(p['pending'] for p in all_packets),
                    errors=list(dict.fromkeys(p['error'] for p in all_packets if p.get('error'))))
