"""Unattended refresh of shared results and prepared analysis inputs."""
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event, Lock, Thread
import pandas as pd

from .cache_refresh import RefreshQueue
from .data import DataSourceError, Query
from .history_freshness import ACTIVE_REFRESH_SECONDS, active_phases, active_season, update_lease
from .result_cache import atomic_text
from .service import DataService

ROOT = Path(__file__).resolve().parents[1]


class HistoryUpdater:
    def __init__(self, data=None, root=None, now=None, interval=ACTIVE_REFRESH_SECONDS):
        self.root = Path(root or ROOT)
        self.data = data or DataService()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.interval = interval
        self.queue = RefreshQueue(workers=2, retry_seconds=60)
        self.stop_event = Event()
        self.lock = Lock()
        self.thread = None
        self.path = self.root/'data/cache/history_updates.json'

    def plan(self, *, refresh=False, only_sport=None):
        """Update active seasons; retain already-prepared advanced/school scopes."""
        from .nba_model_data import NbaModelData
        from .nfl_model_data import NflModelData
        from .ncaaf_model_data import NcaafModelData
        from models.Game_Outcome.EPA.nba_epa_training import NBAEPAData
        now = self.now()
        jobs = {}
        for sport in ('NBA', 'NFL'):
            if only_sport and sport!=only_sport:continue
            year = active_season(sport, now)
            for phase in active_phases(sport, now):
                for kind in ('player', 'team'):
                    query = Query(sport, year, kind, phase)
                    def update(query=query):
                        result = self.data.load(query, refresh=True) if refresh else self.data.load(query)
                        if result.status == 'stale cache':
                            raise DataSourceError(result.notice)
                        if query.sport == 'NBA' and query.entity_type == 'team':
                            NbaModelData(self.root/'data/modeling/nba', self.data)._prepare_dataset(result, query)
                        dates = result.frame.get('event_date')
                        return dict(source_checked_at=result.fetched_at, rows=len(result.frame),
                                    latest_game=str(dates.max()) if dates is not None else None)
                    jobs[f'{sport} {year} {phase} {kind}'] = update
        nfl_year = active_season('NFL', now)
        nfl = self.root/'data/modeling/nfl'
        if only_sport in {None,'NFL'} and (any(nfl.glob('team_games_*.csv')) or any((nfl/'raw').glob('play_by_play_*.csv'))):
            def nfl_models():
                frame, notices = NflModelData(nfl).prepare([nfl_year])
                return dict(rows=len(frame), latest_game=str(frame.event_date.max()), notices=notices)
            jobs['NFL prepared game models'] = nfl_models
        nba_year = active_season('NBA', now)
        epa = self.root/'data/modeling/nba/epa'
        if only_sport in {None,'NBA'} and any(epa.glob('team_games_*.csv')):
            def nba_epa():
                dataset, notices = NBAEPAData(epa, self.data).prepare([nba_year])
                return dict(rows=len(dataset.games), latest_game=str(dataset.games.event_date.max()), notices=notices)
            jobs['NBA EPA event inputs'] = nba_epa
        if only_sport not in {None,'NCAAF'}:return jobs
        college = NcaafModelData(self.root/'data/modeling/ncaaf')
        for path in college.directory.glob(f'team_games_{active_season("NCAAF",now)}_*.manifest.json'):
            try:
                school = json.loads(path.read_text(encoding='utf-8'))['school']
            except (OSError, ValueError, KeyError):
                continue
            failed=self.status().get('sources',{}).get('NCAAF '+school,{}).get('error')
            if not failed and not self.college_due(college,school,active_season('NCAAF',now),now):
                continue
            def school_history(school=school):
                notice = college.prepare_team(school, now.isoformat())
                # Denominators are needed by every simulation player model.
                college.prepare_player_inputs([school], now.isoformat())
                return dict(notices=[notice])
            jobs['NCAAF '+school] = school_history
        return jobs

    @staticmethod
    def college_due(college,school,year,now):
        """Use the saved full schedule to poll around finals, not all day.

        Daily catch-up also covers postponed fixtures and source corrections.
        A game missing from either team or player results continues to retry.
        """
        from .ncaaf_model_data import school_token
        paths=[college.team_path(year,school),college.player_path(year,school),
               college.directory/f'player_team_games_{year}_{school_token(school)}.csv']
        schedule=college.directory/'raw'/f'games_{year}_{school_token(school)}.json'
        if not schedule.exists() or any(not p.exists() for p in paths):return True
        if (now.timestamp()-schedule.stat().st_mtime)>86400:return True
        try:
            games=json.loads(schedule.read_text(encoding='utf-8'))
            covered=[set(pd.read_csv(p,usecols=['game_id'],dtype=str).game_id) for p in paths]
            for game in games:
                start=pd.Timestamp(game['startDate'])
                if pd.isna(start) or start.tzinfo is None:continue
                age=(pd.Timestamp(now)-start).total_seconds()
                if (game.get('completed') or 3*3600<=age<=36*3600) and age>=0:
                    if any(str(game['id']) not in ids for ids in covered):return True
            return False
        except (OSError,ValueError,KeyError):return True

    def status(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return dict(sources={}, interval_seconds=self.interval)

    def _record(self, name, **fields):
        with self.lock:
            state = self.status()
            state.update(interval_seconds=self.interval, checked_at=self.now().isoformat())
            sources = state.setdefault('sources', {})
            sources[name] = {**sources.get(name, {}), **fields}
            atomic_text(self.path, json.dumps(state, indent=2, allow_nan=False))

    @staticmethod
    def checked_recently(record, now, interval):
        stamp = record.get('failed_at') if record.get('error') else record.get('last_success')
        if not stamp or record.get('pending'):return False
        try:return 0 <= (now-datetime.fromisoformat(stamp)).total_seconds() < interval
        except (TypeError,ValueError):return False

    def tick(self, *, wait=True, startup=False):
        with update_lease(self.path.with_suffix('.lock'), wait=startup, stop=self.stop_event) as acquired:
            if not acquired:return self.status()
            state=self.status()
            last=state.get('sources',{}).get('Automatic updater',{})
            # Multiple dashboard/worker starts within a minute share one source check.
            if startup and self.checked_recently(last,self.now(),60):return state
            self._schedule(self.plan(refresh=True) if startup else self.plan())
            # Keep the cross-process lease through publication of every source.
            self.queue.tasks.join()
            self._record('Automatic updater',pending=False,error=None,last_success=self.now().isoformat())
            return self.status()

    def _schedule(self, jobs):
        for name, work in jobs.items():
            def update(name=name, work=work):
                self._record(name, pending=True, attempted_at=self.now().isoformat())
                try:
                    detail = work()
                except Exception as exc:
                    self._record(name, pending=False, error=str(exc), failed_at=self.now().isoformat())
                    raise
                self._record(name, pending=False, error=None, last_success=self.now().isoformat(), detail=detail)
            self.queue.request(name, update)

    def refresh_for_analysis(self, sport):
        """Wait in the replay worker and share a completed refresh across methods."""
        with update_lease(self.path.with_suffix('.lock'), wait=True, stop=self.stop_event) as acquired:
            if not acquired:
                raise DataSourceError('The historical data updater is shutting down.')
            jobs=self.plan(only_sport=sport)
            recent=self.status().get('sources',{})
            now=self.now()
            due={name:work for name,work in jobs.items() if not self.checked_recently(
                recent.get(name,{}),now,60 if recent.get(name,{}).get('error') else self.interval)}
            self._schedule(due)
            self.queue.tasks.join()
            status=self.status().get('sources',{})
            return [name for name in jobs if status.get(name,{}).get('error')]

    def start(self):
        """Start once on server startup; catch up immediately, without blocking UI."""
        if self.thread and self.thread.is_alive():return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            def loop():
                startup=True
                while not self.stop_event.is_set():
                    try:
                        self.tick(startup=startup)
                    except Exception as exc:
                        self._record('Automatic updater', pending=False, error=str(exc))
                    startup=False
                    self.stop_event.wait(self.interval)
            self.thread = Thread(target=loop, name='analysis-history-updater', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.queue.shutdown()
