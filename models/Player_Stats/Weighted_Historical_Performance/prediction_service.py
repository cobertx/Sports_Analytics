"""Saved-history to saved-market integration, independent of the Odds API."""

from dataclasses import asdict, dataclass, field
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import threading

import numpy as np
import pandas as pd

from sports_analytics.data import DataSourceError
from sports_analytics.nba_markets import NBA_TEAM_CODES
from sports_analytics.nfl_markets import NFL_TEAM_CODES
from sports_analytics.display_time import format_eastern
from sports_analytics.result_cache import load_json, save_json
from models.replay import fingerprint as history_fingerprint, input_digest
from .core import MODEL_VERSION, WHPConfig, WHPForecast, WeightedHistoricalPerformance, eligible_history
from .errors import WHPError
from .historical_data import PlayerHistoryRepository, identity_name
from .markets import stat_spec
from .market_comparison import compare_price


def prop_key(row):
    return (row["subject"], row["market_key"].removesuffix("_alternate"))


@dataclass
class PlayerMarketForecast:
    forecasts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    def selection(self, row):
        forecast = self.forecasts.get(prop_key(row))
        if forecast is None:
            return None
        if row["market_key"] == "player_anytime_td":
            if row["selection"] not in {"Yes", "No"}:
                return None
            return forecast.market(.5, "Over" if row["selection"] == "Yes" else "Under")
        if row["line"] is None or row["selection"] not in {"Over", "Under"}:
            return None
        return forecast.market(row["line"], row["selection"])

    def detail(self, row):
        forecast = self.forecasts.get(prop_key(row))
        if forecast is None:
            return self.errors.get(prop_key(row), "WHP unavailable for this market.")
        d = forecast.diagnostics
        return (f"Mean {forecast.mean:.2f}; predictive SD {d['predictive_sd']:.2f}; {d['training_games']} appearances; "
                f"effective N {d['effective_n']:.2f}; {d['validation_games']} walk-forward errors; "
                f"distribution {forecast.family}; last observed game {pd.Timestamp(d['last_game_date']).strftime('%m/%d/%Y')}; "
                f"cutoff {format_eastern(d['as_of'])}." + (' '+d['cache_notice'] if d.get('cache_notice') else ''))


