"""Explicit provider-column mappings; unsupported markets never get proxies."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StatSpec:
    fields: tuple[str, ...]
    opportunity: str | None = None
    count: bool = False
    nonnegative: bool = True


NBA = {
    "player_points": StatSpec(("pts",), "min"),
    "player_rebounds": StatSpec(("reb",), "min", True),
    "player_assists": StatSpec(("ast",), "min", True),
    "player_threes": StatSpec(("fg3m",), "fg3a", True),
    "player_blocks": StatSpec(("blk",), "min", True),
    "player_steals": StatSpec(("stl",), "min", True),
    "player_turnovers": StatSpec(("tov",), "min", True),
    "player_points_assists": StatSpec(("pts", "ast"), "min"),
    "player_points_rebounds": StatSpec(("pts", "reb"), "min"),
    "player_rebounds_assists": StatSpec(("reb", "ast"), "min", True),
    "player_points_rebounds_assists": StatSpec(("pts", "reb", "ast"), "min"),
}
FOOTBALL = {
    "player_pass_yds": StatSpec(("passing_yards",), "attempts", nonnegative=False),
    "player_pass_attempts": StatSpec(("attempts",), count=True),
    "player_pass_completions": StatSpec(("completions",), "attempts", True),
    "player_pass_tds": StatSpec(("passing_tds",), "attempts", True),
    "player_pass_interceptions": StatSpec(("interceptions",), "attempts", True),
    "player_rush_yds": StatSpec(("rushing_yards",), "carries", nonnegative=False),
    "player_rush_attempts": StatSpec(("carries",), count=True),
    "player_rush_tds": StatSpec(("rushing_tds",), "carries", True),
    "player_reception_yds": StatSpec(("receiving_yards",), "targets", nonnegative=False),
    "player_receptions": StatSpec(("receptions",), "targets", True),
    "player_reception_tds": StatSpec(("receiving_tds",), "targets", True),
    # Must be an authoritative all-scoring-TD target. Passing TDs are NOT
    # scorer TDs. Do not silently ignore return/defensive scores.
    "player_tds": StatSpec(("touchdowns",), count=True),
    "player_anytime_td": StatSpec(("touchdowns",), count=True),
}


def stat_spec(sport, market_key):
    return (NBA if sport == "NBA" else FOOTBALL).get(market_key.removesuffix("_alternate"))
