"""Pregame NBA boxes and joint rotation scenarios to settled player-prop forecasts."""

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from models.Player_Stats.Usage_Opportunity.distributions import UsageOpportunityForecast
from models.Player_Stats.Usage_Opportunity.historical_data import (
    TEAM_ALIASES, UsageHistoryRepository, eligible, identity_name, match_player, PlayerHistoryIndex,
)
from models.Player_Stats.Usage_Opportunity.markets import market_spec, prop_key
from models.Player_Stats.Usage_Opportunity.nba_model import simulate_nba
from models.Player_Stats.Usage_Opportunity.prediction_service import UsageMarketForecast
from models.replay import input_digest
from sports_analytics.availability import OUT
from sports_analytics.data import DataSourceError
from sports_analytics.nba_markets import NBA_TEAM_CODES
from sports_analytics.result_cache import load_json, save_json

from .config import RotationConfig
from .rotation import draw_rotation

ROOT = Path(__file__).resolve().parents[3] / "data/modeling/player_stats/playing_time_rotation"


def team_code(name):
    code = NBA_TEAM_CODES.get(name, name)
    return TEAM_ALIASES["NBA"].get(code, code)


class RotationMarketForecast(UsageMarketForecast):
    def detail(self, row):
        fitted = self.forecasts.get(prop_key(row))
        if fitted is None:
            return self.errors.get(prop_key(row), "Playing-Time Rotation is unavailable for this market.")
        d = fitted.diagnostics
        return (f'PTR mean {fitted.mean:.2f} if playing · 90% interval [{fitted.quantile(.05)}, {fitted.quantile(.95)}] · '
                f'{d["play_probability"]:.1%} of rotation paths played · {d["minutes_if_playing"]:.1f} minutes if playing · '
                f'{d["rotation_games"]} saved rotation games · latest input {d["last_game_date"]}.')


