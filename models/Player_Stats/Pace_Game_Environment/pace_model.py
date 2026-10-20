"""Chronological, partially pooled ridge pace; held-forward residual scenarios."""
import numpy as np
import pandas as pd
from models.Player_Stats.Usage_Opportunity.opportunity_model import weights


class PaceModel:
    def __init__(self, config):
        self.config = config
        self.history = pd.DataFrame()
        self.training = []
        self.residuals = []

    def _features(self, history, team, opponent):
        config = self.config
        league_rows = history.tail(2*config.league_window)
        league = float(league_rows.volume.mean())
        def average(club, column):
            rows = history[history.team.eq(club)].tail(config.baseline.team_window)
            if rows.empty:
                return league if column != 'margin' else 0.
            w = weights(len(rows), config.baseline.role_half_life)
            effective = 1/float(w@w)
            prior = league if column != 'margin' else 0.
            return (effective*float(w@rows[column].to_numpy(float))+config.pace_prior*prior)/(effective+config.pace_prior)
        x = np.array([average(team,'volume')-league,average(opponent,'allowed_volume')-league])
        margin = (average(team,'margin')-average(opponent,'margin'))/2
        return league,x,margin

    def _fit(self, training):
        if not training:
            return np.zeros(2)
        rows = training[-2*self.config.league_window:]
        x = np.array([r['x'] for r in rows])
        y = np.array([r['y'] for r in rows])
        w = weights(len(rows),2*self.config.scenario_half_life)*len(rows)
        return np.linalg.solve(x.T@(w[:,None]*x)+self.config.ridge*np.eye(2),x.T@(w*y))

    def fit(self, rows):
        self.history = rows.copy()
        self.training,self.residuals = [],[]
        # Every target-date batch is predicted from strictly earlier available
        # results, before that batch's targets enter any coefficient fit.
        for when,batch in rows.groupby('event_date',sort=True):
            prefix = rows[rows.available_at.lt(when) & rows.event_date.lt(when)]
            if prefix.game_id.nunique() < self.config.minimum_league_games:
                continue
            training = [r for r in self.training if r['available_at'] < when]
            coefficients = self._fit(training)
            additions = []
            for _,r in batch.iterrows():
                league,x,margin = self._features(prefix,r.team,r.opponent)
                prediction = max(1.,float(league+x@coefficients))
                additions.append(dict(x=x.tolist(),y=float(r.volume-league),available_at=r.available_at))
                self.residuals.append(dict(game_id=r.game_id,team=r.team,opponent=r.opponent,
                    event_date=r.event_date,available_at=r.available_at,residual=float(r.volume-prediction),
                    predicted=prediction,predicted_margin=margin,margin=float(r.margin),extra=float(r.extra)))
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
