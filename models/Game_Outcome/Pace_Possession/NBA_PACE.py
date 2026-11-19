"""Forecast shared NBA regulation pace and simulate full-game scores jointly."""

from dataclasses import dataclass, field
from functools import cached_property
import math

import numpy as np
import pandas as pd

from models.Game_Outcome.PPP.NBA_PPP import NBAPPPModel
from models.Game_Outcome.PPP.PPP import MarketProbability, PPPError, PPPForecast
from models.replay import CalibrationRows, calibration_tail, replay_memoized
from models.season_weights import calendar_weights


@dataclass(frozen=True)
class NBAPaceConfig:
    half_life_days: float = 90
    prior_season_weight: float = .2
    pace_ridge: float = 12
    efficiency_ridge: float = 14
    minimum_training_games: int = 100
    minimum_team_games: int = 8
    minimum_calibration_games: int = 80
    maximum_calibration_games: int = 250
    simulations: int = 40_000
    seed: int = 2026

    def __post_init__(self):
        if (not all(math.isfinite(x) and x > 0 for x in
                    (self.half_life_days, self.pace_ridge, self.efficiency_ridge))
                or not 0 < self.prior_season_weight <= 1):
            raise PPPError("PACE needs positive decay, ridge penalties and prior season weight.")
        sizes = (self.minimum_training_games, self.minimum_team_games, self.minimum_calibration_games,
                 self.maximum_calibration_games, self.simulations, self.seed)
        if (any(type(value) is not int for value in sizes) or min(sizes[:2]) < 1
                or not 2 <= self.minimum_calibration_games <= self.maximum_calibration_games
                or not 1000 <= self.simulations <= 200_000 or self.seed < 0):
            raise PPPError("Choose valid history requirements, 1,000–200,000 simulations and a nonnegative seed.")


def simulate(pace, pace_sd, rates, bias, covariance, draws, seed):
    rng = np.random.default_rng(seed)
    rates, bias, covariance = (np.asarray(values, dtype=float) for values in (rates, bias, covariance))
    possessions = np.maximum(1, np.rint(rng.normal(pace, pace_sd, draws)))
    noise = rng.multivariate_normal(bias, covariance, draws)
    scores = np.maximum(0, np.rint(possessions[:, None] * rates + noise)).astype(int)
    tied = np.flatnonzero(scores[:, 0] == scores[:, 1])
    regulation_ties = len(tied)
    # Scale five-minute uncertainty by elapsed time. Every tied game gets
    # another paired overtime period until the simulated game has a winner.
    fraction = 5 / 48
    while len(tied):
        volume = np.maximum(1, np.rint(rng.normal(possessions[tied] * fraction, pace_sd * math.sqrt(fraction))))
        overtime = rng.multivariate_normal(bias * fraction, covariance * fraction, len(tied))
        scores[tied] += np.maximum(0, np.rint(volume[:, None] * rates + overtime)).astype(int)
        tied = tied[scores[tied, 0] == scores[tied, 1]]
    return scores, regulation_ties


@dataclass
class NBAPaceForecast(PPPForecast):
    masses: dict = field(default_factory=dict)
    simulate_scores = staticmethod(simulate)

    @cached_property
    def cumulative(self):
        return {target: (mass[0], np.r_[0, np.cumsum(mass[1])]) for target, mass in self.masses.items()}

    def _price(self, target, line, greater):
        line = float(line)
        if not math.isfinite(line):
            raise PPPError("Market line must be finite.")
        origin, prefix = self.cumulative[target]
        before = min(len(prefix) - 1, max(0, math.ceil(line) - origin))
        after = min(len(prefix) - 1, max(0, math.floor(line) - origin + 1))
        size = int(prefix[-1])
        below, above, push = int(prefix[before]), size - int(prefix[after]), int(prefix[after] - prefix[before])
        return MarketProbability((above if greater else below) / size, (below if greater else above) / size, push / size)

    def spread(self, team, handicap):
        if team == self.home_team:
            return self._price("margin", -float(handicap), True)
        if team == self.away_team:
            return self._price("margin", float(handicap), False)
        raise PPPError("Spread selection does not match either forecast team.")

    def total(self, line, selection="Over"):
        if selection not in ("Over", "Under"):
            raise PPPError("Choose Over or Under for a total market.")
        return self._price("total", line, selection == "Over")

    def team_total(self, team, line, selection="Over"):
        if team not in (self.home_team, self.away_team) or selection not in ("Over", "Under"):
            raise PPPError("Choose a forecast team and Over or Under.")
        return self._price("home" if team == self.home_team else "away", line, selection == "Over")

    def predictive_interval(self, target, coverage):
        origin, prefix = self.cumulative[target]
        quantiles = np.asarray([(1 - coverage) / 2, (1 + coverage) / 2])
        return (origin + np.searchsorted(prefix[1:], quantiles * prefix[-1])).tolist()

    @classmethod
    def from_parameters(cls, home, away, parameters, diagnostics):
        pace, sd, rates, bias, covariance, draws, seed = parameters
        scores, ties = cls.simulate_scores(pace, sd, rates, bias, covariance, draws, seed)
        diagnostics = {**diagnostics, "simulation": parameters,
                       "projected_home_rating": 100 * rates[0], "projected_away_rating": 100 * rates[1]}
        return cls.from_scores(home, away, scores, ties, diagnostics)

    @classmethod
    def from_scores(cls, home, away, scores, regulation_ties, diagnostics):
        home_score, away_score = scores.T
        margin = home_score - away_score
        total = home_score + away_score
        masses = {}
        for target, values in (("home", home_score), ("away", away_score), ("margin", margin), ("total", total)):
            first = int(values.min())
            masses[target] = [first, np.bincount(values - first).tolist()]
        diagnostics = {**diagnostics, "regulation_tie_rate": regulation_ties / len(scores),
                       "score_covariance": np.cov(scores, rowvar=False).tolist()}
        return cls(home, away, float(home_score.mean()), float(away_score.mean()),
                   float(margin.std(ddof=1)), float(total.std(ddof=1)), diagnostics, masses)

    def apply_score_adjustment(self, delta, covariance):
        pace, sd, rates, bias, residual_covariance, draws, seed = self.diagnostics["simulation"]
        # Convert the full-game estimate into a per-possession rate shift so
        # the same lineup adjustment is reflected in regulation and overtime.
        rates = np.maximum(.05, np.asarray(rates) + delta / pace)
        parameters = [pace, sd, rates.tolist(), bias,
                      (np.asarray(residual_covariance) + self.adjustment_covariance(covariance, pace)).tolist(), draws, seed]
        adjusted = self.from_parameters(self.home_team, self.away_team, parameters, self.diagnostics)
        self._replace_distribution(adjusted)

    def _replace_distribution(self, adjusted):
        self.__dict__.update(adjusted.__dict__)
        self.__dict__.pop("cumulative", None)

    @staticmethod
    def adjustment_covariance(covariance, pace):
        return covariance


