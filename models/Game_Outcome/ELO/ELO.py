"""Sequential Elo + ridge power margins + positive O/D scoring (PDF 2-5, 7-10).

The published forecast uses one reconciled margin/total normal baseline. Elo
alone is never used to invent a total. NFL and NBA parameters/calibration differ.
"""

from dataclasses import asdict, dataclass
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from models.Game_Outcome.PPP.PPP import PPPError, PPPForecast
from models.Game_Outcome.PPP.NBA_PPP import NBAPPPForecast, NBAPPPModel


class ELOError(PPPError):
    """Invalid or insufficient Elo/power observations; no substitute forecast."""


@dataclass(frozen=True)
class ELOConfig:
    sport: str = "NFL"
    half_life_days: float = 180
    k_factor: float = 20
    elo_scale: float = 400
    home_elo: float = 50
    season_carry: float = 2 / 3
    power_ridge: float = 10
    scoring_ridge: float = 20
    logistic_ridge: float = 10
    minimum_training_games: int = 50
    minimum_team_games: int = 4
    minimum_calibration_games: int = 40
    maximum_calibration_games: int = 250

    def __post_init__(self):
        positive = (self.half_life_days, self.k_factor, self.elo_scale,
                    self.power_ridge, self.scoring_ridge, self.logistic_ridge)
        if (self.sport not in {"NFL", "NBA", "NCAAF"} or
                not all(math.isfinite(x) and x > 0 for x in positive) or
                not math.isfinite(self.home_elo) or not 0 <= self.season_carry <= 1 or
                any(not isinstance(x, int) or isinstance(x, bool) for x in (
                    self.minimum_training_games, self.minimum_team_games,
                    self.minimum_calibration_games, self.maximum_calibration_games)) or
                min(self.minimum_training_games, self.minimum_team_games) < 1 or
                self.minimum_calibration_games < 3 or
                self.maximum_calibration_games < self.minimum_calibration_games):
            raise ELOError("ELO configuration requires a supported sport, finite parameters and adequate sample counts.")

    @classmethod
    def nba(cls, **overrides):
        values = dict(sport="NBA", half_life_days=90, k_factor=20, home_elo=60,
                      season_carry=.75, power_ridge=20, scoring_ridge=50,
                      minimum_training_games=100, minimum_team_games=10,
                      minimum_calibration_games=100, maximum_calibration_games=300)
        return cls(**(values | overrides))


@dataclass
class ELOForecast(PPPForecast):
    """NFL settlement API backed by independently fitted Elo/power parameters."""


@dataclass
class NBAELOForecast(NBAPPPForecast):
    """NBA settlement retains the common full-game no-tie margin adjustment."""


def elo_probability(home_rating, away_rating, home_advantage=0, scale=400):
    if not all(math.isfinite(x) for x in (home_rating, away_rating, home_advantage, scale)) or scale <= 0:
        raise ELOError("Elo inputs must be finite and the scale positive.")
    logit = math.log(10) * (home_rating - away_rating + home_advantage) / scale
    return 1 / (1 + math.exp(-max(-35., min(35., logit))))


def elo_update(home_rating, away_rating, home_score, away_score, *, k=20, home_advantage=0, scale=400):
    if not all(math.isfinite(x) and x >= 0 for x in (home_score, away_score)) or not math.isfinite(k) or k <= 0:
        raise ELOError("Elo updates require nonnegative final scores and a positive K.")
    probability = elo_probability(home_rating, away_rating, home_advantage, scale)
    result = .5 if home_score == away_score else float(home_score > away_score)
    change = k * (result - probability)  # PDF 2.2: g=1, no unvalidated MOV multiplier.
    return home_rating + change, away_rating - change


def reconcile_scores(total, margin):
    """PDF 1/8 accounting identity, bounded to nonnegative team means."""
    if not all(math.isfinite(x) for x in (total, margin)) or total <= 0:
        raise ELOError("Elo/power total must be positive and finite.")
    margin = max(-total, min(total, margin))
    return np.asarray([(total + margin) / 2, (total - margin) / 2])


