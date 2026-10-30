# Forecasting models

NBA/NFL dashboard market forecasts now support an evidence-gated injury overlay on the seven original methods. See [historical availability comparisons](../docs/AVAILABILITY_ADJUSTMENTS.md) for sources, playing/nonplaying comparisons, workload changes, limitations and replay rules. The underlying model engines and their baseline backtests remain unchanged. NFL weather integration remains a [design proposal](../docs/INJURY_WEATHER_MODEL_INTEGRATION.md).

NBA, NFL and NCAAF forecasts now emphasize the current season across all seven methods. Existing recency weights are multiplied by `prior_season_weight ** seasons_old`, with default `prior_season_weight = 0.2`: an otherwise equal previous-season observation receives one fifth the weight, and an observation two seasons old receives 0.04. This is a per-observation discount, not a fixed 80/20 split of total season weight. Source season-start years are used where supplied; the date fallback follows the app's October NBA and March football boundaries, keeping January football playoffs in the preceding season.

The discount applies to game ratings, player production, workload draws, conversion/yardage estimates, defensive factors and pace/scenario estimation. WHP also discounts the strength of its disjoint older-season prior. Elo retains 20% of a previous season's rating deviation from 1500. Earlier games still count toward existing sample requirements and chronological uncertainty calibration; insufficient data remains unavailable. Undated low-level WHP `predict()` examples retain ordinary recency weights because they carry no season information.

Change **Prior season weight** (nested under **Baseline** for OAM/PGE) in Strategy & Research to experiment; 1 disables the additional discount. Each historical forecast applies weighting at its own cutoff. New settings and weighting code invalidate forecast caches; archived backtest reports retain their original settings and results, so rerun a simulation to evaluate these defaults. The README descriptions reflect this update; previously generated implementation PDFs require rebuilding to include it.

Active market, summary and filter views check the season-weighting policy before reusing calculated probabilities. Filtered Results shows current-policy reports by default; **All saved runs** preserves earlier reports with an explicit weighting label. Restored parlay selections keep their saved quotes but clear older model estimates; remove and add a selection again to recalculate it. Market-implied probabilities continue to describe quoted prices, and historical hit rates continue to describe observed outcomes.

Model implementations are grouped by prediction target. [Game_Outcome](Game_Outcome/README.md) contains PPP, EPA and ELO strategies for team/game outcomes. [Player_Stats](Player_Stats/README.md) is the sibling package for player-statistic forecasts. Each existing methodology folder includes a detailed README, an implementation-guide PDF and an unchanged copy of its source research PDF. The implementation guides describe the actual code, including lookback windows, minimum sample sizes and new-season carryover; the original research papers remain references.

- [PPP Implementation Guide](Game_Outcome/PPP/PPP_Implementation_Guide.pdf)
- [EPA Implementation Guide](Game_Outcome/EPA/EPA_Implementation_Guide.pdf)
- [ELO Implementation Guide](Game_Outcome/ELO/ELO_Implementation_Guide.pdf)
- [WHP Player Implementation Guide](Player_Stats/Weighted_Historical_Performance/WHP_Implementation_Guide.pdf)
- [UO Player Implementation Guide](Player_Stats/Usage_Opportunity/UO_Implementation_Guide.pdf)
- [OAM Player Implementation Guide](Player_Stats/Opponent_Adjusted_Matchup/OAM_Implementation_Guide.pdf)
- [PGE Player Implementation Guide](Player_Stats/Pace_Game_Environment/PGE_Implementation_Guide.pdf)

Rebuild the manuals from their READMEs with `python -m pip install -r requirements/requirements-docs.txt`, then `python scripts/build_model_docs.py`. Use `--methods ELO` to build just that guide. Copies are also written under `output/pdf`. This documentation build makes no data or odds requests and does not change model calculations.

```text
models/
  Game_Outcome/
    PPP/
      PPP.py                 NFL points-per-play model and shared pricing
      NBA_PPP.py             NBA points-per-possession model
      README.md              Formulas, examples, code/PDF mapping and limitations
      PPP_Implementation_Guide.pdf
      Football_Betting_PPP_Arbitrage_Strategy.pdf
      __init__.py
      __main__.py
    EPA/
      EPA.py                 NFL EPA ratings and fitted scoring bridge
      NBA_EPA.py             NBA event-based EPV, signed residuals and forecast
      nba_epa_training.py    Score reconstruction and state sufficient statistics
      nba_epa_data.py        NBA public event/possession input audits
      prepare_nba_epa.py     NBA input preparation command
      NBA_EPA_SETUP.md       NBA EPV setup, reconciliation and research limitations
      README.md              NFL EPA methodology and NBA implementation status
      EPA_Implementation_Guide.pdf
      EPA_Sports_Forecasting_NFL_NBA_College_Football.pdf
      __init__.py
      __main__.py
    ELO/
      ELO.py                 Sequential Elo, power margins and positive scoring
      README.md              Formulas, lookback, season carry and PDF mapping
      ELO_Implementation_Guide.pdf
      Elo_Power_Rating_Models_NFL_NBA_CFB.pdf
      __init__.py
      __main__.py
    README.md
    __init__.py
  Player_Stats/
    Weighted_Historical_Performance/
      core.py                Weighted player forecasts and predictive calibration
      historical_data.py     NBA/NFL saved sources and scoped college player boxes
      prediction_service.py  Saved market pricing, frozen estimates and audits
      README.md              Complete implementation and new-season behavior
      INPUT_SCHEMA.md        Canonical player-game import fields
      WHP_Implementation_Guide.pdf
      Weighted_Historical_Performance_NBA_NFL_NCAAF.pdf
    README.md
    __init__.py
```

