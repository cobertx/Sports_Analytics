"""Nullable NBA boxes, missed seasons, method availability and visible reasons."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_research import player_inputs
from models.Player_Stats.Usage_Opportunity.opportunity_model import observed
from models.Player_Stats.Usage_Opportunity.simulation import integer_pool
from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig
from models.Player_Stats.Usage_Opportunity.historical_data import UsageHistoryRepository, eligible
from models.Player_Stats.Opponent_Adjusted_Matchup.config import MatchupConfig
from models.Player_Stats.Opponent_Adjusted_Matchup.historical_data import MatchupHistoryRepository
from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
from models.Player_Stats.Pace_Game_Environment.historical_data import PaceHistoryRepository
from models.Player_Stats.Weighted_Historical_Performance.historical_data import PlayerHistoryRepository, seasons_for
from models.Player_Stats.Weighted_Historical_Performance.core import eligible_history, WeightedHistoricalPerformance, WHPConfig
from sports_analytics.data import Query
from sports_analytics.service import DataService
from sports_analytics.whp_service import WHPMarketService
from sports_analytics.usage_opportunity_service import UsageOpportunityMarketService
from sports_analytics.opponent_adjusted_service import OpponentAdjustedMarketService
from sports_analytics.pace_game_environment_service import PaceGameEnvironmentMarketService
from sports_analytics.market_view import market_sections
from sports_analytics.nba_markets import NBA_TEAM_CODES


def inputs():
    players, teams = player_inputs()
    identities = [*('early:'+identity for identity in players.game_id.unique()), *players.game_id.unique()]
    dates = dict(zip(identities,pd.date_range('2024-10-22',periods=70,freq='2D',tz='UTC')))
    result = {}
    for kind, frame in [('player',players),('team',teams)]:
        frame = pd.concat([frame.assign(game_id='early:'+frame.game_id),frame],ignore_index=True)
        frame['event_date'] = frame.game_id.map(dates)
        frame['available_at'] = frame.event_date+pd.Timedelta(hours=8)
        frame['team'] = frame.team.replace({'BOS':'IND','DET':'BKN'})
        frame['opponent'] = frame.opponent.replace({'BOS':'IND','DET':'BKN'})
        frame['season'] = 2024;frame['entity_type'] = kind
        current = frame[frame.game_id.isin(['33','34'])].copy()
        current['event_date'] = current.game_id.map({'33':pd.Timestamp('2026-10-22',tz='UTC'),
                                                   '34':pd.Timestamp('2026-10-23',tz='UTC')})
        current['available_at'] = current.event_date+pd.Timedelta(hours=8)
        current['game_id'] = 'espn:'+current.game_id;current['season'] = 2026
        if kind == 'team':
            # Opening-week ESPN team boxes do not publish total team minutes.
            # Concatenating them with older NBA Stats boxes produces nullable
            # Float64 values which isfinite(...).all(skipna=True) used to admit.
            frame['min'] = frame['min'].astype('Float64')
            current['min'] = pd.array([pd.NA]*len(current),dtype='Float64')
        result[kind] = frame, current
    return result


def snapshot():
    return dict(sport='NBA',game_id='401909862',home_team='Brooklyn Nets',away_team='Indiana Pacers',
        pulled_at='2026-10-25T16:54:38Z',commence_time='2026-10-25T22:10:00Z',rows=[
            dict(subject='Player A',market_key='player_points_alternate',market='Alternate points',
                line=line,selection='Over',odds=-110,group='Points',updated='Not reported') for line in (12.5,20.5)])


class NBAForecastChecks(TestCase):
    def test_nullable_missing_channels_are_excluded_before_recent_window(self):
        for dtype in ('Float64','Int64'):
            frame = pd.DataFrame(dict(game_id=['a','b','c','d'],event_date=pd.date_range('2026-01-01',periods=4,tz='UTC'),
                min=pd.Series([240,240,None,240],dtype=dtype),fga=pd.Series([90,91,92,None],dtype=dtype)))
            frozen = frame.copy(deep=True)
            actual = observed(frame,['min','fga'],2,2)
            self.assertEqual(actual.game_id.tolist(),['a','b'])
            self.assertFalse(actual[['min','fga']].isna().any().any())
            pd.testing.assert_frame_equal(frame,frozen)
            with self.assertRaisesRegex(ValueError,'only 2 available'):
                observed(frame,['min','fga'],3,20)
        plain = frame.astype({'min':'float64','fga':'float64'})
        self.assertEqual(observed(plain,['min','fga'],2,2).game_id.tolist(),['a','b'])

    def test_invalid_team_pools_and_whp_targets_fail_explicitly(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error',RuntimeWarning)
            for values in ([1,np.nan],[1,np.inf]):
                with self.assertRaisesRegex(ValueError,'finite observed'):
                    integer_pool(values)
        np.testing.assert_array_equal(integer_pool([-1,0,1.2,2.8]),[0,0,1,3])
        frame = pd.DataFrame(dict(game_id=['a','b'],event_date=pd.date_range('2025-01-01',periods=2,tz='UTC'),
            available_at=pd.date_range('2025-01-02',periods=2,tz='UTC'),value=pd.Series([10,None],dtype='Int64')))
        with self.assertRaisesRegex(ValueError,'missing is not zero'):
            eligible_history(frame,pd.Timestamp('2026-01-01',tz='UTC'))

    def test_returning_player_uses_older_saved_season_with_no_download_or_future_data(self):
        self.assertEqual(seasons_for('NBA','2026-10-25T22:10:00Z'),[2024,2025,2026])
        self.assertEqual(seasons_for('NBA','2026-01-01T00:00:00Z'),[2023,2024,2025])
        self.assertEqual(seasons_for('NFL','2026-10-25T22:10:00Z'),[2025,2026])
        self.run_methods(include_future=True)

    def test_nullable_optional_workload_does_not_block_direct_whp(self):
        dates = pd.date_range('2026-01-01',periods=30,tz='UTC')
        frame = pd.DataFrame(dict(game_id=[str(i) for i in range(30)],event_date=dates,
            available_at=dates+pd.Timedelta(hours=8),value=[10+i%7 for i in range(30)],
            opportunity=pd.array([None if i%3 == 0 else 30 for i in range(30)],dtype='Int64')))
        cutoff = pd.Timestamp('2026-03-01',tz='UTC')
        actual = WeightedHistoricalPerformance().forecast(frame,cutoff)
        expected = WeightedHistoricalPerformance().forecast(frame.drop(columns='opportunity'),cutoff)
        self.assertEqual((actual.mean,actual.variance),(expected.mean,expected.variance))
        with self.assertRaisesRegex(ValueError,'complete historical opportunities'):
            WeightedHistoricalPerformance(replace(WHPConfig(),beta=.5)).forecast(frame,cutoff)

    def test_nba_team_codes_and_opponents_use_the_same_sport_mapping(self):
        codes = sorted(set(NBA_TEAM_CODES.values()))
        dates = pd.date_range('2026-01-01',periods=len(codes),tz='UTC')
        frame = pd.DataFrame(dict(game_id=[str(i) for i in range(len(codes))],entity_id=codes,
            entity_name=codes,event_date=dates,available_at=dates+pd.Timedelta(hours=8),
            team=codes,opponent=codes))
        actual = eligible(frame,pd.Timestamp('2026-03-01',tz='UTC'),'NBA')
        self.assertEqual(actual.team.tolist(),codes)
        self.assertEqual(actual.opponent.tolist(),codes)
        aliases = frame.head(4).copy()
        aliases['team'] = aliases['opponent'] = ['WSH','BRK','CHO','PHO']
        actual = eligible(aliases,pd.Timestamp('2026-03-01',tz='UTC'),'NBA')
        self.assertEqual(actual.team.tolist(),['WAS','BKN','CHA','PHX'])
        self.assertEqual(actual.team.tolist(),actual.opponent.tolist())
        actual = eligible(aliases,pd.Timestamp('2026-03-01',tz='UTC'),'NCAAF')
        self.assertEqual(actual.team.tolist(),aliases.team.tolist())
        aliases.loc[0,'opponent'] = None
        self.assertTrue(pd.isna(eligible(aliases,pd.Timestamp('2026-03-01',tz='UTC'),'NBA').iloc[0].opponent))
        aliases['team'] = aliases['opponent'] = ['WAS','LA','JAC','WSH']
        actual = eligible(aliases,pd.Timestamp('2026-03-01',tz='UTC'),'NFL')
        self.assertEqual(actual.team.tolist(),['WSH','LAR','JAX','WSH'])
        self.assertEqual(actual.team.tolist(),actual.opponent.tolist())
        self.run_methods(washington=True)

    def run_methods(self, include_future=False, washington=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);data = DataService(root/'sports.sqlite3');source = inputs()
            source_snapshot = snapshot()
            if washington:
                source_snapshot['away_team'] = 'Washington Wizards'
                for frames in source.values():
                    for frame in frames:
                        for column in ('team','opponent'):
                            # Mixed source aliases must map to the market's WAS.
                            frame[column] = frame[column].replace({'IND':'WSH'})
            for kind, frames in source.items():
                for season, frame in zip((2024,2026),frames):
                    with patch('sports_analytics.service.fetch',return_value=frame):
                        data.load(Query('NBA',season,kind))
            player_repo = PlayerHistoryRepository(root=root/'whp',data=data,client=Mock())
            base = replace(UsageOpportunityConfig(),simulations=1000)
            kinds = [
                ('WHP',lambda:WHPMarketService(player_repo)),
                ('UO',lambda:UsageOpportunityMarketService(repository=UsageHistoryRepository(data=data,root=root/'uo',player_repository=player_repo),config=base)),
                ('OAM',lambda:OpponentAdjustedMarketService(repository=MatchupHistoryRepository(data=data,root=root/'oam',player_repository=player_repo),
                    config=replace(MatchupConfig(),baseline=base))),
                ('PGE',lambda:PaceGameEnvironmentMarketService(repository=PaceHistoryRepository(data=data,root=root/'pge',player_repository=player_repo),
                    config=replace(PaceConfig(),baseline=base))),
            ]
            frozen = {kind:[frame.copy(deep=True) for frame in frames] for kind,frames in source.items()}
            results = {}
            with patch('requests.sessions.Session.request',side_effect=AssertionError('Forecasts must stay offline')), warnings.catch_warnings():
                warnings.simplefilter('error',RuntimeWarning)
                for name, make in kinds:
                    service = make();result = service.forecast(source_snapshot);results[name] = result
                    for row in source_snapshot['rows']:
                        price = result.selection(row)
                        self.assertIsNotNone(price,(name,result.errors))
                        self.assertTrue(np.isfinite([price.win,price.loss,price.push]).all())
                        self.assertAlmostEqual(price.win+price.loss+price.push,1)
                    self.assertNotIn('Calculated probability unavailable',str(market_sections(source_snapshot,'Points',result,method=name)))
                    if name != 'WHP':
                        self.assertEqual(next(iter(result.forecasts.values())).simulations,1000)
                    for fitted in result.forecasts.values():
                        self.assertLess(pd.Timestamp(fitted.diagnostics['last_game_date'],tz='UTC'),pd.Timestamp(snapshot()['pulled_at']))
                if include_future:
                    # Appending the linked game's result must not change any
                    # forecast, including fits sourced from an older season.
                    for kind, frames in source.items():
                        current = frames[1];future = current.tail(1).copy()
                        future['game_id'] = snapshot()['game_id']
                        future['event_date'] = pd.Timestamp('2026-10-25T22:10:00Z')
                        future['available_at'] = pd.Timestamp('2026-10-26T08:00:00Z')
                        future['pts'] = 999
                        with patch('sports_analytics.service.fetch',return_value=pd.concat([current,future],ignore_index=True)):
                            data.load(Query('NBA',2026,kind),refresh=True)
                    for name, make in kinds:
                        revised = make().forecast(source_snapshot)
                        for key, fitted in results[name].forecasts.items():
                            self.assertEqual(fitted.mean,revised.forecasts[key].mean,name)
                            self.assertEqual(fitted.variance,revised.forecasts[key].variance,name)
            for kind, frames in source.items():
                for original, copied in zip(frames,frozen[kind]):
                    pd.testing.assert_frame_equal(original,copied)
            player_repo.client.json.assert_not_called()

    def test_insufficient_current_team_role_shows_a_visible_reason(self):
        source = snapshot()
        forecast = SimpleNamespace(selection=lambda row:None,detail=lambda row:
            'Current-team opportunity history: Need 5 complete observed games; only 2 available.')
        rendered = str(market_sections(source,'Points',forecast,method='UO'))
        self.assertIn('Calculated probability unavailable',rendered)
        self.assertIn('only 2 available',rendered)
        self.assertEqual(rendered.count('Calculated probability unavailable'),1)


if __name__ == '__main__':
    main()
