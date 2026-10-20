# FanDuel Market parlay analysis slip

Click a saved **Market odds** button to add that selection to the bottom slip.
Click the selected button again or its remove control to remove the leg. The
header collapses/expands the slip; Clear all empties it. Selected market buttons
are blue and expose their pressed state for keyboard/screen-reader access.

The slip supports multiple games and sports. It persists in the current browser
tab's session storage, including page reloads. It hides outside FanDuel Market
without discarding selections. Each leg retains the clicked saved price and
snapshot time. Loading a new snapshot does not silently reprice existing legs;
clicking the same selection in a newer snapshot replaces its saved quote.
Exactly duplicated rows have distinct button IDs but one semantic selection,
preventing duplicate legs. Prices without valid odds cannot be selected.

## Per-leg estimates

The slip uses the requested defaults, **PPP** for NBA/NFL/NCAAF alternate spreads and
totals and **WHP** for player statistics, independently of the table's currently
selected methodology. It shows saved American odds, their raw implied
probability, the default model's probability and fair American odds. Source
history/cutoffs are handled by the existing forecast services. Unsupported
markets, insufficient player history or other unavailable
inputs retain the market price and show no calculated estimate. No missing model
probability is replaced with zero or with the bookmaker's probability.

WHP reads saved player data. PPP uses its existing cached-forecast/history path;
that service may prepare public historical inputs when an estimate is missing.
Selecting a leg never requests live bookmaker odds or spends Odds API credits.
Model estimates and the clicked quote are frozen together in the session slip.

## Combined price and probability

Convert American A to decimal D:

```text
A: D = 1 + A / 100
-A: D = 1 + 100 / abs(A)
Combined decimal odds = product of each leg's D
Market implied parlay probability = 1 / combined decimal odds
American odds = 100 * (D - 1) if D >= 2, else -100 / (D - 1)
Calculated probability = product of each leg's default-model fair probability
Calculated fair odds = American conversion of 1 / calculated probability
```

For example, two -110 legs yield decimal 3.644628..., American +264.4628...
and raw market implied probability 27.43764...%. Model probabilities of 75% and
80% yield 60% and fair odds -150. Calculation retains precision; displayed
American odds round to whole numbers and probabilities to two decimals.

Combined calculated probabilities assume independent legs. Multiple legs from
the same game are allowed, as requested, but **both their price and probability
are explicitly labeled independent-leg estimates**. Multiplying individual
quotes cannot reproduce FanDuel's correlation-adjusted SGP/SGP+ quote. The app
has no bookmaker SGP-pricing endpoint and does not apply an invented correlation
factor. Nested props, combined stats and game/player correlations remain
dependent even when not logically contradictory.

The conflict check catches opposing spreads, incompatible totals and incompatible
integer thresholds on the same player's same statistic. A conflict suppresses
the combined market quote and reports zero probability of every leg winning.
This is not a complete FanDuel eligibility validator; compatible-looking picks
may still be ineligible or differently priced at the sportsbook.

## Pushes, voids and missing values

Each displayed model probability is win/(win + loss), excluding pushes, matching
the existing market tables. Their product is conditional on all legs settling
without pushes. Full-win mass is tracked separately in the summary helper when
available. A pushed/void leg changes the eventual parlay by removing that leg;
the displayed full-ticket quote is not a simulation of every possible settlement
and reduced-payout subset. Unknown model estimates leave the combined model
probability/odds unavailable, while the market-price product can still be shown.
Probabilities of zero or one have no finite two-sided fair American price.

The implementation uses log-space market multiplication to avoid overflowing
for unusually large slips. Individual quotes are frozen, so comparisons do not
imply that every price was simultaneously available or is still executable.

## Files and verification

- `sports_analytics/parlay.py`: quote identity, add/toggle/update behavior, conflict bounds and price math.
- `sports_analytics/parlay_view.py`: grouped leg cards and combined metrics.
- `sports_analytics/market_view.py`: every saved market price becomes an accessible button.
- `sports_analytics/dashboard.py`: default-model estimates, session state and drawer callbacks.
- `sports_analytics/assets/parlay.css` and `parlay.js`: responsive docked layout and actual-height page spacing.

Local tests cover American/decimal conversion, product prices/probabilities,
same-game labeling, pushes, missing estimates, duplicates, quote replacement,
integer conflicts and price-button identities. Browser checks use saved NBA/NFL
quotes, real WHP player estimates and a deterministic PPP fixture to verify
add/remove, selected prices, combined arithmetic, collapse, session restoration,
cross-game/sport selections, mobile layout and Clear all without API requests.
CI includes independent parlay arithmetic and import/route checks.

FanDuel's official [bet-type guide](https://www.fanduel.com/sports-betting-strategy)
describes parlays, SGP/SGP+ and removal of pushed legs. Its
[published house rules](https://policies.fanduel.com/SB/NJ/2026-10-15T12-42-58.html)
distinguish standard parlays from explicitly offered correlated same-game bets.
This is a local analysis slip with no wagering or cash-out controls.
