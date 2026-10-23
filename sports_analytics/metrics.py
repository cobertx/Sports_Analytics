"""Statistics relevant to one entity's position and available observations."""

import re

import pandas as pd

from .data import META


ROLE_POSITIONS = {
    "passer": {"QB", "QUARTERBACK"},
    "runner": {"RB", "FB", "HB", "TB", "RUNNINGBACK", "FULLBACK", "HALFBACK"},
    "receiver": {"WR", "TE", "SE", "FL", "WIDERECEIVER", "TIGHTEND"},
    "defender": {"DB", "CB", "S", "SS", "FS", "SAF", "SAFETY", "LB", "ILB", "MLB", "OLB",
                 "DL", "DT", "DE", "NT", "EDGE", "DEF", "DEFENSE", "CORNERBACK", "LINEBACKER",
                 "DEFENSIVEEND", "DEFENSIVETACKLE", "DEFENSIVEBACK"},
    "kicker": {"K", "PK", "KICKER", "PLACEKICKER"},
    "punter": {"P", "PUNTER"},
    "returner": {"KR", "PR", "RS", "RET", "RETURNER"},
    "lineman": {"OL", "OT", "OG", "C", "G", "T", "LT", "RT", "LG", "RG", "LS", "OC",
                "CENTER", "GUARD", "TACKLE", "LONGSNAPPER", "OFFENSIVELINE"},
}
ROLE_GROUPS = {
    "passer": {"passing", "rushing", "fumbles", "fantasy", "penalties"},
    "runner": {"rushing", "receiving", "fumbles", "fantasy", "penalties"},
    "receiver": {"receiving", "fumbles", "fantasy", "penalties"},
    "defender": {"defense", "fumbles", "penalties"},
    "kicker": {"kicking", "penalties"},
    "punter": {"punting", "penalties"},
    "returner": {"returns", "fumbles", "penalties"},
    "lineman": {"penalties", "fumbles"},
}


def metric_group(metric):
    if metric in {"completions", "attempts", "sacks_suffered", "interceptions"} or metric.startswith(("passing_", "sack_")):
        return "passing"
    if metric == "carries" or metric.startswith("rushing_"):
        return "rushing"
    if metric in {"receptions", "targets"} or metric.startswith("receiving_"):
        return "receiving"
    if metric.startswith(("def_", "tackles_", "interceptions_")):
        return "defense"
    if metric.startswith(("fg_", "pat_", "gwfg_")):
        return "kicking"
    if metric.startswith(("pt_", "punting_")):
        return "punting"
    if metric.startswith(("punt_return", "kickoff_return", "kick_return")):
        return "returns"
    if metric.startswith(("fumble", "fumbles_")):
        return "fumbles"
    if metric.startswith("fantasy_points"):
        return "fantasy"
    if metric in {"penalties", "penalty_yards"}:
        return "penalties"
    return "other"


def position_roles(frame):
    roles = set()
    if "position" in frame:
        for value in frame["position"].dropna().unique():
            for part in re.split(r"[/,;\-]", str(value).upper()):
                position = re.sub(r"\s+", "", part)
                roles.update(role for role, positions in ROLE_POSITIONS.items() if position in positions)
    return roles


def relevant_metrics(frame, sport, kind):
    """Keep position-appropriate zero stats, but exclude other players' columns.

    Where a provider has no position label, use the selected player's recorded
    stat families. This does not infer or display a fabricated position.
    """
    numeric = [name for name in frame if name not in META and pd.api.types.is_numeric_dtype(frame[name])]
    present = frame[numeric].notna().any()
    metrics = [name for name in numeric if present[name]]
    if kind != "player" or sport == "NBA":
        return metrics
    roles = position_roles(frame)
    active = frame[metrics].fillna(0).ne(0).any()
    active_groups = {metric_group(name) for name in metrics if active[name]}
    if not roles:
        return [name for name in metrics if (metric_group(name) in active_groups and metric_group(name) != "other")
                or (metric_group(name) == "other" and active[name])]
    allowed = set().union(*(ROLE_GROUPS[role] for role in roles))
    if "passer" in roles and "receiving" in active_groups:
        allowed.add("receiving")
    if "receiver" in roles and "rushing" in active_groups:
        allowed.add("rushing")
    # A receiver, back, or defender may also be a return specialist.
    if "returns" in active_groups:
        allowed.add("returns")
    return [name for name in metrics if metric_group(name) in allowed or name == 'touchdowns']


def preferred_metric(metrics):
    priority = ("pts", "points", "passing_yards", "rushing_yards", "receiving_yards",
                "def_tackles_solo", "fg_made", "pt_yards")
    return next((name for name in priority if name in metrics), metrics[0] if metrics else None)


FOOTBALL_CATEGORIES = {
    "scoring": "Scoring", "offense": "Offense", "passing": "Passing", "rushing": "Rushing", "receiving": "Receiving", "defense": "Defense",
    "kicking": "Kicking", "punting": "Punting", "special_teams": "Special teams", "returns": "Returns", "fumbles": "Fumbles",
    "penalties": "Penalties", "fantasy": "Fantasy", "other": "Other",
}

