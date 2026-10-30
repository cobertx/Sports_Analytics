"""Chronological, partially pooled ridge pace; held-forward residual scenarios."""
import numpy as np
import pandas as pd
from models.replay import replay_memoized
from models.Player_Stats.Usage_Opportunity.opportunity_model import history_weights
from models.season_weights import season_ids


class PaceModel:
    def __init__(self, config):
        self.config = config
        self.history = pd.DataFrame()
        self.training = []
        self.residuals = []
        self._fit_config = None

    def _feature_lookup(self, history):
        """Share the same eligible league and team summaries within a date batch."""
        config = self.config
        league_rows = history.tail(2*config.league_window)
        league = float(np.average(league_rows.volume, weights=history_weights(
            league_rows, 2*config.scenario_half_life, config.baseline)))
        summaries = {}
        def average(club, column):
            if club not in summaries:
                rows = history[history.team.eq(club)].tail(config.baseline.team_window)
                w = history_weights(rows, config.baseline.role_half_life, config.baseline) if len(rows) else None
                summaries[club] = rows, w, 1/float(w@w) if len(rows) else 0.
            rows,w,effective = summaries[club]
            if rows.empty:
                return league if column != 'margin' else 0.
            prior = league if column != 'margin' else 0.
            return (effective*float(w@rows[column].to_numpy(float))+config.pace_prior*prior)/(effective+config.pace_prior)
        def features(team, opponent):
            x = np.array([average(team,'volume')-league,average(opponent,'allowed_volume')-league])
            margin = (average(team,'margin')-average(opponent,'margin'))/2
            return league,x,margin
        return features

    def _features(self, history, team, opponent):
        return self._feature_lookup(history)(team, opponent)

    def _fit(self, training):
        if not training:
            return np.zeros(2)
        rows = training[-2*self.config.league_window:]
        x = np.array([r['x'] for r in rows])
        y = np.array([r['y'] for r in rows])
        w = history_weights(pd.DataFrame(rows),2*self.config.scenario_half_life,self.config.baseline)*len(rows)
        return np.linalg.solve(x.T@(w[:,None]*x)+self.config.ridge*np.eye(2),x.T@(w*y))

    @replay_memoized
    def _validation_batch(self, prefix, batch, training):
        coefficients = self._fit(training)
        features = self._feature_lookup(prefix)
        additions, residuals = [], []
        for r in batch.itertuples(index=False):
            league,x,margin = features(r.team,r.opponent)
            prediction = max(1.,float(league+x@coefficients))
            additions.append(dict(x=x.tolist(),y=float(r.volume-league),available_at=r.available_at,
                                  event_date=r.event_date,season=r.season))
            residuals.append(dict(game_id=r.game_id,team=r.team,opponent=r.opponent,
                event_date=r.event_date,available_at=r.available_at,season=r.season,residual=float(r.volume-prediction),
                predicted=prediction,predicted_margin=margin,margin=float(r.margin),extra=float(r.extra)))
        return additions, residuals

    def fit(self, rows):
        try:
            return self._extend_fit(rows)
        except Exception:
            self._fit_config = None
            raise

    def _extend_fit(self, rows):
        rows = rows.assign(season=season_ids(rows, self.config.baseline.sport))
        previous = self.history
        extend = (self._fit_config == self.config and not previous.empty and
                  len(rows) >= len(previous) and rows.iloc[:len(previous)].equals(previous) and
                  (len(rows) == len(previous) or rows.iloc[len(previous):].event_date.min() > previous.event_date.max()))
        offset = len(previous) if extend else 0
        self.history = rows.copy()
        self._fit_config = self.config
        if not extend:
            self.training,self.residuals = [],[]
        # Every target-date batch is predicted from strictly earlier available
        # results, before that batch's targets enter any coefficient fit.
        for when,batch in rows.iloc[offset:].groupby('event_date',sort=True):
            prefix = rows[rows.available_at.lt(when) & rows.event_date.lt(when)]
            if prefix.game_id.nunique() < self.config.minimum_league_games:
                continue
            # _fit only uses this suffix. Exclude older, unused records before
            # fingerprinting the memoized fold; retain the full audit history.
            training = [r for r in self.training if r['available_at'] < when][-2*self.config.league_window:]
            additions, residuals = self._validation_batch(prefix, batch, training)
            self.residuals.extend(residuals)
            self.training.extend(additions)
        if len({r['game_id'] for r in self.residuals}) < self.config.minimum_league_games:
            raise ValueError('PGE needs more chronological history for held-forward pace errors; prepare the prior season.')
        self.coefficients = self._fit(self.training)
        return self

    def predict(self, team, opponent):
        league,x,margin = self._features(self.history,team,opponent)
        return dict(volume=max(1.,float(league+x@self.coefficients)),margin=margin,
                    league_volume=league,own_deviation=float(x[0]),opponent_deviation=float(x[1]),
                    coefficients=self.coefficients.tolist())

    def validation(self):
        error = np.array([r['residual'] for r in self.residuals])
        return dict(team_games=len(error),mae=float(np.abs(error).mean()),rmse=float(np.sqrt((error**2).mean())),
                    bias=float(error.mean()),note='Expanding chronological volume errors; no betting-accuracy claim.')
