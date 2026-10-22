"""Saved player and team inputs; downloads require the explicit preparation CLI."""
from pathlib import Path
import pandas as pd
from models.replay import replay_repository
from sports_analytics.data import DataSourceError, Query
from models.Player_Stats.Weighted_Historical_Performance.historical_data import (
    PlayerHistoryRepository, model_frame, seasons_for, identity_name, atomic_json,
)
from models.Player_Stats.Weighted_Historical_Performance.prediction_service import WHPMarketService
from .errors import UsageOpportunityError

ROOT = Path(__file__).resolve().parents[3] / 'data/modeling/player_stats/usage_opportunity'


def eligible(frame, cutoff):
    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None or pd.isna(cutoff):
        raise UsageOpportunityError('Forecast timestamps must include a timezone.')
    data = model_frame(frame)
    data = data[data.event_date.lt(cutoff) & data.available_at.lt(cutoff)
                & data.available_at.ge(data.event_date)].copy()
    data['game_id'] = data.game_id.astype(str)
    data['entity_id'] = data.entity_id.astype(str)
    data['team'] = data.team.astype(str).replace({'WAS': 'WSH', 'LA': 'LAR', 'JAC': 'JAX',
                                               'BRK': 'BKN', 'CHO': 'CHA', 'PHO': 'PHX'})
    if data.duplicated(['game_id', 'entity_id']).any():
        data = data.drop_duplicates()
        if data.duplicated(['game_id', 'entity_id']).any():
            raise UsageOpportunityError('Conflicting duplicate game identities in historical data.')
    return data.sort_values(['event_date', 'game_id', 'entity_id'])


class UsageHistoryRepository:
    def __init__(self, data=None, root=None, player_repository=None):
        self.players = player_repository or PlayerHistoryRepository(data=data)
        self.data = data or self.players.data
        self.root = Path(root or ROOT)

    @replay_repository
    def load(self, sport, start, teams):
        if sport == 'NCAAF':
            from sports_analytics.ncaaf_model_data import NcaafModelData,seasons_for_start
            data=NcaafModelData(directory=self.players.college_directory)
            return self.players.load(sport,start,teams),data.player_team_frames(seasons_for_start(start))
        if sport not in {'NBA', 'NFL'}:
            raise DataSourceError('Usage & Opportunity is currently available for NBA and NFL only.')
        players = self.players.load(sport, start, teams)
        frames = []
        for year in seasons_for(sport, start):
            for phase in ('regular', 'postseason'):
                query = Query(sport, year, 'team', phase)
                saved = self.data.cached(self.data.key(query))
                if saved is not None:
                    frames.append(saved.frame)
        if not frames:
            raise DataSourceError('Usage & Opportunity needs saved team history as well as player history. Run its --prepare CLI.')
        team_data = pd.concat(frames, ignore_index=True)
        if sport == 'NFL' and self.players.schedule_path.exists():
            schedule = pd.read_csv(self.players.schedule_path, dtype={'game_id': str}, usecols=['game_id', 'gameday'])
            dates = schedule.drop_duplicates('game_id', keep=False).set_index('game_id').gameday
            team_data['event_date'] = pd.to_datetime(team_data.event_date, utc=True, errors='coerce', format='mixed').fillna(
                pd.to_datetime(team_data.game_id.astype(str).map(dates), utc=True, errors='coerce'))
        return players, team_data

    def prepare(self, sport, start, teams=(), refresh=False):
        if sport == 'NCAAF':
            from sports_analytics.ncaaf_model_data import NcaafModelData
            data=NcaafModelData(directory=self.players.college_directory)
            _,notices=data.prepare(teams,start,refresh)
            data.prepare_player_inputs(teams,start,refresh)
            return ' '.join(notices)
        if sport not in {'NBA', 'NFL'}:
            raise DataSourceError('Usage & Opportunity preparation supports NBA and NFL only.')
        notices = [self.players.prepare(sport, start, teams, refresh)]
        total = 0
        for year in seasons_for(sport, start):
            for phase in ('regular', 'postseason'):
                query = Query(sport, year, 'team', phase)
                try:
                    saved = self.data.cached(self.data.key(query))
                    result = saved if saved is not None and not refresh else self.data.load(query, refresh=refresh)
                    total += len(result.frame)
                    if result.notice:
                        notices.append(result.notice)
                except DataSourceError as exc:
                    notices.append(f'{sport} {year} {phase}: {exc}')
        if not total:
            raise DataSourceError('No team history prepared. ' + ' '.join(notices))
        return f'{total:,} saved team-game rows. ' + ' '.join(dict.fromkeys(notices))


def match_player(data, snapshot, subject, cutoff):
    # Share the existing exact identity + latest known team rules, not the WHP
    # statistical engine. This also rejects stale/ambiguous name matches.
    return WHPMarketService._player(data, snapshot, subject, cutoff)