class ELOModel:
    def __init__(self, config=None):
        self.config = config or ELOConfig()

    def _season(self, cutoff):
        return cutoff.year - (cutoff.month < (10 if self.config.sport == "NBA" else 3))

    def _history(self, observations, as_of):
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            raise ELOError("ELO cutoff must include a timezone.")
        cutoff = cutoff.tz_convert("UTC")
        frame = observations.copy()
        if self.config.sport == "NFL":
            required = {"offensive_points", "non_offensive_points", "offensive_drives", "defensive_drives", "season", "week", "neutral"}
            if not required <= set(frame):
                raise ELOError("Prepare NFL drive/scoring observations before fitting ELO.")
            frame["possessions"] = (frame.offensive_drives + frame.defensive_drives) / 2
        # Reuse complete-pair/date/score validation, never any PPP fitted means.
        frame = NBAPPPModel()._history(frame, cutoff)
        if self.config.sport == "NBA":
            frame["offensive_points"] = frame.points
            frame["non_offensive_points"] = 0.
            frame["offensive_drives"] = frame.possessions
            frame["defensive_drives"] = frame.possessions
            frame["neutral"] = frame.get("neutral", 0)
            frame["season"] = frame.event_date.dt.year - (frame.event_date.dt.month < 10).astype(int)
            if frame.points.eq(frame.points_allowed).any():
                raise ELOError("Full-game NBA results cannot be tied.")
        numeric = ["offensive_points", "non_offensive_points", "offensive_drives", "defensive_drives", "neutral", "season"]
        if (not np.isfinite(frame[numeric].to_numpy(float)).all() or
                (frame[["offensive_points", "non_offensive_points"]] < 0).any().any() or
                (frame[["offensive_drives", "defensive_drives"]] <= 0).any().any() or
                not frame.neutral.isin([0, 1]).all() or
                not np.allclose(frame.offensive_points + frame.non_offensive_points, frame.points)):
            raise ELOError("ELO requires positive opportunities and reconciled scoring components.")
        paired = frame.merge(frame, on="game_id", suffixes=("", "_other"))
        paired = paired[paired.team.ne(paired.team_other)]
        if (not np.allclose(paired.offensive_drives, paired.defensive_drives_other) or
                not paired.season.eq(paired.season_other).all() or
                not paired.neutral.eq(paired.neutral_other).all() or
                not paired.available_at.eq(paired.available_at_other).all()):
            raise ELOError("ELO paired games disagree on volume, season, venue or availability.")
        if self.config.sport == "NFL" and (not np.isfinite(frame.week.to_numpy(float)).all() or
                not paired.week.eq(paired.week_other).all()):
            raise ELOError("NFL ELO requires consistent game weeks.")
        return frame

    def _weights(self, frame, cutoff):
        return np.exp2(-(pd.Timestamp(cutoff) - frame.event_date).dt.total_seconds().to_numpy() /
                       86400 / self.config.half_life_days)

    def _elo(self, history, cutoff):
        """Freeze each date's predictions; apply updates only after availability."""
        ratings, pending = {}, []
        current_season = None
        def transition(season):
            nonlocal current_season
            if current_season is not None and season > current_season:
                carry = self.config.season_carry ** (season-current_season)
                for team in ratings:
                    ratings[team] = 1500 + carry * (ratings[team]-1500)
            current_season = season if current_season is None else max(season, current_season)
        def publish(at):
            nonlocal pending
            ready = [item for item in pending if item[0] < at]
            pending = [item for item in pending if item[0] >= at]
            for _, home, away, change in sorted(ready):
                ratings[home] = ratings.get(home, 1500.) + change
                ratings[away] = ratings.get(away, 1500.) - change
        games = history[history.is_home.eq(1)].sort_values(["event_date", "game_id"])
        for when, group in games.groupby("event_date", sort=True):
            publish(when)
            for season in sorted(group.season.unique()):
                transition(int(season))
            for h in group.itertuples(index=False):
                home, away = ratings.get(h.team, 1500.), ratings.get(h.opponent, 1500.)
                probability = elo_probability(home, away, 0 if h.neutral else self.config.home_elo, self.config.elo_scale)
                result = .5 if h.points == h.points_allowed else float(h.points > h.points_allowed)
                pending.append((h.available_at, h.team, h.opponent, self.config.k_factor * (result-probability)))
        publish(pd.Timestamp(cutoff))
        transition(self._season(pd.Timestamp(cutoff)))
        return ratings

    def _fit(self, history, cutoff):
        if history.game_id.nunique() < self.config.minimum_training_games:
            raise ELOError("Not enough earlier league games for ELO/power fitting.")
        teams = {team: index for index, team in enumerate(sorted(set(history.team) | set(history.opponent)))}
        n = len(teams)
        home_games = history[history.is_home.eq(1)]
        x = np.zeros((len(home_games), n+1))
        x[:, 0] = 1-home_games.neutral.to_numpy()
        x[np.arange(len(x)), 1+home_games.team.map(teams).to_numpy()] = 1
        x[np.arange(len(x)), 1+home_games.opponent.map(teams).to_numpy()] = -1
        weight = self._weights(home_games, cutoff)
        penalty = np.full(n+1, self.config.power_ridge)
        penalty[0] = 1
        power = np.linalg.solve(x.T @ (weight[:, None]*x) + np.diag(penalty),
                                x.T @ (weight*(home_games.points-home_games.points_allowed).to_numpy()))
        power[1:] -= power[1:].mean()
        # Joint opponent-adjusted positive scoring per drive (NFL) / possession (NBA).
        x = np.zeros((len(history), 2+2*n))
        x[:, 0] = 1
        x[:, 1] = (2*history.is_home.to_numpy()-1)*(1-history.neutral.to_numpy())/2
        x[np.arange(len(x)), 2+history.team.map(teams).to_numpy()] = 1
        x[np.arange(len(x)), 2+n+history.opponent.map(teams).to_numpy()] = -1
        weight = self._weights(history, cutoff)
        exposure, points = history.offensive_drives.to_numpy(float), history.offensive_points.to_numpy(float)
        league = float(np.dot(weight, points)/np.dot(weight, exposure))
        if league <= 0:
            raise ELOError("ELO scoring needs positive league efficiency.")
        beta = np.zeros(2+2*n)
        beta[0] = math.log(league)
        penalty = np.full(len(beta), self.config.scoring_ridge)
        penalty[0], penalty[1] = 0, self.config.scoring_ridge/10
        def objective(value):
            eta = np.clip(x @ value, -15, 5)
            return float(np.dot(weight, exposure*np.exp(eta)-points*eta) + .5*np.dot(penalty, value*value))
        for _ in range(60):
            prediction = exposure*np.exp(np.clip(x @ beta, -15, 5))
            gradient = x.T @ (weight*(prediction-points)) + penalty*beta
            hessian = x.T @ ((weight*prediction)[:, None]*x) + np.diag(penalty)
            change = np.linalg.solve(hessian, gradient)
            step = 1.
            before = objective(beta)
            while step > 1/1024 and objective(beta-step*change) > before:
                step /= 2
            beta -= step*change
            if np.max(np.abs(step*change)) < 1e-7:
                break
        for start, sign in [(2, 1), (2+n, -1)]:
            center = beta[start:start+n].mean()
            beta[start:start+n] -= center
            beta[0] += sign*center
        return {"teams": teams, "power": power, "scoring": beta, "elo": self._elo(history, cutoff)}

    def _raw(self, history, fitted, home, away, cutoff, neutral=False):
        own = {team: history[history.team.eq(team)] for team in (home, away)}
        if home == away or any(len(rows) < self.config.minimum_team_games for rows in own.values()):
            raise ELOError("Both teams need enough earlier appearances for ELO.")
        def average(rows, column):
            return float(np.average(rows[column], weights=self._weights(rows, cutoff)))
        pace = np.mean([average(own[t], col) for t in (home, away)
                        for col in ("offensive_drives", "defensive_drives")])
        mapping, beta, power = fitted["teams"], fitted["scoring"], fitted["power"]
        n = len(mapping)
        league_extra = average(history, "non_offensive_points")
        means, details = [], {}
        for team, opponent, sign in [(home, away, 1), (away, home, -1)]:
            rate = math.exp(float(beta[0]+beta[2+mapping[team]]-beta[2+n+mapping[opponent]] +
                                  (0 if neutral else sign*beta[1]/2)))
            rows = own[team]
            w = self._weights(rows, cutoff)
            extra = (float(np.dot(w, rows.non_offensive_points))+2*league_extra)/(w.sum()+2)
            means.append(float(pace*rate+extra))
            details[team] = {"elo": float(fitted["elo"].get(team, 1500.)),
                             "power_points": float(power[1+mapping[team]]),
                             "offense_log_effect": float(beta[2+mapping[team]]),
                             "defense_quality_log_effect": float(beta[2+n+mapping[team]]),
                             "expected_opportunities": float(pace), "scoring_per_opportunity": rate,
                             "additional_points": float(extra), "prior_games": len(rows)}
        margin = float(power[1+mapping[home]]-power[1+mapping[away]]+(0 if neutral else power[0]))
        probability = elo_probability(details[home]["elo"], details[away]["elo"],
                                      0 if neutral else self.config.home_elo, self.config.elo_scale)
        return {"total": sum(means), "power_margin": margin, "od_margin": means[0]-means[1],
                "elo_probability": probability, "teams": details}

    def _calibrate(self, errors):
        """Fit Elo calibration and PDF 8 margin blend on forward-produced records."""
        actual = np.asarray([[r["home_score"], r["away_score"]] for r in errors])
        totals = np.asarray([r["total"] for r in errors])
        margins = np.asarray([r["power_margin"] for r in errors])
        raw_means = np.stack([reconcile_scores(t, m) for t, m in zip(totals, margins)])
        initial = actual-raw_means
        sigma = float(np.std(initial[:, 0]-initial[:, 1], ddof=1))
        if not math.isfinite(sigma) or sigma <= 0:
            raise ELOError("ELO held-out margins do not support nonzero uncertainty.")
        probs = np.clip([r["elo_probability"] for r in errors], 1e-9, 1-1e-9)
        logits = np.log(probs/(1-probs))
        non_ties = actual[:, 0] != actual[:, 1]
        target = (actual[non_ties, 0] > actual[non_ties, 1]).astype(float)
        if len(target) < self.config.minimum_calibration_games or len(np.unique(target)) < 2:
            raise ELOError("ELO calibration requires enough non-tied home wins and losses.")
        x = np.column_stack([np.ones(len(target)), logits[non_ties]])
        coefficient, prior = np.asarray([0., 1.]), np.asarray([0., 1.])
        penalty = np.diag([self.config.logistic_ridge, self.config.logistic_ridge])
        # Positive slope via bounded Newton steps; regularize toward identity.
        for _ in range(60):
            p = 1/(1+np.exp(-np.clip(x@coefficient, -35, 35)))
            gradient = x.T@(p-target)+penalty@(coefficient-prior)
            hessian = x.T@((p*(1-p))[:, None]*x)+penalty
            change = np.linalg.solve(hessian, gradient)
            next_coefficient = coefficient-change
            next_coefficient[1] = max(0., next_coefficient[1])
            if np.max(np.abs(next_coefficient-coefficient)) < 1e-8:
                coefficient = next_coefficient
                break
            coefficient = next_coefficient
        calibrated = 1/(1+np.exp(-np.clip(coefficient[0]+coefficient[1]*logits, -20, 20)))
        elo_margins = np.asarray([sigma*NormalDist().inv_cdf(float(p)) for p in calibrated])
        difference = elo_margins-margins
        denominator = float(difference@difference)
        blend = float(np.clip(np.dot(difference, actual[:, 0]-actual[:, 1]-margins)/denominator, 0, 1)) if denominator > 1e-12 else 0.
        predicted = np.stack([reconcile_scores(t, (1-blend)*m+blend*e)
                              for t, m, e in zip(totals, margins, elo_margins)])
        residuals = actual-predicted
        covariance = np.cov(residuals, rowvar=False, ddof=1)
        variance_margin = float(covariance[0, 0]+covariance[1, 1]-2*covariance[0, 1])
        variance_total = float(covariance.sum())
        if not np.isfinite(covariance).all() or min(variance_margin, variance_total) <= 0:
            raise ELOError("ELO calibration cannot support a finite spread/total distribution.")
        return {"logistic": coefficient, "blend": blend, "elo_margin_scale": sigma,
                "bias": residuals.mean(axis=0), "covariance": covariance,
                "margin_sd": math.sqrt(variance_margin), "total_sd": math.sqrt(variance_total),
                "margin_mae": float(np.abs(residuals[:, 0]-residuals[:, 1]).mean()),
                "total_mae": float(np.abs(residuals.sum(axis=1)).mean())}

    def predict(self, observations, home_team, away_team, as_of, neutral=False):
        history = self._history(observations, as_of)
        errors = []
        grouping = [history.season, history.week] if self.config.sport in {"NFL", "NCAAF"} else history.event_date
        # Only the latest eligible calibration pairs are needed. Evaluate folds
        # newest-first, while each fit still sees strictly earlier information.
        for _, held in reversed(list(history.groupby(grouping, sort=True))):
            cutoff = held.event_date.min()
            training = history[history.available_at.lt(cutoff) & history.event_date.lt(cutoff)]
            if training.game_id.nunique() < self.config.minimum_training_games:
                continue
            fitted = self._fit(training, cutoff)
            for game_id, game in held.groupby("game_id", sort=True):
                h, a = game[game.is_home.eq(1)].iloc[0], game[game.is_home.eq(0)].iloc[0]
                try:
                    raw = self._raw(training, fitted, h.team, a.team, cutoff, bool(h.neutral))
                except ELOError:
                    continue
                errors.append({"game_id": game_id, "date": h.event_date.isoformat(),
                               "home_score": float(h.points), "away_score": float(a.points),
                               **{k: raw[k] for k in ("total", "power_margin", "od_margin", "elo_probability")}})
            if len(errors) >= self.config.maximum_calibration_games:
                break
        errors = sorted(errors, key=lambda row: (row["date"], row["game_id"]))[-self.config.maximum_calibration_games:]
        if len(errors) < self.config.minimum_calibration_games:
            raise ELOError(f"ELO requires {self.config.minimum_calibration_games} chronological calibration games; found {len(errors)}.")
        calibrated = self._calibrate(errors)
        raw = self._raw(history, self._fit(history, as_of), home_team, away_team, as_of, neutral)
        p = raw["elo_probability"]
        logit = calibrated["logistic"][0]+calibrated["logistic"][1]*math.log(p/(1-p))
        p = 1/(1+math.exp(-max(-20., min(20., logit))))
        elo_margin = calibrated["elo_margin_scale"]*NormalDist().inv_cdf(p)
        margin = (1-calibrated["blend"])*raw["power_margin"]+calibrated["blend"]*elo_margin
        means = reconcile_scores(raw["total"], margin)
        means = np.maximum(0., means+(calibrated["bias"].mean() if neutral else calibrated["bias"]))
        diagnostics = {"method": "Sequential Elo / ridge power margin / positive O-D scoring / fitted normal reconciliation",
                       "forecast_method": "ELO", "sport": self.config.sport, "as_of": pd.Timestamp(as_of).isoformat(),
                       "training_games": int(history.game_id.nunique()), "last_game_date": history.event_date.max().date().isoformat(),
                       "validation_games": len(errors), "validation_predictions": errors, "teams": raw["teams"],
                       "raw_elo_probability": raw["elo_probability"], "calibrated_elo_probability": p,
                       "elo_margin": elo_margin, "power_margin": raw["power_margin"], "od_margin": raw["od_margin"],
                       "od_total": raw["total"], "elo_blend_weight": calibrated["blend"],
                       "elo_logistic_coefficients": calibrated["logistic"].tolist(),
                       "elo_margin_scale": calibrated["elo_margin_scale"], "score_bias": calibrated["bias"].tolist(),
                       "score_covariance": calibrated["covariance"].tolist(), "margin_mae": calibrated["margin_mae"],
                       "total_mae": calibrated["total_mae"],
                       "limitations": "Normal research baseline. Hyperparameters are initial settings, not tuned production values. Calibration cohort fits are not untouched-future validation. No roster, weather, state-space, MOV, drive/possession or overtime simulator."}
        forecast_type = NBAELOForecast if self.config.sport == "NBA" else ELOForecast
        return forecast_type(home_team, away_team, float(means[0]), float(means[1]),
                             calibrated["margin_sd"], calibrated["total_sd"], diagnostics)