# CFBD team box scores use different names from nflverse player/team logs.
# Keep those source names intact in saved data and classify them only for NCAAF.
NCAAF_CATEGORIES = {
    "first_downs": "offense", "third_down_conversions": "offense", "third_down_attempts": "offense",
    "fourth_down_conversions": "offense", "fourth_down_attempts": "offense", "total_yards": "offense",
    "turnovers": "offense", "net_passing_yards": "passing", "yards_per_pass": "passing",
    "interceptions": "passing", "yards_per_rush_attempt": "rushing",
    "passes_deflected": "defense", "qb_hurries": "defense", "sacks": "defense",
    "tackles": "defense", "defensive_tds": "defense", "passes_intercepted": "defense",
    "interception_tds": "defense", "interception_yards": "defense",
    "total_fumbles": "fumbles", "kicking_points": "kicking",
}
NBA_CATEGORIES = {
    "scoring": "Scoring", "shooting": "Shooting", "rebounding": "Rebounding", "playmaking": "Playmaking",
    "defense": "Defense", "turnovers": "Turnovers", "fouls": "Fouls", "playing_time": "Playing time",
    "impact": "Plus/minus", "other": "Other",
}


def statistic_category(metric, sport):
    if metric in {"scoring_margin", "game_total", "points_assists", "points_rebounds", "points_rebounds_assists"}:
        return 'scoring'
    if metric == 'rebounds_assists':return 'playmaking'
    if sport != "NBA":
        if sport == "NCAAF" and metric in NCAAF_CATEGORIES:
            return NCAAF_CATEGORIES[metric]
        if metric in {"points", "pts"} or sport == "NFL" and metric in {"pf", "pa"}:
            return "scoring"
        if metric == "special_teams_tds":
            return "special_teams"
        return metric_group(metric)
    if metric == "pts":
        return "scoring"
    if metric in {"fgm", "fga", "fg_pct", "fg3m", "fg3a", "fg3_pct", "ftm", "fta", "ft_pct"}:
        return "shooting"
    return {"reb": "rebounding", "oreb": "rebounding", "dreb": "rebounding", "ast": "playmaking",
            "stl": "defense", "blk": "defense", "tov": "turnovers", "pf": "fouls",
            "min": "playing_time", "plus_minus": "impact"}.get(metric, "other")


def statistic_categories(metrics, sport):
    """Offer only categories containing a selectable, position-relevant metric."""
    available = {statistic_category(metric, sport) for metric in metrics}
    labels = NBA_CATEGORIES if sport == "NBA" else FOOTBALL_CATEGORIES
    return [{"label": label, "value": group} for group, label in labels.items() if group in available]


def metric_label(metric, sport):
    market_labels = {'scoring_margin':'Scoring margin', 'game_total':'Game total points',
                     'points_assists':'Points + Assists', 'points_rebounds':'Points + Rebounds',
                     'rebounds_assists':'Rebounds + Assists', 'points_rebounds_assists':'Points + Rebounds + Assists'}
    if metric in market_labels:return market_labels[metric]
    if sport == "NCAAF":
        college = {
            "points": "Points scored", "net_passing_yards": "Net passing yards",
            "yards_per_pass": "Yards per pass", "yards_per_rush_attempt": "Yards per rushing attempt",
            "interceptions": "Interceptions thrown", "passes_intercepted": "Defensive interceptions",
            "qb_hurries": "Quarterback hurries", "kicking_points": "Kicking points",
        }
        if metric in college:
            return college[metric]
    if sport == "NFL" and metric in {"pf", "pa"}:
        return {"pf": "PF (Points for)", "pa": "PA (Points against)"}[metric]
    basketball = {
        "pts": "Points", "reb": "Rebounds", "oreb": "Offensive rebounds", "dreb": "Defensive rebounds",
        "ast": "Assists", "stl": "Steals", "blk": "Blocks", "tov": "Turnovers", "pf": "Personal fouls",
        "min": "Minutes played", "plus_minus": "Plus/minus", "fgm": "Field goals made", "fga": "Field goal attempts",
        "fg_pct": "Field goal percentage", "fg3m": "Three-pointers made", "fg3a": "Three-point attempts",
        "fg3_pct": "Three-point percentage", "ftm": "Free throws made", "fta": "Free throw attempts",
        "ft_pct": "Free throw percentage",
    }
    if sport == "NBA" and metric in basketball:
        return basketball[metric]
    labels = {
        "completions": "Completions", "attempts": "Passing attempts", "carries": "Carries",
        "receptions": "Receptions", "targets": "Targets",
        "passing_tds": "Passing touchdowns", "rushing_tds": "Rushing touchdowns",
        "receiving_tds": "Receiving touchdowns",
        "fg_made": "Field goals made", "fg_att": "Field goal attempts", "fg_pct": "Field goal percentage",
        "pat_made": "Extra points made", "pat_att": "Extra point attempts", "pat_pct": "Extra point percentage",
    }
    return labels.get(metric, metric.replace("_", " ").capitalize()
                      .replace(" tds", " touchdowns").replace(" yds", " yards")
                      .replace(" epa", " EPA").replace(" cpoe", " CPOE"))
