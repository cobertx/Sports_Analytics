"""Attributed news headlines and public NWS forecasts, refreshed outside page requests."""
from datetime import datetime,timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import RLock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .cache_refresh import RefreshQueue
from .data import DataSourceError,HttpClient
from .nfl_model_data import atomic_text

ROOT=Path(__file__).resolve().parents[1]
SPORTS={'NBA':'basketball/nba','NFL':'football/nfl'}
PAGES={'NBA':[('injuries','Injury News'),('roster','Roster News')],
       'NFL':[('news','Injury & Roster News'),('weather','Weather News')]}
NWS='https://api.weather.gov'
INJURY=re.compile(r'\binjur\w*|\b(?:out|questionable|doubtful|concussion|ankle|knee|hamstring|surgery|rehab|illness|inactive|ruled|sidelined|ACL|Achilles|medical|fracture)\b',re.I)
ROSTER=re.compile(r'\b(?:roster|trade\w*|sign(?:s|ed|ing|ings)?|waiv\w*|releas\w*|acquir\w*|activat\w*|contract\w*|suspend\w*|suspension|retir\w*|practice.squad|free.agent\w*|cut|cuts|claimed)\b',re.I)

# Approximate city centers, explicitly presented as city forecasts, not stadium coordinates.
LOCATIONS={name:(lat,lon) for name,lat,lon in [
    ('Atlanta, GA',33.749,-84.388),('Baltimore, MD',39.29,-76.612),('Buffalo, NY',42.886,-78.879),
    ('Charlotte, NC',35.227,-80.843),('Chicago, IL',41.878,-87.63),('Cincinnati, OH',39.103,-84.512),
    ('Cleveland, OH',41.499,-81.694),('Denver, CO',39.739,-104.99),('Detroit, MI',42.331,-83.046),
    ('East Rutherford, NJ',40.833,-74.097),('Foxborough, MA',42.065,-71.248),('Glendale, AZ',33.538,-112.186),
    ('Green Bay, WI',44.513,-88.014),('Houston, TX',29.76,-95.37),('Indianapolis, IN',39.7684,-86.1581),
    ('Inglewood, CA',33.962,-118.353),('Jacksonville, FL',30.332,-81.656),('Kansas City, MO',39.1,-94.579),
    ('Landover, MD',38.934,-76.897),('Las Vegas, NV',36.17,-115.14),('Miami Gardens, FL',25.942,-80.246),
    ('Minneapolis, MN',44.978,-93.265),('Nashville, TN',36.163,-86.782),('New Orleans, LA',29.951,-90.072),
    ('Philadelphia, PA',39.953,-75.165),('Pittsburgh, PA',40.44,-79.996),('Santa Clara, CA',37.354,-121.955),
    ('Seattle, WA',47.606,-122.332),('Tampa, FL',27.951,-82.457),('Arlington, TX',32.736,-97.108)]}


def route(value):
    found=re.fullmatch(r'#(nba|nfl)-information-(injuries|roster|news|weather)',value or '')
    if found and found[2] in dict(PAGES[found[1].upper()]):return dict(sport=found[1].upper(),kind=found[2])
    return None


def timestamp(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError,ValueError):return None


def time_text(value):
    stamp=timestamp(value)
    return stamp.astimezone(ZoneInfo('America/Indianapolis')).strftime('%m/%d/%Y %H:%M %Z') if stamp else 'Time unavailable'


def safe_url(value,hosts):
    if not isinstance(value,str):return None
    parsed=urlparse(value)
    return value if parsed.scheme=='https' and parsed.hostname in hosts and not parsed.username else None


def official_sources(sport,now=None):
    if sport not in SPORTS:raise ValueError('Choose NBA or NFL.')
    day=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/Indianapolis'))
    season=day.year-(day.month<9)
    return ([dict(title='NBA official injury reports',url=f'https://official.nba.com/nba-injury-report-{season}-{str(season+1)[-2:]}-season/',kind='injuries',
                  note='League-published participation reports; open the latest timestamped report.'),
             dict(title='NBA official transactions',url='https://www.nba.com/players/transactions',kind='roster',note='League transaction log for signings, trades and other roster moves.')]
            if sport=='NBA' else [dict(title='NFL official injury reports',url='https://www.nfl.com/injuries/',kind='injuries',note='Practice participation and reported game status; select the appropriate week.'),
             dict(title='NFL official transactions',url='https://www.nfl.com/transactions/',kind='roster',note='League transaction log; choose the date and transaction category.')])


def news_items(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('articles'),list):raise DataSourceError('ESPN returned an invalid news response.')
    result=[];seen=set()
    for article in payload['articles']:
        if not isinstance(article,dict):continue
        headline=article.get('headline');links=article.get('links')
        web=links.get('web') if isinstance(links,dict) else None
        link=web.get('href') if isinstance(web,dict) else None
        link=safe_url(link,{'www.espn.com','espn.com','www.espn.co.uk'})
        if not isinstance(headline,str) or not headline.strip() or not link or link in seen:continue
        kinds=[kind for kind,pattern in [('injuries',INJURY),('roster',ROSTER)] if pattern.search(headline)]
        if not kinds:continue
        stamp=timestamp(article.get('published'));seen.add(link)
        teams=sorted({str(c.get('description')) for c in article.get('categories',[]) or [] if isinstance(c,dict) and c.get('type')=='team' and c.get('description')})
        result.append(dict(headline=headline.strip()[:300],url=link,published=stamp.isoformat() if stamp else None,
                           kinds=kinds,teams=teams,source='ESPN'))
    return sorted(result,key=lambda item:item['published'] or '',reverse=True)


