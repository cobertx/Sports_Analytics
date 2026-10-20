"""Offline CI checks that do not depend on the local-only test folders.

Run from any directory: python scripts/check_project.py
No source data, credentials, fitted caches or external requests are required.
"""

import argparse
import importlib
from pathlib import Path
import subprocess
import sys
import tomllib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_layout():
    # Windows can display lowercase models while Git still records Models.
    # Check the next commit's index too; a filesystem-only check misses this.
    if (ROOT / ".git").exists():
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        tracked = [name.decode("utf-8") for name in result.stdout.split(b"\0") if name]
        wrong = [name for name in tracked if name.startswith("Models/")]
        require(not wrong,
                "Git still tracks Models/ with uppercase M. Record the rename with "
                "git mv Models __models_case_fix, then git mv __models_case_fix models, "
                "and include the staged renames in your commit.")
        require(any(name.startswith("models/") for name in tracked), "Git must track the lowercase models/ package.")
    names = {path.name for path in ROOT.iterdir()}
    require("models" in names and "Models" not in names, "Model directory must be named exactly models (lowercase).")
    require("requirements" in names, "Requirements belong under requirements/.")
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files = config["tool"]["setuptools"]["dynamic"]["dependencies"]["file"]
    require(files == ["requirements/requirements.txt"], "Package dependency path is outdated.")
    for name in ("requirements.txt", "requirements-docs.txt", "requirements-logos.txt"):
        require((ROOT / "requirements" / name).is_file(), f"Missing requirements/{name}")
    require("models*" in config["tool"]["setuptools"]["packages"]["find"]["include"],
            "Package discovery must include lowercase models.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout-only", action="store_true", help="Check Git paths and project layout before installing dependencies")
    args = parser.parse_args()
    check_layout()
    if args.layout_only:
        print("Passed: Git paths, lowercase models/ and requirements/ layout.")
        return
    modules = [
        "models.Game_Outcome.PPP.PPP", "models.Game_Outcome.PPP.NBA_PPP",
        "models.Game_Outcome.EPA.EPA", "models.Game_Outcome.EPA.NBA_EPA",
        "models.Game_Outcome.EPA.nba_epa_data", "models.Game_Outcome.EPA.nba_epa_training",
        "models.Game_Outcome.ELO.ELO", "models.Player_Stats.Weighted_Historical_Performance",
        "models.Player_Stats.Weighted_Historical_Performance.prediction_service",
        "sports_analytics.dashboard", "sports_analytics.nfl_markets", "sports_analytics.nba_markets",
        "sports_analytics.ncaaf_markets", "sports_analytics.whp_service",
    ]
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("CI must not make external requests")) as requests:
        for name in modules:
            importlib.import_module(name)
        from sports_analytics.dashboard import create_app
        from sports_analytics.market_view import NBA_LOGO_DIRECTORY, NFL_LOGO_DIRECTORY, logo_badge
        from models.Player_Stats.Weighted_Historical_Performance import WeightedHistoricalPerformance

        value = WeightedHistoricalPerformance(half_life=3, window=10).predict(
            [17, 22, 19, 25, 21, 28, 23, 31, 26, 29])["weighted_mean"]
        require(abs(value - 26.108708) < .00001, "WHP weighted forecast smoke check failed.")
        app = create_app()
        with app.server.test_client() as client:
            for route in ("/", "/_dash-layout", "/_dash-dependencies"):
                require(client.get(route).status_code == 200, f"Dashboard route failed: {route}")
            for sport, directory, count in (("NBA", NBA_LOGO_DIRECTORY, 30), ("NFL", NFL_LOGO_DIRECTORY, 32)):
                require(directory.parent.parent == ROOT / "images", f"{sport} logo path must use images/.")
                paths = list(directory.glob("*.png"))
                require(len(paths) >= count, f"Expected at least {count} {sport} team logos.")
                for path in paths:
                    badge = logo_badge(path.stem, "TEAM", sport)
                    response = client.get(badge.children.src)
                    require(response.status_code == 200 and response.data.startswith(b"\x89PNG\r\n\x1a\n"),
                            f"Logo route failed: {sport} {path.name}")
        requests.assert_not_called()
    print(f"Passed: project layout, {len(modules)} imports, WHP math, Dash routes and NBA/NFL logos. No external requests.")


if __name__ == "__main__":
    main()
