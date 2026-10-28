"""NFL Points Per Play: sections 1–5 of the supplied football strategy PDF.

2.2: multiplicative baseline; 2.3: ridge-regularized offense/defense log-link
production model with play exposure and decaying game weights; 3: shared drive
pace, plays/drive, and separate non-offensive scoring; 4.1: normal total/margin
approximations with variance learned from chronological held-out forecasts;
5: American odds, break-even probabilities and push-aware fair prices.

This is the report's normal baseline, not its preferred possession simulator.
Integer continuity correction represents pushes; it does not reproduce football
key-number frequencies. Weather/injury inputs are not assumed when unavailable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from models.replay import replay_memoized, calibration_tail
from models.season_weights import calendar_weights


class PPPError(ValueError):
    """Insufficient or invalid model observations; never substitute sample odds."""


def implied_probability(american_odds):
    if american_odds is None:
        return None
    value = float(american_odds)
    if not math.isfinite(value) or abs(value) < 100:
        raise PPPError("American odds must be finite and at least +100 or at most -100.")
    return 100 / (value + 100) if value > 0 else -value / (-value + 100)


def fair_american_odds(probability):
    if probability is None:
        return None
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise PPPError("Probability must be between zero and one.")
    if probability in (0, 1):
        return None  # No finite two-sided fair price at an endpoint.
    return 100 * (1 - probability) / probability if probability <= .5 else -100 * probability / (1 - probability)


def matchup_ppp(offensive_ppp, defensive_ppp, league_ppp):
    if (not all(math.isfinite(value) and value >= 0 for value in (offensive_ppp, defensive_ppp)) or
            not math.isfinite(league_ppp) or league_ppp <= 0):
        raise PPPError("Matchup PPP requires nonnegative rates and a positive league rate.")
    return offensive_ppp * defensive_ppp / league_ppp


@dataclass(frozen=True)
class MarketProbability:
    win: float
    loss: float
    push: float

    @property
    def probability(self):
        """Conditional win probability; comparable to a refundable bet's 1/odds."""
        settled = self.win + self.loss
        return self.win / settled if settled > 0 else None

    @property
    def odds(self):
        return fair_american_odds(self.probability)


@dataclass
class PPPForecast:
    home_team: str
    away_team: str
    home_mean: float
    away_mean: float
    margin_sd: float
    total_sd: float
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self):
        if (not all(math.isfinite(value) for value in (self.home_mean, self.away_mean, self.margin_sd, self.total_sd)) or
                min(self.home_mean, self.away_mean) < 0 or min(self.margin_sd, self.total_sd) <= 0):
            raise PPPError("Forecast score means and residual standard deviations are invalid.")

    @property
    def total_mean(self):
        return self.home_mean + self.away_mean

    @property
    def margin_mean(self):
        return self.home_mean - self.away_mean

    @staticmethod
    def _settlement(mean, sd, threshold, greater=True, nonnegative=False):
        if not math.isfinite(threshold):
            raise PPPError("Market line must be finite.")
        normal = NormalDist(mean, sd)
        def cdf(integer):
            return 0.0 if nonnegative and integer < 0 else normal.cdf(integer + .5)
        below = cdf(math.ceil(threshold) - 1)
        above = 1 - cdf(math.floor(threshold))
        push = max(0.0, 1 - below - above)
        return MarketProbability(above if greater else below, below if greater else above, push)

    def spread(self, team, handicap):
        if team not in (self.home_team, self.away_team):
            raise PPPError("Spread selection does not match either forecast team.")
        mean = self.margin_mean if team == self.home_team else -self.margin_mean
        return self._settlement(mean, self.margin_sd, -float(handicap))

    def total(self, line, selection="Over"):
        if selection not in ("Over", "Under"):
            raise PPPError("Choose Over or Under for a total market.")
        return self._settlement(self.total_mean, self.total_sd, float(line), selection == "Over", nonnegative=True)

    def normal_over(self, line):
        """Uncorrected section 4.1 formula, also useful for verifying its example."""
        return 1 - NormalDist(self.total_mean, self.total_sd).cdf(line)


