"""Timestamped structured availability; headlines never become numeric inputs."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import pandas as pd
import requests

from .data import DataSourceError
from .information import SPORTS, timestamp
from .nfl_model_data import atomic_text

ROOT = Path(__file__).resolve().parents[1]/'data/information_variable/availability'
OUT = {'out', 'injured reserve', 'ir', 'suspension', 'suspended', 'physically unable to perform'}
SCHEMA_VERSION = 2


def parse(payload, sport, fetched_at):
    if (not isinstance(payload, dict) or not isinstance(payload.get('injuries'), list) or
            payload.get('status','success')!='success'):
        raise DataSourceError('The structured injury feed returned an unsupported schema.')
    rows = []
    for team in payload['injuries']:
        if not isinstance(team, dict) or not isinstance(team.get('injuries'), list):
            raise DataSourceError('The injury feed is missing team participation records.')
        for item in team['injuries']:
            if not isinstance(item,dict) or not isinstance(item.get('athlete'),dict):
                raise DataSourceError('The injury feed is missing a structured athlete identity.')
            athlete = item.get('athlete') or {}
            club = athlete.get('team') or {}
            name = athlete.get('displayName'); status = item.get('status')
            if isinstance(status, dict):status = status.get('type') or status.get('name')
            if not name or not isinstance(status, str) or not team.get('displayName'):
                raise DataSourceError('The injury feed is missing player, team or status identities.')
            published = timestamp(item.get('date'))
            if published is None or published > timestamp(fetched_at):
                continue
            player_id = str(athlete.get('id') or '')
            if not player_id:
                # This public feed identifies athletes through their canonical
                # player-card links rather than an athlete.id field.
                for link in athlete.get('links',[]):
                    parsed = urlparse(link.get('href',''))
                    match = re.search(r'/player/(?:[^/]+/)*id/(\d+)(?:/|$)',parsed.path)
                    if parsed.hostname in {'espn.com','www.espn.com'} and match:
                        player_id = match[1];break
            if not player_id:
                raise DataSourceError('The injury feed is missing a stable athlete ID.')
            rows.append(dict(player=name, player_id=player_id,
                             team=team['displayName'], team_code=club.get('abbreviation'),
                             status=status, reported_at=published.isoformat(),
                             return_date=(item.get('details') or {}).get('returnDate'),
                             source='ESPN structured injury status'))
    return dict(version=SCHEMA_VERSION,sport=sport, fetched_at=fetched_at, reports=rows,
                source_url=f'https://site.api.espn.com/apis/site/v2/sports/{SPORTS[sport]}/injuries')


class AvailabilityStore:
    """Shared five-minute cache and immutable, retrieval-time historical snapshots.

    Calls occur in calculation workers, never navigation callbacks. The first
    fetch has a bounded timeout; subsequent methods share the same local packet.
    Past games only read archives: current news must never leak into replays.
    """
    def __init__(self, directory=None, fetch=None, now=None):
        self.directory = Path(directory or ROOT)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.fetch = fetch or self._fetch
        self.locks = {sport: Lock() for sport in SPORTS}
        self.failed = {}

    @staticmethod
    def _fetch(url):
        try:
            response = requests.get(url, timeout=(3, 6), headers={'User-Agent':'SportsAnalytics/0.1'})
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DataSourceError('Structured injury status could not be refreshed; the baseline forecast is retained.') from exc

    @staticmethod
    def read(path):
        try:
            packet = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(packet,dict) and timestamp(packet.get('fetched_at')) and isinstance(packet.get('reports'), list):return packet
        except (OSError, ValueError):pass
        return None

    def revision(self, sport):
        if sport not in SPORTS:return []
        path = self.directory/(sport.lower()+'.json')
        interval = int(self.now().timestamp()//300)
        try:
            stat = path.stat()
            return [str(path), stat.st_size, stat.st_mtime_ns, interval]
        except OSError:return [str(path),None,interval]

    def load(self, snapshot):
        sport = snapshot.get('sport')
        if sport not in SPORTS:return None, 'Availability adjustments cover NBA and NFL.'
        start, pulled = timestamp(snapshot.get('commence_time')), timestamp(snapshot.get('pulled_at'))
        now = self.now()
        if not start or not pulled:return None, 'Timestamped game information is required for availability adjustments.'
        if start <= now:
            cutoff = min(start, pulled)
            candidates = []
            for day in (cutoff, cutoff-pd.Timedelta(days=1)):
                candidates.extend((self.directory/'archive'/sport.lower()/day.strftime('%Y-%m-%d')).glob('*.json'))
            for path in sorted(candidates, reverse=True):
                saved = self.read(path)
                if saved and saved.get('sport') == sport:
                    age = (cutoff-timestamp(saved['fetched_at'])).total_seconds()
                    if 0 <= age <= 1800:return saved, None
            return None, 'No fresh pregame availability archive for this historical snapshot; baseline retained.'
        if (start-now).total_seconds() > (48*3600 if sport == 'NBA' else 7*86400):
            return None, 'Game is outside the availability-report window; baseline retained.'
        path = self.directory/(sport.lower()+'.json')
        with self.locks[sport]:
            now = self.now()
            saved = self.read(path)
            age = (now-timestamp(saved['fetched_at'])).total_seconds() if saved else float('inf')
            if saved and saved.get('version')==SCHEMA_VERSION and saved.get('sport') == sport and 0 <= age <= 300:return saved, None
            failure = self.failed.get(sport)
            if failure and 0 <= (now-failure[0]).total_seconds() < 60:return None, failure[1]
            try:
                packet = parse(self.fetch(f'https://site.api.espn.com/apis/site/v2/sports/{SPORTS[sport]}/injuries'), sport, self.now().isoformat())
                encoded = json.dumps(packet, sort_keys=True, allow_nan=False)
                stamp = timestamp(packet['fetched_at'])
                archive = self.directory/'archive'/sport.lower()/stamp.strftime('%Y-%m-%d')/(stamp.strftime('%H%M%S%f')+'_'+sha256(encoded.encode()).hexdigest()[:12]+'.json')
                atomic_text(archive, encoded)
                atomic_text(path, encoded)
                self.failed.pop(sport, None)
                return packet, None
            except (DataSourceError, OSError, ValueError) as exc:
                message = str(exc)
                self.failed[sport] = (now, message)
                # Do not silently apply yesterday's statuses as today's news.
                return None, message


def revision(sport):
    """Cheap cache invalidation shared by retained provider views."""
    return AvailabilityStore().revision(sport)