class WHPMarketService:
    def __init__(self, repository=None):
        self.repository = repository or PlayerHistoryRepository()
        self._memory = OrderedDict()
        self._memory_lock = threading.Lock()
        self._ready = threading.Condition(self._memory_lock)
        self._pending = set()

    @staticmethod
    def _valid_saved(stored):
        identity = stored['identity']
        forecast = WHPForecast(**stored['forecast'])
        forecast.market(.5)  # Validate finite moments and settlement support.
        d = forecast.diagnostics
        required = {'predictive_sd','training_games','effective_n','validation_games','last_game_date','as_of'}
        if not isinstance(d,dict) or not required <= set(d):return False
        if (d.get('code_sha256')!=identity['code_sha256'] or d.get('input_sha256')!=identity['input_sha256'] or
                d.get('model_version')!=identity['model_version'] or d.get('config')!=identity['config'] or
                d['as_of']!=identity['cutoff']):return False
        if not all(math.isfinite(d[key]) and d[key]>=0 for key in ('predictive_sd','training_games','effective_n','validation_games')):
            return False
        if not math.isclose(d['predictive_sd']**2,forecast.variance,rel_tol=1e-8):return False
        last = pd.Timestamp(d['last_game_date'])
        if pd.isna(last) or last.tz_localize('UTC')>=pd.Timestamp(identity['cutoff']):return False
        return (isinstance(forecast.nonnegative,bool) and isinstance(forecast.samples,list) and
                isinstance(forecast.weights,list) and len(forecast.samples)==len(forecast.weights)==d['training_games'] and
                np.isfinite(np.asarray(forecast.samples,float)).all() and np.isfinite(np.asarray(forecast.weights,float)).all() and
                np.asarray(forecast.weights).min()>=0 and math.isclose(sum(forecast.weights),1.,rel_tol=1e-8))

    def prepare(self, snapshot, refresh=False):
        return self.repository.prepare(snapshot["sport"], snapshot["commence_time"],
                                       [snapshot["home_team"], snapshot["away_team"]], refresh)

    @staticmethod
    def _player(data, snapshot, subject, cutoff):
        names = data["_whp_name"] if "_whp_name" in data else data["entity_name"].map(identity_name)
        found = data[names.eq(identity_name(subject))].copy()
        found = found[found["event_date"].lt(cutoff) & found["available_at"].lt(cutoff)]
        codes = NBA_TEAM_CODES if snapshot["sport"] == "NBA" else NFL_TEAM_CODES
        clubs = {identity_name(name) for name in (snapshot["home_team"], snapshot["away_team"])}
        clubs.update(identity_name(codes[name]) for name in (snapshot["home_team"], snapshot["away_team"]) if name in codes)
        club_aliases = {"was": "wsh", "la": "lar", "oak": "lv", "sd": "lac", "jac": "jax",
                        "pho": "phx", "brk": "bkn", "cho": "cha"}
        def club(value):
            name = identity_name(value)
            return club_aliases.get(name, name) if snapshot["sport"] != "NCAAF" else name
        clubs = {club(name) for name in clubs}
        candidates = []
        for identity, rows in found.groupby("entity_id"):
            latest = rows.sort_values(["event_date", "game_id"]).iloc[-1]
            if club(latest.get("market_team", latest["team"])) in clubs:
                candidates.append(identity)
        if len(candidates) != 1:
            raise WHPError(f"Cannot uniquely match {subject} to a saved player ID on this matchup's teams. "
                           "Prepare history or add a canonical import; fuzzy name matches are not used.")
        selected = data[data["entity_id"].eq(candidates[0])].copy()
        selected = selected[selected["available_at"].lt(cutoff) & selected["event_date"].lt(cutoff)]
        if selected.empty or (cutoff - selected["event_date"].max()).days > 450:
            raise WHPError("Player history is too old for a current role forecast.")
        return selected

    def forecast(self, snapshot):
        sport = snapshot.get("sport", "NFL")
        snapshot = {**snapshot, "sport": sport}
        cutoff = min(pd.Timestamp(snapshot["pulled_at"]), pd.Timestamp(snapshot["commence_time"]))
        if cutoff.tzinfo is None:
            raise DataSourceError("Snapshot and game timestamps must include a timezone.")
        data = self.repository.load(sport, snapshot["commence_time"], [snapshot["home_team"], snapshot["away_team"]])
        data = data[data["event_date"].lt(cutoff) & data["available_at"].lt(cutoff)].copy()
        data["_whp_name"] = data["entity_name"].map(identity_name)
        result = PlayerMarketForecast()
        config = WHPConfig.for_sport(sport)
        implementation = sha256(b"".join((Path(__file__).parent/name).read_bytes() for name in
            ["core.py", "weights.py", "distributions.py", "opportunity.py", "markets.py", "market_comparison.py", "prediction_service.py", "historical_data.py", "../../season_weights.py"])).hexdigest()
        # Check the actual history contents on every request, including revised
        # observations with unchanged dates. Criteria do not change a forecast.
        cache_key = sha256(repr((implementation, asdict(config), history_fingerprint(data),
            str(self.repository.root), {key: snapshot.get(key) for key in
            ('sport', 'game_id', 'home_team', 'away_team', 'pulled_at', 'commence_time', 'rows')})).encode()).hexdigest()
        with self._ready:
            self._ready.wait_for(lambda:cache_key not in self._pending)
            cached = self._memory.get(cache_key)
            try:available = cached is not None and (cached[1] is None or cached[1].is_file())
            except OSError:available = cached is not None
            if available:
                self._memory.move_to_end(cache_key)
                return deepcopy(cached[0])
            self._pending.add(cache_key)
        try:
            return self._calculate(snapshot,sport,cutoff,data,result,config,implementation,cache_key)
        finally:
            with self._ready:
                self._pending.discard(cache_key)
                self._ready.notify_all()

    def _calculate(self,snapshot,sport,cutoff,data,result,config,implementation,cache_key):
        players, failed_players = {}, {}
        for row in snapshot["rows"]:
            if not row["market_key"].startswith("player_") or prop_key(row) in result.forecasts or prop_key(row) in result.errors:
                continue
            key = prop_key(row)
            spec = stat_spec(sport, row["market_key"])
            if spec is None:
                result.errors[key] = "WHP does not model first/last scorer order or unsupported prop definitions."
                continue
            try:
                subject = row['subject']
                if subject in failed_players:
                    raise WHPError(failed_players[subject])
                if subject not in players:
                    try:
                        players[subject] = self._player(data, snapshot, subject, cutoff)
                    except WHPError as exc:
                        failed_players[subject] = str(exc)
                        raise
                player = players[subject].copy()
                if not set(spec.fields) <= set(player):
                    raise WHPError("Missing historical target columns: " + ", ".join(set(spec.fields) - set(player)) +
                                   ". Missing statistics are not treated as zero.")
                target = player[list(spec.fields)].apply(pd.to_numeric, errors="coerce")
                player["value"] = target.sum(axis=1, min_count=len(spec.fields))
                # A combination is summed within each game, preserving covariance.
                player = player[np.isfinite(player["value"])].copy()
                if spec.opportunity and spec.opportunity in player:
                    player["opportunity"] = player[spec.opportunity]
                sample = eligible_history(player, cutoff)
                columns = [c for c in ["game_id", "entity_id", "event_date", "available_at", "season", "value", "opportunity", "team"] if c in sample]
                fingerprint = input_digest(sample[columns])
                identity = {"model_version": MODEL_VERSION, "code_sha256": implementation,
                            "sport": sport, "player": row["subject"], "market": key[1],
                            "cutoff": cutoff.isoformat(), "input_sha256": fingerprint, "config": asdict(config)}
                digest = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                path = self.repository.root / "forecasts" / sport.lower() / f"{digest}.json"
                stored = load_json(path,self.repository.root,identity,self._valid_saved)
                forecast = WHPForecast(**stored['forecast']) if stored else None
                if forecast is None:
                    forecast = WeightedHistoricalPerformance(config).forecast(sample, cutoff, nonnegative=spec.nonnegative, count=spec.count)
                    forecast.diagnostics.update(player_id=str(sample.iloc[-1]["entity_id"]),
                        player_name=row["subject"], sport=sport, market=key[1], input_sha256=fingerprint,
                        code_sha256=implementation, source_availability="Conservative result-time proxy unless explicit available_at was supplied.")
                    if not save_json(path,{"identity":identity,"forecast":asdict(forecast)},self.repository.root):
                        forecast.diagnostics['cache_notice'] = 'Forecast available for this session; local storage could not be updated.'
                result.forecasts[key] = forecast
            except WHPError as exc:
                result.errors[key] = str(exc)
        # Audit the exact quote comparison without changing the original snapshot.
        evaluations = []
        quotes = {}
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):
                continue
            key = (row['subject'], row['market_key'], row['line'], row['selection'])
            quotes.setdefault(key, []).append(row['odds'])
        for row in snapshot["rows"]:
            if not row["market_key"].startswith("player_"):
                continue
            try:
                price = result.selection(row)
                opposite_side = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}.get(row["selection"])
                opposite_quotes = quotes.get((row['subject'], row['market_key'], row['line'], opposite_side), [])
                comparison = compare_price(price, row["odds"], opposite_quotes[0] if len(opposite_quotes) == 1 else None)
            except ValueError:
                price, comparison = None, {}
            evaluations.append({"subject": row["subject"], "market": row["market_key"], "line": row["line"],
                "selection": row["selection"], "market_odds": row["odds"], **comparison,
                "win": price.win if price else None, "loss": price.loss if price else None,
                "push": price.push if price else None,
                "detail": result.detail(row)})
        audit = {"sport": sport, "game_id": snapshot["game_id"], "snapshot": snapshot["pulled_at"],
                 "cutoff": cutoff.isoformat(), "implementation": implementation, "evaluations": evaluations}
        digest = sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest()
        audit_path = self.repository.root / "evaluations" / f"{digest}.json"
        saved = save_json(audit_path,audit,self.repository.root)
        if not saved:
            for forecast in result.forecasts.values():
                forecast.diagnostics['cache_notice'] = 'Forecast available for this session; its local audit could not be saved.'
        try:archive = audit_path if audit_path.is_file() else None
        except OSError:archive = None
        with self._memory_lock:
            self._memory[cache_key] = (deepcopy(result), archive)
            self._memory.move_to_end(cache_key)
            if len(self._memory) > 8:
                self._memory.popitem(last=False)
        return result
