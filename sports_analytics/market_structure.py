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
TRADE_PAGE_SIZE = 1000
MAX_PAGES = 8  # Per API tier, so a busy live feed cannot starve the archive.
TRADE_REFRESH_SECONDS = 180
TRADE_CHART_POINTS = 500
BOOK_DEPTH = 30
TRADE_WINDOWS = {'all':None, '7d':timedelta(days=7), '24h':timedelta(days=1)}
CACHE_VERSION = 4


def validate(value):
    if not isinstance(value, dict) or value.get('provider') not in (*PROVIDERS,'DraftKings') or value.get('sport') not in {'NBA','NFL','NCAAF'}:
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
    if not isinstance(window,str) or window not in TRADE_WINDOWS:
        raise ValueError('Choose available history, the last 7 days or the last 24 hours.')
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
    depth = sorted(levels, key=lambda row:row['price'], reverse=True)[:BOOK_DEPTH]
    return dict(contracts=float(sum((Decimal(str(row['size'])) for row in levels), Decimal(0))),
        dollars=float(sum((Decimal(str(row['price']))*Decimal(str(row['size'])) for row in levels), Decimal(0))),
        levels=depth[:10], depth=depth)


def trade_summary(rows, provider, market, lower, upper):
    totals = {side:dict(prints=0, contracts=Decimal(0), dollars=Decimal(0), buys=0, sells=0,
                       buy_dollars=Decimal(0), sell_dollars=Decimal(0),
                       buy_contracts=Decimal(0), sell_contracts=Decimal(0)) for side in ('yes','no')}
    unknown = outside = invalid = 0
    seen = set(); history = []; first = last = None
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
        first = stamp if first is None else min(first, stamp)
        last = stamp if last is None else max(last, stamp)
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
    history.sort(key=lambda row:row['time'])
    chart_count = len(history)
    if chart_count > TRADE_CHART_POINTS:
        older = history[:-20]
        points = TRADE_CHART_POINTS-20
        history = [older[i*(len(older)-1)//(points-1)] for i in range(points)] + history[-20:]
    return dict(accounting_version=2,sides={side:{key:float(value) if isinstance(value, Decimal) else value for key,value in total.items()}
                       for side,total in totals.items()}, unclassified=unknown, invalid=invalid, outside=outside,
                first_trade=first.isoformat() if first else None, last_trade=last.isoformat() if last else None,
                chart_executions=chart_count, price_history=history)


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
        sources=(*PROVIDERS,'DraftKings') if request['provider']=='DraftKings' else PROVIDERS
        for provider in sources:
            if provider in {'FanDuel','DraftKings'}:
                providers[provider] = dict(pending=False, data=dict(markets=[], notice=
                    f'Exact bet counts, money wagered and live betting splits are unavailable through the connected {provider} odds feed.'))
                continue
            key = self.key(request, provider);saved = self.saved(key)
            stamp = parse_time(saved.get('fetched_at')) if saved else None
            if refresh or not stamp or not 0 <= (self.now()-stamp).total_seconds() < 30:
                def update(provider=provider, key=key):
                    prior = self.saved(key) or {}
                    older = {row['ticker']:row for row in prior.get('markets', [])}
                    result = self.load(request, provider, previous=older, refresh_trades=refresh)
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
        rows = []; errors = []; completed = 0; successful_pages = 0
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
            for _ in range(MAX_PAGES):
                params = (dict(ticker=market['ticker'], min_ts=max(0,int(lower.timestamp())-1),
                               max_ts=int(upper.timestamp())+1, limit=TRADE_PAGE_SIZE)
                    if provider == 'Kalshi' else dict(condition=market['condition_id'], taker_only='true', limit=TRADE_PAGE_SIZE))
                if cursor:
                    params['cursor'] = cursor
                try:
                    data = self.json(route, params)
                    page = data.get('trades') if provider == 'Kalshi' else data.get('data')
                    if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
                        raise DataSourceError('The exchange returned an invalid trade page.')
                    if len(page) > TRADE_PAGE_SIZE:
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
            else:
                tier = 'Kalshi archive' if route.endswith('/historical/trades') else provider+' trade feed'
                errors.append(f'{tier} reached its {MAX_PAGES:,}-page read limit.')
        if not successful_pages:
            raise DataSourceError('; '.join(errors) or 'Trade data unavailable.')
        complete = completed == len(routes) and not errors
        result = trade_summary(rows, provider, market, lower, upper)
        # An empty feed conflicts with a positive volume snapshot for the same
        # full-history scope. It cannot establish a zero trading distribution.
        all_history = lower == datetime(1970,1,1,tzinfo=timezone.utc)
        is_daily = upper-lower == TRADE_WINDOWS['24h']
        headline = numeric(market.get('volume' if all_history else 'volume_24h')) if all_history or is_daily else None
        if not rows and headline is not None and headline>0:
            complete=False
            errors.append('No trades were returned despite positive reported volume; the side distribution is unavailable.')
        result.update(complete=complete, error='; '.join(errors) or None, fetched_at=self.now().isoformat(),
                      start=lower.isoformat(), end=upper.isoformat(), scanned=len(rows), pages=successful_pages,
                      page_size=TRADE_PAGE_SIZE)
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
                asks = sorted(book_levels(book.get('asks')), key=lambda row:row['price'])[:BOOK_DEPTH]
                sides[side].update(asks=asks[:10], ask_depth=asks)
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

    def load(self, request, provider, *, previous=None, refresh_trades=False):
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
        interval = TRADE_WINDOWS[request['window']]
        lower = upper-interval if interval else datetime(1970,1,1,tzinfo=timezone.utc)
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
            tasks = dict(book=lambda:self.orderbook(provider, market))
            saved_trades = (previous or {}).get(identity, {}).get('trades') or {}
            trade_time = parse_time(saved_trades.get('fetched_at'))
            if (not refresh_trades and saved_trades.get('page_size') == TRADE_PAGE_SIZE and trade_time
                    and 0 <= (upper-trade_time).total_seconds() < TRADE_REFRESH_SECONDS):
                record['trades'] = saved_trades
            else:
                tasks['trades'] = lambda:self.pages(provider, market, lower, upper)
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