## NFL and NBA ELO / Power Rating

Operational research baseline. [Read the implementation documentation](Game_Outcome/ELO/README.md).
Sequential neutral Elo and ridge point margins are reconciled using fitted
chronological calibration; a separate positive O/D and shared drive/possession
model supplies totals. The NBA and NFL configurations, error samples and
archives are independent. The saved-market dropdown updates ELO probability,
fair odds, Edge, criteria and all four charts. Advanced simulators and college
football's separate hierarchy remain unimplemented.

```powershell
python -m models.Game_Outcome.ELO --sport NFL --seasons 2025 2026 --home BAL --away CIN --as-of 2026-10-20T02:49:50+00:00
python -m models.Game_Outcome.ELO --sport NBA --seasons 2025 2026 --home DET --away BOS --as-of 2026-10-20T03:48:47+00:00
```

## NFL PPP model

Operational. [Read the complete PPP documentation](Game_Outcome/PPP/README.md), covering the PPP PDF sections 1-5: scoring definitions, joint offense/defense log-link ratings, shared drives/plays, non-offensive scoring, chronological calibration, normal spread/total probabilities, pushes and fair prices. The preferred drive-state Monte Carlo is explicitly identified as unimplemented.

```powershell
python -m models.Game_Outcome.PPP --seasons 2025 2026 --home SF --away WSH --as-of 2026-10-19T23:35:23+00:00
```

## NFL EPA model

Operational. [Read the complete EPA documentation](Game_Outcome/EPA/README.md), covering signed passing/rushing EPA, weighted ridge team effects, the fitted scoring bridge, EPA-specific forward score errors, covariance, pricing and the exact limits of the PDF implementation.

```powershell
python -m models.Game_Outcome.EPA --seasons 2025 2026 --home BAL --away CIN --as-of 2026-10-19T23:35:23+00:00
```

## NBA PPP model

Operational. [Read the NBA-specific PPP explanation](Game_Outcome/PPP/README.md#8-nba-specific-ppp-implementation). NBA efficiency uses estimated possessions and the EPA research note's additive PPP/shared-pace baseline. It has independent NBA calibration and no-tie spread settlement.

```powershell
python -m models.Game_Outcome.PPP.NBA_PPP --seasons 2025 --home GSW --away LAL --as-of 2026-10-19T23:00:00+00:00
```

## NBA EPA/EPV model

Operational event-based research baseline. [The EPA README](Game_Outcome/EPA/README.md#10-nba-event-based-epv-and-pregame-forecast) explains scoreboard reconciliation, pre-action state valuation, signed possession residuals, projected shared pace and chronological score-error calibration. [The NBA setup guide](Game_Outcome/EPA/NBA_EPA_SETUP.md) records actual reconstruction results and remaining validation work. This model uses public event data; it does not reproduce continuous tracking EPV or a possession simulator.

```powershell
python -m models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025 --download
python -m models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025
python -m models.Game_Outcome.EPA.NBA_EPA --seasons 2025 --home DET --away BOS --as-of 2026-10-20T03:48:47+00:00
```

The second command is an offline raw-input audit using existing archives. The third prepares reconciled inputs and fits a forecast, downloading missing public inputs if needed. Historical source downloads consume no Odds API credits.

## Imports, integration and data locations

Canonical imports use `models.Game_Outcome.PPP.PPP`, `models.Game_Outcome.PPP.NBA_PPP`, `models.Game_Outcome.EPA.EPA`, `models.Game_Outcome.EPA.NBA_EPA`, `models.Game_Outcome.EPA.nba_epa_training` and `models.Game_Outcome.ELO`. The raw audit utility remains `models.Game_Outcome.EPA.nba_epa_data`. Package-level NFL imports such as `from models.Game_Outcome.PPP import PPPModel` and `from models.Game_Outcome.EPA import EPAModel` remain supported. Direct scripts also run from their new subfolders.

Dash orchestration stays in `sports_analytics/ppp_service.py`, `nba_ppp_service.py`, `epa_service.py`, `nba_epa_service.py`, `elo_service.py` and `market_view.py`. The shared historical adapters remain in `sports_analytics`; the methodology folders contain calculation/preparation code rather than app callbacks.

Model inputs, frozen forecasts and the NBA EPV readiness report retain their existing `data/modeling/nfl` and `data/modeling/nba` locations. Saved market CSVs remain in `data/market_data`. Moving Python files does not change fitted algorithms, snapshot cutoffs, archive identities or market pulls.

The complete READMEs distinguish formula checks, fitted calibration and untouched-future validation. These approaches are research baselines rather than the complete production simulators described in the PDFs. NCAAF now has separate PPP, provider-PPA EPA and ELO game-outcome modules, plus WHP player forecasts. Their scoped CSVs, transfer history and settings are documented in [NCAAF forecasts](../docs/NCAAF_FORECASTS.md); the existing NFL/NBA PDF guides describe those sports' implementations.

Run shared offline verification with `python scripts/check_project.py`. Local tests, if present, can be run separately with `python -m unittest discover -s tests -q`; test folders are not published to the repository.