@dataclass(frozen=True)
class PPPConfig:
    half_life_days: float = 180
    ridge_penalty: float = 20
    minimum_training_games: int = 50
    minimum_team_games: int = 4
    minimum_calibration_games: int = 40
    maximum_calibration_games: int = 250
    prior_season_weight: float = .2

    def __post_init__(self):
        if (not 0 < self.prior_season_weight <= 1 or
                not math.isfinite(self.half_life_days) or self.half_life_days <= 0 or
                not math.isfinite(self.ridge_penalty) or self.ridge_penalty <= 0 or
                min(self.minimum_training_games, self.minimum_team_games) < 1 or
                self.minimum_calibration_games < 2 or self.maximum_calibration_games < self.minimum_calibration_games):
            raise PPPError("PPP configuration requires positive decay/regularization and enough validation games.")


class PPPModel:
    def __init__(self, config=None):
        self.config = config or PPPConfig()

    def _history(self, frame, as_of):
        required = {"game_id", "team", "opponent", "event_date", "available_at", "offensive_points", "points",
                    "offensive_plays", "defensive_plays", "offensive_drives", "defensive_drives",
                    "offensive_points_allowed", "non_offensive_points", "is_home", "neutral", "season", "week"}
        if not required <= set(frame):
            raise PPPError("Prepare the NFL team/game modeling dataset before fitting PPP.")
        frame = frame.copy()
        frame["event_date"] = pd.to_datetime(frame["event_date"], utc=True)
        frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True)
        cutoff = pd.Timestamp(as_of)
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        frame = frame[frame["available_at"].lt(cutoff) & frame["event_date"].lt(cutoff.normalize())].copy()
        numeric = ["offensive_points", "points", "offensive_plays", "defensive_plays", "offensive_drives",
                   "defensive_drives", "offensive_points_allowed", "non_offensive_points", "is_home", "neutral"]
        if (frame.empty or frame.duplicated(["game_id", "team"]).any() or
                not np.isfinite(frame[numeric].to_numpy(dtype=float)).all() or
                (frame[numeric] < 0).any().any() or
                (frame[["offensive_plays", "defensive_plays", "offensive_drives", "defensive_drives"]] <= 0).any().any()):
            raise PPPError("Model training requires unique, complete, finite team-game observations.")
        if not frame.groupby("game_id").size().eq(2).all():
            raise PPPError("Each training game must contain both teams.")
        frame["weight"] = calendar_weights(frame, cutoff, self.config.half_life_days, 'NFL', self.config.prior_season_weight)
        return frame.sort_values(["event_date", "game_id", "is_home"])

    @replay_memoized
    def _fit(self, frame):
        if frame["game_id"].nunique() < self.config.minimum_training_games:
            raise PPPError("Not enough completed games to fit PPP ratings.")
        teams = sorted(set(frame["team"]) | set(frame["opponent"]))
        index = {team: i for i, team in enumerate(teams)}
        n = len(teams)
        x = np.zeros((len(frame), 2 + 2 * n))
        x[:, 0] = 1
        x[:, 1] = (2 * frame["is_home"].to_numpy() - 1) * (1 - frame["neutral"].to_numpy()) / 2
        x[np.arange(len(frame)), 2 + frame["team"].map(index).to_numpy()] = 1
        x[np.arange(len(frame)), 2 + n + frame["opponent"].map(index).to_numpy()] = 1
        y = frame["offensive_points"].to_numpy(float)
        exposure = frame["offensive_plays"].to_numpy(float)
        weights = frame["weight"].to_numpy(float)
        league = np.dot(weights, y) / np.dot(weights, exposure)
        if league <= 0:
            raise PPPError("Positive league offensive scoring is required to fit PPP.")
        coefficients = np.zeros(x.shape[1])
        coefficients[0] = math.log(league)
        penalty = np.full(x.shape[1], self.config.ridge_penalty)
        penalty[0], penalty[1] = 0, self.config.ridge_penalty / 10
        def objective(beta):
            eta = np.clip(x @ beta, -15, 5)
            return np.sum(weights * (exposure * np.exp(eta) - y * eta)) + .5 * np.dot(penalty, beta * beta)
        for _ in range(35):
            predicted = exposure * np.exp(np.clip(x @ coefficients, -15, 5))
            gradient = x.T @ (weights * (predicted - y)) + penalty * coefficients
            hessian = x.T @ ((weights * predicted)[:, None] * x) + np.diag(penalty)
            change = np.linalg.solve(hessian, gradient)
            step, current = 1.0, objective(coefficients)
            while step > 1 / 1024 and objective(coefficients - step * change) > current:
                step /= 2
            coefficients -= step * change
            if np.max(np.abs(step * change)) < 1e-7:
                break
        # Center both sets of team effects for an identifiable intercept.
        for start in (2, 2 + n):
            center = coefficients[start:start + n].mean()
            coefficients[start:start + n] -= center
            coefficients[0] += center
        return {"teams": index, "coefficients": coefficients, "league": league}

    def _means(self, frame, ratings, home, away, neutral=False):
        n = len(ratings["teams"])
        own = {team: frame[frame["team"].eq(team)] for team in (home, away)}
        if any(len(rows) < self.config.minimum_team_games for rows in own.values()):
            raise PPPError(f"Both teams need at least {self.config.minimum_team_games} prior games for a PPP estimate.")
        def average(rows, column):
            return np.average(rows[column], weights=rows["weight"])
        # Share a matchup-level drive forecast; each side has its own plays/drive.
        drives = np.mean([average(own[home], "offensive_drives"), average(own[home], "defensive_drives"),
                          average(own[away], "offensive_drives"), average(own[away], "defensive_drives")])
        league_delta = np.average(frame["non_offensive_points"], weights=frame["weight"])
        means, detail = [], {}
        for team, opponent, sign in ((home, away, 1), (away, home, -1)):
            rows, other = own[team], own[opponent]
            length = (average(rows, "offensive_plays") / average(rows, "offensive_drives") +
                      average(other, "defensive_plays") / average(other, "defensive_drives")) / 2
            plays = drives * length
            beta, indices = ratings["coefficients"], ratings["teams"]
            rate = math.exp(beta[0] + beta[1] * sign * (not neutral) / 2 + beta[2 + indices[team]] + beta[2 + n + indices[opponent]])
            delta = (np.dot(rows["weight"], rows["non_offensive_points"]) + 2 * league_delta) / (rows["weight"].sum() + 2)
            means.append(float(plays * rate + delta))
            detail[team] = {"expected_drives": float(drives), "expected_plays": float(plays),
                            "matchup_ppp": rate, "additional_points": float(delta), "prior_games": len(rows)}
        return means, detail

    @replay_memoized
    def _validation_fold(self, training, held_out, cutoff):
        training = self._history(training, cutoff)
        ratings = self._fit(training)
        errors = []
        for game_id, game in held_out.groupby("game_id"):
            home = game[game["is_home"].eq(1)]
            away = game[game["is_home"].eq(0)]
            if len(home) != 1 or len(away) != 1:
                continue
            h, a = home.iloc[0], away.iloc[0]
            try:
                means, _ = self._means(training, ratings, h["team"], a["team"], bool(h["neutral"]))
            except PPPError:
                continue
            errors.append({"game_id": game_id, "date": h["event_date"].isoformat(),
                           "home_error": float(h["points"] - means[0]), "away_error": float(a["points"] - means[1])})
        return errors

    def predict(self, observations, home_team, away_team, as_of, neutral=False):
        if home_team == away_team:
            raise PPPError("Choose two different NFL teams.")
        history = self._history(observations, as_of)
        # Entire game weeks are held out: no observation from a predicted week
        # enters its fit. Residuals are genuinely forward predictions, not fit errors.
        def evaluate(held_out):
            training = history[history["available_at"].lt(held_out["event_date"].min())]
            if training["game_id"].nunique() < self.config.minimum_training_games:
                return []
            # Outer forecast weights change each game; the fold recomputes its
            # own weights at its original cutoff, so omit outer weights from keys.
            return self._validation_fold(training.drop(columns="weight"),
                held_out.drop(columns="weight"), held_out["event_date"].min())
        errors = calibration_tail(history, ["season", "week"], self.config.maximum_calibration_games, evaluate)
        if len(errors) < self.config.minimum_calibration_games:
            raise PPPError(f"PPP needs at least {self.config.minimum_calibration_games} chronological validation games; found {len(errors)}.")
        residuals = np.array([[row["home_error"], row["away_error"]] for row in errors])
        covariance = np.cov(residuals, rowvar=False, ddof=1)
        margin_variance = covariance[0, 0] + covariance[1, 1] - 2 * covariance[0, 1]
        total_variance = covariance[0, 0] + covariance[1, 1] + 2 * covariance[0, 1]
        if min(margin_variance, total_variance) <= 0:
            raise PPPError("Historical validation residuals do not support a nonzero score distribution.")
        means, details = self._means(history, self._fit(history), home_team, away_team, neutral)
        # Correct historical mean forecast error without using the target outcome.
        means = np.maximum(0, np.asarray(means) + residuals.mean(axis=0))
        diagnostics = {"method": "PPP opponent-adjusted log-link / normal baseline (PDF 2.3, 3, 4.1, 5)",
                       "as_of": pd.Timestamp(as_of).isoformat(), "training_games": history["game_id"].nunique(),
                       "last_game_date": history["event_date"].max().date().isoformat(), "validation_games": len(errors),
                       "margin_mae": float(np.abs(residuals[:, 0] - residuals[:, 1]).mean()),
                       "total_mae": float(np.abs(residuals.sum(axis=1)).mean()), "score_covariance": covariance.tolist(),
                       "teams": details, "validation_residuals": errors,
                       "limitations": "Normal baseline; continuity-corrected pushes approximate football key numbers. No weather or injury adjustments."}
        return PPPForecast(home_team, away_team, float(means[0]), float(means[1]),
                           math.sqrt(margin_variance), math.sqrt(total_variance), diagnostics)


