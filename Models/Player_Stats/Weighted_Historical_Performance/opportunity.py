"""PDF sections 4-6: ratio-of-weighted-totals workload decomposition."""

import numpy as np
from .errors import WHPError


def rate_projection(values, opportunities, weights, projected_opportunity):
    x, o, w = (np.asarray(a, dtype=float) for a in (values, opportunities, weights))
    if (x.shape != o.shape or x.shape != w.shape or not all(np.isfinite(a).all() for a in (x,o,w)) or
            (w < 0).any() or not np.isclose(w.sum(),1) or
            (o < 0).any() or not np.isfinite(projected_opportunity) or projected_opportunity < 0):
        raise WHPError("Opportunity inputs must be aligned, observed and nonnegative.")
    exposure = float(w @ o)
    if exposure <= 0:
        raise WHPError("No positive historical opportunity for a rate forecast.")
    rate = float(w @ x) / exposure
    return dict(rate=rate, historical_opportunity=exposure,
                projected_opportunity=float(projected_opportunity), rate_mean=rate * projected_opportunity)


def carry_share(player_carries, team_carries, weights, projected_team_carries):
    """Optional NCAAF share module; no team-play volume is silently invented."""
    player, team = np.asarray(player_carries, float), np.asarray(team_carries, float)
    if player.shape != team.shape:
        raise WHPError("Player/team carries must be aligned.")
    if (player < 0).any() or (player > team).any():
        raise WHPError("Player carries must be between zero and team carries.")
    return rate_projection(player, team, weights, projected_team_carries)["rate_mean"]
