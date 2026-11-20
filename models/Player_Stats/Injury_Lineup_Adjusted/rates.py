"""Condition box-score roles and conversions on joint availability states."""

import numpy as np

from models.Player_Stats.Usage_Opportunity.efficiency_model import conversion
from models.Player_Stats.Usage_Opportunity.opportunity_model import history_weights


class LineupRates:
    def __init__(self, historical_minutes, games, minutes, watched, stint_starts, config):
        self.config = config
        self.evidence = {}
        self.role_priority = historical_minutes.reindex(columns=watched, fill_value=0).mean().to_numpy(float)
        observed = historical_minutes.reindex(columns=watched, fill_value=0).gt(0).astype(int)
        for player in watched:
            observed.loc[games.event_date.lt(stint_starts[player]), player] = -1
        self.historical = observed
        states = np.column_stack([minutes.get(player, np.zeros(config.baseline.simulations)) > 1e-8
                                  for player in watched])
        patterns, indices = np.unique(states, axis=0, return_inverse=True)
        self.groups = [(pattern, np.flatnonzero(indices == i)) for i, pattern in enumerate(patterns)]

    def _supported_weights(self, weights, matches, comparable):
        samples = []
        for mask in (matches, comparable & ~matches):
            filtered = weights * mask
            total = filtered.sum()
            if mask.sum() < self.config.minimum_lineup_games or total <= 0:
                return None
            filtered = filtered / total
            effective = 1 / (filtered @ filtered)
            if effective < self.config.minimum_lineup_games - 1:
                return None
            samples.append((filtered, effective))
        return samples[0]

    def _matching(self, frame, weights):
        history = self.historical.reindex(frame.game_id).fillna(-1).to_numpy(int)
        known = history >= 0
        for pattern, draws in self.groups:
            matches = history == pattern
            cohort = self._supported_weights(weights, matches.all(axis=1), known.all(axis=1))
            if cohort is None:
                # Keep one supported contrast, prioritizing larger missing
                # roles. Pool against history instead of stacking effects.
                priority = sorted(range(len(pattern)), key=lambda j: (pattern[j], -self.role_priority[j], j))
                for j in priority:
                    cohort = self._supported_weights(weights, matches[:, j], known[:, j])
                    if cohort is not None:
                        break
            if cohort is not None:
                yield draws, *cohort

    def sample_roles(self, frame, config, rng, *, identity):
        weights = history_weights(frame, config.role_half_life, config)
        uniforms = rng.random(config.simulations)
        indices = np.searchsorted(np.cumsum(weights), uniforms, side="right")
        conditioned = 0.
        for draws, matched, effective in self._matching(frame, weights):
            share = effective / (effective + self.config.role_shrinkage_games)
            blended = (1 - share) * weights + share * matched
            indices[draws] = np.searchsorted(np.cumsum(blended), uniforms[draws], side="right")
            conditioned += len(draws) * share
        self.evidence.setdefault(identity, {})["role_conditioned_weight"] = conditioned / config.simulations
        return frame.iloc[indices.clip(max=len(frame)-1)].reset_index(drop=True)

    def sample_conversion(self, frame, made, attempts, config, rng, *, identity):
        draws, pooled, sample = conversion(frame, made, attempts, config, rng)
        weights = history_weights(sample, config.efficiency_half_life, config)
        shots = sample[attempts].to_numpy(float)
        makes = sample[made].to_numpy(float)
        conditioned = 0
        for paths, matched, effective in self._matching(sample, weights):
            trials = float(matched @ shots) * effective
            if trials <= 0:
                continue
            successes = float(matched @ makes) * effective
            prior = self.config.shooting_prior_attempts
            draws[paths] = rng.beta(prior * pooled["mean"] + successes,
                                    prior * (1 - pooled["mean"]) + max(0., trials - successes), len(paths))
            conditioned += len(paths)
        self.evidence.setdefault(identity, {})[attempts + "_conditioned_fraction"] = conditioned / config.simulations
        return draws, {**pooled, "scenario_mean": float(draws.mean())}, sample