def main():
    import argparse
    from sports_analytics.nfl_model_data import NflModelData, atomic_text
    parser = argparse.ArgumentParser(description=__doc__)
    season = datetime.now().year - (datetime.now().month < 3)
    parser.add_argument("--seasons", nargs="+", type=int, default=[season - 1, season])
    parser.add_argument("--refresh", action="store_true", help="Refresh public source files")
    parser.add_argument("--home", help="Home NFL abbreviation, e.g. SF")
    parser.add_argument("--away", help="Away NFL abbreviation, e.g. WSH")
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    data = NflModelData()
    frame, notices = data.prepare(args.seasons, refresh=args.refresh)
    print(f"Prepared {frame.game_id.nunique()} games / {len(frame)} team-game rows in {data.directory}")
    for notice in notices:
        print(notice)
    if args.home and args.away:
        from dataclasses import asdict
        import json
        forecast = PPPModel().predict(frame, args.home, args.away, args.as_of)
        destination = data.directory / "forecasts" / f"ppp_{args.home.lower()}_{args.away.lower()}.json"
        atomic_text(destination, json.dumps(asdict(forecast), indent=2))
        print(f"Projected points: {args.away} {forecast.away_mean:.2f}, {args.home} {forecast.home_mean:.2f}; saved {destination}")


if __name__ == "__main__":
    main()
