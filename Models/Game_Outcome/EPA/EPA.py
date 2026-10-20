"""NFL EPA forecasting from the supplied EPA sports-forecasting research note.

Sections 2-3: signed provider EPA, play-type-specific opponent-adjusted ridge
ratings, centered team effects, exponential recency, predicted pass/rush mix.
Section 4.2: fit offensive points = drives * b + plays * EPA advantage * kappa;
non-offensive scoring is added separately. Sections 7-8: normal total/margin
distributions with covariance estimated from chronological held-out games.

This implements the paper's first-stage scoring bridge and normal baseline,
not its advanced possession simulator or NBA/college-specific models. Shared
cutoff, pace, validation and settlement utilities come from PPP; EPA has its own
signed efficiency ratings, trained scoring bridge and out-of-sample residuals.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math

import numpy as np
import pandas as pd

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Models.Game_Outcome.PPP.PPP import PPPConfig, PPPError, PPPForecast, PPPModel


class EPAError(PPPError):
    """EPA observations or calibration are not sufficient to estimate a forecast."""


def expected_points_added(before, after, reward=0.0, possession_changed=False):
    """Section 2.2 signed reward identity; actual fits use provider-defined EPA."""
    if not all(math.isfinite(value) for value in (before, after, reward)):
        raise EPAError("Expected-points inputs must be finite.")
    return reward + (-after if possession_changed else after) - before


def scoring_bridge(drives, plays_per_drive, epa_advantage, baseline_points_per_drive, conversion):
    values = (drives, plays_per_drive, epa_advantage, baseline_points_per_drive, conversion)
    if not all(math.isfinite(value) for value in values) or min(drives, plays_per_drive, baseline_points_per_drive, conversion) < 0:
        raise EPAError("Scoring-bridge inputs are invalid.")
    return drives * baseline_points_per_drive + conversion * drives * plays_per_drive * epa_advantage


@dataclass(frozen=True)
class EPAConfig(PPPConfig):
    ridge_penalty: float = 200
    bridge_penalty: float = 25

    def __post_init__(self):
        try:
            super().__post_init__()
        except PPPError as exc:
            raise EPAError(str(exc).replace("PPP", "EPA")) from None
        if not math.isfinite(self.bridge_penalty) or self.bridge_penalty <= 0:
            raise EPAError("EPA scoring-bridge regularization must be positive.")


@dataclass
class EPAForecast(PPPForecast):
    """The common spread/total/push interface, parameterized by an EPA forecast."""


class EPAModel(PPPModel):
    def __init__(self, config=None):
        super().__init__(config or EPAConfig())

    def _history(self, frame, as_of):
        required = {f"{side}_{kind}_epa_{stat}" for side in ("off", "def") for kind in ("pass", "rush") for stat in ("plays", "sum")}
        required |= {"off_epa_plays", "def_epa_plays"}
        if not required <= set(frame):
            raise EPAError("Prepare the NFL modeling data with signed play-by-play EPA before fitting EPA.")
        frame = super()._history(frame, as_of)
        if (not np.isfinite(frame[sorted(required)].to_numpy(float)).all() or
                frame["off_epa_plays"].ne(frame["offensive_plays"]).any() or
                frame["def_epa_plays"].ne(frame["defensive_plays"]).any() or
                any((frame[f"{side}_pass_epa_plays"] + frame[f"{side}_rush_epa_plays"]).ne(frame[f"{side}_epa_plays"]).any()
                    for side in ("off", "def")) or
                (frame[[f"{side}_{kind}_epa_plays" for side in ("off", "def") for kind in ("pass", "rush")]] <= 0).any().any()):
            raise EPAError("EPA requires complete, finite passing/rushing EPA coverage for both teams; missing EPA is not zero.")
        return frame

    def _fit(self, frame):
        if frame["game_id"].nunique() < self.config.minimum_training_games:
            raise EPAError("Not enough completed games to fit EPA ratings.")
        teams = sorted(set(frame["team"]) | set(frame["opponent"]))
        indices = {team: index for index, team in enumerate(teams)}
        n = len(teams)
        x = np.zeros((len(frame), 2 + 2 * n))
        x[:, 0] = 1
        x[:, 1] = (2 * frame["is_home"].to_numpy() - 1) * (1 - frame["neutral"].to_numpy()) / 2
        x[np.arange(len(frame)), 2 + frame["team"].map(indices).to_numpy()] = 1
        x[np.arange(len(frame)), 2 + n + frame["opponent"].map(indices).to_numpy()] = 1
        penalty = np.full(x.shape[1], self.config.ridge_penalty)
        penalty[0], penalty[1] = 0, self.config.ridge_penalty / 10
        ratings = {"teams": indices, "coefficients": {}}
        advantages = {}
        for kind in ("pass", "rush"):
            count = frame[f"off_{kind}_epa_plays"].to_numpy(float)
            mean = frame[f"off_{kind}_epa_sum"].to_numpy(float) / count
            weight = frame["weight"].to_numpy(float) * count
            beta = np.linalg.solve(x.T @ (weight[:, None] * x) + np.diag(penalty), x.T @ (weight * mean))
            for start in (2, 2 + n):
                center = beta[start:start + n].mean()
                beta[start:start + n] -= center
                beta[0] += center
            ratings["coefficients"][kind] = beta
            advantages[kind] = x @ beta - beta[0]
        share = frame["off_pass_epa_plays"].to_numpy(float) / frame["off_epa_plays"].to_numpy(float)
        # This is a training regression on observed game exposures. The target
        # game's mix and exposures are forecast only from its earlier history.
        delta = share * advantages["pass"] + (1 - share) * advantages["rush"]
        bridge_x = np.column_stack([frame["offensive_drives"], frame["offensive_plays"] * delta])
        y, weight = frame["offensive_points"].to_numpy(float), frame["weight"].to_numpy(float)
        bridge_penalty = np.array([0.0, self.config.bridge_penalty])
        hessian = bridge_x.T @ (weight[:, None] * bridge_x) + np.diag(bridge_penalty)
        target = bridge_x.T @ (weight * y)
        # Two-coefficient nonnegative ridge fit, including boundary solutions.
        solution = np.linalg.solve(hessian, target)
        candidates = [np.array([max(0.0, target[0] / hessian[0, 0]), 0.0]),
                      np.array([0.0, max(0.0, target[1] / hessian[1, 1])]), np.zeros(2)]
        if np.all(solution >= 0):
            candidates.append(solution)
        ratings["bridge"] = min(candidates, key=lambda beta: float(np.dot(weight, (y - bridge_x @ beta) ** 2) + np.dot(bridge_penalty, beta ** 2)))
        return ratings

    def _means(self, frame, ratings, home, away, neutral=False):
        own = {team: frame[frame["team"].eq(team)] for team in (home, away)}
        if any(len(rows) < self.config.minimum_team_games for rows in own.values()):
            raise EPAError(f"Both teams need at least {self.config.minimum_team_games} prior games for an EPA estimate.")
        def average(rows, column):
            return float(np.average(rows[column], weights=rows["weight"]))
        drives = np.mean([average(own[home], "offensive_drives"), average(own[home], "defensive_drives"),
                          average(own[away], "offensive_drives"), average(own[away], "defensive_drives")])
        league_extra = average(frame, "non_offensive_points")
        n, indices = len(ratings["teams"]), ratings["teams"]
        baseline, conversion = map(float, ratings["bridge"])
        means, details = [], {}
        for team, opponent, sign in ((home, away, 1), (away, home, -1)):
            rows, other = own[team], own[opponent]
            length = (average(rows, "offensive_plays") / average(rows, "offensive_drives") +
                      average(other, "defensive_plays") / average(other, "defensive_drives")) / 2
            share = (average(rows, "off_pass_epa_plays") / average(rows, "off_epa_plays") +
                     average(other, "def_pass_epa_plays") / average(other, "def_epa_plays")) / 2
            by_type = {}
            for kind in ("pass", "rush"):
                beta = ratings["coefficients"][kind]
                by_type[kind] = float(beta[2 + indices[team]] + beta[2 + n + indices[opponent]] +
                                      beta[1] * sign * (not neutral) / 2)
            advantage = share * by_type["pass"] + (1 - share) * by_type["rush"]
            additional = (np.dot(rows["weight"], rows["non_offensive_points"]) + 2 * league_extra) / (rows["weight"].sum() + 2)
            offense = max(0.0, scoring_bridge(float(drives), length, advantage, baseline, conversion))
            means.append(float(offense + additional))
            details[team] = {"expected_drives": float(drives), "expected_plays": float(drives * length),
                             "predicted_pass_share": share, "epa_advantage": advantage, "epa_by_type": by_type,
                             "baseline_points_per_drive": baseline, "epa_conversion": conversion,
                             "additional_points": float(additional), "prior_games": len(rows)}
        return means, details

    def predict(self, observations, home_team, away_team, as_of, neutral=False):
        try:
            result = super().predict(observations, home_team, away_team, as_of, neutral)
        except PPPError as exc:
            raise EPAError(str(exc).replace("PPP", "EPA")) from None
        result.diagnostics.update(method="EPA pass/rush opponent-adjusted ridge / fitted scoring bridge / normal baseline (PDF 2-4, 7-8, 10)",
            forecast_method="EPA", limitations="First-stage EPA scoring bridge with normal residuals and approximate integer pushes. No drive-state simulator, injury or weather adjustments.")
        return EPAForecast(**asdict(result))


def main():
    import argparse
    import json
    from sports_analytics.nfl_model_data import NflModelData, atomic_text
    parser = argparse.ArgumentParser(description=__doc__)
    season = datetime.now().year - (datetime.now().month < 3)
    parser.add_argument("--seasons", nargs="+", type=int, default=[season - 1, season])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--home")
    parser.add_argument("--away")
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    data = NflModelData()
    frame, notices = data.prepare(args.seasons, refresh=args.refresh)
    print(f"Prepared {frame.game_id.nunique()} games / {len(frame)} team-game rows in {data.directory}")
    for notice in notices:
        print(notice)
    if args.home and args.away:
        result = EPAModel().predict(frame, args.home, args.away, args.as_of)
        destination = data.directory / "forecasts" / f"epa_{args.home.lower()}_{args.away.lower()}.json"
        atomic_text(destination, json.dumps(asdict(result), indent=2))
        print(f"Projected points: {args.away} {result.away_mean:.2f}, {args.home} {result.home_mean:.2f}; saved {destination}")


if __name__ == "__main__":
    main()
