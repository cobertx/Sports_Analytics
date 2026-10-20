"""NBA adjusted points-per-possession baseline with chronological calibration.

Follows sections 6.2/6.3 and 7 of the EPA research note: offense PPP plus
opposing defense PPP minus league PPP, shared pace, joint score residuals.
Box-score possessions are estimates, not event-level EPV or exact PBP counts.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Models.Game_Outcome.PPP.PPP import MarketProbability, PPPError, PPPForecast


class NBAPPPForecast(PPPForecast):
    def spread(self, team, handicap):
        settled = super().spread(team, handicap)
        # Full-game NBA scores cannot tie after overtime. Remove the normal
        # zero-margin bin before settling every handicap, including pick'em.
        zero = self._settlement(self.margin_mean, self.margin_sd, 0).push
        denominator = 1 - zero
        if denominator <= 0:
            raise PPPError("NBA margin distribution has no non-tied outcomes.")
        handicap = float(handicap)
        return MarketProbability(max(0., settled.win - (zero if handicap > 0 else 0)) / denominator,
                                 max(0., settled.loss - (zero if handicap < 0 else 0)) / denominator,
                                 max(0., settled.push - (zero if handicap == 0 else 0)) / denominator)


@dataclass(frozen=True)
class NBAPPPConfig:
    half_life_days: float = 90
    shrinkage_games: float = 10
    minimum_training_games: int = 100
    minimum_team_games: int = 10
    minimum_calibration_games: int = 100
    maximum_calibration_games: int = 300

    def __post_init__(self):
        if (not math.isfinite(self.half_life_days) or self.half_life_days <= 0 or
                not math.isfinite(self.shrinkage_games) or self.shrinkage_games < 0 or
                min(self.minimum_training_games, self.minimum_team_games) < 1 or
                self.minimum_calibration_games < 2 or self.maximum_calibration_games < self.minimum_calibration_games):
            raise PPPError("NBA PPP requires positive decay and enough training and calibration games.")


def matchup_ppp(offense, opposing_defense, league):
    """Research note section 6.2 additive NBA baseline, in points/possession."""
    result = offense + opposing_defense - league
    if not math.isfinite(result) or result <= 0:
        raise PPPError("NBA matchup efficiency must be positive and finite.")
    return result


class NBAPPPModel:
    def __init__(self, config=None):
        self.config = config or NBAPPPConfig()

    def _history(self, frame, as_of):
        required = {"game_id", "team", "opponent", "event_date", "available_at", "points", "points_allowed", "possessions", "is_home"}
        if not required <= set(frame):
            raise PPPError("Prepare NBA team-game observations before fitting NBA PPP.")
        result = frame.copy()
        for column in ("event_date", "available_at"):
            result[column] = pd.to_datetime(result[column], utc=True, errors="raise")
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            raise PPPError("NBA forecast cutoff must include its timezone.")
        cutoff = cutoff.tz_convert("UTC")
        result = result[result.available_at.lt(cutoff) & result.event_date.lt(cutoff.normalize())].copy()
        numeric = result[["points", "points_allowed", "possessions", "is_home"]].to_numpy(float)
        if (result.empty or result.duplicated(["game_id", "team"]).any() or not np.isfinite(numeric).all() or
                (numeric[:, :2] < 0).any() or (numeric[:, 2] <= 0).any() or
                not result.groupby("game_id").size().eq(2).all() or not result.is_home.isin([0, 1]).all()):
            raise PPPError("NBA observations must contain complete, unique, finite pairs of team box scores.")
        paired = result.merge(result, on="game_id", suffixes=("", "_other"))
        paired = paired[paired.team.ne(paired.team_other)]
        if (len(paired) != len(result) or not paired.team.eq(paired.opponent_other).all() or
                not paired.opponent.eq(paired.team_other).all() or
                not paired.points.eq(paired.points_allowed_other).all() or
                not paired.is_home.ne(paired.is_home_other).all() or
                not paired.event_date.eq(paired.event_date_other).all() or
                not np.allclose(paired.possessions, paired.possessions_other)):
            raise PPPError("NBA team rows disagree on opponent, score, date, venue or possessions.")
        return result.sort_values(["event_date", "game_id", "is_home"])

    def _ratings(self, history, as_of):
        if history.game_id.nunique() < self.config.minimum_training_games:
            raise PPPError("Not enough completed NBA games for PPP ratings.")
        weights = np.exp2(-(pd.Timestamp(as_of) - history.event_date).dt.total_seconds() / 86400 / self.config.half_life_days)
        league = float(np.dot(weights, history.points) / np.dot(weights, history.possessions))
        pace = float(np.average(history.possessions, weights=weights))
        result = {"league": league, "pace": pace, "teams": {}}
        prior = self.config.shrinkage_games
        # Scale weights to equivalent recent games to keep shrinkage stable
        # during the offseason, when every observation is older.
        weights = weights / weights.max()
        for team, group in history.groupby("team"):
            w = weights.loc[group.index].to_numpy()
            exposure = float(np.dot(w, group.possessions))
            result["teams"][team] = {
                "offense": (float(np.dot(w, group.points)) + prior * pace * league) / (exposure + prior * pace),
                "defense": (float(np.dot(w, group.points_allowed)) + prior * pace * league) / (exposure + prior * pace),
                "pace": (exposure + prior * pace) / (w.sum() + prior),
                "games": len(group),
            }
        return result

    def _means(self, ratings, home, away):
        if home == away or any(team not in ratings["teams"] or ratings["teams"][team]["games"] < self.config.minimum_team_games
                               for team in (home, away)):
            raise PPPError("Both NBA teams need enough earlier appearances for a forecast.")
        h, a = (ratings["teams"][team] for team in (home, away))
        pace = (h["pace"] + a["pace"]) / 2
        efficiencies = [matchup_ppp(h["offense"], a["defense"], ratings["league"]),
                        matchup_ppp(a["offense"], h["defense"], ratings["league"])]
        return np.asarray(efficiencies) * pace, {"possessions": pace, "home_ppp": efficiencies[0], "away_ppp": efficiencies[1]}

    def predict(self, frame, home_team, away_team, as_of, neutral=False):
        history = self._history(frame, as_of)
        errors = []
        # Refit by held-out date. Actual target pace/scores never enter a mean.
        dates = history.event_date.drop_duplicates().sort_values()
        for held_date in dates:
            held = history[history.event_date.eq(held_date)]
            training = history[history.available_at.lt(held_date) & history.event_date.lt(held_date)]
            if training.game_id.nunique() < self.config.minimum_training_games:
                continue
            ratings = self._ratings(training, held_date)
            for game_id, game in held.groupby("game_id"):
                h, a = game[game.is_home.eq(1)].iloc[0], game[game.is_home.eq(0)].iloc[0]
                try:
                    means, _ = self._means(ratings, h.team, a.team)
                except PPPError:
                    continue
                errors.append({"game_id": game_id, "date": held_date.isoformat(),
                               "home_error": float(h.points - means[0]), "away_error": float(a.points - means[1])})
        errors = errors[-self.config.maximum_calibration_games:]
        if len(errors) < self.config.minimum_calibration_games:
            raise PPPError(f"NBA PPP needs {self.config.minimum_calibration_games} chronological validation games; found {len(errors)}.")
        residuals = np.asarray([[row["home_error"], row["away_error"]] for row in errors])
        covariance = np.cov(residuals, rowvar=False, ddof=1)
        margin_var = float(covariance[0, 0] + covariance[1, 1] - 2 * covariance[0, 1])
        total_var = float(covariance.sum())
        if min(margin_var, total_var) <= 0:
            raise PPPError("NBA held-out residuals do not support a nonzero score distribution.")
        means, details = self._means(self._ratings(history, as_of), home_team, away_team)
        bias = residuals.mean(axis=0)
        # Home/away empirical bias is learned from prior games only. Neutral
        # venues retain common bias without the learned home differential.
        means += bias.mean() if neutral else bias
        if (means < 0).any():
            raise PPPError("NBA calibration produced invalid score means.")
        diagnostics = {"method": "NBA additive adjusted PPP / shared possessions / normal residual baseline",
                       "as_of": pd.Timestamp(as_of).isoformat(), "last_game_date": history.event_date.max().date().isoformat(),
                       "training_games": history.game_id.nunique(), "validation_games": len(errors),
                       "score_covariance": covariance.tolist(), "validation_residuals": errors, "teams": details,
                       "limitations": "Estimated box-score possessions; full-game overtime included. No EPV, injury, lineup or explicit overtime simulator."}
        return NBAPPPForecast(home_team, away_team, float(means[0]), float(means[1]), math.sqrt(margin_var), math.sqrt(total_var), diagnostics)


def main():
    import argparse
    from dataclasses import asdict
    from datetime import datetime, timezone
    import json
    from sports_analytics.nba_model_data import NbaModelData
    from sports_analytics.nfl_model_data import atomic_text
    now = datetime.now(timezone.utc)
    season = now.year - (now.month < 10)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", nargs="+", type=int, default=[season - 1, season])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--home", help="NBA abbreviation, e.g. GSW")
    parser.add_argument("--away", help="NBA abbreviation, e.g. LAL")
    parser.add_argument("--as-of", default=now.isoformat())
    args = parser.parse_args()
    data = NbaModelData()
    frame, notices = data.prepare(args.seasons, refresh=args.refresh)
    print(f"Prepared {frame.game_id.nunique()} NBA games in {data.directory}")
    for notice in notices:
        print(notice)
    if args.home and args.away:
        forecast = NBAPPPModel().predict(frame, args.home, args.away, args.as_of)
        path = data.directory / "forecasts" / f"ppp_{args.home.lower()}_{args.away.lower()}.json"
        atomic_text(path, json.dumps(asdict(forecast), indent=2))
        print(f"Projected points: {args.away} {forecast.away_mean:.2f}, {args.home} {forecast.home_mean:.2f}; saved {path}")


if __name__ == "__main__":
    main()