class PlayingTimeRotationMarketService:
    version = "ptr-1"
    title = "Playing-Time Rotation"
    config_class = RotationConfig
    forecast_class = RotationMarketForecast
    cache_root = ROOT
    limitations = ("Regulation-only saved-box rotation scenarios. Prop probabilities condition on playing. "
                   "Availability uses confirmed absences known by the quote cutoff when supplied.")

    def __init__(self, repository=None, config=None, *, persist=True, availability=None):
        self.repository = repository or UsageHistoryRepository(root=self.cache_root)
        self.config = config or self.config_class()
        self.persist = persist
        self.availability = availability

    def prepare(self, snapshot, refresh=False):
        self.config_class.for_sport(snapshot["sport"])
        return self.repository.prepare("NBA", snapshot["commence_time"],
                                       [snapshot["home_team"], snapshot["away_team"]], refresh)

    def _availability_packet(self, snapshot):
        return self.availability.load(snapshot) if self.availability is not None else (None, None)

    def _availability(self, snapshot, cutoff, clubs):
        packet, error = self._availability_packet(snapshot)
        # A newer injury packet must not update an older quote's information set.
        fetched = pd.to_datetime((packet or {}).get("fetched_at"), errors="coerce", utc=True)
        if error or pd.isna(fetched) or fetched > cutoff:
            return {}
        game_date = pd.Timestamp(snapshot["commence_time"]).date()
        latest = {}
        for report in packet["reports"]:
            club = team_code(report.get("team_code") or report["team"])
            published = pd.to_datetime(report.get("reported_at"), errors="coerce", utc=True)
            if club not in clubs or pd.isna(published) or published > cutoff or published > fetched:
                continue
            key = (club, identity_name(report["player"]))
            previous = latest.get(key)
            status = report["status"].casefold()
            if previous is None or published > previous[0]:
                latest[key] = (published, {status}, report)
            elif published == previous[0]:
                previous[1].add(status)
        reports, excluded = [], []
        for (club, player), (_, statuses, report) in sorted(latest.items()):
            if len(statuses) != 1 or statuses & {"active", "healthy"}:
                continue
            reports.append(dict(team=club, player=report["player"], status=report["status"]))
            return_date = pd.to_datetime(report.get("return_date"), errors="coerce")
            if statuses & OUT and (pd.isna(return_date) or return_date.date() > game_date):
                excluded.append([club, player])
        return dict(rotation=True, fetched_at=packet["fetched_at"], injuries=reports, excluded=excluded)

    def _roster(self, players, clubs):
        latest = players.drop_duplicates("entity_id", keep="last")
        return latest.loc[latest.team.isin(clubs)]

    def _team_model(self, players, teams, roster, known, cutoff, club, seed, availability):
        return (draw_rotation(players, teams, known, cutoff, self.config,
                              np.random.default_rng(seed + int.from_bytes(club.encode(), "big"))), None)

    def _implementation_files(self):
        source = Path(__file__).parent
        return [*source.glob("*.py"), *(source.parent/"Usage_Opportunity").glob("*.py"),
                *(source.parent/"Weighted_Historical_Performance").glob("*.py"),
                source.parents[1]/"season_weights.py"]

    def forecast(self, snapshot):
        if snapshot["sport"] != "NBA":
            raise DataSourceError(f"{self.title} supports NBA player statistics.")
        stamps = [pd.Timestamp(snapshot[key]) for key in ("pulled_at", "commence_time")]
        if any(pd.isna(stamp) or stamp.tzinfo is None for stamp in stamps):
            raise DataSourceError("Snapshot and tipoff timestamps must include a timezone.")
        cutoff = min(stamps)
        names = [snapshot["home_team"], snapshot["away_team"]]
        clubs = [team_code(name) for name in names]
        rows = [row for row in snapshot["rows"] if row["market_key"].startswith("player_")]
        if not rows:
            return self.forecast_class()

        players, teams = self.repository.load("NBA", snapshot["commence_time"], names)
        players, teams = eligible(players, cutoff, "NBA"), eligible(teams, cutoff, "NBA")
        latest = self._roster(players, clubs)
        availability = self._availability(snapshot, cutoff, clubs)
        excluded = {tuple(item) for item in availability.get("excluded", [])}
        assignments = {row.entity_id: row.team for row in latest.itertuples()
                       if (row.team, identity_name(row.entity_name)) not in excluded}
        team_players = players.loc[players.team.isin(clubs)]
        team_boxes = teams.loc[teams.team.isin(clubs)]
        subject_names = {identity_name(row["subject"]) for row in rows}
        named = players.loc[players.entity_name.map(identity_name).isin(subject_names)].copy()
        named["_whp_name"] = named.entity_name.map(identity_name)
        if named.empty or team_boxes.empty:
            raise DataSourceError("No saved NBA matchup player and team boxes before the forecast cutoff.")

        identity, path = None, None
        if self.persist:
            files = sorted(self._implementation_files())
            identity = dict(implementation=sha256(b"".join(file.read_bytes() for file in files)).hexdigest(),
                            input_sha256=input_digest(team_players, team_boxes, named, latest),
                            cutoff=cutoff.isoformat(), config=asdict(self.config), availability=availability,
                            snapshot={key:snapshot[key] for key in ("game_id", "home_team", "away_team")},
                            props=[list(key) for key in sorted({prop_key(row) for row in rows})])
            digest = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            path = self.repository.root / "forecasts" / f"{digest}.json"
            cached = load_json(path, self.repository.root, identity)
            if cached is not None:
                try:
                    result = self.forecast_class(
                        {tuple(key): UsageOpportunityForecast(**value) for key, value in cached["forecasts"]},
                        {tuple(key): value for key, value in cached["errors"]})
                    result.availability = availability
                    return result
                except (TypeError, ValueError, KeyError):
                    pass

        result = self._simulate(snapshot, cutoff, rows, named, team_players, team_boxes, latest, assignments, excluded, availability)
        result.availability = availability
        if path is not None:
            save_json(path, dict(identity=identity, forecasts=[(key, asdict(value)) for key, value in result.forecasts.items()],
                                 errors=list(result.errors.items())), self.repository.root)
        return result

    def _simulate(self, snapshot, cutoff, rows, named, players, teams, roster, assignments, excluded, availability):
        result, matched, failures = self.forecast_class(), {}, {}
        props = {prop_key(row) for row in rows}
        index = PlayerHistoryIndex(named)
        for subject, market in sorted(props):
            if market_spec("NBA", market) is None:
                result.errors[subject, market] = f"{self.title} does not support this NBA statistic."
            elif subject not in matched and subject not in failures:
                try:
                    history = match_player(named, snapshot, subject, cutoff, index=index)
                    current = history.iloc[-1]
                    if (current.team, identity_name(subject)) in excluded:
                        raise DataSourceError("Reported unavailable before the forecast cutoff. No settled prop estimate.")
                    same_team = history.loc[history.team.eq(current.team)]
                    minutes = pd.to_numeric(same_team["min"], errors="coerce")
                    matched[subject] = dict(team=current.team, entity_id=current.entity_id,
                                            history=same_team.loc[minutes.gt(0)])
                except (ValueError, DataSourceError, KeyError) as exc:
                    failures[subject] = str(exc)

        config = self.config.baseline
        # Use common random inputs for the rotation-based methods when their
        # scenario inputs are identical.
        seed = int(sha256(f'PTR:{snapshot["game_id"]}:{config.seed}'.encode()).hexdigest()[:16], 16)
        grouped = {club: group for club, group in teams.groupby("team")}
        rotations, box_scores, lineups = {}, {}, {}
        for club in sorted({player["team"] for player in matched.values()}):
            try:
                history = grouped.get(club, teams.iloc[:0])
                known = {identity for identity, team in assignments.items() if team == club}
                rotations[club], context = self._team_model(players.loc[players.team.eq(club)], history,
                    roster.loc[roster.team.eq(club)], known, cutoff, club, seed, availability)
                if context is not None:
                    lineups[club] = context
                box_scores[club] = history
            except DataSourceError as exc:
                failures.update({subject: str(exc) for subject, player in matched.items() if player["team"] == club})
        for subject, player in matched.items():
            rotation = rotations.get(player["team"])
            if rotation is not None and player["entity_id"] not in rotation.minutes:
                failures[subject] = "The player has no recent appearances in the saved rotation for this team."
        matched = {subject: player for subject, player in matched.items() if subject not in failures}
        if matched:
            try:
                active_clubs = sorted({player["team"] for player in matched.values()})
                unique_players = {player["entity_id"]: player for player in matched.values()}
                simulated = simulate_nba(dict(sorted(unique_players.items())),
                                         {club: box_scores[club] for club in active_clubs}, config, np.random.default_rng(seed),
                                         rotation={club: rotation.minutes for club, rotation in rotations.items()}, lineup=lineups)
                failures.update({subject:simulated.errors[player["entity_id"]] for subject,player in matched.items()
                                 if player["entity_id"] in simulated.errors})
            except (ValueError, DataSourceError) as exc:
                failures.update({subject: str(exc) for subject in matched})
        for subject, market in sorted(props):
            key = (subject, market)
            if key in result.errors:
                continue
            if subject in failures:
                result.errors[key] = failures[subject]
                continue
            player = matched[subject]
            rotation = rotations[player["team"]]
            entity_id = player["entity_id"]
            minutes = rotation.minutes[entity_id]
            played = minutes > 1e-8
            if not played.any() or entity_id not in simulated.players:
                result.errors[key] = "No eligible playing-time draws for this player."
                continue
            stats = simulated.players[entity_id]
            values = sum(stats[field] for field in market_spec("NBA", market).fields)
            detail = simulated.diagnostics[entity_id]
            fitted_minutes = minutes[played]
            diagnostics = {**detail, "model_version": self.version, "as_of": cutoff.isoformat(),
                           "last_game_date": max(detail["last_game_date"], rotation.last_game_date),
                           "rotation_games": rotation.training_games, "play_probability": float(played.mean()),
                           "minutes_mean": float(minutes.mean()), "minutes_if_playing": float(fitted_minutes.mean()),
                           "minutes_interval_if_playing": np.quantile(fitted_minutes, [.05, .95]).tolist(),
                           "unconditional_mean": float(values.mean()), "unconditional_zero": float((values == 0).mean()),
                           "limitations": self.limitations}
            result.forecasts[key] = UsageOpportunityForecast.from_samples(values[played], diagnostics)
        return result
