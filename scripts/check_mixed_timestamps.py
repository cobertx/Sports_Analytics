"""Equivalent forecast history for CSV, ISO, offset and fractional timestamps."""
from dataclasses import asdict, replace
from pathlib import Path
import sys
from unittest import TestCase, main

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_backtesting import team_history
from sports_analytics.backtesting_service import game_model
from sports_analytics.history_freshness import retain_first_availability
from sports_analytics.nba_model_data import aggregate_team_games
from models.Player_Stats.Weighted_Historical_Performance.core import eligible_history
from models.season_weights import season_ids


def mixed_dates(frame):
    result=frame.copy()
    for column in ('event_date','available_at'):
        values=[]
        for i,value in enumerate(frame[column]):
            stamp=pd.Timestamp(value)
            if i%4==0:values.append(stamp.isoformat())
            elif i%4==1:values.append(stamp.isoformat(sep=' '))
            elif i%4==2:values.append(stamp.isoformat().replace('+00:00','Z'))
            else:values.append(stamp.tz_convert('America/New_York').isoformat())
        result[column]=values
    return result


class MixedTimestampChecks(TestCase):
    def setUp(self):
        self.frame=team_history()
        self.frame['available_at']+=pd.Timedelta(microseconds=123456)
        self.mixed=mixed_dates(self.frame)

    def test_all_nine_game_models_preserve_history_weights_and_historical_cutoffs(self):
        for sport in ('NFL','NBA','NCAAF'):
            for key in ('ppp','epa','elo'):
                model=game_model(sport,key)
                for cutoff in ('2025-12-01T12:00:00Z','2025-08-13T08:00:00.123456Z'):
                    with self.subTest(sport=sport,model=key,cutoff=cutoff):
                        expected=model._history(self.frame,cutoff)
                        actual=model._history(self.mixed,cutoff)
                        pd.testing.assert_frame_equal(actual,expected,check_dtype=False)
                        self.assertTrue(actual.event_date.lt(pd.Timestamp(cutoff).normalize()).all())
                        self.assertTrue(actual.available_at.lt(pd.Timestamp(cutoff)).all())

    def test_complete_ppp_epa_and_elo_forecasts_match_canonical_timestamps(self):
        for key in ('ppp','epa','elo'):
            with self.subTest(method=key):
                model=game_model('NFL',key)
                model.config=replace(model.config,minimum_training_games=6,minimum_team_games=2,
                    minimum_calibration_games=4,maximum_calibration_games=10)
                expected=model.predict(self.frame,'A','B','2025-12-01T12:00:00Z')
                actual=model.predict(self.mixed,'A','B','2025-12-01T12:00:00Z')
                self.assertEqual(asdict(actual),asdict(expected))

    def test_invalid_team_timestamps_still_raise_instead_of_entering_a_forecast(self):
        for sport in ('NFL','NBA','NCAAF'):
            with self.subTest(sport=sport):
                broken=self.mixed.copy();broken.loc[0,'available_at']='not-a-date'
                with self.assertRaises(ValueError):game_model(sport,'ppp')._history(broken,'2025-12-01T12:00:00Z')

    def test_player_history_does_not_silently_drop_valid_mixed_dates(self):
        source=self.frame.drop_duplicates('game_id').assign(value=lambda f:f.points)
        cutoff='2025-08-13T08:00:00.123456Z'
        expected=eligible_history(source,cutoff)
        actual=eligible_history(mixed_dates(source),cutoff)
        pd.testing.assert_frame_equal(actual,expected,check_dtype=False)
        self.assertTrue(actual.available_at.lt(pd.Timestamp(cutoff)).all())

    def test_prior_season_inference_accepts_mixed_event_dates(self):
        for sport in ('NBA','NFL','NCAAF'):
            pd.testing.assert_series_equal(season_ids(self.mixed.drop(columns='season'),sport),
                season_ids(self.frame.drop(columns='season'),sport),check_dtype=False)

    def test_first_observed_availability_is_retained_across_csv_formats(self):
        old=self.frame.drop_duplicates('game_id').assign(entity_id='player')
        recent=old.assign(available_at=old.available_at+pd.Timedelta(hours=1))
        expected=retain_first_availability(recent.copy(),old,['game_id','entity_id'])
        actual=retain_first_availability(mixed_dates(recent),mixed_dates(old),['game_id','entity_id'])
        self.assertEqual(actual.available_at.tolist(),expected.available_at.tolist())

    def test_nba_preparation_keeps_both_sides_with_mixed_timestamp_formats(self):
        boxes=self.frame.assign(pts=self.frame.points,fga=90,fta=20,oreb=10,tov=12)
        boxes['matchup']=boxes.groupby('game_id').team.transform(lambda rows:rows.iloc[1]+' @ '+rows.iloc[0])
        pd.testing.assert_frame_equal(aggregate_team_games(mixed_dates(boxes)),aggregate_team_games(boxes))


if __name__=='__main__':main()
