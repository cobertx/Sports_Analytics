"""Exact equivalence of optimized OAM/PGE history preparation; no HTTP."""
from dataclasses import asdict,replace
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.season_weights import apply_season_weights, season_ids
from models.replay import replay_cache,replay_instance
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
    def test_incremental_forecasts_preserve_full_distributions_and_live_persistence(self):
        from scripts.check_research import player_inputs
        from models.Player_Stats.Opponent_Adjusted_Matchup import prediction_service as matchup
        from models.Player_Stats.Pace_Game_Environment import prediction_service as environment
        players,teams=player_inputs()
        for module,kind,config in [
            (matchup,matchup.OpponentAdjustedMarketService,MatchupConfig()),
            (environment,environment.PaceGameEnvironmentMarketService,PaceConfig()),
        ]:
            config=replace(config,minimum_league_games=4,league_window=16,
                           baseline=replace(config.baseline,simulations=1000,minimum_games=3))
            results=[]
            for persist in [True,False]:
                with tempfile.TemporaryDirectory() as directory,replay_cache(),patch(
                        'requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
                    repo=SimpleNamespace(root=Path(directory),load=lambda *args:(players.copy(),teams.copy()))
                    service=kind(repo,config,persist=persist)
                    cold=patch.object(module,'replay_instance',side_effect=lambda cls,cfg:cls(cfg)) if persist else nullcontext()
                    forecasts=[]
                    with cold:
                        for number in [32,33,34]:
                            game=teams[teams.game_id.eq(str(number))].iloc[0]
                            snapshot=dict(sport='NBA',game_id=str(number),home_team='Boston Celtics',away_team='Detroit Pistons',
                                commence_time=game.event_date.isoformat(),pulled_at=(game.event_date-pd.Timedelta(microseconds=1)).isoformat(),
                                rows=[dict(subject=name,market_key='player_points',market='Points',line=15.5,
                                    selection='Over',odds=None,group='Player Stats',updated='Not reported')
                                    for name in ['Player A','Player B','Player C']])
                            fitted=service.forecast(snapshot)
                            self.assertEqual(fitted.errors,{})
                            forecasts.append({key:asdict(value) for key,value in fitted.forecasts.items()})
                    self.assertEqual(bool(list(Path(directory).rglob('*.json'))),persist)
                    results.append(forecasts)
            self.assertEqual(results[0],results[1],kind.__name__)

    def test_incremental_defense_equals_cold_fits_after_appends_revisions_and_windows(self):
        rows=history().rename(columns=dict(team='offense',opponent='defense',volume='actual',allowed_volume='exposure'))
        rows['component']='nfl:catch:WR'
        rows=pd.concat([rows,rows.assign(component='nfl:rush_ypc:RB')],ignore_index=True)
        for sport in ['NBA','NFL','NCAAF']:
            config=replace(MatchupConfig.for_sport(sport),league_window=16,minimum_league_games=4,
                           defense_window=8,minimum_defense_games=2)
            revised=rows.copy();revised.loc[revised.index[5],'actual']+=7
            variants=[rows[rows.event_date.lt(rows.event_date.max())],rows,rows,revised,rows.iloc[8:]]
            actual=defense.ScheduleAdjustedDefense(config)
            for frame in variants:
                expected=defense.ScheduleAdjustedDefense(config).fit(frame)
                actual.fit(frame)
                self.assertEqual(actual.records,expected.records)
                self.assertEqual(actual.historical,expected.historical)
                for component in frame.component.unique():
                    for club in ['A','B','C','D']:
                        self.assertEqual(actual.profile(club,component),expected.profile(club,component))
            changed=replace(config,defense_prior=config.defense_prior+2)
            actual.config=changed;actual.fit(rows)
            expected=defense.ScheduleAdjustedDefense(changed).fit(rows)
            self.assertEqual(actual.records,expected.records)

    def test_incremental_pace_processes_only_new_dates_and_rebuilds_changed_inputs(self):
        rows=history();config=replace(PaceConfig(),minimum_league_games=4)
        actual=pace.PaceModel(config)
        prefix=rows.iloc[:-4]
        actual.fit(prefix)
        with patch.object(actual,'_validation_batch',wraps=actual._validation_batch) as batch:
            actual.fit(rows)
        self.assertEqual(batch.call_count,1)
        revised=rows.copy();revised.loc[revised.index[12],'volume']+=5
        # Appending another game at an existing date must replay that entire date.
        for frame in [rows,rows.iloc[:-2],rows,revised,rows.iloc[20:]]:
            expected=reference_fit(config,frame)
            actual.fit(frame)
            self.assertEqual(actual.training,expected.training)
            self.assertEqual(actual.residuals,expected.residuals)
            np.testing.assert_array_equal(actual.coefficients,expected.coefficients)

    def test_incremental_instances_are_separate_between_replays_and_configurations(self):
        config=PaceConfig()
        with replay_cache():
            first=replay_instance(pace.PaceModel,config)
            self.assertIs(first,replay_instance(pace.PaceModel,config))
            self.assertIsNot(first,replay_instance(pace.PaceModel,replace(config,ridge=config.ridge+1)))
        with replay_cache():self.assertIsNot(first,replay_instance(pace.PaceModel,config))

    def test_failed_incremental_batches_are_discarded_before_retry(self):
        rows=history();config=replace(PaceConfig(),minimum_league_games=4)
        model=pace.PaceModel(config).fit(rows.iloc[:-8])
        original=model._validation_batch
        calls=[]
        def interrupted(*args):
            calls.append(1)
            if len(calls)==2:raise ValueError('Interrupted fit')
            return original(*args)
        with patch.object(model,'_validation_batch',side_effect=interrupted),self.assertRaisesRegex(ValueError,'Interrupted'):
            model.fit(rows)
        model.fit(rows);expected=reference_fit(config,rows)
        self.assertEqual(model.training,expected.training)
        self.assertEqual(model.residuals,expected.residuals)
        np.testing.assert_array_equal(model.coefficients,expected.coefficients)

        features=rows.rename(columns=dict(team='offense',opponent='defense',volume='actual',allowed_volume='exposure')).assign(component='nba:oreb')
        config=replace(MatchupConfig(),minimum_league_games=4,minimum_defense_games=2)
        model=defense.ScheduleAdjustedDefense(config).fit(features.iloc[:-8])
        with patch.object(defense,'shrunk_factor',side_effect=ValueError('Interrupted fit')),self.assertRaisesRegex(ValueError,'Interrupted'):
            model.fit(features)
        model.fit(features);expected=defense.ScheduleAdjustedDefense(config).fit(features)
        self.assertEqual(model.records,expected.records)
        self.assertEqual(model.historical,expected.historical)

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
