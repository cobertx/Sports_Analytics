# Playing-Time Rotation (PTR)

PTR forecasts NBA player props from saved player and team box scores. Choose
**Playing-Time Rotation (PTR)** in the NBA player forecast selector or Quick
Filtering. **Backtesting → Player Stats** and **Strategy & Research** also list PTR.

The model samples recent complete team rotations with recency and season weights.
It shrinks positive minutes toward comparable historical roles and reallocates
the sampled minutes under a 48-minute player cap. Every rotation contains 240
regulation minutes, including players whose props are absent from the snapshot.
Historical overtime is normalized to regulation time. Confirmed absences from a
supplied injury packet can change the rotation when its report and retrieval
timestamps are at or before the quote cutoff.

Player opportunity shares scale with minutes before the simulation draws from
integer team pools. It projects points, rebounds, assists, made threes, steals,
blocks, turnovers, and supported combinations. The probability of playing and
minutes conditional on playing are reported separately. Prop win, loss, and push
probabilities use draws in which the player participated. A nonappearance is
outside that distribution.

Replay uses the same saved-box model with injury packets disabled and evaluates
recorded appearances. Saved reports include actual minutes, projected minutes
conditional on playing, and the modeled share of rotation paths played. The
reported participation share is based on complete historical team boxes and
recent team membership inferred from appearances. These inputs do not provide
timestamped starting lineups, substitution paths, minutes restrictions, or an
overtime forecast.

## Run against a saved snapshot

```powershell
python -m models.Player_Stats.Playing_Time_Rotation saved_snapshot.json
```

Provide NBA `game_id`, `home_team`, `away_team`, timezone-aware `commence_time`
and `pulled_at`, and the normal player market `rows`. Use `--prepare` to download
the public historical player and team boxes if they are missing. Both the
appearance and its publication timestamp, or the repository's conservative
publication proxy, must precede the forecast cutoff.
