"""Market-to-history links and exact, recorded-statistic selection helpers."""

import re
import unicodedata
from urllib.parse import parse_qs, urlencode

import pandas as pd

from models.Player_Stats.Weighted_Historical_Performance.markets import stat_spec
from .display_time import eastern_datetime
from .nba_markets import NBA_TEAM_CODES
from .nfl_markets import NFL_TEAM_CODES


NBA_COMBINATIONS = {
    "points_assists": ("pts", "ast"),
    "points_rebounds": ("pts", "reb"),
    "rebounds_assists": ("reb", "ast"),
    "points_rebounds_assists": ("pts", "reb", "ast"),
}


def history_href(sport, kind, name, market="", snapshot=None):
    parameters = dict(sport=sport, name=name, market=market)
    stamp = (snapshot or {}).get("commence_time") or ((snapshot or {}).get("game") or {}).get("start")
    if stamp:
        try:
            day = eastern_datetime(stamp)
            # January football and spring NBA games belong to the prior starting year.
            parameters["season"] = day.year - (day.month < (7 if sport == "NBA" else 3))
        except (TypeError, ValueError, AttributeError, OverflowError):
            pass
    return f"#historical-{kind}?" + urlencode(parameters)


def history_target(location_hash):
    path, _, query = (location_hash or "").partition("?")
    match = re.fullmatch(r"#historical-(player|team)", path)
    if not match or not query:
        return None
    values = parse_qs(query)
    if any(len(v) != 1 for v in values.values()):
        return None
    sport = values.get("sport", [""])[0]
    name = values.get("name", [""])[0]
    if sport not in {"NBA", "NFL", "NCAAF"} or not name.strip() or len(name) > 200:
        return None
    result = dict(sport=sport, kind=match[1], name=name, market=values.get("market", [""])[0])
    if "season" in values:
        try:
            result["season"] = int(values["season"][0])
        except ValueError:
            return None
    return result


def normalized_name(value):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().casefold()
    # Providers disagree about punctuation and suffixes; never do fuzzy matching.
    value = re.sub(r"[^a-z0-9]", "", value)
    return re.sub(r"(?:jr|sr|iii|ii|iv)$", "", value)


def target_entity(frame, target):
    """Resolve only an unambiguous name/code, retaining the provider's ID."""
    names = frame["entity_name"].map(normalized_name)
    wanted = normalized_name(target["name"])
    exact = frame.loc[frame["entity_name"].astype(str).str.casefold().eq(target["name"].casefold()), "entity_id"].unique()
    if len(exact) == 1:
        return exact[0]
    mask = names.eq(wanted)
    if target["kind"] == "team":
        codes = NBA_TEAM_CODES if target["sport"] == "NBA" else NFL_TEAM_CODES if target["sport"] == "NFL" else {}
        code = next((code for name, code in codes.items() if normalized_name(name) == wanted), target["name"])
        mask |= frame["team"].astype(str).str.casefold().eq(code.casefold())
        mask |= frame["entity_id"].astype(str).str.casefold().eq(code.casefold())
    matches = frame.loc[mask, "entity_id"].unique()
    return matches[0] if len(matches) == 1 else None


def matches_dataset(target, dataset):
    return bool(target and dataset and target["sport"] == dataset["sport"] and target["kind"] == dataset["kind"])


def market_metrics(frame, sport, kind):
    """Add exact sums/differences; missing components remain missing."""
    derived = {}
    if sport == "NBA" and kind == "player":
        for name, fields in NBA_COMBINATIONS.items():
            if set(fields) <= set(frame):
                derived[name] = frame[list(fields)].sum(axis=1, min_count=len(fields))
    if kind == "team":
        points = "pts" if sport == "NBA" else "pf" if sport == "NFL" else "points"
        if points in frame:
            allowed = frame["pa"] if sport == "NFL" and "pa" in frame else None
            if sport == "NBA" and "plus_minus" in frame:
                allowed = frame[points] - frame["plus_minus"]
            if allowed is None and {"game_id", "team", "opponent"} <= set(frame):
                lookup = frame.drop_duplicates(["game_id", "team"], keep=False).set_index(["game_id", "team"])[points]
                allowed = pd.Series(lookup.reindex(pd.MultiIndex.from_arrays([frame.game_id, frame.opponent])).to_numpy(), index=frame.index)
            if allowed is not None:
                derived["scoring_margin"] = frame[points] - allowed
                derived["game_total"] = frame[points] + allowed
    return frame.assign(**derived) if derived else frame


def target_metric(target, available, frame=None):
    key = target["market"].removesuffix("_alternate")
    if target["kind"] == "team":
        candidates = (["game_total"] if key in {"totals", "alternate_totals"} else
                      ["scoring_margin"] if key in {"spreads", "alternate_spreads"} else [])
        candidates += ["pts", "pf", "points"]
    else:
        spec = stat_spec(target["sport"], key)
        if spec and len(spec.fields) > 1:
            candidates = [key.removeprefix("player_")]
        else:
            candidates = list(spec.fields) if spec else []
        if key == 'player_pass_interceptions':
            candidates = ['passing_interceptions', 'interceptions']
        if key in {"player_anytime_td", "player_1st_td", "player_last_td", "player_tds"}:
            # Scorer-order history is not provided by a box score. Show labeled
            # touchdown counts rather than fabricate first/last scorer records.
            candidates = ["touchdowns", "receiving_tds", "rushing_tds", "special_teams_tds"]
            if frame is not None:
                candidates = ['touchdowns'] + sorted(candidates[1:], reverse=True,
                    key=lambda field: pd.to_numeric(frame[field],errors='coerce').sum() if field in frame else -1)
    return next((name for name in candidates if name in available), None)