def ridge(design, targets, weights, penalty):
    weighted = design * weights[:, None]
    regularizer = np.eye(design.shape[1]) * penalty
    regularizer[0, 0] = 0  # League intercept is unpenalized.
    return np.linalg.solve(design.T @ weighted + regularizer, weighted.T @ targets)


class NBAPaceModel(NBAPPPModel):
    method = "PACE"
    forecast_class = NBAPaceForecast
    description = "NBA shared pace and opponent-adjusted possession efficiency"
    covariance_key = "regulation_score_covariance"
    residual_variance_floor = 1.
    limitations = ("Box-score possessions and five-minute overtime approximation. Historical regulation games "
                   "calibrate score noise. Travel, rest and lineup scenarios are outside the base fit.")

    def __init__(self, config=None):
        super().__init__(config or NBAPaceConfig())

    def _history(self, frame, as_of):
        if "elapsed_minutes" not in frame:
            raise PPPError(f"{self.method} needs NBA team box scores with observed game minutes. Refresh the saved NBA data.")
        minutes = pd.to_numeric(frame.elapsed_minutes, errors="coerce")
        periods = (minutes - 48) / 5
        valid = periods.ge(0) & np.isclose(periods, periods.round(), equal_nan=False)
        data = frame.loc[valid].assign(elapsed_minutes=minutes[valid])
        history = super()._history(data, as_of)
        if not history.groupby("game_id").elapsed_minutes.nunique().eq(1).all():
            raise PPPError("Paired NBA team boxes disagree on game duration.")
        if (history.points <= 0).any():
            raise PPPError(f"{self.method} requires positive final team scores.")
        return history

    @replay_memoized
    def _fit(self, history, cutoff):
        home = history.loc[history.is_home.eq(1)]
        if len(home) < self.config.minimum_training_games:
            raise PPPError(f"Not enough completed NBA games with observed duration to fit {self.method}.")
        teams = pd.Index(sorted(history.team.unique()))
        n = len(teams)
        weights = calendar_weights(home, cutoff, self.config.half_life_days, "NBA",
                                   self.config.prior_season_weight).to_numpy(copy=True)
        weights /= weights.max()
        hi, ai = teams.get_indexer(home.team), teams.get_indexer(home.opponent)
        rows = np.arange(len(home))
        design = np.zeros((len(home), 1 + n))
        design[:, 0] = design[rows, 1 + hi] = design[rows, 1 + ai] = 1
        pace = ridge(design, home.possessions.to_numpy() * 48 / home.elapsed_minutes.to_numpy(),
                     weights, self.config.pace_ridge)
        efficiency = self._fit_efficiency(home, n, hi, ai, weights)
        counts = history.team.value_counts().to_dict()
        return dict(teams={team: i for i, team in enumerate(teams)}, counts=counts, pace=pace, efficiency=efficiency)

    def _fit_efficiency(self, home, n, hi, ai, weights):
        # Fit log scoring rate so the offense and opposing defense effects
        # combine multiplicatively. Neutral games have no home/away effect.
        rows = np.arange(len(home))
        design = np.zeros((len(home) * 2, 2 + n * 2))
        design[:, 0] = 1
        venue = (~home["neutral"].fillna(False).to_numpy(dtype=bool)).astype(float) if "neutral" in home else np.ones(len(home))
        design[rows, 1], design[rows + len(home), 1] = venue, -venue
        design[rows, 2 + hi] = design[rows, 2 + n + ai] = 1
        design[rows + len(home), 2 + ai] = design[rows + len(home), 2 + n + hi] = 1
        rates = np.r_[home.points / home.possessions, home.points_allowed / home.possessions]
        return ridge(design, np.log(rates), np.tile(weights * home.possessions.to_numpy() / 100, 2),
                     self.config.efficiency_ridge)

    def _project(self, fit, home, away, neutral):
        pace, hi, ai = self._pace_matchup(fit, home, away)
        return pace, self._matchup_rates(fit["efficiency"], len(fit["teams"]), hi, ai, neutral)

    def _pace_matchup(self, fit, home, away):
        if home == away or min(fit["counts"].get(home, 0), fit["counts"].get(away, 0)) < self.config.minimum_team_games:
            raise PPPError(f"Both NBA teams need enough earlier appearances for {self.method}.")
        hi, ai = fit["teams"][home], fit["teams"][away]
        pace = fit["pace"]
        return float(pace[0] + pace[1 + hi] + pace[1 + ai]), hi, ai

    @staticmethod
    def _matchup_rates(efficiency, n, hi, ai, neutral):
        venue = efficiency[1] if not neutral else 0
        return np.exp(efficiency[0] + np.array([venue + efficiency[2 + hi] + efficiency[2 + n + ai],
                                               -venue + efficiency[2 + ai] + efficiency[2 + n + hi]]))

    @staticmethod
    def _scoring_residual(points, possessions, rates):
        return points - possessions * rates

    def _forecast_details(self, fit, home, away):
        return {}

    @replay_memoized
    def _validation_fold(self, training, held, cutoff):
        fit = self._fit(training, cutoff)
        records = []
        for row in held.itertuples(index=False):
            try:
                venue = getattr(row, "neutral", False)
                pace, rates = self._project(fit, row.team, row.opponent, bool(venue) if pd.notna(venue) else False)
            except PPPError:
                continue
            errors = self._scoring_residual(np.asarray([row.points, row.points_allowed]), row.possessions, rates)
            records.append(dict(game_id=row.game_id, date=row.event_date.isoformat(),
                                pace_error=row.possessions * 48 / row.elapsed_minutes - pace,
                                score_error=errors.tolist()))
        return records

    def _calibration(self, history):
        held_home = history.loc[history.is_home.eq(1)]
        folds = CalibrationRows(history)

        def evaluate(held):
            cutoff = held.event_date.iloc[0]
            training = history.loc[history.event_date.lt(cutoff) & history.available_at.lt(cutoff)]
            if len(training) < 2 * self.config.minimum_training_games:
                return []
            return folds.fold(self, self._validation_fold, training, held, cutoff)

        regulation = held_home.loc[held_home.elapsed_minutes.eq(48)]
        records = calibration_tail(regulation, "event_date", self.config.maximum_calibration_games, evaluate)
        if len(records) < self.config.minimum_calibration_games:
            raise PPPError(f"{self.method} requires {self.config.minimum_calibration_games} chronological regulation score pairs; found {len(records)}.")
        return records

    def predict(self, frame, home_team, away_team, as_of, neutral=False):
        history = self._history(frame, as_of)
        records = self._calibration(history)
        errors = np.asarray([record["score_error"] for record in records])
        pace_errors = np.asarray([record["pace_error"] for record in records])
        fit = self._fit(history, as_of)
        pace, rates = self._project(fit, home_team, away_team, neutral)
        pace = max(1., float(pace + pace_errors.mean()))
        bias = errors.mean(axis=0)
        if neutral:
            bias[:] = bias.mean()
        # A small variance floor keeps integer simulations usable in
        # near-deterministic calibration samples.
        covariance = np.cov(errors, rowvar=False) + np.eye(2) * self.residual_variance_floor
        pace_sd = max(.5, float(pace_errors.std(ddof=1)))
        diagnostics = dict(method=self.description, forecast_method=self.method, as_of=pd.Timestamp(as_of).isoformat(),
                           last_game_date=history.event_date.max().date().isoformat(),
                           training_games=len(history)//2, validation_games=len(records),
                           regulation_calibration_games=len(errors), projected_possessions=pace,
                           pace_mae=float(np.abs(pace_errors).mean()), pace_sd=pace_sd,
                           simulations=self.config.simulations, includes_overtime=True, limitations=self.limitations)
        diagnostics[self.covariance_key] = covariance.tolist()
        diagnostics.update(self._forecast_details(fit, home_team, away_team))
        parameters = [pace, pace_sd, rates.tolist(), bias.tolist(), covariance.tolist(),
                      self.config.simulations, self.config.seed]
        return self.forecast_class.from_parameters(home_team, away_team, parameters, diagnostics)

