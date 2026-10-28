"""Exact equivalence of optimized OAM/PGE history preparation; no HTTP."""
from dataclasses import replace
from pathlib import Path
import sys
from unittest import TestCase, main
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.season_weights import apply_season_weights, season_ids
from models.replay import replay_cache
from models.Player_Stats.Opponent_Adjusted_Matchup.config import MatchupConfig
from models.Player_Stats.Opponent_Adjusted_Matchup import schedule_strength as defense
from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
from models.Player_Stats.Pace_Game_Environment import pace_model as pace
from models.Player_Stats.Pace_Game_Environment.game_environment import GameEnvironment
from models.Player_Stats.Usage_Opportunity.opportunity_model import observed, history_weights


def reference_weights(records, half_life, config):
    return apply_season_weights(defense.recency(len(records), half_life), pd.DataFrame(records),
                               config.baseline.sport, config.baseline.prior_season_weight)


def reference_features(model, history, team, opponent):
    """Original per-column estimators, intentionally without summary reuse."""
    config = model.config
    league_rows = history.tail(2*config.league_window)
    league = float(np.average(league_rows.volume, weights=history_weights(
        league_rows, 2*config.scenario_half_life, config.baseline)))
    def average(club, column):
        rows = history[history.team.eq(club)].tail(config.baseline.team_window)
        if rows.empty:
            return league if column != 'margin' else 0.
        w = history_weights(rows, config.baseline.role_half_life, config.baseline)
        effective = 1/float(w@w)
        prior = league if column != 'margin' else 0.
        return (effective*float(w@rows[column].to_numpy(float))+config.pace_prior*prior)/(effective+config.pace_prior)
    return (league, np.array([average(team,'volume')-league,average(opponent,'allowed_volume')-league]),
            (average(team,'margin')-average(opponent,'margin'))/2)


def reference_fit(config, rows):
    model = pace.PaceModel(config)
    rows = rows.assign(season=season_ids(rows, config.baseline.sport))
    model.history = rows.copy()
    for when,batch in rows.groupby('event_date', sort=True):
        prefix = rows[rows.available_at.lt(when) & rows.event_date.lt(when)]
        if prefix.game_id.nunique() < config.minimum_league_games:
            continue
        # Keep all eligible records here to independently verify the fit suffix.
        coefficients = model._fit([r for r in model.training if r['available_at'] < when])
        for r in batch.itertuples(index=False):
            league,x,margin = reference_features(model, prefix, r.team, r.opponent)
            prediction = max(1.,float(league+x@coefficients))
            model.training.append(dict(x=x.tolist(),y=float(r.volume-league),available_at=r.available_at,
                                       event_date=r.event_date,season=r.season))
            model.residuals.append(dict(game_id=r.game_id,team=r.team,opponent=r.opponent,
                event_date=r.event_date,available_at=r.available_at,season=r.season,
                residual=float(r.volume-prediction),predicted=prediction,predicted_margin=margin,
                margin=float(r.margin),extra=float(r.extra)))
    model.coefficients = model._fit(model.training)
    return model


def history():
    rng, rows = np.random.default_rng(892), []
    for day in range(28):
        date = pd.Timestamp('2025-09-01', tz='UTC')+pd.Timedelta(days=3*day)
        for number,(home,away) in enumerate([('A','B'),('C','D')]):
            volume = rng.uniform(60,110,2)
            margin = float(rng.integers(-30,31))
            for i,(club,opponent) in enumerate([(home,away),(away,home)]):
                rows.append(dict(game_id=f'{day:02d}-{number}',team=club,opponent=opponent,
                    event_date=date,available_at=date+pd.Timedelta(days=3 if day%5==0 else 1),
                    season=2024 if day<10 else 2025,volume=float(volume[i]),allowed_volume=float(volume[1-i]),
                    margin=margin if i==0 else -margin,extra=5. if day%4==0 else 0.,duration=48.,source='box'))
    return pd.DataFrame(rows)