def main():
    import argparse
    import json
    from datetime import datetime, timezone
    from sports_analytics.nfl_model_data import NflModelData, atomic_text
    from sports_analytics.nba_model_data import NbaModelData
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=["NFL", "NBA"], default="NFL")
    parser.add_argument("--seasons", nargs="+", type=int)
    parser.add_argument("--home", required=True)
    parser.add_argument("--away", required=True)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--refresh", action="store_true", help="Refresh public historical sources, never odds")
    args = parser.parse_args()
    model = ELOModel(ELOConfig.nba() if args.sport == "NBA" else ELOConfig())
    cutoff = pd.Timestamp(args.as_of)
    if cutoff.tzinfo is None:
        parser.error("--as-of must include its timezone")
    season = model._season(cutoff.tz_convert("UTC"))
    data = NbaModelData() if args.sport == "NBA" else NflModelData()
    observations, notices = data.prepare(args.seasons or [season-1, season], refresh=args.refresh)
    forecast = model.predict(observations, args.home, args.away, args.as_of)
    forecast.diagnostics["source_notices"] = notices
    destination = data.directory/"forecasts"/f"elo_{args.home.lower()}_{args.away.lower()}.json"
    atomic_text(destination, json.dumps(asdict(forecast), indent=2))
    print(f"{args.sport} ELO: {args.away} {forecast.away_mean:.2f}, {args.home} {forecast.home_mean:.2f}; {destination}")


if __name__ == "__main__":
    main()
