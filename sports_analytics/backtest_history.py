"""Fill NBA replay history before selecting evaluation targets; preserve model rules."""
from contextlib import contextmanager
import os
from pathlib import Path
from time import sleep

import pandas as pd

from .data import DataSourceError,Query
from .nba_model_data import NbaModelData


@contextmanager
def source_lock(path):
    """Share public source preparation between concurrent replay processes."""
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as lock:
        if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        if os.name=='nt':
            import msvcrt
            while True:
                try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError as exc:
                    if exc.errno not in {13,36} and getattr(exc,'winerror',None) not in {5,32,33}:raise
                    sleep(.2)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:
            lock.seek(0)
            if os.name=='nt':msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(lock,fcntl.LOCK_UN)


class NBAReplayHistory:
    def __init__(self,backtesting,root=None):
        from .backtesting_service import ROOT
        self.backtesting=backtesting
        self.root=Path(root or ROOT)
        self.boxes=NbaModelData(self.root/'data/modeling/nba',service=backtesting.data)
        self.locks=Path(backtesting.data.path).parent/'backtest_sources'

    def box_season(self,season,phases,refresh_current=False):
        for phase in phases:
            with source_lock(self.locks/f'nba_{season}_{phase}.lock'):
                if not refresh_current and (self.boxes.directory/f'team_games_{season}_{phase}.csv').exists():continue
                query=Query('NBA',season,'team',phase)
                dataset=self.backtesting.data.cached(self.backtesting.data.key(query))
                # Finished seasons are reused locally, independent of live-page TTLs.
                if (dataset is None or not self.backtesting.data.compatible(query,dataset)
                        or refresh_current and not self.backtesting.data.fresh(query,dataset)):
                    dataset=self.backtesting.data.load(query)
                if dataset.frame.empty:
                    raise DataSourceError(f'No completed NBA {season} {phase} box scores are available.')
                self.boxes._prepare_dataset(dataset,query)

    def epa_season(self,season,progress):
        from models.Game_Outcome.EPA.nba_epa_training import NBAEPAData
        directory=self.boxes.directory/'epa'
        with source_lock(self.locks/f'nba_{season}_epa.lock'):
            if all((directory/name).exists() for name in (f'team_games_{season}.csv',f'state_statistics_{season}.npz')):return
            # EPA still uses reconciled possession states, never a box-score proxy.
            NBAEPAData(directory,service=self.backtesting.data)._season(season,progress=progress)

    def ensure(self,key,entity,limit,lower,upper,include_playoffs,config,progress=None):
        from .backtesting_service import eligible_pairs,season_phases
        phases=['regular','postseason'] if include_playoffs else ['regular']
        now=self.backtesting.now()
        latest=min(now,upper-pd.Timedelta(nanoseconds=1))
        current=now.year-(now.month<7)
        first=latest.year-(latest.month<7)
        available=0;earliest=None
        def notify(message):
            if progress:progress(0,limit,message)
        for season in range(first,1995,-1):
            notify(f'Loading NBA {season}-{str(season+1)[-2:]} saved/downloaded history for {entity}: {available:,}/{limit:,} games')
            try:
                # No postseason exists yet before spring of a season's ending year.
                active=[phase for phase in phases if phase=='regular' or latest>=pd.Timestamp(f'{season+1}-04-01',tz='UTC')]
                self.box_season(season,active,refresh_current=season==current)
                if key=='epa' and season<current:
                    notify(f'Downloading/reconciling NBA EPA {season}-{str(season+1)[-2:]} events; saved inputs are reused')
                    self.epa_season(season,lambda done,total,message:notify(message))
                targets,_=self.backtesting.game_inputs('NBA','ppp')
                targets=season_phases(targets,'NBA');targets=targets[targets.season_type.isin(phases)]
                games=eligible_pairs(targets,entity,lower,upper,now,limit=limit)
                available=len(games)
                if games:earliest=games[0].event_date.min()
                if available<limit:
                    if lower>pd.Timestamp(f'{season}-07-01',tz='UTC'):break
                    continue
                try:training,_=self.backtesting.game_inputs('NBA',key)
                except DataSourceError:continue
                training=season_phases(training,'NBA');training=training[training.season_type.isin(phases)]
                prior=training[training.event_date.lt(earliest)&training.available_at.lt(earliest)]
                warmup=config.minimum_training_games+config.maximum_calibration_games
                teams=set(games[0].team)
                if (prior.game_id.nunique()>=warmup and all(
                        prior[prior.team.eq(team)].game_id.nunique()>=config.minimum_team_games for team in teams)):
                    notify(f'Ready: latest {limit:,} {entity} games plus earlier training/calibration history')
                    return
                notify(f'Loading earlier training/calibration history before {earliest.date()}; {limit:,} evaluation games retained')
            except (DataSourceError,OSError,ValueError) as exc:
                raise DataSourceError(f'Could not prepare the requested {limit:,} {entity} games: '
                    f'{available:,} matching games currently saved. NBA {season} source preparation failed: {exc}') from exc
        raise DataSourceError(f'Only {available:,} of the requested {limit:,} {entity} games have usable saved history '
                              'and earlier model training data. Widen the evaluation dates or retry the historical source; '
                              'the run has not been reported as a completed full sample.')
