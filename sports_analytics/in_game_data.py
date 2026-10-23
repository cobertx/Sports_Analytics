"""Recorded shot/play locations, with a reusable on-disk index of saved seasons.

No model forecasts or invented tracking coordinates are used in these charts.
Only the selected game's events are sent to the browser.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from threading import RLock

import pandas as pd

from .data import DataSourceError, HttpClient, Query
from .nfl_model_data import NflModelData, RELEASES, SCHEDULE, atomic_text, canonical_team
from .service import DataService

ROOT = Path(__file__).resolve().parents[1]
VERSION = 1
NFL_VERSION = 2
NBA_COLUMNS = {'game_id', 'action_number', 'order_index', 'period', 'clock', 'team_tricode',
               'person_id', 'player_name', 'player_name_i', 'x_legacy', 'y_legacy',
               'shot_result', 'is_field_goal', 'shot_value', 'description'}
NFL_COLUMNS = {'game_id', 'play_id', 'posteam', 'play_type', 'play_deleted', 'qtr', 'time',
               'down', 'ydstogo', 'yardline_100', 'yards_gained', 'pass_location', 'run_location',
               'passer_player_name', 'rusher_player_name', 'receiver_player_name', 'fixed_drive',
               'drive', 'desc', 'epa', 'touchdown', 'td_team', 'interception', 'sack', 'fumble_lost',
               'defteam', 'home_team', 'away_team', 'fixed_drive_result', 'drive_time_of_possession',
               'shotgun', 'no_huddle', 'qb_scramble', 'qb_dropback'}
CHARTING_COLUMNS = {'offense_personnel', 'defense_personnel', 'offense_formation', 'defense_formation',
    'defense_man_zone_type', 'defense_coverage_type', 'defenders_in_box', 'n_defense_box',
    'number_of_pass_rushers', 'n_pass_rushers', 'n_blitzers', 'qb_location', 'is_no_huddle',
    'is_play_action', 'is_screen_pass', 'is_rpo'}
NFL_COLUMNS |= CHARTING_COLUMNS


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def pick(row, *keys):
    return next((row[k] for k in keys if k in row and row[k] not in ('', None)), '')


def charting_fields(row):
    """Preserve recorded labels only; personnel does not imply man/zone coverage."""
    def text(key):
        value = str(row.get(key, '')).strip()
        return value if value.lower() not in {'', 'na', 'nan', 'none', 'null'} else None

    def flag(*keys):
        value = str(pick(row, *keys)).lower()
        return True if value in {'1', '1.0', 'true'} else False if value in {'0', '0.0', 'false'} else None

    return dict(offense_personnel=text('offense_personnel'), defense_personnel=text('defense_personnel'),
        offense_formation=text('offense_formation'), defense_formation=text('defense_formation'),
        coverage_family=text('defense_man_zone_type'), coverage_scheme=text('defense_coverage_type'),
        defenders_in_box=number(pick(row, 'defenders_in_box', 'n_defense_box')),
        pass_rushers=number(pick(row, 'number_of_pass_rushers', 'n_pass_rushers')),
        blitzers=number(row.get('n_blitzers')), qb_location=text('qb_location'),
        shotgun=flag('shotgun'), no_huddle=flag('is_no_huddle', 'no_huddle'),
        play_action=flag('is_play_action'), screen=flag('is_screen_pass'), rpo=flag('is_rpo'))


def charted_play(row):
    identity = number(row.get('play_id'))
    if identity is None or not identity.is_integer():
        return None
    return dict(id=str(int(identity)), order=identity, **charting_fields(row))


def nba_shot(row):
    """NBA legacy coordinates are basket-relative, in tenths of a foot."""
    if number(pick(row, 'is_field_goal', 'isFieldGoal', 'SHOT_ATTEMPTED_FLAG')) != 1:
        return None
    x = number(pick(row, 'x_legacy', 'xLegacy', 'LOC_X'))
    y = number(pick(row, 'y_legacy', 'yLegacy', 'LOC_Y'))
    result = str(pick(row, 'shot_result', 'shotResult')).lower()
    made_flag = number(row.get('SHOT_MADE_FLAG'))
    made = True if result == 'made' or made_flag == 1 else False if result == 'missed' or made_flag == 0 else None
    player = str(pick(row, 'player_name_i', 'playerNameI', 'player_name', 'playerName', 'PLAYER_NAME'))
    period = number(pick(row, 'period', 'PERIOD'))
    shot_type = str(pick(row, 'actionType', 'action_type', 'SHOT_TYPE')).lower()
    value = number(pick(row, 'shot_value', 'shotValue'))
    if value not in (2, 3):
        value = 3 if shot_type.startswith('3') else 2 if shot_type.startswith('2') else None
    return dict(id=str(pick(row, 'action_number', 'actionNumber', 'GAME_EVENT_ID')),
                order=number(pick(row, 'order_index', 'actionNumber', 'GAME_EVENT_ID')) or 0,
                period=int(period) if period is not None else None,
                clock=str(pick(row, 'clock', 'CLOCK')), team=str(pick(row, 'team_tricode', 'teamTricode', 'TEAM_NAME')),
                player=player, player_id=str(pick(row, 'person_id', 'personId', 'PLAYER_ID')) or player,
                x=x / 10 if x is not None else None, y=y / 10 if y is not None else None,
                made=made, value=value,
                description=str(pick(row, 'description', 'ACTION_TYPE')))


def nfl_play(row):
    kind = row.get('play_type')
    if kind not in {'pass', 'run', 'qb_kneel', 'qb_spike'} or number(row.get('play_deleted')) == 1:
        return None
    team = canonical_team(row.get('posteam', ''))
    if not team:
        return None
    yardline = number(row.get('yardline_100'))
    gained = number(row.get('yards_gained'))
    start = 100 - yardline if yardline is not None and 0 <= yardline <= 100 else None
    # Retain missing locations in the event table, never place them at zero.
    end = start + gained if start is not None and gained is not None else None
    direction = row.get('pass_location' if kind in {'pass', 'qb_spike'} else 'run_location', '')
    direction = direction if direction in {'left', 'middle', 'right'} else 'unknown'
    period = number(row.get('qtr'))
    return dict(id=str(row.get('play_id', '')), order=number(row.get('play_id')) or 0,
                period=int(period) if period is not None else None, clock=row.get('time', ''), team=team,
                defense=canonical_team(row.get('defteam', '')), kind=kind,
                drive=str(pick(row, 'fixed_drive', 'drive')), drive_result=row.get('fixed_drive_result', ''),
                drive_duration=row.get('drive_time_of_possession', ''), down=number(row.get('down')),
                to_go=number(row.get('ydstogo')), start=start, end=end, yards=gained, direction=direction,
                passer=row.get('passer_player_name', ''), rusher=row.get('rusher_player_name', ''),
                receiver=row.get('receiver_player_name', ''), description=row.get('desc', ''),
                epa=number(row.get('epa')), touchdown=number(row.get('touchdown')) == 1 and
                canonical_team(row.get('td_team', '')) == team, interception=number(row.get('interception')) == 1,
                sack=number(row.get('sack')) == 1, fumble_lost=number(row.get('fumble_lost')) == 1,
                call='QB kneel' if kind == 'qb_kneel' else 'QB spike' if kind == 'qb_spike' else
                'Dropback' if kind == 'pass' or number(row.get('qb_scramble')) == 1 or number(row.get('qb_dropback')) == 1 else 'Run',
                **charting_fields(row))


def valid_game(sport, game_id):
    pattern = r'\d{10}' if sport == 'NBA' else r'\d{4}_\d{2}_[A-Z0-9]{2,3}_[A-Z0-9]{2,3}'
    return sport in {'NBA', 'NFL'} and bool(re.fullmatch(pattern, str(game_id)))


class InGameService:
    def __init__(self, data=None, directory=None, model_directory=None, client=None, nfl_data=None):
        self.data = data or DataService()
        self.directory = Path(directory or self.data.path.parent / 'in_game')
        self.models = Path(model_directory or ROOT / 'data/modeling')
        self.client = client or HttpClient()
        self.nfl = nfl_data or NflModelData(self.models / 'nfl', client=self.client)
        self._lock = RLock()

    @staticmethod
    def validate(sport, season, phase='regular'):
        if sport not in {'NBA', 'NFL'}:
            raise ValueError('In-game charts are available for NBA and NFL.')
        Query(sport, season, season_type=phase)

    @contextmanager
    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory / 'events.sqlite3', timeout=60)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, signature TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS events (source TEXT, game TEXT, event TEXT, payload TEXT, '
                           'PRIMARY KEY (source, game, event))')
                yield db
        finally:
            db.close()

    def _nfl_source(self, name, url, refresh=False):
        path = self.nfl.directory / 'raw' / name
        if path.is_file() and not refresh:
            return path, ''
        self.nfl._source_notices = []
        try:
            path = self.nfl._source(name, url, refresh)
        except DataSourceError:
            if not path.is_file():
                raise
            return path, f'Refresh failed; displaying saved {name}.'
        return path, ' '.join(self.nfl._source_notices)

    def catalog(self, sport, season, phase='regular', refresh=False):
        self.validate(sport, season, phase)
        notice = ''
        if sport == 'NBA':
            query = Query(sport, season, season_type=phase)
            cached = None if refresh else self.data.cached(self.data.key(query))
            prepared = self.models / 'nba' / f'team_games_{season}_{phase}.csv'
            if cached is not None:
                frame = cached.frame
            elif prepared.is_file() and not refresh:
                frame = pd.read_csv(prepared, dtype=str, keep_default_na=False)
            else:
                loaded = self.data.load(query, refresh=refresh)
                frame, notice = loaded.frame, loaded.notice
            games = []
            for identity, group in frame.groupby('game_id', sort=False):
                row = group.iloc[0]
                teams = sorted(set(group['team'].astype(str)) | set(group['opponent'].astype(str)))
                matchup = str(row.get('matchup', ''))
                if not matchup or matchup == 'nan':
                    home = number(row.get('is_home'))
                    matchup = f"{row['opponent']} @ {row['team']}" if home == 1 else f"{row['team']} @ {row['opponent']}" if home == 0 else ' vs '.join(teams)
                games.append(dict(id=str(identity).zfill(10), date=str(row['event_date'])[:10],
                                  teams=teams, matchup=matchup))
        else:
            with self._lock:
                path, notice = self._nfl_source('games.csv', SCHEDULE, refresh)
                frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            frame = frame[frame['season'].eq(str(season)) &
                          (frame['game_type'].eq('REG') if phase == 'regular' else frame['game_type'].isin(['WC', 'DIV', 'CON', 'SB']))]
            # Scheduled games without a score do not yet have offensive play data.
            frame = frame[pd.to_numeric(frame['home_score'], errors='coerce').notna()]
            games = [dict(id=r.game_id, date=r.gameday, teams=[canonical_team(r.away_team), canonical_team(r.home_team)],
                          matchup=f'{canonical_team(r.away_team)} @ {canonical_team(r.home_team)}', week=r.week)
                     for r in frame.itertuples()]
        games = [g for g in games if valid_game(sport, g['id'])]
        return dict(sport=sport, season=season, phase=phase, games=sorted(games, key=lambda g: (g['date'], g['id']), reverse=True), notice=notice)

    def _indexed(self, path, sport, game_id):
        """Index each source revision once, transactionally, with only needed columns."""
        source = str(path.resolve())
        stat = path.stat()
        signature = f'{VERSION if sport == "NBA" else NFL_VERSION}:{stat.st_size}:{stat.st_mtime_ns}'
        charting = sport in {'participation', 'ftn'}
        columns, normalize = ((CHARTING_COLUMNS | {'nflverse_game_id', 'game_id', 'play_id', 'nflverse_play_id'}, charted_play)
                              if charting else (NBA_COLUMNS, nba_shot) if sport == 'NBA' else (NFL_COLUMNS, nfl_play))
        with self._lock, self.connect() as db:
            cached = db.execute('SELECT signature FROM sources WHERE source=?', (source,)).fetchone()
            if not cached or cached[0] != signature:
                db.execute('DELETE FROM events WHERE source=?', (source,))
                with pd.read_csv(path, usecols=lambda name: name in columns, dtype=str,
                                 keep_default_na=False, chunksize=25000) as chunks:
                    for chunk in chunks:
                        if charting:
                            chunk = chunk.rename(columns={old: new for old, new in
                                [('nflverse_game_id', 'game_id'), ('nflverse_play_id', 'play_id')] if new not in chunk})
                        required = {'game_id', 'play_id'} if charting else {'game_id', 'is_field_goal' if sport == 'NBA' else 'play_type'}
                        if not required <= set(chunk):
                            raise DataSourceError('Saved play-by-play is missing required event columns.')
                        if not charting:
                            chunk = chunk[chunk['is_field_goal'].eq('1')] if sport == 'NBA' else chunk[chunk['play_type'].isin(['pass', 'run', 'qb_kneel', 'qb_spike'])]
                        batch = []
                        for row in chunk.to_dict('records'):
                            event = normalize(row)
                            if event and event['id'] and valid_game('NFL' if charting else sport, row['game_id']):
                                batch.append((source, row['game_id'], event['id'], json.dumps(event, allow_nan=False)))
                        db.executemany('INSERT OR REPLACE INTO events VALUES (?,?,?,?)', batch)
                db.execute('INSERT OR REPLACE INTO sources VALUES (?,?)', (source, signature))
            records = db.execute('SELECT payload FROM events WHERE source=? AND game=?', (source, game_id)).fetchall()
        return sorted((json.loads(row[0]) for row in records), key=lambda r: r['order'])

    def nfl_charting(self, events, season, game_id, download=False):
        """Join exact game/play IDs; fetching supplementary seasons is explicit."""
        events = [dict(event) for event in events]
        by_id = {event['id']: event for event in events}
        notices, sources = [], []
        for kind, stem, minimum in [('participation', 'pbp_participation', 2016), ('ftn', 'ftn_charting', 2022)]:
            name = f'{stem}_{season}.csv'
            path = self.nfl.directory / 'raw' / name
            if season < minimum:
                continue
            try:
                if download:
                    path, notice = self._nfl_source(name, f'{RELEASES}/{stem}/{name}', refresh=True)
                    if notice: notices.append(notice)
                if not path.is_file():
                    continue
                rows = self._indexed(path, kind, game_id)
                for row in rows:
                    if row['id'] in by_id:
                        # FTN's public subset complements participation; missing values never erase it.
                        by_id[row['id']].update({k: v for k, v in row.items() if k not in {'id', 'order'} and v is not None})
                sources.append(dict(name=stem, matched=sum(r['id'] in by_id for r in rows),
                    saved_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')))
            except (DataSourceError, OSError, ValueError) as exc:
                notices.append(f'{stem} unavailable: {exc}')
        counts = {}
        for event in events:
            key = (event['team'], event['drive'])
            counts[key] = counts.get(key, 0) + 1
            event['drive_play_number'] = counts[key]
        return events, dict(sources=sources, notices=notices)

    @staticmethod
    def _revision(path):
        stat = path.stat()
        return f'{stat.st_size}:{stat.st_mtime_ns}'

    def _nba_live(self, game_id):
        url = f'https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json'
        payload = self.client.json(url)
        game = payload.get('game', {}) if isinstance(payload, dict) else {}
        if str(game.get('gameId', '')) != game_id or not isinstance(game.get('actions'), list):
            raise DataSourceError('NBA play-by-play did not match the selected game.')
        events = {}
        for row in game['actions']:
            event = nba_shot(row)
            if event and event['id']:
                events[event['id']] = event
        return sorted(events.values(), key=lambda r: r['order']), url

    def game(self, sport, season, game_id, refresh=False, charting=False):
        self.validate(sport, season)
        if (not valid_game(sport, game_id) or (sport == 'NFL' and not game_id.startswith(f'{season}_')) or
                (sport == 'NBA' and game_id[3:5] != f'{season % 100:02}')):
            raise ValueError('Choose a valid game from the selected season.')
        notice = ''
        if sport == 'NFL':
            with self._lock:
                path, notice = self._nfl_source(f'play_by_play_{season}.csv', f'{RELEASES}/pbp/play_by_play_{season}.csv', refresh)
                events = self._indexed(path, sport, game_id)
                events, charting_info = self.nfl_charting(events, season, game_id, download=charting)
            source = 'nflverse / nflfastR play-by-play'
            stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
        else:
            archive = self.models / 'nba/epa/raw' / f'nba_play_by_play_{season + 1}.csv.gz'
            saved = self.directory / 'nba' / f'{game_id}.json'
            cached = None
            if saved.is_file():
                try:
                    cached = json.loads(saved.read_text(encoding='utf-8'))
                    if cached.get('version') != VERSION or cached.get('game_id') != game_id:
                        cached = None
                    if cached and cached.get('archive_revision') and archive.is_file() and cached['archive_revision'] != self._revision(archive):
                        cached = None
                except (ValueError, OSError):
                    cached = None
            if cached and not refresh:
                return cached
            events = self._indexed(archive, sport, game_id) if archive.is_file() and not refresh else []
            if events:
                source = 'Saved NBA play-by-play (sportsdataverse archive)'
                stamp = datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
            else:
                try:
                    events, source = self._nba_live(game_id)
                    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                except DataSourceError:
                    if cached:
                        return {**cached, 'notice': 'Refresh failed; displaying the previously saved game.'}
                    if archive.is_file():
                        events = self._indexed(archive, sport, game_id)
                        if events:
                            source = 'Saved NBA play-by-play (sportsdataverse archive)'
                            stamp = datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
                            notice = 'Refresh failed; displaying the saved archive.'
                        else:
                            raise
                    else:
                        raise
            result = dict(version=VERSION, sport=sport, season=season, game_id=game_id, events=events,
                          source=source, source_at=stamp, notice=notice)
            if source.startswith('Saved NBA'):
                result['archive_revision'] = self._revision(archive)
            # Empty responses may be pregame/incomplete. Do not permanently cache them.
            if events:
                atomic_text(saved, json.dumps(result, allow_nan=False))
            return result
        return dict(version=NFL_VERSION, sport=sport, season=season, game_id=game_id, events=events,
                    source=source, source_at=stamp, notice=notice, charting=charting_info)
