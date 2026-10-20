# NBA modeling inputs

Paired team-game observations and source manifests are prepared by
`sports_analytics.nba_model_data.NbaModelData`, using the existing NBA historical
adapters. Frozen NBA PPP estimates are archived in `forecasts/` by snapshot
cutoff and model configuration. Generated CSVs, manifests and forecasts are
ignored by Git. No betting quotes or API keys are stored here.

See [Models/README.md](../../../Models/Game_Outcome/PPP/README.md#8-nba-specific-ppp-implementation) for possession
definitions, availability cutoffs, calculations and limitations.

Public event-based EPV inputs and their audit report live separately in `epa/`.
Use `python -m Models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025 --download` initially, and omit
`--download` for an offline audit of cached archives. That command only audits
raw inputs. The operational `Models.Game_Outcome.EPA.NBA_EPA` forecast additionally reconstructs
scoring, quarantines unresolved games and fits event-based EPV/team residuals.
Reconciled `team_games_*.csv`, `state_statistics_*.npz`, source manifests,
`state_sample_*.csv` and `quarantine_*.json` remain under `epa/`; independent
EPA forecast archives live under `epa/forecasts/`. See
[NBA EPA/EPV setup](../../../Models/Game_Outcome/EPA/NBA_EPA_SETUP.md).
