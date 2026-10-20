"""League-wide saved inputs are essential for schedule-strength normalization."""
from pathlib import Path
from models.Player_Stats.Usage_Opportunity.historical_data import (
    UsageHistoryRepository, eligible, match_player, identity_name, atomic_json,
)

ROOT=Path(__file__).resolve().parents[3]/'data/modeling/player_stats/opponent_adjusted_matchup'


class MatchupHistoryRepository(UsageHistoryRepository):
    def __init__(self, data=None, root=None, player_repository=None):
        super().__init__(data=data,root=root or ROOT,player_repository=player_repository)