class PlayerBacktestSpeedChecks(TestCase):
    def test_defense_weights_are_exact_and_cache_does_not_share_mutable_arrays(self):
        for sport in ['NBA','NFL','NCAAF']:
            config = MatchupConfig.for_sport(sport)
            for half_life in [4.,8.]:
                for discount in [.2,.7]:
                    config = replace(config,baseline=replace(config.baseline,prior_season_weight=discount))
                    for seasons in [(2025,)*12,(2023,2024,2025,2024,2025),(2024,2025)]:
                        records = [dict(season=season) for season in seasons]
                        expected = reference_weights(records,half_life,config)
                        with patch.object(defense.pd,'DataFrame',side_effect=AssertionError('Numeric seasons need no table')):
                            actual = defense.record_weights(records,half_life,config)
                        np.testing.assert_array_equal(actual,expected)
                        actual[:] = 0
                        np.testing.assert_array_equal(defense.record_weights(records,half_life,config),expected)

    def test_legacy_season_inference_is_unchanged(self):
        for sport in ['NBA','NFL','NCAAF']:
            config = MatchupConfig.for_sport(sport)
            for records in [[],[dict(event_date='2025-09-30'),dict(event_date='2025-10-01')],
                            [dict(season='2024',event_date='2025-01-01'),dict(season=None,event_date='2025-10-01')]]:
                np.testing.assert_array_equal(defense.record_weights(records,8,config),reference_weights(records,8,config))

    def test_defense_fit_and_profiles_match_original_weights_with_delayed_results(self):
        rows = history().rename(columns=dict(team='offense',opponent='defense',volume='actual',allowed_volume='exposure'))
        rows['component'] = 'nfl:catch:WR'
        for sport in ['NBA','NFL','NCAAF']:
            config = replace(MatchupConfig.for_sport(sport),league_window=16,minimum_league_games=4,
                             defense_window=8,minimum_defense_games=2)
            actual = defense.ScheduleAdjustedDefense(config).fit(rows)
            with patch.object(defense,'record_weights',reference_weights):
                expected = defense.ScheduleAdjustedDefense(config).fit(rows)
                profiles = [expected.profile(club,'nfl:catch:WR') for club in ['A','B','C','D']]
            self.assertEqual(actual.records,expected.records)
            self.assertEqual(actual.historical,expected.historical)
            self.assertEqual([actual.profile(club,'nfl:catch:WR') for club in ['A','B','C','D']],profiles)

    def test_pace_refits_match_original_estimators_for_prefixes_revisions_and_windows(self):
        rows = history()
        for sport in ['NBA','NFL','NCAAF']:
            config = replace(PaceConfig.for_sport(sport),league_window=12,minimum_league_games=4,
                             baseline=replace(PaceConfig.for_sport(sport).baseline,team_window=8))
            revised = rows.copy()
            revised.loc[revised.index[-10],'volume'] += 13
            variants = [rows.iloc[:-4],rows,revised,rows.iloc[24:].drop(columns='season')]
            with replay_cache() as cache:
                for frame in variants:
                    expected = reference_fit(config,frame)
                    actual = pace.PaceModel(config).fit(frame)
                    self.assertEqual(actual.training,expected.training)
                    self.assertEqual(actual.residuals,expected.residuals)
                    np.testing.assert_array_equal(actual.coefficients,expected.coefficients)
                    self.assertEqual(actual.validation(),expected.validation())
                    for team,opponent in [('A','B'),('C','D'),('new','A')]:
                        league,x,margin = reference_features(expected,expected.history,team,opponent)
                        predicted = actual.predict(team,opponent)
                        self.assertEqual(predicted['volume'],max(1.,float(league+x@expected.coefficients)))
                        self.assertEqual(predicted['margin'],margin)
                self.assertGreater(cache.hits,0)

    def test_pace_batch_computes_team_weights_once(self):
        rows = history()
        model = pace.PaceModel(PaceConfig())
        features = model._feature_lookup(rows)
        with patch.object(pace,'history_weights',wraps=history_weights) as weights:
            features('A','B')
            features('B','A')
        self.assertEqual(weights.call_count,2)

    def test_nba_possession_totals_are_reused_without_changing_pool_values(self):
        config = replace(PaceConfig(),minimum_league_games=4,
                         baseline=replace(PaceConfig().baseline,simulations=1000))
        model = pace.PaceModel(config).fit(history())
        environment = GameEnvironment('NBA',model,['A','B'],config,np.random.default_rng(32))
        state = model.history[model.history.team.eq('A')].iloc[[0,3,7,12]*250].copy()
        totals = model.history.assign(total=lambda f:f.volume*f.duration/48).set_index(['game_id','team']).total.to_dict()
        source = np.array([totals[(str(game),'A')] for game in state.game_id])
        # Repeated stat pools must use the frozen fitted history, without
        # rebuilding a whole-league table or freezing the simulated volume.
        with patch.object(model.history,'assign',side_effect=AssertionError('Totals already prepared')):
            for column in ['volume','allowed_volume']:
                expected = state[column].to_numpy(float)/source*environment.volume['A']
                np.testing.assert_array_equal(environment.nba_pool('A',state,column),expected)
            environment.volume['A'] *= 1.2
            np.testing.assert_array_equal(environment.nba_pool('A',state,'volume'),
                state.volume.to_numpy(float)/source*environment.volume['A'])

    def test_numeric_and_imported_observations_keep_validation_and_ownership(self):
        numeric = history().rename(columns=dict(volume='min')).assign(count=3)
        numeric.loc[numeric.index[0],'count'] = np.nan
        for convert in [lambda f:f,lambda f:f.astype({'count':'Int64','min':'Float64'}),
                        lambda f:f.astype({'count':'string','min':'string'})]:
            frame = convert(numeric)
            original = frame.copy(deep=True)
            expected = frame.copy()
            expected[['count','min']] = expected[['count','min']].apply(pd.to_numeric,errors='coerce')
            finite = np.isfinite(expected[['count','min']].to_numpy(dtype=float,na_value=np.nan)).all(axis=1)
            expected = expected[finite].sort_values(['event_date','game_id']).tail(8)
            actual = observed(frame,['count','min'],3,8)
            pd.testing.assert_frame_equal(actual,expected)
            actual.loc[:,'count'] = 99
            pd.testing.assert_frame_equal(frame,original)
        with self.assertRaisesRegex(ValueError,'integer counts'):
            observed(numeric.assign(count=1.5),['count','min'],3,8)
        with self.assertRaisesRegex(ValueError,'complete observed games'):
            observed(numeric.assign(count=np.nan),['count','min'],3,8)


if __name__ == '__main__':
    main()
