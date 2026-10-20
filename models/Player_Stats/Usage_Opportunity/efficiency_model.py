"""Beta-binomial conversion uncertainty and game-level yardage residuals."""
import numpy as np
from .errors import UsageOpportunityError
from .opportunity_model import weights, observed


def beta_posterior(successes, trials, observation_weights=None, prior=(.5, .5)):
    successes, trials = np.asarray(successes, float), np.asarray(trials, float)
    w = np.ones_like(trials) if observation_weights is None else np.asarray(observation_weights, float)
    if (successes.shape != trials.shape or w.shape != trials.shape
            or not all(np.isfinite(x).all() for x in (successes, trials, w))
            or (successes < 0).any() or (trials < successes).any() or (w < 0).any()
            or not all(np.isfinite(x) and x > 0 for x in prior)):
        raise UsageOpportunityError('Beta conversion inputs require 0 <= successes <= trials and positive priors.')
    a, b = prior[0] + float(w @ successes), prior[1] + float(w @ (trials-successes))
    return dict(alpha=a, beta=b, mean=a/(a+b))


def conversion(frame, success, trial, config, rng, *, adjustment=None, team=None):
    sample = observed(frame, [success, trial], config.minimum_games, config.efficiency_window)
    w = weights(len(sample), config.efficiency_half_life) * len(sample)
    successes = adjustment.neutral_successes(sample, success, trial) if adjustment is not None else sample[success]
    posterior = beta_posterior(successes, sample[trial], w)
    draws = rng.beta(posterior['alpha'], posterior['beta'], config.simulations)
    if adjustment is not None:
        draws = adjustment.conversion(team, sample, success, trial, draws)
    return draws, posterior, sample


def yards_per_event(frame, yards, count, projected_count, config, rng, *, adjustment=None, team=None):
    """Box-score conditional residual bootstrap; not a fake per-play gain feed.

    Conditional spread scales with sqrt(event count); observed game-level
    residuals retain asymmetric/explosive-game tails. Counts and yardage may be 0;
    net rushing/receiving yards may be negative. All output is integer settled.
    """
    sample = observed(frame, [yards, count], config.minimum_games, config.efficiency_window)
    if sample[count].lt(0).any() or ((sample[count] == 0) & sample[yards].ne(0)).any():
        raise UsageOpportunityError('Yardage needs nonnegative event counts and zero production at zero events.')
    positive = sample[sample[count] > 0]
    if positive.empty:
        return np.zeros(config.simulations, dtype=int), dict(rate=0., positive_games=0), sample
    w = weights(len(positive), config.efficiency_half_life)
    outcomes = (adjustment.neutral_yards(positive, yards, count) if adjustment is not None
                else positive[yards].to_numpy(float))
    rate = float(w @ outcomes) / float(w @ positive[count])
    residuals = (outcomes - positive[count].to_numpy(float)*rate) / np.sqrt(positive[count].to_numpy(float))
    residuals -= float(w @ residuals)
    sampled = rng.choice(residuals, size=config.simulations, p=w)
    n = np.asarray(projected_count, int)
    values = np.rint(n*rate + np.sqrt(n)*sampled).astype(int)
    if adjustment is not None:
        values = adjustment.yards(team, sample, yards, count, values)
    values[n == 0] = 0
    return values, dict(rate=rate, positive_games=len(positive), residual_sd=float(np.sqrt(w @ residuals**2))), sample
