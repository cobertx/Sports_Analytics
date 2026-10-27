"""Offline CI checks that do not depend on the local-only test folders.

Run from any directory: python scripts/check_project.py
No source data, credentials, fitted caches or external requests are required.
"""

import argparse
import importlib
import json
import math
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
        "models.Player_Stats.Usage_Opportunity", "models.Player_Stats.Usage_Opportunity.backtesting",
        "sports_analytics.usage_opportunity_service",
        "models.Player_Stats.Usage_Opportunity.ncaaf_model",
        "models.Player_Stats.Opponent_Adjusted_Matchup", "models.Player_Stats.Opponent_Adjusted_Matchup.backtesting",
        "sports_analytics.opponent_adjusted_service",
        "models.Player_Stats.Pace_Game_Environment", "models.Player_Stats.Pace_Game_Environment.backtesting",
        "sports_analytics.pace_game_environment_service",
        "sports_analytics.parlay", "sports_analytics.parlay_view",
        "sports_analytics.ncaaf_model_data", "sports_analytics.ncaaf_forecast_service",
        "models.Game_Outcome.PPP.NCAAF_PPP", "models.Game_Outcome.EPA.NCAAF_EPA", "models.Game_Outcome.ELO.NCAAF_ELO",
        "sports_analytics.dashboard", "sports_analytics.nfl_markets", "sports_analytics.nba_markets",
        "sports_analytics.filtered_results", "sports_analytics.filtered_results_view",
        "sports_analytics.nba_espn",
        "sports_analytics.market_summary", "sports_analytics.market_summary_view", "sports_analytics.market_summary_callbacks",
        "sports_analytics.market_structure", "sports_analytics.market_structure_link", "sports_analytics.market_structure_view",
        "sports_analytics.backtesting_view",
        "sports_analytics.backtesting_summary",
        "sports_analytics.performance_summary",
        "sports_analytics.information", "sports_analytics.information_view",
        "sports_analytics.pnl", "sports_analytics.pnl_view",
        "sports_analytics.cache_refresh",
        "sports_analytics.page_cache",
        "sports_analytics.historical_view",
        "sports_analytics.backtesting_service", "sports_analytics.backtesting_results",
        "sports_analytics.backtest_process", "sports_analytics.backtest_worker",
        "sports_analytics.backtesting_monitor",
        "sports_analytics.ncaaf_markets", "sports_analytics.whp_service",
    ]
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("CI must not make external requests")) as requests:
        for name in modules:
            importlib.import_module(name)
        from sports_analytics.dashboard import create_app
        from sports_analytics.market_view import NBA_LOGO_DIRECTORY, NFL_LOGO_DIRECTORY, NCAAF_LOGO_DIRECTORY, logo_badge
        from models.Player_Stats.Weighted_Historical_Performance import WeightedHistoricalPerformance
        from models.Player_Stats.Usage_Opportunity import UsageOpportunityForecast
        from models.Player_Stats.Usage_Opportunity.efficiency_model import beta_posterior
        from models.Player_Stats.Opponent_Adjusted_Matchup.schedule_strength import shrunk_factor
        from models.Player_Stats.Pace_Game_Environment import PaceForecast
        from sports_analytics.parlay import decimal_odds,american_from_decimal

        value = WeightedHistoricalPerformance(half_life=3, window=10).predict(
            [17, 22, 19, 25, 21, 28, 23, 31, 26, 29])["weighted_mean"]
        require(abs(value - 26.108708) < .00001, "WHP weighted forecast smoke check failed.")
        require(abs(beta_posterior([18], [20], prior=(14, 6))["mean"] - .8) < 1e-12,
                "Usage & Opportunity beta-binomial example failed.")
        estimate = UsageOpportunityForecast.from_samples([0, 1, 2, 2]).market(1)
        require((estimate.win, estimate.loss, estimate.push) == (.5, .25, .25),
                "Usage & Opportunity integer settlement failed.")
        require(abs(shrunk_factor([120]*4, [100]*4, prior_strength=6)["mean"]-1.08)<1e-12,
                "Opponent-adjusted small-sample shrinkage example failed.")
        pace = PaceForecast.from_samples([0,1,2,2]).market(1)
        require((pace.win,pace.loss,pace.push)==(.5,.25,.25),"PGE push-aware empirical pricing failed.")
        require(abs(american_from_decimal(decimal_odds(-110)**2)-264.4628099173554)<1e-8,
                'Parlay decimal-odds multiplication failed.')
        app = create_app()
        from sports_analytics.historical_view import historical_route,statistics_kind,navigation as historical_navigation
        require(historical_route('#historical-player')=='player' and historical_route('#historical-team')=='team' and
                historical_route('#historical-player-extra') is None,'Historical page routes failed.')
        require(len(historical_navigation('NBA','team'))==2 and len(historical_navigation('NFL','player'))==2 and
                len(historical_navigation('NCAAF','player'))==1 and statistics_kind('NCAAF','player')=='team',
                'Historical team/player page availability failed.')
        from sports_analytics.backtesting_view import available_models,model_config,backtesting_route,find_model
        from sports_analytics.backtesting_service import (diagnostics,default_evaluation_games,season_phases,estimation_history,
            default_lookback,parse_lookback,PREDICTIVE_Z)
        from sports_analytics.backtesting_results import figure,error_distribution,error_summary,per_game_axis
        import pandas as pd
        import numpy as np
        require([default_evaluation_games(s) for s in ['NBA','NFL','NCAAF']]==[1000,250,1000] and
                [default_evaluation_games(s,'single') for s in ['NBA','NFL','NCAAF']]==[82,34,24],
                'Backtesting sport/scope sample defaults failed.')
        phases=season_phases(pd.DataFrame({'game_id':['0022500001','0042500001','0052500001','unknown']}),'NBA')
        require(phases.season_type.tolist()==['regular','postseason','postseason','unknown'],
                'Backtesting postseason/unknown-phase classification failed.')
        history=pd.DataFrame({'game_id':['a','b','c'],'team':['A']*3,
            'event_date':pd.to_datetime(['2025-01-01','2025-01-02','2025-01-03'],utc=True),
            'available_at':pd.to_datetime(['2025-01-02','2025-01-03','2025-01-04'],utc=True)})
        require(estimation_history(history,pd.Timestamp('2025-01-04',tz='UTC'),1).game_id.tolist()==['b'],
                'Backtesting lookback/strict availability boundary failed.')
        errors=diagnostics([dict(actual=a,predicted=p,lower=0,upper=10,probability=None,event_actual=None)
                            for a,p in [(2,1),(4,5),(6,8)]],1)
        require(abs(errors['mae']-4/3)<1e-12 and abs(errors['error_variance']-7/3)<1e-12,
                'Backtesting MAE/sample variance smoke check failed.')
        require(abs(PREDICTIVE_Z-1.281551565545)<1e-10 and errors['coverage_80']==1,
                'Backtesting central 80% interval check failed.')
        calibration=diagnostics([dict(actual=0,predicted=0,probability=p,event_actual=y)
                                 for p,y in [(0,0),(.021,1),(.041,0),(1,1)]],1)
        require(len(calibration['reliability'])==4 and sum(b['n'] for b in calibration['reliability'])==4,
                'Backtesting 2% calibration bins / endpoints check failed.')
        distribution=figure('Errors');error_distribution(distribution,[-2,0,1,1,3])
        require(len(distribution.data[0].x)>=20 and sum(distribution.data[0].y)==5 and
                len(distribution.data[1].x)==384 and np.isfinite(distribution.data[1].y).all() and
                (np.asarray(distribution.data[1].y)>=0).all(),'Backtesting histogram/smoothed distribution failed.')
        summary=error_summary([-4,-1,0,2,8])
        require(summary['samples']==5 and summary['mean']==1 and summary['median']==0 and
                abs(summary['sample_sd']-math.sqrt(20))<1e-12 and summary['mae']==3 and
                abs(summary['rmse']-math.sqrt(17))<1e-12,
                'Forecast error statistics must use raw signed errors and sample SD.')
        require(error_summary([2])['sample_sd'] is None and error_summary([2]*4)['sample_sd']==0 and
                len(distribution.layout.annotations)==6 and distribution.layout.meta['error_distribution']['samples']==5,
                'Forecast error chart statistics / small-sample handling failed.')
        same_day=pd.DataFrame({'date':['2025-01-01T00:00:00Z']*3,'label':['A','B','C'],'game_id':['a','b','c']})
        positions,_=per_game_axis(figure('Games'),same_day)
        require(len(set(positions))==3,'Backtesting must retain distinct positions for same-date games.')
        for sport in ['NBA','NFL','NCAAF']:
            for model in available_models(sport):
                require(find_model(sport,backtesting_route(model.href))==model and bool(model_config(model,sport)),
                        f'Backtesting model route/settings failed: {sport} {model.key}')
                label,_=default_lookback(sport,model.group,model.key)
                require(label.endswith('(Model Default)') and parse_lookback(label,sport,model.group,model.key) is None,
                        f'Backtesting model-default lookback failed: {sport} {model.key}')
        college_methods=next(m['callback'].__wrapped__ for m in app.callback_map.values()
                             if m.get('callback') and m['callback'].__wrapped__.__name__=='forecast_methods')
        options,method=college_methods('#ncaaf-game-123','EPA')
        require(method=='EPA' and {r['value'] for r in options}=={'PPP','EPA','ELO'} and
                not any(r.get('disabled') for r in options),'College game-outcome methods must be enabled.')
        player_methods=next(m['callback'].__wrapped__ for m in app.callback_map.values()
                            if m.get('callback') and m['callback'].__wrapped__.__name__=='player_forecast_methods')
        for selected in ['WHP','UO','OAM','PGE']:
            options,method=player_methods('#ncaaf-game-123',selected)
            require(method==selected and {r['value'] for r in options}=={'WHP','UO','OAM','PGE'} and
                    not any(r.get('disabled') for r in options),'College player methods must be selectable.')
        with app.server.test_client() as client:
            for route in ("/", "/_dash-layout", "/_dash-dependencies"):
                require(client.get(route).status_code == 200, f"Dashboard route failed: {route}")
            layout_json=json.dumps(client.get('/_dash-layout').get_json())
            require(all(f'"id": "{name}"' in layout_json for name in
                ['backtest-lookback-note','backtest-available','backtest-progress','backtest-progress-label','backtest-poll']),
                'Backtesting defaults/availability/progress components must exist in the stable layout.')
            require(all(f'"id": "{name}"' in layout_json for name in
                ['backtest-monitor','backtest-monitor-toggle','backtest-monitor-body','backtest-monitor-timer',
                 'backtest-monitor-jobs','backtest-monitor-collapsed']),
                'Global backtest monitor must remain mounted across pages.')
            require(all(f'"id": "{name}"' in layout_json for name in
                ['historical-navigation','historical-page-link','market-page-link','backtesting-page-link']),
                'Sport sidebar page links must exist in the stable layout.')
            for sport, directory, count in (("NBA", NBA_LOGO_DIRECTORY, 30), ("NFL", NFL_LOGO_DIRECTORY, 32),
                                            ("NCAAF", NCAAF_LOGO_DIRECTORY, 100)):
                require(directory.parent.parent == ROOT / "images", f"{sport} logo path must use images/.")
                paths = list(directory.glob("*.png"))
                require(len(paths) >= count, f"Expected at least {count} {sport} team logos.")
                for path in paths:
                    badge = logo_badge(path.stem, "TEAM", sport)
                    response = client.get(badge.children.src)
                    require(response.status_code == 200 and response.data.startswith(b"\x89PNG\r\n\x1a\n"),
                            f"Logo route failed: {sport} {path.name}")
        requests.assert_not_called()
    print(f"Passed: project layout, {len(modules)} imports, WHP math, Dash routes and NBA/NFL/NCAAF logos. No external requests.")


if __name__ == "__main__":
    main()
