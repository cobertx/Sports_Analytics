"""Empirical Monte Carlo distributions, including exact integer push mass."""
from dataclasses import dataclass, field
import math
import numpy as np
from models.Game_Outcome.PPP.PPP import MarketProbability
from .errors import UsageOpportunityError


@dataclass
class UsageOpportunityForecast:
    values: list
    probabilities: list
    simulations: int
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self):
        x, p = np.asarray(self.values, float), np.asarray(self.probabilities, float)
        if (not len(x) or x.shape != p.shape or not np.isfinite(x).all() or not np.isfinite(p).all()
                or (p < 0).any() or not np.isclose(p.sum(), 1) or (np.diff(x) <= 0).any()
                or not np.equal(x % 1, 0).all() or self.simulations < 1):
            raise UsageOpportunityError('Invalid simulated probability mass.')

    @classmethod
    def from_samples(cls, samples, diagnostics=None):
        x = np.asarray(samples)
        if x.ndim != 1 or not len(x) or not np.isfinite(x).all() or not np.equal(x % 1, 0).all():
            raise UsageOpportunityError('Simulation must produce observed-unit integer outcomes.')
        values, counts = np.unique(x, return_counts=True)
        return cls(values.astype(int).tolist(), (counts/len(x)).tolist(), len(x), diagnostics or {})

    @property
    def mean(self):
        return float(np.asarray(self.values) @ np.asarray(self.probabilities))

    @property
    def variance(self):
        return float(np.asarray(self.probabilities) @ (np.asarray(self.values)-self.mean)**2)

    def quantile(self, q):
        return self.values[min(int(np.searchsorted(np.cumsum(self.probabilities), q)), len(self.values)-1)]

    def market(self, line, side='Over'):
        if not math.isfinite(line) or side not in {'Over', 'Under'}:
            raise UsageOpportunityError('Choose a finite line and Over or Under.')
        x, p = np.asarray(self.values), np.asarray(self.probabilities)
        below, above, push = (min(1., max(0., math.fsum(p[mask])))
                              for mask in (x < line, x > line, x == line))
        return MarketProbability(above if side == 'Over' else below, below if side == 'Over' else above, push)

    def summary(self):
        return dict(projected_mean=self.mean, projected_median=self.quantile(.5), projected_std=math.sqrt(self.variance),
                    percentile_05=self.quantile(.05), percentile_95=self.quantile(.95), simulations=self.simulations,
                    diagnostics=self.diagnostics)
