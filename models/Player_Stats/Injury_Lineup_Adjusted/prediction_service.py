"""Point-in-time scenario mixtures for NBA player props."""

from collections import defaultdict
from pathlib import Path

import numpy as np

from models.Player_Stats.Playing_Time_Rotation.prediction_service import (
    PlayingTimeRotationMarketService, RotationMarketForecast,
)
from models.Player_Stats.Playing_Time_Rotation.rotation import rotation_history, sample_rotation, RotationDraws
from models.Player_Stats.Usage_Opportunity.historical_data import identity_name
from models.Player_Stats.Usage_Opportunity.markets import prop_key
from sports_analytics.availability import AvailabilityStore
from sports_analytics.data import DataSourceError

from .config import InjuryLineupConfig
from .rates import LineupRates
from .scenarios import select_scenarios

ROOT = Path(__file__).resolve().parents[3] / "data/modeling/player_stats/injury_lineup_adjusted"
LINEUPS = ROOT / "lineups"


class InjuryLineupForecast(RotationMarketForecast):
    def detail(self, row):
        fitted = self.forecasts.get(prop_key(row))
        if fitted is None:
            return self.errors.get(prop_key(row), "Injury & Lineup Adjustment is unavailable for this market.")
        d = fitted.diagnostics
        evidence = d.get("lineup_evidence", {})
        weight = evidence.get("role_conditioned_weight", 0)
        return (f"ILA mean {fitted.mean:.2f} if playing · 90% interval [{fitted.quantile(.05)}, {fitted.quantile(.95)}] · "
                f'{d["play_probability"]:.1%} of paths played · {d["minutes_if_playing"]:.1f} minutes if playing · '
                f"{weight:.1%} weight on matching lineup role samples · "
                f'latest input {d["last_game_date"]}.')


class InjuryLineupMarketService(PlayingTimeRotationMarketService):
    version = "ila-1"
    title = "Injury & Lineup Adjustment"
    config_class = InjuryLineupConfig
    forecast_class = InjuryLineupForecast
    cache_root = ROOT
    limitations = ("Regulation box-score scenarios with complete observed participation. "
                   "Prop settlement conditions on playing. Sparse lineup groups use pooled rates.")

    def __init__(self, repository=None, config=None, *, persist=True, availability=None, lineup_directory=None):
        super().__init__(repository, config, persist=persist,
                         availability=AvailabilityStore() if availability is None else availability)
        self.lineup_directory = Path(lineup_directory) if lineup_directory is not None else LINEUPS

    def _availability_packet(self, snapshot):
        return self.availability.at_cutoff(snapshot, minutes=self.config.report_max_age_minutes)

    def _availability(self, snapshot, cutoff, clubs):
        report = super()._availability(snapshot, cutoff, clubs)
        supplied = select_scenarios(snapshot, self.lineup_directory, cutoff, clubs)
        return {**report, **supplied, "lineup": True}

    def _implementation_files(self):
        return [*super()._implementation_files(), *Path(__file__).parent.glob("*.py"),
                Path(__file__).resolve().parents[3] / "sports_analytics/availability.py"]

    def _roster(self, players, clubs):
        latest = super()._roster(players, clubs)
        history = players.merge(latest[["entity_id", "team"]].rename(columns={"team": "current"}),
                                on="entity_id", how="inner")
        other = history.loc[history.team.ne(history.current)].groupby("entity_id").event_date.max().rename("departure")
        history = history.merge(other, on="entity_id", how="left")
        current = history.loc[history.team.eq(history.current) &
                              (history.departure.isna() | history.event_date.gt(history.departure))]
        starts = current.groupby("entity_id").event_date.min().rename("stint_start")
        return latest.merge(starts, on="entity_id", how="left", validate="one_to_one")

    def _team_model(self, players, teams, roster, known, cutoff, club, seed, availability):
        wide, games = rotation_history(players, teams)
        names = defaultdict(list)
        for player in roster.itertuples():
            names[identity_name(player.entity_name)].append(player.entity_id)
        starts = roster.set_index("entity_id").stint_start.to_dict()

        def resolve(name):
            matches = names.get(identity_name(name), [])
            if len(matches) != 1:
                raise DataSourceError(f"Cannot uniquely resolve {name} in the saved {club} rotation.")
            return matches[0]

        watched = set()
        for report in availability.get("injuries", []):
            matches = names.get(identity_name(report["player"]), [])
            if report["team"] == club and len(matches) == 1:
                watched.add(matches[0])
        scenarios = availability.get("scenarios") or [dict(label="Saved rotation", probability=1., teams={})]
        probabilities = np.asarray([item["probability"] for item in scenarios])
        size = self.config.baseline.simulations
        # One game-wide scenario index is shared by both clubs.
        selected = np.random.default_rng(seed + 1).choice(len(scenarios), size=size, p=probabilities/probabilities.sum())
        rng = np.random.default_rng(seed + int.from_bytes(club.encode(), "big"))
        minutes, rotations = {}, []
        for i, scenario in enumerate(scenarios):
            if scenario["probability"] == 0:
                continue
            state = scenario["teams"].get(club, {})
            out = {resolve(name) for name in state.get("out", [])}
            active = {resolve(name) for name in state.get("active", [])}
            limits = state.get("minutes_caps", {})
            caps = {resolve(name): value for name, value in limits.items()}
            if len(caps) != len(limits) or active & out or set(caps) & out or not (active | set(caps)) <= known:
                raise DataSourceError(f'{scenario["label"]}: {club} lineup conflicts with a confirmed absence or its own rules.')
            watched.update(out | active | set(caps))
            mask = selected == i
            count = int(mask.sum())
            try:
                rotation = sample_rotation(wide, games, known - out, cutoff, self.config, rng,
                                           active=active, limits=caps, size=max(1, count))
            except DataSourceError as exc:
                raise DataSourceError(f'{scenario["label"]} ({club}): {exc}') from exc
            rotations.append(rotation)
            if count:
                for player, sample in rotation.minutes.items():
                    minutes.setdefault(player, np.zeros(size))[mask] = sample
        result = RotationDraws(minutes, min(item.training_games for item in rotations),
                               max(item.last_game_date for item in rotations))
        watched = sorted(player for player in watched if player in wide and player in starts)
        context = LineupRates(wide, games, minutes, watched, starts, self.config) if watched else None
        return result, context
