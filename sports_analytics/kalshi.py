"""Public, read-only Kalshi sports markets. No credentials or trading endpoints."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from urllib.parse import quote

from .data import DataSourceError, HttpClient
from .nfl_model_data import atomic_text
from .cache_refresh import RefreshQueue

BASE = 'https://external-api.kalshi.com/trade-api/v2'
DOCS = 'https://docs.kalshi.com/getting_started/quick_start_market_data'
SPORTS = {'NBA', 'NFL', 'NCAAF'}
CATEGORIES = ['Game Lines', 'Player Props', 'Passing Props', 'Receiving Props', 'Rushing Props',
              'Touchdowns & Scoring', 'Periods & Team Props', 'Other Markets']
ROOT = Path(__file__).resolve().parents[1]


def ticker(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Z0-9][A-Z0-9_.-]{0,199}', value))


def category(sport, event_ticker):
    family = event_ticker.split('-', 1)[0].removeprefix('KX' + sport)
    if family in {'GAME', 'SPREAD', 'TOTAL', 'TEAMTOTAL'}:
        return 'Game Lines'
    if sport == 'NBA' and family in {'PTS', 'REB', 'AST', 'PRA', 'PR', 'PA', 'RA', '3PT', 'STL', 'BLK', 'STOCK', '2D', '3D', 'DD'}:
        return 'Player Props'
    if sport != 'NBA':
        if 'PASS' in family: return 'Passing Props'
        if any(word in family for word in ['RSH', 'RUSH', 'RRYDS']): return 'Rushing Props'
        if 'REC' in family: return 'Receiving Props'
        if 'TD' in family or family in {'FG', 'SFTY'}: return 'Touchdowns & Scoring'
    if re.match(r'[1-4][HQ]', family) or family.startswith('TEAM'):
        return 'Periods & Team Props'
    return 'Other Markets'


def numeric(value):
    try:
        result = Decimal(str(value))
        return float(result) if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def price(row, key):
    # Dollar strings are authoritative; legacy integer-cent fields are a fallback.
    value = numeric(row.get(key + '_dollars')) if key + '_dollars' in row else numeric(row.get(key))
    if value is not None and key + '_dollars' not in row:
        value /= 100
    notional = numeric(row.get('notional_value_dollars')) or 1
    if value is None or not 0 <= value <= notional:
        return None
    size_key = key + '_size_fp'
    if key.endswith(('bid', 'ask')) and size_key in row and numeric(row[size_key]) == 0:
        return None
    return value


def contract(row, event, sport):
    identity = row.get('ticker')
    if not ticker(identity) or row.get('event_ticker', event['event_ticker']) != event['event_ticker']:
        raise DataSourceError('Kalshi returned a contract for an unexpected event.')
    result = dict(ticker=identity, event_ticker=event['event_ticker'], event_title=event.get('title', ''),
        category=category(sport, event['event_ticker']), title=row.get('title') or row.get('yes_sub_title') or identity,
        yes_label=row.get('yes_sub_title') or 'Yes', no_label=row.get('no_sub_title') or 'No',
        status=row.get('status', ''), notional=numeric(row.get('notional_value_dollars')) or 1,
        close_time=row.get('close_time'), metadata_updated=row.get('updated_time'),
        line=numeric(row.get('floor_strike')),
        rules_primary=row.get('rules_primary', ''), rules_secondary=row.get('rules_secondary', ''))
    for key in ('market_type','strike_type','cap_strike','subtitle'):
        result[key] = row.get(key)
    for key in ['yes_bid', 'yes_ask', 'no_bid', 'no_ask', 'last_price']:
        result[key] = price(row, key)
    for key in ['volume', 'volume_24h', 'open_interest', 'yes_bid_size', 'yes_ask_size']:
        result[key] = numeric(row.get(key + '_fp', row.get(key)))
    return result


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


class KalshiService:
    def __init__(self, directory=None, archive=None, client=None, now=None,background=False):
        # Exchange quotes are not model inputs and must not invalidate model replay caches.
        self.directory = Path(directory or ROOT / 'data/kalshi_market/.cache')
        self.archive = Path(archive or ROOT / 'data/kalshi_market')
        self.client = client or HttpClient()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._memory = OrderedDict()
        self._lock = RLock()
        self._requests = {}
        self.background=background
        self._refresh=RefreshQueue()

    @contextmanager
    def _request_slot(self, key):
        # Identical reads share one fetch; unrelated pages never wait on its HTTP.
        with self._lock:
            slot = self._requests.setdefault(key, [RLock(), 0])
            slot[1] += 1
        try:
            with slot[0]: yield
        finally:
            with self._lock:
                slot[1] -= 1
                if not slot[1]: del self._requests[key]

    def _remember(self, key, saved):
        with self._lock:
            self._memory[key] = saved
            self._memory.move_to_end(key)
            while len(self._memory) > 32:
                self._memory.popitem(last=False)

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory / 'public.sqlite3', timeout=30)
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched_at TEXT, payload TEXT)')
        return db

    def _request(self, path, params, ttl=2, refresh=False):
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        def read_saved():
            with self._lock: saved = self._memory.get(key)
            if saved is None:
                with closing(self._connect()) as db:
                    row = db.execute('SELECT fetched_at,payload FROM responses WHERE key=?', (key,)).fetchone()
                if row:
                    try: saved = dict(fetched_at=row[0], data=json.loads(row[1]), stale=False, notice='')
                    except ValueError: pass
            if saved is not None:self._remember(key,saved)
            return saved
        if self.background and not refresh:
            saved=read_saved()
            stamp=parse_time(saved['fetched_at']) if saved else None
            if saved and stamp and 0<=(self.now()-stamp).total_seconds():
                if (self.now()-stamp).total_seconds()<ttl:return saved
                def update():
                    result=self._request(path,params,ttl,refresh=True)
                    if result['stale']:raise DataSourceError(result['notice'])
                state=self._refresh.request(key,update)
                return {**saved,'stale':True,'updating':not bool(state['error']),
                        'notice':state['error'] or 'Saved Kalshi data shown; updating in the background.'}
        with self._request_slot(key):
            saved=read_saved()
            stamp = parse_time(saved['fetched_at']) if saved else None
            age = (self.now() - stamp).total_seconds() if stamp else float('inf')
            if saved and not refresh and 0 <= age < ttl:
                self._remember(key, saved)
                return saved
            try:
                data = self.client.json(BASE + path, params=params)
                if not isinstance(data, dict) or 'error' in data or not isinstance(data.get('events'), list):
                    raise DataSourceError('Kalshi returned an invalid market-data response.')
                saved = dict(data=data, fetched_at=self.now().isoformat(timespec='milliseconds'), stale=False, notice='')
                with closing(self._connect()) as db, db:
                    db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?)',
                               (key, saved['fetched_at'], json.dumps(data, allow_nan=False)))
            except DataSourceError as exc:
                if not saved:
                    raise DataSourceError(f'Kalshi public data unavailable. {exc}') from None
                saved = {**saved, 'stale': True, 'notice': 'Refresh failed; showing previously fetched Kalshi data. ' + str(exc)}
            self._remember(key, saved)
            return saved

    def _events(self, params, ttl, refresh=False,updates=None):
        events, milestones, stamps, notices = {}, {}, [], []
        cursor, seen = '', set()
        for _ in range(30):
            response = self._request('/events', {**params, **({'cursor': cursor} if cursor else {})}, ttl, refresh)
            if updates is not None and response.get('updating'):updates.append(True)
            data = response['data']
            if not isinstance(data.get('events'), list):
                raise DataSourceError('Kalshi did not return an event list.')
            for event in data['events']:
                if not ticker(event.get('event_ticker')):
                    raise DataSourceError('Kalshi returned an invalid event ticker.')
                events[event['event_ticker']] = event
            for milestone in data.get('milestones', []):
                milestones[milestone.get('id')] = milestone
            stamps.append(response['fetched_at'])
            if response['notice']: notices.append(response['notice'])
            cursor = data.get('cursor') or ''
            if not cursor: break
            if cursor in seen: raise DataSourceError('Kalshi repeated a pagination cursor; refresh to retry.')
            seen.add(cursor)
        else:
            raise DataSourceError('Kalshi returned too many event pages; refresh to retry.')
        return list(events.values()), list(milestones.values()), min(stamps), list(dict.fromkeys(notices))

    def games(self, sport, days=7, refresh=False):
        if sport not in SPORTS or days not in {1, 7, 14}:
            raise ValueError('Choose NBA, NFL or NCAAF and a 1, 7 or 14 day window.')
        prefix = f'KX{sport}'
        updates=[]
        events, milestones, stamp, notices = self._events(dict(series_ticker=prefix + 'GAME', status='open',
            with_milestones='true', limit=200), ttl=60, refresh=refresh,updates=updates)
        by_event = {t: m for m in milestones for t in [*m.get('related_event_tickers', []), *m.get('primary_event_tickers', [])]}
        games = []
        lower, upper = self.now() - timedelta(hours=12), self.now() + timedelta(days=days)
        for event in events:
            identity = event['event_ticker']
            if not identity.startswith(prefix + 'GAME-'): continue
            milestone = by_event.get(identity, {})
            start = parse_time(milestone.get('start_date'))
            if start and not lower <= start <= upper: continue
            # Exact provider links, never fuzzy team-name matching across games.
            related = [t for t in milestone.get('related_event_tickers', []) if ticker(t) and t.startswith(prefix)]
            games.append(dict(sport=sport, id=identity, title=event.get('title') or milestone.get('title') or identity,
                matchup_title=milestone.get('title') or event.get('title') or identity,
                start=start.isoformat() if start else None, events=sorted(set([identity, *related])),
                milestone_id=milestone.get('id')))
        return dict(sport=sport, games=sorted(games, key=lambda g: (g['start'] or '9999', g['title'])),
                    fetched_at=stamp, notices=notices,updating=bool(updates))

    def quotes(self, game, selected='Game Lines', refresh=False, save=False):
        sport = game.get('sport')
        if sport not in SPORTS or selected not in CATEGORIES or not ticker(game.get('id')) or not game['id'].startswith(f'KX{sport}GAME-'):
            raise ValueError('Choose a Kalshi game and market category.')
        tickers = sorted({t for t in game.get('events', []) if ticker(t) and t.startswith(f'KX{sport}') and category(sport, t) == selected})
        if not tickers:
            return dict(sport=sport, game_id=game['id'], category=selected, rows=[], fetched_at=None, notices=[], saved=None)
        rows, stamps, notices, updates = {}, [], [], []
        # Limit URLs as well as results. A batch retains exact event linkage.
        for start in range(0, len(tickers), 20):
            batch = tickers[start:start + 20]
            events, _, stamp, warnings = self._events(dict(tickers=','.join(batch), with_nested_markets='true', limit=200),
                                                     ttl=2, refresh=refresh,updates=updates)
            stamps.append(stamp); notices.extend(warnings)
            for event in events:
                if event['event_ticker'] not in batch:
                    raise DataSourceError('Kalshi returned an event outside the selected game.')
                if not isinstance(event.get('markets'), list):
                    raise DataSourceError('Kalshi did not include contracts for this event.')
                for raw in event['markets']:
                    row = contract(raw, event, sport)
                    rows[row['ticker']] = row
        result = dict(sport=sport, game_id=game['id'], category=selected, rows=list(rows.values()),
                      fetched_at=min(stamps), notices=list(dict.fromkeys(notices)), saved=None,updating=bool(updates))
        if save:
            result['saved'] = self.save(result)
        return result

    def all_quotes(self, game, refresh=False, save=False):
        """Use the same exact game linkage and cache for a deliberate filter scan."""
        selected = [c for c in CATEGORIES if any(category(game['sport'], event)==c for event in game['events'])]
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='kalshi-filter') as pool:
            snapshots = list(pool.map(lambda c:self.quotes(game,c,refresh=refresh,save=save),selected))
        return dict(sport=game['sport'],game_id=game['id'],category='All Markets',
                    rows=[row for snapshot in snapshots for row in snapshot['rows']],
                    fetched_at=min((s['fetched_at'] for s in snapshots if s['fetched_at']),default=None),
                    notices=list(dict.fromkeys(n for s in snapshots for n in s['notices'])),saved=None,
                    updating=any(s.get('updating') for s in snapshots))

    def save(self, snapshot):
        directory = self.archive / snapshot['sport'].lower() / snapshot['game_id'] / snapshot['category'].replace(' ', '_')
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:12]
        stem = self.now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + digest
        atomic_text(directory / (stem + '.json'), json.dumps(snapshot, ensure_ascii=False, indent=2))
        buffer = io.StringIO()
        if snapshot['rows']:
            writer = csv.DictWriter(buffer, fieldnames=['sport', 'game_id', 'quote_fetched_at', 'price_unit', *snapshot['rows'][0]])
            writer.writeheader()
            writer.writerows(dict(sport=snapshot['sport'], game_id=snapshot['game_id'], quote_fetched_at=snapshot['fetched_at'],
                                  price_unit='USD per contract', **row)
                             for row in snapshot['rows'])
        atomic_text(directory / (stem + '.csv'), buffer.getvalue())
        return str(directory / (stem + '.csv'))

    def history(self, game, selected):
        if game.get('sport') not in SPORTS or not ticker(game.get('id')) or selected not in CATEGORIES:
            raise ValueError('Choose a Kalshi game and category.')
        directory = self.archive / game['sport'].lower() / game['id'] / selected.replace(' ', '_')
        return [p.name for p in sorted(directory.glob('*.json'), reverse=True)]

    def saved(self, game, selected, filename):
        if (game.get('sport') not in SPORTS or not ticker(game.get('id')) or selected not in CATEGORIES or
                not re.fullmatch(r'\d{8}T\d{12}Z_[a-f0-9]{12}\.json', filename or '')):
            raise ValueError('Choose an available Kalshi snapshot.')
        path = self.archive / game['sport'].lower() / game['id'] / selected.replace(' ', '_') / filename
        result = json.loads(path.read_text(encoding='utf-8'))
        if (result.get('sport'), result.get('game_id'), result.get('category')) != (game['sport'], game['id'], selected):
            raise DataSourceError('The saved Kalshi snapshot does not match this game and category.')
        return result

    @staticmethod
    def url(event_ticker):
        series = event_ticker.split('-', 1)[0]
        slug = {'KXNFLGAME': 'nfl-game', 'KXNBAGAME': 'nba-game', 'KXNCAAFGAME': 'college-football-game'}.get(series, 'sports')
        return f'https://kalshi.com/markets/{quote(series.lower(), safe="")}/{slug}/{quote(event_ticker.lower(), safe="")}'
