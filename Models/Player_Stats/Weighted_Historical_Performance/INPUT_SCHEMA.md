# Canonical model-service imports

Optional imports live at `data/modeling/player_stats/whp/imports/nba.csv`, `nfl.csv` or `ncaaf.csv`. Import rows supplement source history; they must not overlap existing player/game identities. Conflicting overlaps are rejected. Back up and verify data before replacing a provider history with a canonical import.

| Column | Requirement |
| --- | --- |
| `game_id` | Required stable game ID, string |
| `entity_id` | Required stable player ID, string; same namespace across seasons |
| `entity_name` | Required full player name matching the market, accent/suffix normalization allowed |
| `team` | Required team code (NBA/NFL) or canonical school name |
| `event_date` | Required game start/date; timezone-aware preferred |
| `available_at` | Result availability timestamp, timezone-aware; omitted timestamps use the documented conservative proxy |
| `season` | Recommended start-year season |
| `market_team` | NCAAF: full current displayed team name, e.g. Alabama Crimson Tide |
| `participation_status` | Recommended description of inclusion/participation policy |

NBA targets use `pts,reb,ast,fg3m,stl,blk,tov`, with optional `min,fg3a`. Football targets use `passing_yards,attempts,completions,passing_tds,interceptions,carries,rushing_yards,rushing_tds,receptions,receiving_yards,receiving_tds`, with optional `targets`. `passing_interceptions` is also accepted and canonicalized. All target fields are game counts/net yards, not season totals or rates.

The optional `touchdowns` field must be an audited **all credited player scoring TD** count for the intended bookmaker rules. Passing TDs must not be included merely because the player threw the pass. Avoid summing overlapping return/defensive/fumble categories. Supply explicit zero observations for eligible appearances when zero TDs were scored. With enough complete history this field enables anytime TD and total scorer TD probabilities. It does not enable first/last scorer.

Never fill unavailable targets with zero. DNPs and inactive roster entries must not be inserted as zero-stat appearances. Include all qualifying played games according to a consistent participation policy, including genuine zero-opportunity outcomes. If a trade/transfer changes the current team, preserve the same player ID; names alone never establish cross-source identity. For NCAAF transfers, add the prior school's games with the same canonical ID and the correct historical `team`; `market_team` should reflect the relevant displayed school per row.

The standalone CLI has a narrower interface: one player/target CSV with `game_id,event_date,available_at,value` and optional `opportunity`. A multi-player wide service import cannot be passed directly as that target CSV without selecting the player and building its target column first.
