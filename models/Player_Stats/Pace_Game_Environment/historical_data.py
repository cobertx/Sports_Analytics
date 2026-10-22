"""Existing saved boxes plus optional, saved nflverse play-by-play. No HTTP."""
from functools import lru_cache
from pathlib import Path
import pandas as pd
from models.replay import replay_repository
from models.Player_Stats.Usage_Opportunity.historical_data import (
    UsageHistoryRepository, eligible, match_player, identity_name, atomic_json,
)
from models.Player_Stats.Weighted_Historical_Performance.historical_data import seasons_for
from sports_analytics.data import DataSourceError

ROOT = Path(__file__).resolve().parents[3] / 'data/modeling/player_stats/pace_game_environment'
PBP_ROOT = ROOT.parents[1] / 'nfl/raw'


@lru_cache(maxsize=4)
def _pbp_summary(path, size, modified):
    # Stat arguments invalidate the in-process read cache after archive refresh.
    columns = ['game_id', 'posteam', 'play_type', 'qtr', 'qb_dropback', 'qb_scramble',
               'sack', 'pass_attempt', 'two_point_attempt', 'play_deleted', 'drive']
    header = pd.read_csv(path, nrows=0).columns
    required = set(columns) - {'play_deleted'}
    if not required <= set(header):
        return pd.DataFrame()
    data = pd.read_csv(path, usecols=[c for c in columns if c in header], dtype={'game_id': str}, low_memory=False)
    data = data[data.play_type.isin(['pass', 'run', 'qb_kneel', 'qb_spike'])
                & data.posteam.notna() & data.two_point_attempt.fillna(0).eq(0)].copy()
    if 'play_deleted' in data:
        data = data[data.play_deleted.fillna(0).eq(0)]
    data['reg'] = data.qtr.le(4).astype(int)
    data['ot'] = data.qtr.gt(4).astype(int)
    data['designed_pass'] = (data.qb_dropback.eq(1) | data.play_type.isin(['pass', 'qb_spike'])).astype(int)
    data['attempt'] = (data.pass_attempt.eq(1) & data.sack.ne(1)).astype(int)
    data['scramble'] = (data.designed_pass.eq(1) & data.qb_scramble.eq(1)).astype(int)
    data['sack_count'] = (data.designed_pass.eq(1) & data.sack.eq(1)).astype(int)
    data['other'] = data.designed_pass-data.attempt-data.scramble-data.sack_count
    if (data.other < 0).any():
        raise ValueError('Overlapping NFL pass-attempt/sack/scramble classifications.')
    result = data.groupby(['game_id', 'posteam']).agg(
        pge_reg_plays=('reg', 'sum'), pge_ot_plays=('ot', 'sum'),
        pge_pass=('designed_pass', 'sum'), pge_attempt=('attempt', 'sum'),
        pge_scramble=('scramble', 'sum'), pge_sack=('sack_count', 'sum'),
        pge_other=('other', 'sum'), pge_drives=('drive', 'nunique')).reset_index().rename(columns={'posteam': 'team'})
    return result


@lru_cache(maxsize=4)
def _player_scrambles(path, size, modified):
    columns = ['game_id','posteam','rusher_player_id','qb_scramble','play_type','two_point_attempt','play_deleted']
    header = pd.read_csv(path,nrows=0).columns
    if not set(columns)-{'play_deleted'} <= set(header):
        return pd.DataFrame()
    data = pd.read_csv(path,usecols=[c for c in columns if c in header],dtype={'game_id':str},low_memory=False)
    data = data[data.qb_scramble.eq(1) & data.play_type.eq('run') & data.two_point_attempt.fillna(0).eq(0)].copy()
    if 'play_deleted' in data:
        data = data[data.play_deleted.fillna(0).eq(0)]
    return (data.groupby(['game_id','posteam','rusher_player_id']).size().rename('pge_scramble').reset_index()
            .rename(columns={'posteam':'team','rusher_player_id':'entity_id'}))


class PaceHistoryRepository(UsageHistoryRepository):
    def __init__(self, data=None, root=None, player_repository=None, pbp_root=None):
        super().__init__(data=data, root=root or ROOT, player_repository=player_repository)
        self.pbp_root = Path(pbp_root or PBP_ROOT)

    def prepare(self, sport, start, teams=(), refresh=False):
        notice = super().prepare(sport,start,teams,refresh)
        if sport == 'NFL':
            from sports_analytics.nfl_model_data import NflModelData
            try:
                NflModelData(directory=self.pbp_root.parent).prepare(seasons_for(sport,start),refresh=refresh)
                notice += ' Saved nflverse play-by-play prepared for regulation, overtime and non-attempt pass outcomes.'
            except (DataSourceError,OSError,ValueError) as exc:
                notice += f' Play-by-play unavailable; explicitly labeled box-score volume fallback will be used: {exc}'
        return notice

    @replay_repository
    def load(self, sport, start, teams):
        players, boxes = super().load(sport, start, teams)
        if sport == 'NFL':
            parts,player_parts = [],[]
            # Saved archives only; no implicit refresh or paid market call.
            for path in sorted(self.pbp_root.glob('play_by_play_*.csv')):
                stat = path.stat()
                part = _pbp_summary(str(path), stat.st_size, stat.st_mtime_ns)
                if not part.empty:
                    parts.append(part)
                    player_part = _player_scrambles(str(path),stat.st_size,stat.st_mtime_ns)
                    if not player_part.empty:
                        player_parts.append(player_part)
            if parts:
                summary = pd.concat(parts).drop_duplicates(['game_id', 'team'], keep='last')
                boxes = boxes.copy()
                boxes['game_id'] = boxes.game_id.astype(str)
                boxes['team'] = boxes.team.replace({'WAS': 'WSH', 'LA': 'LAR', 'JAC': 'JAX'})
                summary['team'] = summary.team.replace({'WAS': 'WSH', 'LA': 'LAR', 'JAC': 'JAX'})
                boxes = boxes.merge(summary, on=['game_id', 'team'], how='left', validate='many_to_one')
                if player_parts:
                    player_summary = pd.concat(player_parts).drop_duplicates(['game_id','team','entity_id'],keep='last')
                    player_summary['team'] = player_summary.team.replace({'WAS':'WSH','LA':'LAR','JAC':'JAX'})
                    players = players.copy()
                    players['game_id'],players['entity_id'] = players.game_id.astype(str),players.entity_id.astype(str)
                    players['team'] = players.team.replace({'WAS':'WSH','LA':'LAR','JAC':'JAX'})
                    players = players.merge(player_summary,on=['game_id','team','entity_id'],how='left',validate='many_to_one')
                    covered = set(zip(summary.game_id,summary.team))
                    known = [(g,t) in covered for g,t in zip(players.game_id,players.team)]
                    players.loc[known,'pge_scramble'] = players.loc[known,'pge_scramble'].fillna(0)
        return players, boxes