class InformationService:
    def __init__(self,directory=None,client=None,now=None):
        self.directory=Path(directory or ROOT/'data/information_variable')
        self.client=client or HttpClient();self.now=now or (lambda:datetime.now(timezone.utc))
        self._refresh=RefreshQueue(workers=2);self.lock=RLock()

    def read(self,key):
        with self.lock:
            try:
                value=json.loads((self.directory/(key+'.json')).read_text(encoding='utf-8'))
                return value if isinstance(value,dict) and timestamp(value.get('fetched_at')) and isinstance(value.get('data'),dict) else None
            except (OSError,ValueError):return None

    def load(self,kind,sport='NFL',latitude=None,longitude=None,refresh=False):
        if sport not in SPORTS or kind not in {'news','weather'} or (kind=='weather' and sport!='NFL'):raise ValueError('Choose a supported information page.')
        if kind=='weather':
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (latitude,longitude)) or not -90<=latitude<=90 or not -180<=longitude<=180:
                raise ValueError('Choose valid latitude and longitude coordinates.')
            latitude,longitude=round(latitude,4),round(longitude,4)
        scope=dict(kind=kind,sport=sport,latitude=latitude if kind=='weather' else None,longitude=longitude if kind=='weather' else None)
        key=kind+'_'+sport.lower()+'_'+sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()[:16]
        cached=self.read(key)
        if cached and cached.get('scope')!=scope:cached=None
        age=(self.now()-timestamp(cached['fetched_at'])).total_seconds() if cached else float('inf')
        expired=not 0<=age<(300 if kind=='news' else 600)
        if refresh or expired:
            def work():
                data=dict(items=news_items(self.client.json(f'https://site.api.espn.com/apis/site/v2/sports/{SPORTS[sport]}/news',params={'limit':100}))) if kind=='news' else self.weather(latitude,longitude)
                saved=dict(scope=scope,fetched_at=self.now().isoformat(),data=data)
                with self.lock:atomic_text(self.directory/(key+'.json'),json.dumps(saved,allow_nan=False))
            state=self._refresh.request(key,work,force=refresh)
        else:state=self._refresh.state(key)
        return dict(scope=scope,cached=cached,pending=state['pending'],error=state['error'],stale=expired)

    def weather(self,latitude,longitude):
        headers={'User-Agent':'SportsAnalytics/0.1 (local research dashboard)','Accept':'application/geo+json'}
        point=self.client.json(f'{NWS}/points/{latitude},{longitude}',headers=headers)
        properties=point.get('properties',{}) if isinstance(point,dict) else {}
        forecast=safe_url(properties.get('forecastHourly'),{'api.weather.gov'}) if isinstance(properties,dict) else None
        if not forecast:raise DataSourceError('NWS has no hourly forecast for this point. Its coverage is U.S. locations; use the linked national weather services for international games.')
        response=self.client.json(forecast,headers=headers)
        raw=response.get('properties',{}) if isinstance(response,dict) else {}
        periods=raw.get('periods') if isinstance(raw,dict) else None
        if not isinstance(periods,list) or not periods:raise DataSourceError('NWS hourly forecast is temporarily unavailable.')
        hours=[]
        for p in periods:
            if not isinstance(p,dict) or not timestamp(p.get('startTime')) or not timestamp(p.get('endTime')):continue
            hours.append({key:p.get(key) for key in ['startTime','endTime','temperature','temperatureUnit','windSpeed','windDirection','shortForecast','probabilityOfPrecipitation']})
        if not hours:raise DataSourceError('NWS returned no valid forecast hours.')
        alerts=[];notice=''
        try:
            raw_alerts=self.client.json(f'{NWS}/alerts/active',params={'point':f'{latitude},{longitude}'},headers=headers)
            if not isinstance(raw_alerts.get('features'),list):raise DataSourceError('NWS alerts response is invalid.')
            for feature in raw_alerts['features']:
                props=feature.get('properties',{});link=safe_url(feature.get('id'),{'api.weather.gov','alerts.weather.gov'})
                if link:alerts.append(dict(headline=props.get('headline') or props.get('event'),event=props.get('event'),severity=props.get('severity'),
                    sent=props.get('sent'),ends=props.get('ends') or props.get('expires'),url=link))
        except DataSourceError:notice='Weather alerts could not be checked; this does not mean there are no active alerts.'
        relative=properties.get('relativeLocation',{}).get('properties',{})
        return dict(hours=hours[:72],alerts=alerts,notice=notice,issued_at=raw.get('updateTime') or raw.get('generatedAt'),
                    location=', '.join(str(relative[k]) for k in ['city','state'] if relative.get(k)),
                    source_url=f'https://forecast.weather.gov/MapClick.php?lat={latitude}&lon={longitude}')
