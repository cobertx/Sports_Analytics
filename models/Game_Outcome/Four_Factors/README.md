# NBA Four Factors (FF)

The model fits opponent-adjusted effective field-goal percentage, turnover rate, offensive rebound share and free-throw attempts per field-goal attempt. Separate regressions estimate free-throw accuracy and rebound opportunities per field-goal attempt. Each fit uses a league intercept and regularized team offense, opposing defense and venue terms. Its weights reflect the factor's own box-score exposure, recency and the app's prior-season discount.

Offensive rebound share is multiplied by the predicted rebound opportunities per field-goal attempt to obtain offensive rebounds per attempt. The scoring identity uses estimated possessions of FGA + 0.44 FTA − ORB + TOV and points of 2 FGM + 3PM + FTM. Forecasts enter that identity directly. AODE remains a separate comparison method.

The simulation uses the shared PACE possession estimator and samples paired errors from earlier held-forward regulation games. Each donor supplies both teams' factor errors and a shared pace error together. An additional small residual reconciles scoring per team's estimated possessions with scoring at the shared pace. This captures the denominator difference without adding a second draw of scoring randomness already explained by the factors. Impossible matchup scenarios are omitted before sampling.

The default is 50,000 seeded draws with the app's time-scaled overtime approximation. The same final scores price moneylines, spreads, game totals and team totals with exact push mass. The forecast archive retains the factor projections, calibration inputs and seed, and backtests report empirical intervals. Confirmed availability can adjust the simulated score rates through the app's historical overlay. The underlying factor regressions use observed team boxes and venue.
