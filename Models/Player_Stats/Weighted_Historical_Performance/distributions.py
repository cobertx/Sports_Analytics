"""PDF section 7: integer-aware predictive settlement, including refunds."""

import math
from statistics import NormalDist
import numpy as np

from Models.Game_Outcome.PPP.PPP import MarketProbability
from .errors import WHPError


def count_cdf(k, mean, variance):
    if k < 0:
        return 0.
    if mean == 0:
        return 1.
    # Log-space sums avoid underflow at large means. Count props are small;
    # bound pathological user lines before allocating a support vector.
    if k > max(10000, mean + 100 * math.sqrt(variance)):
        return 1.
    n = np.arange(int(k) + 1, dtype=float)
    if variance <= mean * (1 + 1e-8):
        logs = -mean + n * math.log(mean) - np.array([math.lgamma(v + 1) for v in n])
    else:
        r = mean * mean / (variance - mean)
        p = r / (r + mean)
        logs = (np.array([math.lgamma(v + r) - math.lgamma(r) - math.lgamma(v + 1) for v in n])
                + r * math.log(p) + n * math.log1p(-p))
    return min(1., float(np.exp(logs).sum()))


def settlement(mean, variance, line, side="Over", family="normal", nonnegative=True,
               *, samples=None, weights=None):
    if (side not in {"Over", "Under"} or not all(math.isfinite(v) for v in (mean, variance, line))
            or variance <= 0 or (nonnegative and mean < 0)):
        raise WHPError("Invalid predictive distribution, line or selection.")
    if family == "empirical":
        x, w = np.asarray(samples, float), np.asarray(weights, float)
        if (x.shape != w.shape or not len(x) or not np.isfinite(x).all() or
                not np.isfinite(w).all() or (w < 0).any() or not np.isclose(w.sum(),1)):
            raise WHPError("Empirical settlement needs weighted observations.")
        below, above = float(w[x < line].sum()), float(w[x > line].sum())
    else:
        if family == "count":
            if mean < 0:
                raise WHPError("Count mean cannot be negative.")
            cdf = lambda k: count_cdf(k, mean, max(mean, variance))
        elif family == "normal":
            normal = NormalDist(mean, math.sqrt(variance))
            # Rounding to integer bins. Nonnegative targets are left-censored
            # at zero; net football yardage keeps its negative support.
            cdf = lambda k: 0. if nonnegative and k < 0 else normal.cdf(k + .5)
        else:
            raise WHPError("Unknown predictive distribution family.")
        below, above = cdf(math.ceil(line) - 1), 1 - cdf(math.floor(line))
    push = max(0., 1 - above - below)
    return MarketProbability(above if side == "Over" else below,
                             below if side == "Over" else above, push)
