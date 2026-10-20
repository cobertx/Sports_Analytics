"""PDF sections 2-3: normalized recency weights and effective sample size."""

import numpy as np
from .errors import WHPError


def recency_weights(n, half_life=5, method="exponential", *, ages=None, custom=None):
    if n < 1 or not np.isfinite(half_life) or half_life <= 0:
        raise WHPError("Weights need observations and a positive half-life.")
    if method == "uniform":
        raw = np.ones(n)
    elif method == "linear":
        raw = np.arange(1, n + 1, dtype=float)
    elif method == "custom":
        raw = np.asarray(custom, dtype=float)
    elif method in {"exponential", "calendar"}:
        age = np.arange(n - 1, -1, -1) if ages is None else np.asarray(ages, dtype=float)
        if len(age) != n or not np.isfinite(age).all() or (age < 0).any():
            raise WHPError("Recency ages must be finite, nonnegative and aligned with history.")
        raw = np.exp2(-(age - age.min()) / half_life)
    else:
        raise WHPError("Unknown recency weighting method.")
    if raw.shape != (n,) or not np.isfinite(raw).all() or (raw < 0).any() or raw.sum() <= 0:
        raise WHPError("Weights must be nonnegative, finite, aligned and have positive sum.")
    return raw / raw.sum()


def summarize(values, weights):
    x, w = np.asarray(values, dtype=float), np.asarray(weights, dtype=float)
    if x.shape != w.shape or not np.isfinite(x).all():
        raise WHPError("Statistics and weights must be finite and aligned.")
    mean = float(w @ x)
    variance = float(w @ ((x - mean) ** 2))
    square_sum = float(w @ w)
    return dict(weighted_mean=mean, weighted_variance=variance,
                corrected_variance=variance / (1 - square_sum) if square_sum < 1 else 0.,
                descriptive_std=variance ** .5, effective_n=1 / square_sum)
