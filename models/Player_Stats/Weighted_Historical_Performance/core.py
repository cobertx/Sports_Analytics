"""Weighted Historical Performance: point forecast plus held-out uncertainty.

No source downloads or sportsbook calls occur in this mathematical engine.
"""

from dataclasses import asdict, dataclass, field
import math
from statistics import NormalDist
import numpy as np
import pandas as pd

from .distributions import settlement
from .errors import WHPError
from .opportunity import rate_projection
from .weights import recency_weights, summarize

MODEL_VERSION = "whp-1"


@dataclass(frozen=True)
class WHPConfig:
    window: int = 20
    half_life: float = 5
    prior_strength: float = 5
    beta: float = 1  # Direct benchmark; workload blending is opt-in, not tuned.
    minimum_games: int = 5
    minimum_calibration: int = 6
    maximum_calibration: int = 40
    weighting: str = "exponential"
    distribution: str = "auto"

    def __post_init__(self):
        if (any(not isinstance(v, int) or isinstance(v, bool) for v in
                (self.window, self.minimum_games, self.minimum_calibration, self.maximum_calibration)) or
                not 2 <= self.minimum_games <= self.window or
                not 2 <= self.minimum_calibration <= self.maximum_calibration or
                not all(math.isfinite(v) for v in (self.half_life, self.prior_strength, self.beta)) or
                self.half_life <= 0 or self.prior_strength < 0 or not 0 <= self.beta <= 1 or
                self.weighting not in {"exponential", "calendar", "linear", "uniform"} or
                self.distribution not in {"auto", "normal", "count", "empirical"}):
            raise WHPError("Invalid WHP configuration.")

    @classmethod
    def for_sport(cls, sport):
        if sport == "NBA":
            return cls()
        if sport == "NFL":
            return cls(window=16, half_life=3, minimum_games=3, minimum_calibration=4)
        if sport == "NCAAF":
            return cls(window=12, half_life=3, prior_strength=8, minimum_games=4, minimum_calibration=4)
        raise WHPError("Choose NBA, NFL or NCAAF.")


def eligible_history(frame, as_of):
    required = {"game_id", "event_date", "available_at", "value"}
    if not required <= set(frame):
        raise WHPError("History needs game IDs, dates, availability times and target values.")
    data = frame.copy()
    for key in ("event_date", "available_at"):
        data[key] = pd.to_datetime(data[key], utc=True, errors="coerce")
    cutoff = pd.Timestamp(as_of)
    if pd.isna(cutoff) or cutoff.tzinfo is None:
        raise WHPError("Prediction cutoff must include a timezone.")
    data["value"] = pd.to_numeric(data["value"], errors="coerce")
    data = data[data["event_date"].lt(cutoff) & data["available_at"].lt(cutoff)
                & data["available_at"].ge(data["event_date"])].copy()
    if data["game_id"].isna().any() or data["game_id"].duplicated().any():
        raise WHPError("Player history must contain one uniquely identified observation per game.")
    if not np.isfinite(data["value"]).all() or not np.equal(data["value"] % 1, 0).all():
        raise WHPError("Player statistics must be observed finite integers; missing is not zero.")
    return data.sort_values(["event_date", "game_id"]).reset_index(drop=True)


@dataclass
class WHPForecast:
    mean: float
    variance: float
    family: str
    nonnegative: bool
    samples: list
    weights: list
    diagnostics: dict = field(default_factory=dict)

    def market(self, line, side="Over"):
        return settlement(self.mean, self.variance, float(line), side, self.family,
                          self.nonnegative, samples=self.samples, weights=self.weights)


