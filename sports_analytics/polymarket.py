"""Public Gamma discovery and CLOB book reads; no wallet or trading endpoints."""
from collections import OrderedDict
from contextlib import closing,contextmanager
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

import requests

from .data import DataSourceError, HttpClient
from .kalshi import CATEGORIES, numeric, parse_time
from .nfl_model_data import atomic_text
from .cache_refresh import RefreshQueue

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DOCS = 'https://docs.polymarket.com/market-data/discover-markets'
SPORTS = {'NBA': 'nba', 'NFL': 'nfl', 'NCAAF': 'cfb'}
ROOT = Path(__file__).resolve().parents[1]


def category(market):
    kind = str(market.get('sportsMarketType') or '').lower()
    if re.match(r'(first_|second_|q[1-4]_|[1-4][hq])', kind): return 'Periods & Team Props'
    if kind in {'moneyline', 'spreads', 'totals', 'team_totals'}: return 'Game Lines'
    if 'pass' in kind: return 'Passing Props'
    if 'receiv' in kind or 'reception' in kind: return 'Receiving Props'
    if 'rush' in kind: return 'Rushing Props'
    if 'touchdown' in kind or kind in {'anytime_td', 'first_td', 'last_td'}: return 'Touchdowns & Scoring'
    if kind in {'points', 'rebounds', 'assists', 'threes', 'blocks', 'steals', 'turnovers',
                'points_rebounds_assists', 'points_rebounds', 'points_assists', 'rebounds_assists'}: return 'Player Props'
    if kind.startswith('team_'): return 'Periods & Team Props'
    return 'Other Markets'


