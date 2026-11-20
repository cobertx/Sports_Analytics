"""Validate and select pregame snapshots of supplied game-wide lineup scenarios."""

import json
import math
import re

import pandas as pd

from models.Player_Stats.Playing_Time_Rotation.prediction_service import team_code
from sports_analytics.data import DataSourceError


def _names(values):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise DataSourceError("Scenario participants must be a list of exact saved player names.")
    return [value.strip() for value in values]


def _validate(packet, clubs):
    scenarios = packet.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 24:
        raise DataSourceError("Supply between one and 24 mutually exclusive lineup scenarios.")
    normalized = []
    for item in scenarios:
        if not isinstance(item, dict):
            raise DataSourceError("Each lineup scenario must be an object.")
        label, probability = item.get("label"), item.get("probability")
        if (not isinstance(label, str) or not label.strip() or type(probability) not in (float, int)
                or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise DataSourceError("Lineup scenarios need a label and finite probability between zero and one.")
        sides = item.get("teams", {})
        if not isinstance(sides, dict):
            raise DataSourceError("Lineup scenario teams must be an object keyed by team.")
        teams = {}
        for team, state in sides.items():
            club = team_code(team)
            if (club not in clubs or club in teams or not isinstance(state, dict)
                    or set(state) - {"out", "active", "minutes_caps"}):
                raise DataSourceError("Use matchup teams and the out, active or minutes_caps scenario fields.")
            caps = state.get("minutes_caps", {})
            if (not isinstance(caps, dict) or any(not isinstance(name, str) or not name.strip()
                    or type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 48
                    for name, value in caps.items())):
                raise DataSourceError("Scenario minute caps must be greater than zero and at most 48. Use out for zero.")
            teams[club] = dict(out=_names(state.get("out", [])), active=_names(state.get("active", [])),
                               minutes_caps={name.strip(): float(value) for name, value in caps.items()})
        normalized.append(dict(label=label.strip(), probability=float(probability), teams=teams))
    mass = math.fsum(item["probability"] for item in normalized)
    if abs(mass - 1) > 1e-9 or len({item["label"] for item in normalized}) != len(normalized):
        raise DataSourceError("Lineup scenario labels must be unique and probabilities must sum to one.")
    return normalized


def select_scenarios(snapshot, directory, cutoff, clubs):
    game_id = str(snapshot["game_id"]).removeprefix("espn:")
    versions = []
    if re.fullmatch(r"(?:\d{1,24}|KXNBAGAME-[A-Z0-9._-]{1,180})", game_id):
        path = directory / f"{game_id}.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            raise DataSourceError(f"Cannot read lineup scenarios for game {game_id}: {exc}") from exc
        else:
            versions.extend(saved if isinstance(saved, list) else [saved])
    if "lineup_scenarios" in snapshot:
        versions.append(snapshot["lineup_scenarios"])
    eligible = []
    for packet in versions:
        try:
            retrieved = pd.Timestamp(packet["available_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataSourceError("A lineup scenario version needs a timezone-aware available_at timestamp.") from exc
        if pd.isna(retrieved) or retrieved.tzinfo is None:
            raise DataSourceError("A lineup scenario version needs a timezone-aware available_at timestamp.")
        if retrieved <= cutoff:
            eligible.append((retrieved, packet))
    if not eligible:
        return {}
    retrieved, packet = max(reversed(eligible), key=lambda item: item[0])
    return dict(scenarios_at=retrieved.isoformat(), scenarios=_validate(packet, clubs))
