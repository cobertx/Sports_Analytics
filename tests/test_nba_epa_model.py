"""Event-label reconstruction, chronological NBA EPV forecasts and saved caches."""

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from Models.Game_Outcome.EPA.NBA_EPA import FEATURES, NBAEPAConfig, NBAEPAForecast, NBAEPAModel, expected_possession_value, state_features
from Models.Game_Outcome.EPA.nba_epa_training import NBAEPADataset, reconcile_game
from Models.Game_Outcome.PPP.PPP import PPPError
from sports_analytics.nba_epa_service import NBAEPAMarketService


def example_game():
    box = pd.DataFrame([
        dict(game_id="0022500001", entity_id="11", team="GSW", matchup="LAL @ GSW", pts=4, event_date="2025-10-01"),
        dict(game_id="0022500001", entity_id="22", team="LAL", matchup="LAL @ GSW", pts=3, event_date="2025-10-01")])
    events = pd.DataFrame([
        dict(order_index=0, period=1, seconds_remaining=720, team_id=0, action_type="period", sub_type="start", description="Start", score_home=0, score_away=0),
        dict(order_index=1, period=1, seconds_remaining=700, team_id=11, action_type="Made Shot", sub_type="2pt", description="Two points", score_home=2, score_away=0),
        dict(order_index=2, period=1, seconds_remaining=680, team_id=22, action_type="Made Shot", sub_type="3pt", description="Three points", score_home=2, score_away=3),
        dict(order_index=3, period=1, seconds_remaining=680, team_id=11, action_type="Free Throw", sub_type="Technical", description="Technical FT", score_home=3, score_away=3),
        dict(order_index=4, period=1, seconds_remaining=680, team_id=11, action_type="Free Throw", sub_type="Technical", description="Technical FT", score_home=4, score_away=3)])
    possessions = pd.DataFrame([
        dict(period=1, possession_number=1, offense_team_id=11, defense_team_id=22, start_order_index=0, end_order_index=1, points=99, count_as_possession=True, possession_start_type="OffDeadball"),
        dict(period=1, possession_number=2, offense_team_id=22, defense_team_id=11, start_order_index=2, end_order_index=4, points=-1, count_as_possession=True, possession_start_type="OffMadeShot")])
    return possessions, events, box


