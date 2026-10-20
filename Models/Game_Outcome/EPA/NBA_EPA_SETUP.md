# NBA event-based EPV setup and readiness

## Decision and current status

NBA EPA is enabled as a public **event-based Expected Possession Value (EPV)**
research baseline. Select EPA above the saved NBA market tables to update
probability, fair odds, Edge, criteria highlights and four comparison charts.
PPP remains the default. This EPV model is independent of NFL EPA and does not
reproduce continuous optical-tracking EPV. Expected-point quantities are converted
to market probabilities using independently calibrated normal score errors.

No new API account, Odds API credits or Python packages are required. Existing
public archives are cached locally. Initial forecasts can download missing
public data; viewing saved forecasts and applying criteria do not pull live odds.

## Verified sources

The public [SportsDataverse NBA play-by-play release](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_pbp)
and [derived possessions release](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_possessions)
both returned HTTP 200 during this setup. Their upstream source is NBA Stats.
The direct NBA CDN play-by-play endpoint returned HTTP 403 from this environment.
Use versioned public archives for historical preparation; incremental current
season access and publication lag still need to be monitored separately.

Project season **2025 means 2025-26**. These archive filenames use ending year
**2026**. `prepare_nba_epa.py` handles that conversion. Keep NBA game IDs as
zero-padded strings; the existing ESPN market game IDs are different identifiers.
Use NBA game IDs for model joins and the existing team/time matching for quotes.

The possession archive contains offense/defense team IDs, period, possession
number, start/end order indices and clocks, outcome points, counted-possession
flags, start type, box-score components, lineup IDs and lineup-source indicators.
The event archive contains ordered actions, period/game clock, scores, players,
shot results/locations, possession identifiers and lineup fields.

The inspected schemas have **no shot-clock field or continuous player/ball
tracking**. Shot coordinates are event locations. A public event-state model
must use available features and mark those omissions explicitly. Ten populated
lineup IDs do not prove that the inferred lineups are correct.

## Preparation commands

```powershell
# Already completed for 2025-26 during setup.
python -m Models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025 --download

# Repeat the same audit offline using the saved inputs.
python -m Models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025

# Suggested research history: 2023-24, 2024-25, 2025-26.
python -m Models.Game_Outcome.EPA.prepare_nba_epa --seasons 2023 2024 2025 --download

# Reconstruct inputs, fit NBA EPV/EPA and export a standalone forecast.
python -m Models.Game_Outcome.EPA.NBA_EPA --seasons 2025 --home DET --away BOS --as-of 2026-10-20T03:48:47+00:00
```

To compare additional seasons against independent box scores, populate the
existing team cache for both phases before running the audit:

```powershell
python -m sports_analytics.ingest NBA --seasons 2023 2024 --entity team --season-type regular
python -m sports_analytics.ingest NBA --seasons 2023 2024 --entity team --season-type postseason
```

Raw archives, source/hash manifests and `readiness.json` are written under
`data/modeling/nba/epa`. They are ignored by Git. `--download` fetches missing
archives; `--download --refresh` explicitly replaces cached copies. The audit
reads the existing box-score cache without making historical or odds requests.

## Initial audit findings

The 2025-26 archives cover **1,315 regular/postseason games**, **665,170 events**
and **261,965 possession records**. All cached box-score games have both archive
types. Preseason, All-Star and play-in ID prefixes are excluded from this first
scope; play-in requires an explicit later scope/phase policy.

However, possession scoring differs from box scores in **313 of 2,630 team-game
records**, with **13 invalid possession records**. Final play-by-play scores
differ in **2 of 2,630 team-game records**. Exact game/team IDs and differences
are saved in the readiness report. This local audit is not a claim that the
archive is generally unusable; scoring/attribution conventions must be resolved
before treating its derived possessions as training labels.

Reconstruct and reconcile scoring from the event sequence. Check technical and
flagrant free throws, and-ones, offensive rebounds, score corrections, retained
possessions, period endings and overtime. Keep non-counted scoring records
separate from possession denominators; never discard their points or spread a
final-score discrepancy arbitrarily across possessions. Quarantine unresolved
games with reasons and report the exclusion rate and possible selection bias.

## Reconciled forecasting pipeline

`nba_epa_training.py` now rebuilds possession rewards from ordered scoreboard
increments, rather than trusting derived possession-point labels. Technical/
defensive three-second points are accounted for separately. Both final scores
must match independent box scores exactly; unresolved corrections, ownership,
boundary or score inconsistencies quarantine the entire game. Raw archives are
preserved. The initial reconstructed cohort has:

| Measurement | Count |
| --- | ---: |
| Source regular/postseason games | 1,315 |
| Reconciled games | 1,175 |
| Quarantined games | 140 |
| Pre-action state observations | 587,738 |
| Corrected derived possession-point labels | 582 |

The exclusions are 95 ambiguous increments/corrections, 44 opposing-possession
scoring cases and one final-score mismatch. These are revision-specific audit
results, not a claim that all public games pass reconciliation. The retained
sample can be nonrandom. See `quarantine_2025.json` for reasons.

Each prepared season has `team_games_*.csv`, a signature manifest,
`state_statistics_*.npz` (per-game X'X/X'y), a state sample and a quarantine report.
The raw audit's `nba_epa_ready=false` remains an honest audit-only status; it is
not used as the separate fitted model's dashboard gate.

`NBA_EPA.py` fits remaining-possession EPV from 17 pre-action clock/period,
possession-start and prior-action/context features. Current-action outcomes,
future lineups and actual target pace are excluded. Signed opponent-adjusted
team residuals target `ordinary_points / possessions - mean_start_EPV`, avoiding
double counting. Earlier offensive/defensive contexts and actual possession
counts project pregame efficiency and shared pace; administrative points are
added separately.

Both fitted stages are recomputed on earlier available games for each held-out
ISO week. Score bias/covariance from the latest 300 eligible held-out games
parameterizes a normal margin/total baseline; at least 100 training and 100
calibration games are required. Integer pushes are retained and NBA spread
settlement removes the impossible final tie bin. Historical final scores include
overtime; there is no explicit possession/overtime simulator.

`NBAEPAMarketService` freezes snapshot cutoffs and stores independent EPA artifacts
under `epa/forecasts/`. Cache identity includes model version/configuration and
game/cutoff; artifacts record hashes for prepared inputs. Missing EPA data never
falls back to PPP values under an EPA heading. Full formulas and defaults are in
[the methodology documentation](README.md#10-nba-event-based-epv-and-pregame-forecast).

## Remaining research and validation

The enabled implementation is a research baseline. Validate state values and
game probabilities on untouched seasons using Brier score, log loss, reliability
plots and tail coverage; calibration error fits do not establish future accuracy.
The two state/residual stages share their prior training cohort and are not
internally cross-fitted. Assess that design, quarantine selection bias, archive
revision effects and season-to-season changes before treating estimates as
validated betting probabilities. Current source publication delays also matter.

Verified lineup/rotation and timestamped injury features, shot clock, tracking,
an integer possession simulator and explicit overtime transitions are further
extensions. They are not inputs or capabilities claimed by the enabled model.

The [pbpstats parser](https://pbpstats.readthedocs.io/en/latest/) is a possible
aid for possession/event-order reconstruction; its current-source compatibility
must be tested against these archives before adding it as a dependency. The
[Cervone et al. EPV paper](https://arxiv.org/abs/1408.0777) describes the richer
optical-tracking approach. That approach is outside this public-data baseline.