class WeightedHistoricalPerformance:
    def __init__(self, config=None, *, half_life=5, window=20, prior_strength=0):
        self.config = config or WHPConfig(half_life=half_life, window=window,
                                         prior_strength=prior_strength,
                                         minimum_games=min(5, window))

    def weights(self, n):
        return recency_weights(n, self.config.half_life, self.config.weighting)

    def predict(self, history, prior=None):
        """PDF section 13 estimator, usable independently of distribution fitting."""
        x = np.asarray(history, float)[-self.config.window:]
        if not len(x) or not np.isfinite(x).all() or (prior is not None and not math.isfinite(prior)):
            raise WHPError("History and optional prior must be finite.")
        w = self.weights(len(x))
        result = summarize(x, w)
        alpha = result["effective_n"] / (result["effective_n"] + self.config.prior_strength)
        result.update(adjusted_mean=alpha * result["weighted_mean"] + (1-alpha) * prior
                      if prior is not None else result["weighted_mean"], weights=w)
        return result

    @staticmethod
    def over_probability(mean, calibrated_predictive_std, line):
        if not math.isfinite(calibrated_predictive_std) or calibrated_predictive_std <= 0:
            raise WHPError("Predictive SD must be positive.")
        return 1 - NormalDist(mean, calibrated_predictive_std).cdf(line)

    def _point(self, history, as_of, opportunity_override=None):
        sample = history.tail(self.config.window)
        if len(sample) < self.config.minimum_games:
            raise WHPError(f"Need at least {self.config.minimum_games} completed target observations.")
        ages = ((pd.Timestamp(as_of) - sample["event_date"]).dt.total_seconds() / 86400
                if self.config.weighting == "calendar" else None)
        w = recency_weights(len(sample), self.config.half_life, self.config.weighting, ages=ages)
        result = summarize(sample["value"], w)
        # Disjoint older appearances form an empirical player prior. Never add
        # invented rookie or cross-position production when this pool is absent.
        prior_sample = history.iloc[:-len(sample)]
        prior = float(prior_sample["value"].mean()) if len(prior_sample) >= 3 else None
        opportunity = {}
        if "opportunity" in sample:
            o = pd.to_numeric(sample["opportunity"], errors="coerce")
            if np.isfinite(o).all() and o.ge(0).all() and float(w @ o) > 0:
                projected = float(w @ o) if opportunity_override is None else opportunity_override
                opportunity = rate_projection(sample["value"], o, w, projected)
        if opportunity_override is not None and not opportunity:
            raise WHPError("A workload override needs complete historical opportunities.")
        if self.config.beta < 1 and not opportunity:
            raise WHPError("Workload blending needs complete historical opportunities.")
        blended = (self.config.beta * result["weighted_mean"] +
                   (1 - self.config.beta) * opportunity.get("rate_mean", result["weighted_mean"]))
        alpha = result["effective_n"] / (result["effective_n"] + self.config.prior_strength) if prior is not None else 1.
        result.update(adjusted_mean=alpha * blended + (1-alpha) * (prior or 0),
                      prior=prior, prior_games=len(prior_sample), alpha=alpha, **opportunity)
        return result, sample, w

    def forecast(self, frame, as_of, *, nonnegative=True, count=False, opportunity_override=None):
        history = eligible_history(frame, as_of)
        if (nonnegative or count or self.config.distribution == "count") and history["value"].lt(0).any():
            raise WHPError("A nonnegative count/statistic cannot contain negative observations.")
        point, sample, w = self._point(history, as_of, opportunity_override)
        # At each held-out game's start, exclude even earlier-starting games
        # whose results were not yet available. Realized workload is never used.
        records = []
        for _, game in history.tail(self.config.maximum_calibration).iterrows():
            past = history[history["available_at"].lt(game["event_date"])
                           & history["event_date"].lt(game["event_date"])]
            if len(past) < self.config.minimum_games:
                continue
            fitted, _, _ = self._point(past, game["event_date"])
            records.append({"game_id": str(game["game_id"]), "event_date": game["event_date"].isoformat(),
                            "actual": float(game["value"]), "predicted": fitted["adjusted_mean"]})
        if len(records) < self.config.minimum_calibration:
            raise WHPError(f"Need {self.config.minimum_calibration} walk-forward errors after the minimum history; "
                           f"only {len(records)} available. Prepare the prior season too.")
        errors = np.array([r["actual"] - r["predicted"] for r in records])
        mse = float(np.mean(errors ** 2))
        # Positive floor is discretization resolution, not an assumed SE(mean).
        variance = max(.25, mse)
        family = self.config.distribution
        if family == "auto":
            family = "count" if count else "normal"
        if family == "count":
            variance = max(variance, point["adjusted_mean"], point["corrected_variance"])
        diagnostics = {**point, "model_version": MODEL_VERSION, "config": asdict(self.config),
            "as_of": pd.Timestamp(as_of).isoformat(), "training_games": len(sample),
            "eligible_games": len(history), "validation_games": len(records),
            "last_game_date": sample.iloc[-1]["event_date"].date().isoformat(),
            "predictive_sd": math.sqrt(variance), "calibration_rmse": math.sqrt(mse),
            "calibration_mae": float(np.abs(errors).mean()), "calibration_bias": float(-errors.mean()),
            "calibration_records": records,
            "calibration_note": "Walk-forward residual calibration; defaults are research candidates, not tuned parameters."}
        return WHPForecast(point["adjusted_mean"], variance, family, nonnegative,
                           sample["value"].astype(float).tolist(), w.tolist(), diagnostics)
