"""Public exchange activity, bounded background reads and reusable local snapshots."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock

from .cache_refresh import RefreshQueue
from .data import DataSourceError, HttpClient
from .kalshi import BASE, CATEGORIES, KalshiService, numeric, parse_time, ticker
from .nfl_model_data import atomic_text
from .polymarket import CLOB, PolymarketService

DATA = 'https://data-api.polymarket.com/v2'
PROVIDERS = ('FanDuel', 'Kalshi', 'Polymarket')
MAX_PAGES = 4
CACHE_VERSION = 4


def validate(value):
    if not isinstance(value, dict) or value.get('provider') not in PROVIDERS or value.get('sport') not in {'NBA','NFL','NCAAF'}:
        raise ValueError('Choose a supported market.')
    game = value.get('game') or {}
    if not parse_time(game.get('start')):
        raise ValueError('A scheduled start is required to match the same game across sources.')
    for side in ('away','home'):
        name = (game.get(side) or {}).get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 150:
            raise ValueError('Both matchup teams are required.')
    focus = value.get('focus');reference = value.get('reference')
    for key in (focus, reference):
        if key is not None and (not isinstance(key, list) or len(key) != 6
                or not all(isinstance(key[i], str) and len(key[i]) < 200 for i in (0,1,2,4))
                or not isinstance(key[3], (int,float)) or isinstance(key[3], bool) or numeric(key[3]) is None or not isinstance(key[5], bool)):
            raise ValueError('This market definition is unavailable.')
    category = value.get('category')
    window = value.get('window', 'all')
    if window not in {'all', '24h'}:
        raise ValueError('Choose available history or the last 24 hours.')
    return dict(sport=value['sport'], game=dict(start=game['start'],
        home=dict(name=game['home']['name']), away=dict(name=game['away']['name'])),
        focus=focus, reference=reference, provider=value['provider'], ticker=str(value.get('ticker', ''))[:200],
        label=str(value.get('label') or 'Market')[:300], category=category if category in CATEGORIES else 'Other Markets', window=window)


def market_category(request):
    if not request['focus']:
        return request['category']
    key = request['focus'][1]
    if not key.startswith('player_'):
        return 'Game Lines'
    if request['sport'] == 'NBA':
        return 'Player Props'
    if 'pass' in key:
        return 'Passing Props'
    if 'reception' in key or 'receiv' in key:
        return 'Receiving Props'
    if 'rush' in key:
        return 'Rushing Props'
    return 'Touchdowns & Scoring'


def decimal(value):
    number = numeric(value)
    return Decimal(str(value)) if number is not None else None


def book_levels(levels, *, cents=False):
    if not isinstance(levels, list):
        raise DataSourceError('The exchange returned an invalid order book.')
    result = []
    for level in levels:
        if isinstance(level, dict):
            price, size = numeric(level.get('price')), numeric(level.get('size'))
        elif isinstance(level, list) and len(level) >= 2:
            price, size = numeric(level[0]), numeric(level[1])
        else:
            raise DataSourceError('The exchange returned an invalid price level.')
        if cents and price is not None:
            price /= 100
        if price is None or size is None or not 0 <= price <= 1 or size < 0:
            raise DataSourceError('The exchange returned an invalid price or quantity.')
        if size > 0:
            result.append(dict(price=price, size=size))
    return result


def book_summary(levels):
    return dict(contracts=float(sum((Decimal(str(row['size'])) for row in levels), Decimal(0))),
        dollars=float(sum((Decimal(str(row['price']))*Decimal(str(row['size'])) for row in levels), Decimal(0))),
        levels=sorted(levels, key=lambda row:row['price'], reverse=True)[:10])


def trade_summary(rows, provider, market, lower, upper):
    totals = {side:dict(prints=0, contracts=Decimal(0), dollars=Decimal(0), buys=0, sells=0,
                       buy_dollars=Decimal(0), sell_dollars=Decimal(0),
                       buy_contracts=Decimal(0), sell_contracts=Decimal(0)) for side in ('yes','no')}
    unknown = outside = invalid = 0
    seen = set(); history = []
    for row in rows:
        if provider == 'Kalshi':
            if row.get('ticker') != market['ticker']:
                raise DataSourceError('Kalshi returned a trade for a different contract.')
            identity = row.get('trade_id')
            if not identity:
                invalid += 1; continue
            if identity in seen:
                continue
            seen.add(identity)
            stamp = parse_time(row.get('created_time'))
            outcome = row.get('taker_outcome_side')
            book_side = row.get('taker_book_side')
            book_outcome = {'bid':'yes','ask':'no'}.get(book_side)
            if ((outcome is not None and outcome not in {'yes','no'})
                    or (book_side is not None and book_outcome is None)
                    or (outcome and book_outcome and outcome != book_outcome)):
                invalid += 1; continue
            side = outcome or book_outcome or row.get('taker_side')
            size = decimal(row.get('count_fp', row.get('count')))
            # Canonical trade direction does not flip its Yes-price basis.
            price = decimal(row.get('yes_price_dollars'))
            if price is None and row.get('yes_price') is not None:
                price = decimal(row['yes_price'])/100 if decimal(row['yes_price']) is not None else None
            action = 'BUY'
        else:
            if row.get('condition_id') != market['condition_id']:
                raise DataSourceError('Polymarket returned a trade for a different market.')
            try:
                stamp = datetime.fromtimestamp(float(row.get('timestamp')), tz=timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                stamp = None
            side = next((s for s in ('yes','no') if str(row.get('token_id')) == str(market.get(s+'_token'))), None)
            price, size, action = decimal(row.get('price')), decimal(row.get('size')), row.get('side')
            if action not in {'BUY','SELL'}:
                invalid += 1; continue
        if not stamp or price is None or size is None or not 0 <= price <= 1 or size <= 0:
            invalid += 1; continue
        if not lower <= stamp <= upper:
            outside += 1; continue
        if side not in totals:
            unknown += 1; continue
        total = totals[side];total['prints'] += 1;total['contracts'] += size;total['dollars'] += price*size
        if provider == 'Kalshi':
            history.append(dict(time=stamp.isoformat(), yes_price=float(price), contracts=float(size), side=side))
        if action:
            # Kalshi reports a common Yes-price basis, including No takers.
            # Direction can also represent selling the opposite outcome. These
            # legacy buy_* keys store direction-priced value, not cash paid.
            outcome_price = 1-price if provider == 'Kalshi' and side == 'no' else price
            total['buys' if action == 'BUY' else 'sells'] += 1
            total['buy_dollars' if action == 'BUY' else 'sell_dollars'] += outcome_price*size
            total['buy_contracts' if action == 'BUY' else 'sell_contracts'] += size
    return dict(accounting_version=2,sides={side:{key:float(value) if isinstance(value, Decimal) else value for key,value in total.items()}
                       for side,total in totals.items()}, unclassified=unknown, invalid=invalid, outside=outside,
                price_history=sorted(history, key=lambda row:row['time']))


def flow_summary(trades, provider):
    """Repair legacy directional totals without inventing bets or positions.

    Kalshi's stored ``dollars`` aggregate uses the YES-price basis on both sides.
    Each classified execution has one taker direction, so its count is ``prints``
    and NO-direction value is contracts minus that YES-priced aggregate. Older
    snapshots left buy_* at zero even when these execution aggregates were known.
    """
    if not trades:return None
    result=deepcopy(trades)
    for side in ('yes','no'):
        values=result.setdefault('sides',{}).setdefault(side,{})
        if provider=='Kalshi':
            values['buys']=values.get('prints')
            values['buy_contracts']=values.get('contracts')
            contracts,dollars=decimal(values.get('contracts')),decimal(values.get('dollars'))
            current=numeric(values.get('buy_dollars')) if result.get('accounting_version')==2 else None
            values['buy_dollars']=(current if current is not None else float(dollars if side=='yes' else contracts-dollars)
                if contracts is not None and dollars is not None and 0<=dollars<=contracts else None)
        for field in ('buys','buy_dollars','buy_contracts','sells','sell_dollars'):
            value=numeric(values.get(field))
            values[field]=value if value is not None and value>=0 else None
    classified=sum((numeric(side.get('prints')) or 0) for side in result['sides'].values())
    # An empty failed/invalid sample cannot establish zero on either outcome.
    if not classified and not (result.get('complete') and not result.get('invalid') and not result.get('unclassified')):
        for side in result['sides'].values():
            for field in ('buys','buy_dollars','buy_contracts','sells','sell_dollars'):side[field]=None
    result['classified']=classified
    return result


class MarketStructure:
    def __init__(self, directory=None, kalshi=None, polymarket=None, client=None, now=None):
        self.directory = Path(directory or Path(__file__).resolve().parents[1]/'data/market_structure')
        self.kalshi = kalshi or KalshiService()
        self.polymarket = polymarket or PolymarketService()
        self.client = client or HttpClient()
        self.now = now or (lambda:datetime.now(timezone.utc))
        self.queue = RefreshQueue(workers=3)
        self.lock = RLock(); self.memory = OrderedDict()
        self._trade_cutoff = None

    @staticmethod
    def key(request, provider):
        # Equivalent entry points share snapshots; the originating page is not
        # used to select which providers will be read.
        identity = dict(sport=request['sport'], teams=sorted(request['game'][s]['name'].casefold() for s in ('away','home')),
                        start=request['game']['start'], focus=request['focus'], reference=request.get('reference'),
                        version=CACHE_VERSION, window=request.get('window', 'all'))
        if request['focus'] is None:
            identity.update(source=request['provider'], ticker=request['ticker'])
        return sha256(json.dumps([provider, identity], sort_keys=True).encode()).hexdigest()

    def saved(self, key):
        with self.lock:
            if key in self.memory:
                self.memory.move_to_end(key)
                return deepcopy(self.memory[key])
            try:
                result = json.loads((self.directory/(key+'.json')).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
            self.remember(key, result)
            return deepcopy(result)

    def remember(self, key, result):
        with self.lock:
            self.memory[key] = deepcopy(result);self.memory.move_to_end(key)
            while len(self.memory) > 64:
                self.memory.popitem(last=False)

    def read(self, value, refresh=False):
        request = validate(value); providers = {}
        for provider in PROVIDERS:
            if provider == 'FanDuel':
                providers[provider] = dict(pending=False, data=dict(markets=[], notice=
                    'Exact bet counts, money wagered and live betting splits are not available through the connected FanDuel odds feed. A verified FanDuel-specific splits feed is required.'))
                continue
            key = self.key(request, provider);saved = self.saved(key)
            stamp = parse_time(saved.get('fetched_at')) if saved else None
            if refresh or not stamp or not 0 <= (self.now()-stamp).total_seconds() < 30:
                def update(provider=provider, key=key):
                    result = self.load(request, provider)
                    prior = self.saved(key) or {}
                    older = {row['ticker']:row for row in prior.get('markets', [])}
                    for market in result.get('markets', []):
                        for kind in ('trades','book','holders','interest'):
                            previous = older.get(market['ticker'], {}).get(kind)
                            if not market.get(kind) and previous:
                                market[kind] = previous
                                market['errors'].append('Showing saved '+kind+' from '+previous['fetched_at']+'.')
                    with self.lock:
                        atomic_text(self.directory/(key+'.json'), json.dumps(result, allow_nan=False))
                        self.remember(key, result)
                state = self.queue.request(key, update, force=refresh)
            else:
                state = self.queue.state(key)
            providers[provider] = dict(data=saved, pending=state['pending'], error=state['error'])
        return dict(request=request, providers=providers)

    def json(self, url, params=None):
        return self.client.json(url, params=params)

    def trade_cutoff(self):
        cached = self._trade_cutoff
        if cached and 0 <= (self.now()-cached[0]).total_seconds() < 300:
            return cached[1]
        data = self.json(BASE+'/historical/cutoff')
        cutoff = parse_time(data.get('trades_created_ts'))
        if not cutoff:
            raise DataSourceError('Kalshi omitted its historical trade cutoff.')
        self._trade_cutoff = (self.now(), cutoff)
        return cutoff

    def pages(self, provider, market, lower, upper):
        rows = []; errors = []; completed = 0; scanned_pages = 0; successful_pages = 0
        routes = [BASE+'/markets/trades'] if provider == 'Kalshi' else [DATA+'/trades']
        if provider == 'Kalshi':
            try:
                cutoff = self.trade_cutoff()
                if lower <= cutoff:
                    routes.append(BASE+'/historical/trades')
            except (DataSourceError, OSError, ValueError, TypeError) as exc:
                errors.append('Archive coverage unverified: '+str(exc))
        # The same requested range goes to both tiers. Overlap is deduplicated by
        # trade_id, avoiding a gap at fractional-second archive boundaries.
        for route in routes:
            cursor = ''; seen = set()
            while scanned_pages < MAX_PAGES:
                params = (dict(ticker=market['ticker'], min_ts=max(0,int(lower.timestamp())-1), max_ts=int(upper.timestamp())+1, limit=500)
                    if provider == 'Kalshi' else dict(condition=market['condition_id'], taker_only='true', limit=500))
                if cursor:
                    params['cursor'] = cursor
                scanned_pages += 1
                try:
                    data = self.json(route, params)
                    page = data.get('trades') if provider == 'Kalshi' else data.get('data')
                    if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
                        raise DataSourceError('The exchange returned an invalid trade page.')
                    if len(page) > 500:
                        raise DataSourceError('The exchange exceeded the requested trade-page size.')
                    rows.extend(page); successful_pages += 1
                    if provider == 'Kalshi' and 'cursor' not in data:
                        raise DataSourceError('Kalshi omitted its trade coverage cursor.')
                    if provider == 'Polymarket' and not isinstance(data.get('pagination'), dict):
                        raise DataSourceError('Polymarket omitted its trade coverage information.')
                    cursor = data.get('cursor') if provider == 'Kalshi' else data['pagination'].get('next_cursor')
                    if provider == 'Polymarket' and data['pagination'].get('has_more') and not cursor:
                        raise DataSourceError('Polymarket omitted the next trade-page cursor.')
                    if cursor is None or cursor == '':
                        completed += 1; break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise DataSourceError('The exchange repeated its pagination cursor.')
                    seen.add(cursor)
                except (DataSourceError, OSError, ValueError, TypeError) as exc:
                    errors.append(str(exc)); break
        if not successful_pages:
            raise DataSourceError('; '.join(errors) or 'Trade data unavailable.')
        complete = completed == len(routes) and not errors
        if not complete and scanned_pages == MAX_PAGES:
            errors.append('The four-page trade limit was reached before all requested history was checked.')
        result = trade_summary(rows, provider, market, lower, upper)
        # An empty feed conflicts with a positive volume snapshot for the same
        # full-history scope. It cannot establish a zero trading distribution.
        headline=numeric(market.get('volume_24h' if lower>datetime(1970,1,1,tzinfo=timezone.utc) else 'volume'))
        if not rows and headline is not None and headline>0:
            complete=False
            errors.append('No trades were returned despite positive reported volume; the side distribution is unavailable.')
        result.update(complete=complete, error='; '.join(errors) or None, fetched_at=self.now().isoformat(),
                      start=lower.isoformat(), end=upper.isoformat(), scanned=len(rows))
        return result

    def orderbook(self, provider, market):
        if provider == 'Kalshi':
            data = self.json(BASE+'/markets/'+market['ticker']+'/orderbook', dict(depth=0))
            fixed = data.get('orderbook_fp')
            book = fixed if fixed is not None else data.get('orderbook')
            if not isinstance(book, dict):
                raise DataSourceError('Kalshi returned an invalid order book.')
            sides = {side:book_summary(book_levels(book.get(side+'_dollars') if fixed is not None else book.get(side), cents=fixed is None))
                     for side in ('yes','no')}
        else:
            sides = {}
            for side in ('yes','no'):
                token = str(market.get(side+'_token', ''))
                if not token.isdigit():
                    raise DataSourceError('This outcome has no valid Polymarket token.')
                book = self.json(CLOB+'/book', dict(token_id=token))
                if str(book.get('asset_id')) != token:
                    raise DataSourceError('Polymarket returned the wrong outcome book.')
                sides[side] = book_summary(book_levels(book.get('bids')))
                sides[side]['asks'] = sorted(book_levels(book.get('asks')), key=lambda row:row['price'])[:10]
        return dict(sides=sides, fetched_at=self.now().isoformat())

    def holders(self, market):
        data = self.json(DATA+'/holders', dict(condition=market['condition_id'], include_pnl='true', limit=100))
        groups = data.get('data')
        if not isinstance(groups, list) or not isinstance(data.get('pagination'), dict):
            raise DataSourceError('Polymarket returned an invalid holders response.')
        expected = {str(market[s+'_token']) for s in ('yes','no')}
        if any(not isinstance(group, dict) or str(group.get('token_id')) not in expected for group in groups):
            raise DataSourceError('Polymarket returned holders for an unexpected outcome.')
        result = {}
        for side in ('yes','no'):
            token = str(market[side+'_token'])
            group = next((g for g in groups if str(g.get('token_id')) == token), None)
            if group is None:
                result[side] = None; continue
            rows = group.get('holders')
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise DataSourceError('Polymarket returned invalid holder records.')
            def total(field):
                values = [decimal(row.get(field)) for row in rows]
                return float(sum(values, Decimal(0))) if all(value is not None for value in values) else None
            result[side] = dict(wallets=len({r['proxy_wallet'] for r in rows if r.get('proxy_wallet')}),
                shares=total('amount'), entry_cost=total('entry_cost_usdc'), current_value=total('current_value'))
        return dict(sides=result, complete=not bool(data['pagination'].get('next_cursor') or data['pagination'].get('has_more')),
                    fetched_at=self.now().isoformat())

    def interest(self, market):
        data = self.json(DATA+'/oi', dict(condition=market['condition_id']))
        if not isinstance(data.get('data'), list):
            raise DataSourceError('Polymarket returned invalid open interest.')
        row = next((row for row in data['data'] if row.get('condition_id') == market['condition_id']), None)
        return dict(dollars=numeric(row.get('value')) if row else None, fetched_at=self.now().isoformat())

    def load(self, request, provider):
        from .market_summary import entries, matching_game
        from .market_structure_link import reference
        service = self.kalshi if provider == 'Kalshi' else self.polymarket
        catalog = service.games(request['sport'], 14)
        game = matching_game(request['game'], request['sport'], catalog)
        if not game:
            return dict(markets=[], notice='No unambiguous matching game is currently available on '+provider+'.',
                        notices=catalog.get('notices', []), fetched_at=self.now().isoformat())
        snapshot = service.quotes(game, market_category(request))
        if snapshot.get('sport') != request['sport'] or str(snapshot.get('game_id')) != str(game['id']):
            raise DataSourceError('The exchange returned quotes for a different game.')
        bundle = dict(sport=request['sport'], game=request['game'],
            providers={provider:dict(snapshot=snapshot, game=game)})
        candidates = entries(bundle)
        indices = {item['index'] for item in candidates if request['focus'] is not None
                   and item['comparison'] and item['comparison']['key'] == request['focus']}
        exact = set(indices)
        if request.get('reference'):
            source = {**snapshot,'game':game}
            indices.update(i for i,row in enumerate(snapshot['rows']) if any(
                reference(source,row,provider,side) == request['reference'] for side in ('yes','no')))
        if request['focus'] is None and provider == request['provider']:
            indices = {i for i,row in enumerate(snapshot['rows']) if str(row.get('ticker')) == request['ticker']}
        selected = [snapshot['rows'][index] for index in sorted(indices)]
        result = dict(markets=[], fetched_at=self.now().isoformat(), notice='',
                      notices=[*catalog.get('notices', []), *snapshot.get('notices', [])])
        if catalog.get('updating') or snapshot.get('updating'):
            result['notices'].append('Provider discovery or quotes are being refreshed; the quote timestamps identify the saved data used for matching.')
        if not selected:
            result['notice'] = 'No equivalent contract with the same outcome and settlement boundary was found. Different lines and push rules are not combined.'
            return result
        if len(selected) > 4:
            result['notice'] = f'Showing 4 of {len(selected)} equivalent contracts.'
        upper = self.now()
        lower = upper-timedelta(hours=24) if request['window'] == '24h' else datetime(1970,1,1,tzinfo=timezone.utc)
        for market in selected[:4]:
            identity = market.get('ticker')
            if provider == 'Kalshi' and not ticker(identity):
                raise DataSourceError('Kalshi returned an invalid contract identifier.')
            if provider == 'Polymarket' and not re.fullmatch(r'0x[0-9a-fA-F]{64}', market.get('condition_id') or ''):
                raise DataSourceError('Polymarket returned an invalid condition identifier.')
            index = snapshot['rows'].index(market)
            labels = {item['side']:item['comparison']['label'] for item in candidates
                      if item['index'] == index and item['comparison']}
            record = dict(ticker=identity, title=market['title'], labels={side:labels.get(side) or market.get(side+'_label', side.title()) for side in ('yes','no')},
                volume=market.get('volume'), volume_24h=market.get('volume_24h'), open_interest=market.get('open_interest'),
                quote_time=snapshot.get('fetched_at'), errors=[], match='equivalent' if index in exact else 'related', window=request['window'],
                rules=' '.join(market.get(key) or '' for key in ('rules_primary','rules_secondary')))
            tasks = dict(trades=lambda:self.pages(provider, market, lower, upper),
                         book=lambda:self.orderbook(provider, market))
            if provider == 'Polymarket':
                tasks['holders'] = lambda:self.holders(market)
                tasks['interest'] = lambda:self.interest(market)
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='market-structure') as pool:
                pending = {kind:pool.submit(task) for kind,task in tasks.items()}
                for kind, future in pending.items():
                    try:
                        record[kind] = future.result()
                    except (DataSourceError, OSError, ValueError, TypeError, KeyError) as exc:
                        record['errors'].append(kind.title()+': '+str(exc))
            result['markets'].append(record)
        result['fetched_at'] = self.now().isoformat()
        return result
