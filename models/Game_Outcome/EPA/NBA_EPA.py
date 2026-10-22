"""NBA event-based EPV and opponent-adjusted EPA / normal-score baseline.

Fits expected remaining possession points from pre-action context. Starting
EPV plus signed possession residual ratings projects pregame efficiency; actual
future lineups, action outcomes and target pace never enter a prediction.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from models.replay import cached_calculation, fingerprint, calibration_tail
from models.Game_Outcome.PPP.PPP import PPPError
from models.Game_Outcome.PPP.NBA_PPP import NBAPPPConfig, NBAPPPForecast, NBAPPPModel

FEATURES = ("intercept", "clock", "quarter_2", "quarter_3", "quarter_4", "overtime",
            "elapsed", "offensive_rebounds", "points_so_far", "start_made", "start_missed",
            "start_turnover", "start_timeout", "previous_shot", "previous_rebound",
            "previous_turnover", "previous_free_throw")


def state_features(period, clock, elapsed, rebounds, points_so_far, start_type, previous_action):
    """All arguments are observed before the action whose remaining points are labeled."""
    previous = str(previous_action).lower().replace(" ", "")
    return np.asarray([1., clock / (720 if period <= 4 else 300), period == 2, period == 3,
                       period == 4, period > 4, elapsed / 24, rebounds, points_so_far / 3,
                       start_type == "OffMadeShot", start_type == "OffMissedShot",
                       start_type == "OffLiveBallTurnover", start_type == "OffTimeout",
                       "shot" in previous, "rebound" in previous,
                       "turnover" in previous or "steal" in previous, "freethrow" in previous], dtype=float)


def expected_possession_value(features, coefficients):
    """Nonnegative remaining-points estimate for an observed pre-action state."""
    value = float(np.asarray(features, dtype=float) @ np.asarray(coefficients, dtype=float))
    if not math.isfinite(value):
        raise PPPError("NBA state-value inputs must be finite.")
    return max(0., value)


@dataclass(frozen=True)
class NBAEPAConfig(NBAPPPConfig):
    state_ridge: float = 50
    team_ridge: float = 200

    def __post_init__(self):
        super().__post_init__()
        if not all(math.isfinite(x) and x > 0 for x in (self.state_ridge, self.team_ridge)):
            raise PPPError("NBA EPV regularization must be positive and finite.")


@dataclass
class NBAEPAForecast(NBAPPPForecast):
    """NBA full-game spread/total settlement using independently fitted EPV means."""


class NBAEPAModel(NBAPPPModel):
    def __init__(self, config=None):
        super().__init__(config or NBAEPAConfig())

    def _fit_epv(self, history, dataset, as_of):
        if history.game_id.nunique() < self.config.minimum_training_games:
            raise PPPError("Not enough reconciled NBA games for EPA/EPV.")
        ids = history.drop_duplicates("game_id").set_index("game_id").event_date
        indices = [i for i, game_id in enumerate(dataset.game_ids) if game_id in ids.index]
        if len(indices) != len(ids):
            raise PPPError("NBA EPV state statistics are missing for training games.")
        ages = np.asarray([(pd.Timestamp(as_of) - ids[dataset.game_ids[i]]).total_seconds() / 86400 for i in indices])
        weights = np.exp2(-ages / self.config.half_life_days)
        penalty = np.full(len(FEATURES), self.config.state_ridge)
        penalty[0] = 0
        xx = np.einsum("i,ijk->jk", weights, dataset.state_xx[indices])
        xy = np.einsum("i,ij->j", weights, dataset.state_xy[indices])
        state_beta = np.linalg.solve(xx + np.diag(penalty), xy)
        teams = sorted(set(history.team) | set(history.opponent))
        mapping, n = {team: i for i, team in enumerate(teams)}, len(teams)
        x = np.zeros((len(history), 2 + 2*n))
        x[:, 0] = 1
        x[:, 1] = (history.is_home.to_numpy()*2 - 1) / 2
        x[np.arange(len(history)), 2 + history.team.map(mapping).to_numpy()] = 1
        x[np.arange(len(history)), 2 + n + history.opponent.map(mapping).to_numpy()] = 1
        start = history[["start_"+name for name in FEATURES]].to_numpy(float)
        baseline = start @ state_beta
        if not np.isfinite(baseline).all() or (baseline <= 0).any():
            raise PPPError("NBA EPV starting-possession baselines must be positive and finite.")
        # Sum of reward + V(next)-V(before) telescopes to possession points
        # minus initial EPV. The response therefore has expected-points units.
        residual = history.offensive_points.to_numpy() / history.offensive_possessions.to_numpy() - baseline
        w = np.exp2(-(pd.Timestamp(as_of) - history.event_date).dt.total_seconds().to_numpy() / 86400 / self.config.half_life_days)
        w *= history.offensive_possessions.to_numpy()
        ridge = np.full(x.shape[1], self.config.team_ridge)
        ridge[0], ridge[1] = 0, self.config.team_ridge / 10
        team_beta = np.linalg.solve(x.T @ (w[:, None]*x) + np.diag(ridge), x.T @ (w*residual))
        for first in (2, 2+n):
            center = team_beta[first:first+n].mean()
            team_beta[first:first+n] -= center
            team_beta[0] += center
        return {"state_beta": state_beta, "team_beta": team_beta, "teams": mapping}

    def _epv_means(self, history, fitted, home, away, as_of, neutral=False):
        own = {team: history[history.team.eq(team)] for team in (home, away)}
        if home == away or any(len(frame) < self.config.minimum_team_games for frame in own.values()):
            raise PPPError("Both NBA teams need enough reconciled prior games for EPA/EPV.")
        def average(frame, columns):
            weights = np.exp2(-(pd.Timestamp(as_of)-frame.event_date).dt.total_seconds().to_numpy()/86400/self.config.half_life_days)
            return np.average(frame[columns].to_numpy(float), weights=weights, axis=0)
        pace = float(np.mean([average(own[home], "offensive_possessions"), average(own[home], "defensive_possessions"),
                              average(own[away], "offensive_possessions"), average(own[away], "defensive_possessions")]))
        league_extra = float(average(history, "additional_points"))
        means, details = [], {}
        n, beta, mapping = len(fitted["teams"]), fitted["team_beta"], fitted["teams"]
        for team, opponent, sign in [(home, away, 1), (away, home, -1)]:
            frame, other = own[team], own[opponent]
            # Project starting context from earlier own/offensive and opposing
            # defensive possession contexts. No future actual context/pace is used.
            start = (average(frame, ["start_"+x for x in FEATURES]) +
                     average(other, ["def_start_"+x for x in FEATURES])) / 2
            baseline = float(start @ fitted["state_beta"])
            if not math.isfinite(baseline) or baseline <= 0:
                raise PPPError("NBA EPV projected starting value is invalid.")
            advantage = float(beta[0] + beta[2+mapping[team]] + beta[2+n+mapping[opponent]] +
                              (0 if neutral else sign*beta[1]/2))
            efficiency = baseline + advantage
            if not math.isfinite(efficiency) or efficiency <= 0:
                raise PPPError("NBA EPA/EPV fitted an invalid matchup efficiency.")
            extra = (float(average(frame, "additional_points"))*len(frame) + 10*league_extra)/(len(frame)+10)
            means.append(pace*efficiency + extra)
            details[team] = {"expected_possessions": pace, "starting_epv": baseline,
                             "epa_advantage_per_possession": advantage, "expected_points_per_possession": efficiency,
                             "additional_points": extra, "prior_games": len(frame)}
        return np.asarray(means), details

    def _cached_fit_epv(self, history, dataset, cutoff, state_key):
        return cached_calculation(self, 'fit_epv', (history, cutoff, state_key),
                                  lambda: self._fit_epv(history, dataset, cutoff))

    def _validation_fold(self, training, held, cutoff, dataset, state_key):
        fitted = self._cached_fit_epv(training, dataset, cutoff, state_key)
        errors = []
        for game_id, game in held.groupby("game_id"):
            h, a = game[game.is_home.eq(1)].iloc[0], game[game.is_home.eq(0)].iloc[0]
            try:
                means, _ = self._epv_means(training, fitted, h.team, a.team, cutoff)
            except PPPError:
                continue
            errors.append({"game_id": game_id, "date": h.event_date.isoformat(),
                           "home_error": float(h.points-means[0]), "away_error": float(a.points-means[1])})
        return errors

    def predict(self, dataset, home_team, away_team, as_of, neutral=False):
        history = self._history(dataset.games, as_of)
        required = {"offensive_points", "additional_points", "offensive_possessions", "defensive_possessions"} | {prefix+x for prefix in ("start_", "def_start_") for x in FEATURES}
        if not required <= set(history) or not np.isfinite(history[sorted(required)].to_numpy(float)).all():
            raise PPPError("Prepare reconciled NBA EPV states and possession contexts before fitting EPA.")
        # The changing dataset.games is already represented by each training
        # frame. Fingerprint the shared sufficient statistics once per forecast.
        state_key = fingerprint((dataset.game_ids, dataset.state_xx, dataset.state_xy))
        weeks = history.event_date.dt.strftime("%G-%V")
        def evaluate(held):
            cutoff = held.event_date.min()
            training = history[history.available_at.lt(cutoff) & history.event_date.lt(cutoff)]
            if training.game_id.nunique() < self.config.minimum_training_games:
                return []
            return cached_calculation(self, 'epv_validation_fold', (training, held, cutoff, state_key),
                lambda: self._validation_fold(training, held, cutoff, dataset, state_key))
        errors = calibration_tail(history, weeks, self.config.maximum_calibration_games, evaluate)
        if len(errors) < self.config.minimum_calibration_games:
            raise PPPError(f"NBA EPA needs {self.config.minimum_calibration_games} chronological validation games; found {len(errors)}.")
        residuals = np.asarray([[x["home_error"],x["away_error"]] for x in errors])
        covariance = np.cov(residuals, rowvar=False, ddof=1)
        margin_var = covariance[0,0]+covariance[1,1]-2*covariance[0,1]
        total_var = covariance.sum()
        if min(margin_var,total_var) <= 0:
            raise PPPError("NBA EPA validation residuals do not support nonzero score uncertainty.")
        fitted = self._cached_fit_epv(history, dataset, as_of, state_key)
        means, teams = self._epv_means(history, fitted, home_team, away_team, as_of, neutral)
        bias = residuals.mean(axis=0)
        means += bias.mean() if neutral else bias
        diagnostics = {"method": "NBA event-based EPV ridge / signed possession residuals / normal score-error baseline",
                       "forecast_method": "EPA", "as_of": pd.Timestamp(as_of).isoformat(),
                       "last_game_date": history.event_date.max().date().isoformat(), "training_games": history.game_id.nunique(),
                       "validation_games": len(errors), "validation_residuals": errors, "score_covariance": covariance.tolist(),
                       "margin_mae": float(np.abs(residuals[:,0]-residuals[:,1]).mean()),
                       "total_mae": float(np.abs(residuals.sum(axis=1)).mean()), "teams": teams,
                       "epv_features": list(FEATURES), "state_coefficients": fitted["state_beta"].tolist(),
                       "data_quality": dataset.quality,
                       "limitations": "Public event-based EPV research baseline. Normal full-game residuals; no tracking, shot clock, injury/lineup forecast or explicit overtime simulator. Calibration fits are not untouched-future performance."}
        return NBAEPAForecast(home_team, away_team, float(means[0]), float(means[1]), math.sqrt(margin_var), math.sqrt(total_var), diagnostics)


def main():
    import argparse, json
    from dataclasses import asdict
    from datetime import datetime, timezone
    from models.Game_Outcome.EPA.nba_epa_training import NBAEPAData
    from sports_analytics.nfl_model_data import atomic_text
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons",nargs="+",type=int,default=[2025])
    parser.add_argument("--home",required=True)
    parser.add_argument("--away",required=True)
    parser.add_argument("--as-of",default=datetime.now(timezone.utc).isoformat())
    args=parser.parse_args()
    data=NBAEPAData()
    dataset,notices=data.prepare(args.seasons)
    result=NBAEPAModel().predict(dataset,args.home,args.away,args.as_of)
    result.diagnostics["source_notices"]=notices
    path=data.directory/"forecasts"/f"epa_{args.home.lower()}_{args.away.lower()}.json"
    atomic_text(path,json.dumps(asdict(result),indent=2))
    print(f"NBA EPA: {args.away} {result.away_mean:.2f}, {args.home} {result.home_mean:.2f}; {path}")


if __name__ == "__main__":
    main()