def array(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def book_prices(book):
    """Use actual positive-size levels, independent of provider array ordering."""
    def best(side, choose):
        levels = book.get(side, [])
        if not isinstance(levels, list): raise DataSourceError('Invalid Polymarket order book.')
        prices = []
        for level in levels:
            if not isinstance(level, dict): raise DataSourceError('Invalid Polymarket price level.')
            price, size = numeric(level.get('price')), numeric(level.get('size'))
            if price is None or not 0 <= price <= 1 or size is None or size < 0:
                raise DataSourceError('Invalid Polymarket price level.')
            if size > 0: prices.append(price)
        return choose(prices) if prices else None
    last = numeric(book.get('last_trade_price'))
    return best('bids', max), best('asks', min), last if last is not None and 0 <= last <= 1 else None


class PolymarketService:
    def __init__(self, directory=None, archive=None, client=None, now=None,background=False):
        self.directory = Path(directory or ROOT/'data/polymarket_market/.cache')
        self.archive = Path(archive or ROOT/'data/polymarket_market')
        self.client = client or HttpClient()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._memory = OrderedDict()
        self._lock = RLock()
        self._requests={}
        self.background=background
        self._refresh=RefreshQueue()

    @contextmanager
    def _request_slot(self,key):
        with self._lock:
            slot=self._requests.setdefault(key,[RLock(),0]);slot[1]+=1
        try:
            with slot[0]:yield
        finally:
            with self._lock:
                slot[1]-=1
                if not slot[1]:del self._requests[key]

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory/'public.sqlite3', timeout=30)
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched_at TEXT, payload TEXT)')
        return db

    def _request(self, url, params=None, *, body=None, ttl=2, refresh=False):
        key = hashlib.sha256(json.dumps([url, params, body], sort_keys=True).encode()).hexdigest()
        def validate(data):
            if url == CLOB+'/books':
                if not isinstance(data, list): raise DataSourceError('Invalid Polymarket book response.')
                expected = {item['token_id'] for item in body}
                seen = set()
                for book in data:
                    if not isinstance(book, dict) or not str(book.get('asset_id', '')).isdigit():
                        raise DataSourceError('Invalid Polymarket outcome identifier.')
                    asset = str(book['asset_id'])
                    if asset not in expected or asset in seen:
                        raise DataSourceError('Polymarket returned an unexpected or duplicate outcome book.')
                    seen.add(asset)
                    book_prices(book)
            elif url.endswith('/events/keyset'):
                if not isinstance(data, dict) or not isinstance(data.get('events'), list):
                    raise DataSourceError('Invalid Polymarket event response.')
                for event in data['events']:
                    if (not isinstance(event, dict) or not str(event.get('id','')).isdigit() or
                            not isinstance(event.get('markets',[]),list) or
                            not all(isinstance(m,dict) for m in event.get('markets',[])) or
                            not all(str(m.get('id','')).isdigit() for m in event.get('markets',[])) or
                            not isinstance(event.get('teams',[]),list) or
                            not all(isinstance(t,dict) for t in event.get('teams',[]))):
                        raise DataSourceError('Invalid Polymarket event metadata.')
            elif not isinstance(data, list):
                raise DataSourceError('Invalid Polymarket sports response.')
            elif not all(isinstance(row,dict) for row in data):
                raise DataSourceError('Invalid Polymarket sports metadata.')
        def read_saved():
            with self._lock:saved = self._memory.get(key)
            if saved is None:
                with closing(self._connect()) as db:
                    row = db.execute('SELECT fetched_at,payload FROM responses WHERE key=?', (key,)).fetchone()
                if row:
                    try:
                        data = json.loads(row[1]); validate(data)
                        saved = dict(data=data, fetched_at=row[0], notice='')
                    except (ValueError, DataSourceError): pass
            if saved is not None:self._remember(key,saved)
            return saved
        if self.background and not refresh:
            saved=read_saved()
            stamp=parse_time(saved['fetched_at']) if saved else None
            if saved and stamp and 0<=(self.now()-stamp).total_seconds():
                if (self.now()-stamp).total_seconds()<ttl:return saved
                def update():
                    result=self._request(url,params,body=body,ttl=ttl,refresh=True)
                    if result['notice']:raise DataSourceError(result['notice'])
                state=self._refresh.request(key,update)
                return {**saved,'updating':not bool(state['error']),
                        'notice':state['error'] or 'Saved Polymarket data shown; updating in the background.'}
        with self._request_slot(key):
            saved=read_saved()
            stamp = parse_time(saved['fetched_at']) if saved else None
            if saved and stamp and not refresh and 0 <= (self.now()-stamp).total_seconds() < ttl:
                self._remember(key, saved)
                return saved
            try:
                if body is None:
                    data = self.client.json(url, params=params)
                else:
                    # This POST only reads order books. No order/signing API is used.
                    response = self.client.session.post(url, json=body, timeout=(5, 20), allow_redirects=False)
                    if not response.ok: raise DataSourceError(f'Polymarket book data returned HTTP {response.status_code}.')
                    data = response.json()
                validate(data)
                saved = dict(data=data, fetched_at=self.now().isoformat(timespec='milliseconds'), notice='')
                with closing(self._connect()) as db, db:
                    db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?)',
                               (key, saved['fetched_at'], json.dumps(data, allow_nan=False)))
            except (DataSourceError, requests.RequestException, ValueError) as exc:
                message = str(exc) if isinstance(exc, DataSourceError) else 'The public data source could not be reached.'
                if message.startswith('Access was denied.'):
                    message = 'Polymarket public data access was denied for this connection.'
                if not saved: raise DataSourceError('Polymarket public data unavailable. '+message) from None
                saved = {**saved, 'notice':'Refresh failed; showing previously fetched Polymarket data. '+message}
            self._remember(key, saved)
            return saved

    def _remember(self, key, result):
        with self._lock:
            self._memory[key] = result
            self._memory.move_to_end(key)
            while len(self._memory) > 32: self._memory.popitem(last=False)

    def _events(self, params, refresh=False,updates=None):
        events, notices, stamps, seen = {}, [], [], set()
        cursor = None
        for _ in range(30):
            response = self._request(GAMMA+'/events/keyset',
                {**params, 'limit':100, **({'after_cursor':cursor} if cursor else {})}, ttl=60, refresh=refresh)
            if updates is not None and response.get('updating'):updates.append(True)
            for event in response['data']['events']:
                if not isinstance(event, dict) or not str(event.get('id', '')).isdigit():
                    raise DataSourceError('Invalid Polymarket event identifier.')
                events[str(event['id'])] = event
            stamps.append(response['fetched_at'])
            if response['notice']: notices.append(response['notice'])
            cursor = response['data'].get('next_cursor')
            if not cursor: break
            if cursor in seen: raise DataSourceError('Polymarket repeated an event cursor. Refresh to retry.')
            seen.add(cursor)
        else: raise DataSourceError('Polymarket returned too many event pages. Choose a shorter window.')
        return list(events.values()), min(stamps), list(dict.fromkeys(notices))

    def games(self, sport, days=7, refresh=False):
        if sport not in SPORTS or days not in {1,7,14}: raise ValueError('Choose a sport and a 1, 7 or 14 day window.')
        metadata = self._request(GAMMA+'/sports', ttl=3600, refresh=refresh)
        league = next((row for row in metadata['data'] if row.get('sport')==SPORTS[sport]), None)
        if not league or not str(league.get('series', '')).isdigit():
            raise DataSourceError('Polymarket has no current event series for this sport.')
        lower, upper = self.now()-timedelta(hours=12), self.now()+timedelta(days=days)
        # Stable request bounds permit reuse; filter against the exact window below.
        query_lower=self.now().replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=1)
        query_upper=upper.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        updates=[]
        events, stamp, notices = self._events(dict(series_id=str(league['series']), closed='false',
            start_time_min=query_lower.isoformat(), start_time_max=query_upper.isoformat(), order='startTime', ascending='true'), refresh,updates)
        if metadata['notice']: notices.append(metadata['notice'])
        grouped = {}
        for event in events:
            start = parse_time(event.get('startTime'))
            game_id = str(event.get('gameId') or '')
            if (not game_id.isdigit() or not start or not lower <= start <= upper or
                    not str(event.get('slug','')).startswith(SPORTS[sport]+'-')): continue
            if event.get('closed') or not any(not m.get('closed') and not m.get('archived') for m in event.get('markets', [])): continue
            grouped.setdefault(game_id, []).append(event)
        games = []
        for identity, related in grouped.items():
            main = min(related, key=lambda e:(len(e.get('slug','')), str(e['id'])))
            teams = {t.get('ordering'):t for t in main.get('teams', []) if isinstance(t, dict)}
            title = main.get('title') or main['slug']
            names = {side:team.get('alias') or team.get('name') for side,team in teams.items()}
            matchup_title = f"{names['away']} at {names['home']}" if names.get('home') and names.get('away') else title
            choices = {category(m) for e in related for m in e.get('markets', []) if not m.get('closed')}
            games.append(dict(sport=sport, id=str(main['id']), provider_game_id=identity, series_id=str(league['series']),
                slug=main['slug'], title=title, matchup_title=matchup_title, start=main['startTime'],
                teams=[{k:t.get(k) for k in ['name','alias','abbreviation','ordering','logo']} for t in main.get('teams',[])],
                events=[str(e['id']) for e in related], categories=[c for c in CATEGORIES if c in choices]))
        return dict(sport=sport, games=sorted(games,key=lambda g:(g['start'],g['id'])), fetched_at=stamp, notices=notices,updating=bool(updates))

    def quotes(self, game, selected='Game Lines', refresh=False, save=False):
        self._validate_game(game, selected)
        updates=[]
        events, metadata_stamp, notices = self._events(dict(game_id=game['provider_game_id'],
            series_id=game['series_id'], closed='false'), refresh,updates)
        rows, tokens = [], set()
        for event in events:
            if str(event.get('gameId')) != game['provider_game_id'] or not event.get('slug','').startswith(SPORTS[game['sport']]+'-'):
                raise DataSourceError('Polymarket returned an event outside this game.')
            for market in event.get('markets', []):
                selected_category=category(market)
                if market.get('closed') or market.get('archived') or (selected!='All Markets' and selected_category!=selected): continue
                labels = array(market.get('outcomes'))
                assets = array(market.get('positionIds') if market.get('version')=='v2' else market.get('clobTokenIds'))
                if len(labels)!=2:
                    notices.append('A market without two explicit outcome labels was excluded.'); continue
                valid = len(assets)==2 and len(set(map(str,assets)))==2 and all(str(t).isdigit() for t in assets)
                if not valid:
                    assets = [None,None]; notices.append('Some markets have no outcome identifiers; their quotes are unavailable.')
                tradable = market.get('enableOrderBook') is not False and market.get('acceptingOrders') is not False
                if tradable: tokens.update(str(t) for t in assets if t is not None)
                kind = market.get('sportsMarketType') or 'Other markets'
                question = market.get('question') or market.get('slug') or str(market['id'])
                section = kind.replace('_',' ').title()
                if selected_category in {'Player Props','Passing Props','Receiving Props','Rushing Props','Touchdowns & Scoring'}:
                    section = question.split(':',1)[0]+' - '+section if ':' in question else question
                group_key=hashlib.sha256(section.encode()).hexdigest()[:16]
                rows.append(dict(ticker=str(market['id']), event_ticker=str(event['id'])+'-'+group_key,
                    event_title=section, section=section, category=selected_category, title=question,sports_market_type=kind,
                    event_id=str(event['id']),condition_id=market.get('conditionId'),market_version=market.get('version'),
                    yes_label=str(labels[0]), no_label=str(labels[1]), yes_token=assets[0], no_token=assets[1],
                    tradable=tradable, status='Open' if tradable else 'Not accepting orders', notional=1,
                    rules_primary=market.get('description') or '', rules_secondary=market.get('resolutionSource') or '',
                    line=numeric(market.get('line')), volume=numeric(market.get('volume')),
                    volume_24h=numeric(market.get('volume24hr')), open_interest=None,
                    yes_bid=None,yes_ask=None,no_bid=None,no_ask=None,last_price=None,
                    metadata_updated=market.get('updatedAt'),metadata_fetched_at=metadata_stamp,
                    url=self.url(market.get('slug') or game['slug'])))
        books, stamps = {}, []
        tokens = sorted(tokens)
        for offset in range(0,len(tokens),100):
            batch = tokens[offset:offset+100]
            try:
                response = self._request(CLOB+'/books', body=[dict(token_id=t) for t in batch], refresh=refresh)
                if response.get('updating'):updates.append(True)
                for book in response['data']:
                    if str(book['asset_id']) not in batch: raise DataSourceError('Polymarket returned an unexpected outcome book.')
                    books[str(book['asset_id'])] = book
                stamps.append(response['fetched_at'])
                if response['notice']: notices.append(response['notice'])
            except DataSourceError as exc: notices.append(str(exc))
        missing = len(set(tokens)-books.keys())
        if missing: notices.append(f'{missing} outcome books are unavailable; missing quotes are left blank.')
        for row in rows:
            if not row['tradable']: continue
            for side in ['yes','no']:
                book = books.get(str(row[side+'_token']))
                if not book: continue
                row[side+'_bid'],row[side+'_ask'],last = book_prices(book)
                row[side+'_book_timestamp'] = book.get('timestamp')
                if side=='yes': row['last_price']=last
        result = dict(sport=game['sport'],game_id=game['id'],category=selected,rows=rows,
            fetched_at=min(stamps) if stamps else None, metadata_fetched_at=metadata_stamp,
            notices=list(dict.fromkeys(notices)),saved=None,updating=bool(updates))
        if save: result['saved']=self.save(result)
        return result

    def all_quotes(self, game, refresh=False, save=False):
        """Read metadata once and batch all outcome books; archive by normal category."""
        snapshot=self.quotes(game,'All Markets',refresh=refresh)
        if save:
            for selected in dict.fromkeys(row['category'] for row in snapshot['rows']):
                self.save({**snapshot,'category':selected,'rows':[row for row in snapshot['rows'] if row['category']==selected]})
        return snapshot

    @staticmethod
    def _validate_game(game, selected):
        if (game.get('sport') not in SPORTS or selected not in (*CATEGORIES,'All Markets') or
                not all(isinstance(game.get(k),str) and game[k].isdigit() for k in ['id','provider_game_id','series_id'])):
            raise ValueError('Choose a Polymarket game and category.')

    def save(self, snapshot):
        directory=self.archive/snapshot['sport'].lower()/snapshot['game_id']/snapshot['category'].replace(' ','_')
        digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()[:12]
        stem=self.now().strftime('%Y%m%dT%H%M%S%fZ')+'_'+digest
        atomic_text(directory/(stem+'.json'),json.dumps(snapshot,ensure_ascii=False,indent=2))
        buffer=io.StringIO()
        if snapshot['rows']:
            fields=list(dict.fromkeys(key for row in snapshot['rows'] for key in row))
            writer=csv.DictWriter(buffer,fieldnames=['sport','game_id','quote_fetched_at','price_unit',*fields])
            writer.writeheader()
            writer.writerows(dict(sport=snapshot['sport'],game_id=snapshot['game_id'],quote_fetched_at=snapshot['fetched_at'],
                                 price_unit='USD per outcome share',**row) for row in snapshot['rows'])
        atomic_text(directory/(stem+'.csv'),buffer.getvalue())
        return str(directory/(stem+'.csv'))

    def history(self, game, selected):
        self._validate_game(game, selected)
        directory=self.archive/game['sport'].lower()/game['id']/selected.replace(' ','_')
        return [p.name for p in sorted(directory.glob('*.json'),reverse=True)]

    def saved(self, game, selected, filename):
        self._validate_game(game, selected)
        if not re.fullmatch(r'\d{8}T\d{12}Z_[a-f0-9]{12}\.json',filename or ''): raise ValueError('Choose a saved Polymarket snapshot.')
        path=self.archive/game['sport'].lower()/game['id']/selected.replace(' ','_')/filename
        result=json.loads(path.read_text(encoding='utf-8'))
        if (result.get('sport'),result.get('game_id'),result.get('category'))!=(game['sport'],game['id'],selected):
            raise DataSourceError('The saved Polymarket snapshot belongs to another game or category.')
        return result

    @staticmethod
    def url(slug):
        if not isinstance(slug,str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',slug): return 'https://polymarket.com/sports'
        return 'https://polymarket.com/event/'+slug
