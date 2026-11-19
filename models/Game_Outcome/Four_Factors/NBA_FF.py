"""Forecast box-score factors and reconstruct points per possession."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from models.Game_Outcome.Adjusted_Offensive_Defensive_Efficiency.NBA_AODE import finish_overtime
from models.Game_Outcome.PPP.PPP import PPPError
from models.Game_Outcome.Pace_Possession.NBA_PACE import NBAPaceConfig, NBAPaceForecast, NBAPaceModel
from models.replay import replay_memoized


FACTORS = ("efg", "turnover_rate", "offensive_rebound_share", "free_throw_rate", "ft_pct", "rebound_opportunities_per_fga")
N_FACTORS = len(FACTORS)
LATENT_COLUMNS = tuple("latent_" + factor for factor in FACTORS)
EXPOSURE_COLUMNS = tuple("exposure_" + factor for factor in FACTORS)
AWAY_LATENT = tuple("away_" + column for column in LATENT_COLUMNS)
AWAY_EXPOSURE = tuple("away_" + column for column in EXPOSURE_COLUMNS)
LOGIT_COLUMNS = [0, 1, 2, 4]
LOG_COLUMNS = [3, 5]
COUNTS = ("fgm", "fga", "fg3m", "ftm", "fta", "oreb", "opponent_dreb", "tov")
FREE_THROW_POSSESSION_WEIGHT = .44


@dataclass(frozen=True)
class NBAFourFactorsConfig(NBAPaceConfig):
    efficiency_ridge: float = 18.
    simulations: int = 50_000
    seed: int = 20261119


def scoring_rate(efg, turnover_rate, orb_per_fga, free_throw_rate, ft_pct):
    """Section 4 accounting identity for rates from consistent box counts."""
    actions = (1 + FREE_THROW_POSSESSION_WEIGHT * free_throw_rate) / (1 - turnover_rate)
    return (2 * efg + ft_pct * free_throw_rate) / (actions - orb_per_fga)


def observed_factors(counts):
    fgm, fga, threes, ftm, fta, oreb, opponent_dreb, turnovers = counts.T
    actions = fga + FREE_THROW_POSSESSION_WEIGHT * fta + turnovers
    rebounds = oreb + opponent_dreb
    # A zero exposure contributes no fitted weight or calibration error for
    # that factor. The value used for division is immaterial at zero weight.
    factors = np.column_stack([
        (fgm + .5 * threes) / fga, turnovers / actions, oreb / np.maximum(rebounds, 1),
        fta / fga, ftm / np.maximum(fta, 1), rebounds / fga,
    ])
    exposure = np.column_stack([fga, actions, rebounds, fga, fta, fga])
    return factors, exposure, actions - oreb


def valid_box_counts(history):
    counts = history[list(COUNTS)].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    fgm, fga, threes, ftm, fta, oreb, _, turnovers = counts.T
    valid = (np.isfinite(counts).all(axis=1) & (counts >= 0).all(axis=1)
             & np.equal(counts, np.rint(counts)).all(axis=1) & (fga > 0) & (oreb < fga)
             & (fgm <= fga) & (threes <= fgm) & (ftm <= fta)
             & (fga + FREE_THROW_POSSESSION_WEIGHT * fta + turnovers > oreb)
             & (2 * fgm + threes + ftm == history.points.to_numpy()))
    paired = pd.Series(valid, index=history.index).groupby(history.game_id).transform("all")
    return counts, paired


def to_latent(factors):
    transformed = factors.copy()
    transformed[..., 0] /= 1.5  # eFG's physical ceiling is all made threes.
    bounded = np.clip(transformed[..., LOGIT_COLUMNS], 1e-4, 1 - 1e-4)
    transformed[..., LOGIT_COLUMNS] = np.log(bounded) - np.log1p(-bounded)
    transformed[..., LOG_COLUMNS] = np.log(np.maximum(transformed[..., LOG_COLUMNS], 1e-4))
    return transformed


def from_latent(latent):
    factors = np.asarray(latent, dtype=float).copy()
    # logaddexp evaluates the logistic transformation without overflow.
    factors[..., LOGIT_COLUMNS] = np.exp(-np.logaddexp(0, -factors[..., LOGIT_COLUMNS]))
    with np.errstate(over="ignore"):
        factors[..., LOG_COLUMNS] = np.exp(factors[..., LOG_COLUMNS])
    factors[..., 0] *= 1.5
    return factors


def fit_factor_effects(home, n, hi, ai, recency, ridge):
    values = np.vstack([home[list(LATENT_COLUMNS)], home[list(AWAY_LATENT)]])
    exposure = np.vstack([home[list(EXPOSURE_COLUMNS)], home[list(AWAY_EXPOSURE)]])
    exposure_scale = exposure.mean(axis=0)
    if (exposure_scale <= 0).any():
        raise PPPError("Four Factors needs observed exposure for each factor.")
    weights = exposure * np.tile(recency, 2)[:, None] / exposure_scale
    venue = ~home.neutral.fillna(False).to_numpy(dtype=bool) if "neutral" in home else np.ones(len(home), dtype=bool)
    width = 2 + 2 * n
    indices = [np.zeros(len(values), dtype=int), np.ones(len(values), dtype=int),
               2 + np.r_[hi, ai], 2 + n + np.r_[ai, hi]]
    terms = [np.ones(len(values)), np.r_[venue.astype(float), -venue.astype(float)],
             np.ones(len(values)), np.ones(len(values))]
    # Each row has at most four entries. Accumulate their weighted products
    # directly instead of multiplying six dense team-by-opponent designs.
    gram = np.zeros((N_FACTORS, width, width))
    rhs = np.zeros((N_FACTORS, width))
    for i, (column, value) in enumerate(zip(indices, terms)):
        for k in range(N_FACTORS):
            rhs[k] += np.bincount(column, weights[:, k] * value * values[:, k], minlength=width)
        for j in range(i, len(indices)):
            cells = column * width + indices[j]
            products = value * terms[j]
            for k in range(N_FACTORS):
                block = np.bincount(cells, weights[:, k] * products, minlength=width ** 2).reshape(width, width)
                gram[k] += block
                if j != i:
                    gram[k] += block.T
    penalty = np.eye(width) * ridge
    penalty[0, 0] = 0
    return np.linalg.solve(gram + penalty, rhs[..., None])[..., 0].T


def factor_details(factors):
    values = dict(zip(FACTORS, map(float, factors)))
    values["orb_per_fga"] = values["offensive_rebound_share"] * values["rebound_opportunities_per_fga"]
    return values


def simulate(parameters):
    rng = np.random.default_rng(parameters["seed"])
    errors = np.asarray(parameters["paired_errors"])
    center = np.asarray(parameters["matchup_latent"])
    factors = from_latent(center + errors[:, 1:1 + 2 * N_FACTORS].reshape(-1, 2, N_FACTORS))
    efg, turnovers, rebounding, free_throws, accuracy, opportunities = np.moveaxis(factors, -1, 0)
    kappa = rebounding * opportunities
    pace = parameters["pace"] + errors[:, 0]
    # Omit residual combinations that yield impossible rebound rates or pace
    # at this matchup. Every simulated market uses the same accepted donors.
    eligible = np.isfinite(factors).all(axis=(1, 2)) & (kappa < 1).all(axis=1) & (turnovers < 1).all(axis=1) & (pace > 0)
    if np.count_nonzero(eligible) < 2:
        raise PPPError("Four Factors calibration could not produce valid matchup scenarios.")
    pace = pace[eligible]
    rates = scoring_rate(efg[eligible], turnovers[eligible], kappa[eligible], free_throws[eligible], accuracy[eligible])
    rates += errors[eligible, -2:] + parameters["rate_shift"]
    indices = rng.integers(len(pace), size=parameters["draws"])
    possessions = np.maximum(1, np.rint(pace[indices]))
    rate_covariance = np.cov(rates, rowvar=False) + np.eye(2) * .01 ** 2
    game_rates = rates[indices]
    availability_covariance = np.asarray(parameters["availability_covariance"])
    if availability_covariance.any():
        game_rates += rng.multivariate_normal([0., 0.], availability_covariance, len(indices))
        rate_covariance += availability_covariance
    game_rates = np.maximum(0, game_rates)
    scores = np.rint(possessions[:, None] * game_rates).astype(int)
    scores, ties = finish_overtime(scores, possessions, game_rates, float(pace.mean()),
                                   float(pace.std(ddof=1)), rate_covariance, rng)
    return scores, ties, pace


class NBAFourFactorsForecast(NBAPaceForecast):
    @classmethod
    def from_parameters(cls, home, away, parameters, diagnostics):
        scores, ties, pace = simulate(parameters)
        details = [factor_details(factors) for factors in from_latent(parameters["matchup_latent"])]
        ratings = [100 * (scoring_rate(row["efg"], row["turnover_rate"], row["orb_per_fga"],
                                       row["free_throw_rate"], row["ft_pct"]) + shift)
                   for row, shift in zip(details, parameters["rate_shift"])]
        diagnostics = {**diagnostics, "simulation": parameters, "usable_calibration_scenarios": len(pace),
                       "projected_possessions": float(pace.mean()), "pace_sd": float(pace.std(ddof=1)),
                       "projected_home_rating": ratings[0], "projected_away_rating": ratings[1],
                       "matchup_factors": dict(zip((home, away), details))}
        return cls.from_scores(home, away, scores, ties, diagnostics)

    def apply_score_adjustment(self, delta, covariance):
        pace = self.diagnostics["projected_possessions"]
        parameters = self.diagnostics["simulation"]
        adjusted = {**parameters,
                    "rate_shift": (np.asarray(parameters["rate_shift"]) + delta / pace).tolist(),
                    "availability_covariance": (np.asarray(parameters["availability_covariance"]) + covariance / pace ** 2).tolist()}
        self._replace_distribution(self.from_parameters(self.home_team, self.away_team, adjusted, self.diagnostics))


class NBAFourFactorsModel(NBAPaceModel):
    method = "FF"

    def __init__(self, config=None):
        super().__init__(config or NBAFourFactorsConfig())

    def _history(self, frame, as_of):
        if not set(COUNTS) <= set(frame):
            raise PPPError("Four Factors needs complete NBA shooting, turnover and rebound box counts.")
        history = super()._history(frame, as_of)
        counts, valid = valid_box_counts(history)
        history = history.loc[valid].copy()
        if history.empty:
            raise PPPError("No eligible NBA games have reconciled box counts for Four Factors.")
        factors, exposures, own_possessions = observed_factors(counts[valid.to_numpy()])
        history[list(LATENT_COLUMNS)] = to_latent(factors)
        history[list(EXPOSURE_COLUMNS)] = exposures
        # The factors account for scoring per team's estimated possessions.
        # The residual only bridges the team's denominator to shared pace.
        history["shared_pace_rate_error"] = history.points / history.possessions - history.points / own_possessions
        away = history.loc[history.is_home.eq(0)].set_index("game_id")
        history[list(AWAY_LATENT)] = away.loc[history.game_id, list(LATENT_COLUMNS)].to_numpy()
        history[list(AWAY_EXPOSURE)] = away.loc[history.game_id, list(EXPOSURE_COLUMNS)].to_numpy()
        history["away_shared_pace_rate_error"] = away.loc[history.game_id, "shared_pace_rate_error"].to_numpy()
        return history

    def _fit_efficiency(self, home, n, hi, ai, weights):
        return fit_factor_effects(home, n, hi, ai, weights, self.config.efficiency_ridge)

    def _project(self, fit, home, away, neutral):
        pace, hi, ai = self._pace_matchup(fit, home, away)
        effects = fit["efficiency"]
        n = len(fit["teams"])
        venue = 0. if neutral else effects[1]
        latent = np.stack([effects[0] + venue + effects[2 + hi] + effects[2 + n + ai],
                           effects[0] - venue + effects[2 + ai] + effects[2 + n + hi]])
        return pace, latent

    @replay_memoized
    def _validation_fold(self, training, held, cutoff):
        fit = self._fit(training, cutoff)
        complete = held.loc[held[[*EXPOSURE_COLUMNS, *AWAY_EXPOSURE]].gt(0).all(axis=1)]
        records = []
        for row in complete.itertuples(index=False):
            try:
                venue = getattr(row, "neutral", False)
                pace, latent = self._project(fit, row.team, row.opponent, bool(venue) if pd.notna(venue) else False)
            except PPPError:
                continue
            observed = np.asarray([getattr(row, column) for column in (*LATENT_COLUMNS, *AWAY_LATENT)])
            error = [row.possessions - pace, *(observed - latent.ravel()),
                     row.shared_pace_rate_error, row.away_shared_pace_rate_error]
            records.append(dict(game_id=row.game_id, date=row.event_date.isoformat(), error=error))
        return records

    def predict(self, frame, home_team, away_team, as_of, neutral=False):
        history = self._history(frame, as_of)
        records = self._calibration(history)
        errors = np.asarray([record["error"] for record in records])
        if neutral:
            exchange_sides = [0, *range(1 + N_FACTORS, 1 + 2 * N_FACTORS), *range(1, 1 + N_FACTORS), -1, -2]
            errors = np.vstack([errors, errors[:, exchange_sides]])
        fit = self._fit(history, as_of)
        pace, latent = self._project(fit, home_team, away_team, neutral)
        parameters = dict(pace=pace, matchup_latent=latent.tolist(), paired_errors=errors.tolist(),
                          rate_shift=[0., 0.], availability_covariance=[[0., 0.], [0., 0.]],
                          draws=self.config.simulations, seed=self.config.seed)
        diagnostics = dict(method="NBA opponent-adjusted Four Factors", forecast_method=self.method,
                           as_of=pd.Timestamp(as_of).isoformat(), last_game_date=history.event_date.max().date().isoformat(),
                           training_games=len(history)//2, validation_games=len(records), regulation_calibration_games=len(records),
                           pace_mae=float(np.abs(errors[:, 0]).mean()), simulations=self.config.simulations,
                           includes_overtime=True,
                           limitations="Estimated box-score possessions and time-scaled overtime. Base fit uses team and venue effects.")
        return NBAFourFactorsForecast.from_parameters(home_team, away_team, parameters, diagnostics)
