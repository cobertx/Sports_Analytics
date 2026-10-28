"""Connect saved game identities to cached, reproducible PPP estimates."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

from models.Game_Outcome.PPP.PPP import PPPError, PPPForecast, PPPModel
from models import season_weights
from .data import DataSourceError
from .nfl_markets import NFL_TEAM_CODES
from .nfl_model_data import NflModelData, atomic_text

MODEL_VERSION = 1


class PPPMarketService:
    method = "PPP"
    forecast_class = PPPForecast
    model_version = MODEL_VERSION
    sport = "NFL"
    team_codes = NFL_TEAM_CODES
    season_start_month = 3
    input_patterns = ("team_games_*.csv",)

    def __init__(self, data=None, model=None):
        self.data = data or NflModelData()
        self.model = model or PPPModel()
        self._lock = threading.Lock()
        self._memory = {}

    def forecast(self, snapshot):
        try:
            home, away = self.team_codes[snapshot["home_team"]], self.team_codes[snapshot["away_team"]]
            pulled = datetime.fromisoformat(snapshot["pulled_at"].replace("Z", "+00:00"))
            kickoff = datetime.fromisoformat(snapshot["commence_time"].replace("Z", "+00:00"))
            if pulled.tzinfo is None or kickoff.tzinfo is None:
                raise ValueError("Timezone required")
            cutoff = min(pulled, kickoff).astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise DataSourceError(f"{self.method} requires recognized {self.sport} teams and timestamped saved game information.") from None
        identity = {"version": self.model_version, "home": home, "away": away, "as_of": cutoff.isoformat(),
                    "season_weighting": hashlib.sha256(Path(season_weights.__file__).read_bytes()).hexdigest(),
                    "config": asdict(self.model.config), "neutral": bool(snapshot.get("game", {}).get("neutral", False))}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self._lock:
            if key in self._memory:
                return self._memory[key]
            path = self.data.directory / "forecasts" / f"{self.method.lower()}_{key}.json"
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    forecast = self.forecast_class(**saved["forecast"])
                    if saved["identity"] == identity:
                        self._memory[key] = forecast
                        return forecast
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            season = cutoff.year - (cutoff.month < self.season_start_month)
            try:
                frame, notices = self.data.prepare([season - 1, season])
                forecast = self.model.predict(frame, home, away, cutoff.isoformat(), neutral=identity["neutral"])
            except (PPPError, DataSourceError, OSError, ValueError) as exc:
                raise DataSourceError(f"{self.method} estimate unavailable. {exc}") from None
            forecast.diagnostics["source_notices"] = notices
            forecast.diagnostics["snapshot_after_kickoff"] = pulled > kickoff
            forecast.diagnostics["prepared_at"] = datetime.now(timezone.utc).isoformat()
            forecast.diagnostics["input_files"] = [
                {"file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                for source in sorted({source for pattern in self.input_patterns for source in self.data.directory.glob(pattern)})
                if str(season) in source.stem or str(season - 1) in source.stem]
            try:
                atomic_text(path, json.dumps({"identity": identity, "forecast": asdict(forecast)}, indent=2))
            except OSError:
                forecast.diagnostics["source_notices"].append("This estimate could not be archived; check modeling-folder access.")
            self._memory[key] = forecast
            return forecast
