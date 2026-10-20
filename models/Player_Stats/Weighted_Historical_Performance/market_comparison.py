"""PDF section 10: margin removal and push-aware expected return."""

from models.Game_Outcome.PPP.PPP import implied_probability


def compare_price(estimate, odds, opposing_odds=None):
    implied = implied_probability(odds)
    opposite = implied_probability(opposing_odds) if opposing_odds is not None else None
    no_vig = implied / (implied + opposite) if implied is not None and opposite is not None else None
    probability = estimate.probability if estimate else None
    payout = (odds / 100 if odds > 0 else 100 / -odds) if implied is not None else None
    return dict(market_implied=implied, market_no_vig=no_vig, probability=probability,
                fair_odds=estimate.odds if estimate else None,
                edge=probability - implied if probability is not None and implied is not None else None,
                expected_net_per_dollar=estimate.win * payout - estimate.loss
                if estimate is not None and payout is not None else None)