def dataset(days=150):
    rng = np.random.default_rng(912)
    rows, xx, xy, game_ids = [], [], [], []
    teams = ["GSW", "LAL", "BOS", "DET"]
    for day in range(days):
        date = pd.Timestamp("2025-10-01", tz="UTC")+pd.Timedelta(days=day)
        home, away = teams[day%4], teams[(day+1+(day//4)%2)%4]
        game_id = f"fixture_{day}"
        points = rng.integers(90, 131, size=2)
        pace = float(rng.integers(90, 106))
        start = state_features(1, 720, 0, 0, 0, "OffDeadball", "")
        x = np.stack([start, state_features(1, 700, 20, 0, 0, "OffDeadball", "Missed Shot"),
                      state_features(3, 500, 0, 0, 0, "OffMadeShot", "Made Shot")])
        y = rng.uniform(.4, 2, 3)
        xx.append(x.T@x)
        xy.append(x.T@y)
        game_ids.append(game_id)
        for i, (team, opponent) in enumerate([(home,away),(away,home)]):
            rows.append(dict(game_id=game_id, team=team, opponent=opponent, event_date=date.isoformat(),
                             available_at=(date+pd.Timedelta(days=2)).isoformat(), is_home=int(i==0),
                             points=float(points[i]), points_allowed=float(points[1-i]), possessions=pace,
                             offensive_possessions=pace, defensive_possessions=pace, offensive_points=float(points[i]), additional_points=0,
                             **{"start_"+name:float(value) for name,value in zip(FEATURES,start)},
                             **{"def_start_"+name:float(value) for name,value in zip(FEATURES,start)}))
    return NBAEPADataset(pd.DataFrame(rows),game_ids,np.asarray(xx),np.asarray(xy),{"fixture":True})


class NBAEPAModelTests(unittest.TestCase):
    def test_epv_state_values_are_finite_and_nonnegative(self):
        x=state_features(1,720,0,0,0,'OffDeadball','')
        beta=np.zeros(len(FEATURES)); beta[0]=1.25
        self.assertEqual(expected_possession_value(x,beta),1.25)
        beta[0]=-1
        self.assertEqual(expected_possession_value(x,beta),0)
        beta[0]=np.nan
        with self.assertRaises(PPPError):
            expected_possession_value(x,beta)

    def test_score_labels_rebuilt_technical_points_separate_and_state_precedes_reward(self):
        p,e,box=example_game()
        rows,xx,xy,count,corrected,sample=reconcile_game(p,e,box)
        home=next(row for row in rows if row['team']=='GSW')
        away=next(row for row in rows if row['team']=='LAL')
        self.assertEqual((home['offensive_points'],home['additional_points'],home['points']),(2,2,4))
        self.assertEqual((away['offensive_points'],away['additional_points']),(3,0))
        self.assertEqual(corrected,2)
        self.assertEqual(count,5)
        before_shot=next(row for row in sample if row['order_index']==1)
        self.assertEqual(before_shot['points_so_far'],0)
        self.assertEqual(before_shot['remaining_points'],2)
        changed=p.copy(); changed['points']=777
        other=reconcile_game(changed,e,box)
        self.assertEqual(other[0],rows)
        self.assertTrue(np.array_equal(other[1],xx))
        self.assertTrue(np.array_equal(other[2],xy))

    def test_ambiguous_score_correction_wrong_ownership_and_incomplete_game_rejected(self):
        p,e,box=example_game()
        for kind in ['negative_score','wrong_owner','bad_final']:
            a,b,c=p.copy(),e.copy(),box.copy()
            if kind=='negative_score':
                b.loc[3,'score_home']=1
            elif kind=='wrong_owner':
                a.loc[0,['offense_team_id','defense_team_id']]=[22,11]
            else:
                c.loc[0,'pts']=10
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                reconcile_game(a,b,c)

    def test_chronological_state_statistics_and_future_poisoning_do_not_leak(self):
        d=dataset()
        config=NBAEPAConfig(minimum_training_games=20,minimum_team_games=4,minimum_calibration_games=15,maximum_calibration_games=30)
        model=NBAEPAModel(config)
        cutoff='2026-01-20T00:00:00+00:00'
        first=model.predict(d,'GSW','LAL',cutoff)
        self.assertIsInstance(first,NBAEPAForecast)
        self.assertEqual(first.diagnostics['validation_games'],30)
        self.assertGreater(first.margin_sd,0)
        self.assertGreater(first.total_sd,0)
        self.assertEqual(first.spread('GSW',0).push,0)
        self.assertAlmostEqual(first.total(220).probability+first.total(220,'Under').probability,1)
        poisoned=NBAEPADataset(d.games.copy(),d.game_ids.copy(),d.state_xx.copy(),d.state_xy.copy(),d.quality)
        future=pd.to_datetime(poisoned.games.available_at,utc=True).ge(pd.Timestamp(cutoff))
        future_ids=set(poisoned.games.loc[future,'game_id'])
        poisoned.games.loc[future,['points','points_allowed','offensive_points']]=9999
        for i,game_id in enumerate(poisoned.game_ids):
            if game_id in future_ids:
                poisoned.state_xx[i]*=10000
                poisoned.state_xy[i]*=10000
        second=model.predict(poisoned,'GSW','LAL',cutoff)
        self.assertEqual(asdict(first),asdict(second))
        with self.assertRaises(PPPError):
            model.predict(d,'UNKNOWN','LAL',cutoff)

    def test_epa_snapshot_cache_is_independent_and_restores_without_training(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Mock(directory=Path(directory)); data.prepare.return_value=(dataset(),[])
            model=Mock(config=NBAEPAConfig())
            model.predict.return_value=NBAEPAForecast('GSW','LAL',115,110,13,18,{'as_of':'2026-10-20T03:00:00+00:00'})
            snapshot={'home_team':'Golden State Warriors','away_team':'Los Angeles Lakers',
                      'pulled_at':'2026-10-20T03:00:00+00:00','commence_time':'2026-10-21T00:00:00Z'}
            with patch('requests.sessions.Session.request',side_effect=AssertionError('No odds requests')):
                first=NBAEPAMarketService(data,model).forecast(snapshot)
                restored=NBAEPAMarketService(data,model).forecast(snapshot)
            self.assertEqual(asdict(first),asdict(restored))
            self.assertIsInstance(restored,NBAEPAForecast)
            model.predict.assert_called_once()
            self.assertEqual(len(list((Path(directory)/'forecasts').glob('epa_*.json'))),1)


if __name__ == '__main__':
    unittest.main()
