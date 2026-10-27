"""Local Quick Filtering reports; browsing never pulls prices or runs models."""
from datetime import datetime, timezone
from contextlib import contextmanager, closing
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3

from .data import DataSourceError
from .kalshi import parse_time
from .market_view import MARKET_NAMES, selection_text

SPORTS = {'NBA', 'NFL', 'NCAAF'}
PROVIDERS = {'FanDuel', 'Kalshi', 'Polymarket'}


def route(value):
    found = re.fullmatch(r'#(nba|nfl|ncaaf)-filtered-results', value or '')
    return found[1].upper() if found else None


class FilteredResults:
    def __init__(self, path=None, now=None):
        self.path = Path(path or Path(__file__).resolve().parents[1]/'data/cache/filtered_results.sqlite3')
        self.now = now or (lambda:datetime.now(timezone.utc))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                connection.execute('''CREATE TABLE IF NOT EXISTS quick_runs (
                    run_id TEXT PRIMARY KEY, sport TEXT NOT NULL, provider TEXT NOT NULL,
                    game_id TEXT NOT NULL, starts REAL NOT NULL, completed REAL NOT NULL, payload TEXT NOT NULL)''')
                connection.execute('CREATE INDEX IF NOT EXISTS quick_games ON quick_runs(sport, provider, game_id, completed DESC)')
                yield connection
        finally:
            connection.close()

    def save(self, snapshot, ranked, criteria, *, provider='FanDuel', game=None, request=None):
        sport = snapshot.get('sport');game_id = str(snapshot.get('game_id', ''))
        if sport not in SPORTS or provider not in PROVIDERS:
            raise ValueError('Choose a supported sport and source for filtered results.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',game_id):
            raise ValueError('The filtered game has no valid identifier.')
        start = parse_time(snapshot.get('commence_time') if provider == 'FanDuel' else (game or {}).get('start'))
        if not start:
            raise ValueError('The filtered game needs its scheduled start.')
        if provider == 'FanDuel':
            matchup = snapshot['away_team']+' at '+snapshot['home_team']
            href = f'#{sport.lower()}-game-{game_id}'
        else:
            from .kalshi_view import matchup as teams
            resolved = teams(game)
            matchup = ' at '.join(team['name'] for team in resolved[0]) if resolved else game.get('title',game_id)
            href = f'#{sport.lower()}-{provider.lower()}-game-{game_id}'
        matches = []
        seen = set()
        for item in ranked['matches']:
            row = item['row']
            probability, implied, edge = (item[key] for key in ('probability','implied','edge'))
            if (not all(isinstance(value,(int,float)) and math.isfinite(value) for value in (probability,implied,edge))
                    or not 0 <= probability <= 1 or not 0 <= implied <= 1):
                raise ValueError('Filtered results need finite probabilities, prices and edges.')
            if provider == 'FanDuel':
                key = (row['market_key'],row['subject'],row['selection'],row.get('line'))
                selection = selection_text(row)
                market = MARKET_NAMES.get(row['market_key'],(row['market'],))[0]
                category = 'Player Props' if row['market_key'].startswith('player_') else 'Game Lines'
                odds = row.get('odds')
            else:
                key = (row['ticker'],item['side'])
                selection = row['title']+' — '+row.get(item['side']+'_label',item['side'].title())
                market = row.get('section') or row.get('event_title') or row['title']
                category = row['category']
                odds = 100*(1-implied)/implied if 0 < implied <= .5 else -100*implied/(1-implied) if .5 < implied < 1 else None
            if key in seen:
                continue
            seen.add(key)
            matches.append(dict(selection=selection,market=market,category=category,probability=probability,
                implied=implied,edge=edge,odds=odds,method=item['method'],estimates=item.get('estimates',[])))
        report = dict(sport=sport,provider=provider,game_id=game_id,matchup=matchup,start=start.isoformat(),
            href=href,quote_time=snapshot.get('pulled_at') or snapshot.get('fetched_at'),criteria=criteria,
            request=request,matches=matches)
        # Re-rendering the same completed scan must not duplicate its report.
        run_id = sha256(json.dumps(report,sort_keys=True,allow_nan=False).encode()).hexdigest()
        report.update(run_id=run_id,saved_at=self.now().isoformat())
        try:
            with self.connect() as connection:
                connection.execute('INSERT OR IGNORE INTO quick_runs VALUES (?,?,?,?,?,?,?)',
                    (run_id,sport,provider,game_id,start.timestamp(),self.now().timestamp(),json.dumps(report,allow_nan=False)))
        except sqlite3.Error as exc:
            raise DataSourceError('The filtered-results report could not be saved locally.') from exc
        return run_id

    def upcoming(self, sport, scope='latest'):
        if sport not in SPORTS or scope not in {'latest','all'}:
            raise ValueError('Choose a sport and saved-run view.')
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path,timeout=15)) as connection:
            rows = connection.execute('''SELECT payload FROM (
                SELECT payload, ROW_NUMBER() OVER (
                    PARTITION BY provider,game_id ORDER BY completed DESC,rowid DESC) AS position,
                    FIRST_VALUE(starts) OVER (
                    PARTITION BY provider,game_id ORDER BY completed DESC,rowid DESC) AS latest_start
                FROM quick_runs WHERE sport=?) WHERE latest_start>? AND (?='all' OR position=1)''',
                (sport,self.now().timestamp(),scope)).fetchall()
        result = []
        for (payload,) in rows:
            report = json.loads(payload)
            for index,match in enumerate(report['matches']):
                result.append({**{key:report[key] for key in
                    ('sport','provider','game_id','matchup','start','href','quote_time','criteria','run_id','saved_at')},
                    **match,'id':report['run_id']+':'+str(index)})
        return sorted(result,key=lambda row:(-row['probability'],-row['edge'],row['start'],row['provider'],row['id']))
