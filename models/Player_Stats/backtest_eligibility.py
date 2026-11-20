"""Participation evidence used to select realized player outcomes for replay."""

import numpy as np
import pandas as pd


NBA_MINUTES = 10
FOOTBALL_SNAPS = 10
MIN_PASS_ATTEMPTS = 5
MIN_CARRIES = 3
MIN_RECEIVING_OPPORTUNITIES = 2
STANDALONE_WORKLOAD = (
    'NBA needs recorded min. Football needs offense_snaps or recorded attempts, '
    'carries, targets or receptions.'
)


def recorded_count(frame, column):
    if column not in frame:
        return pd.Series(np.nan, index=frame.index)
    values = pd.to_numeric(frame[column], errors='coerce')
    return values.where(values.ge(0) & np.isfinite(values))


def receiving_opportunities(frame):
    return recorded_count(frame, 'targets').combine_first(recorded_count(frame, 'receptions'))


def recorded_workload_rows(frame, sport, *, prior_workload=False):
    """Filter evaluation rows while callers keep their original training data."""
    evidence = {'min'} if sport == 'NBA' else {'offense_snaps', 'attempts', 'carries', 'targets', 'receptions'}
    if not prior_workload and not evidence.intersection(frame.columns):
        raise ValueError(STANDALONE_WORKLOAD)
    keep = pd.Series(True, index=frame.index)
    if 'did_not_play' in frame:
        keep &= ~frame['did_not_play'].astype('string').str.strip().str.lower().isin({'true', '1', '1.0'})
    if 'participation_status' in frame:
        status = frame['participation_status'].astype('string').str.strip().str.lower()
        keep &= ~status.isin({'dnp', 'did not play', 'inactive', 'out', 'did_not_play'})
    if sport == 'NBA':
        return frame[keep & recorded_count(frame, 'min').ge(NBA_MINUTES)]
    if sport == 'NCAAF' and 'entity_id' in frame:
        # CFBD uses negative school IDs for some team-level box score entries.
        keep &= ~frame['entity_id'].astype('string').str.startswith('-', na=False)
    snaps = recorded_count(frame, 'offense_snaps')
    if prior_workload:
        fallback = True
    else:
        fallback = (recorded_count(frame, 'attempts').ge(MIN_PASS_ATTEMPTS)
                    | recorded_count(frame, 'carries').ge(MIN_CARRIES)
                    | receiving_opportunities(frame).ge(MIN_RECEIVING_OPPORTUNITIES))
    return frame[keep & (snaps.ge(FOOTBALL_SNAPS) | (snaps.isna() & fallback))]


def snapshot_workload_rows(actuals, snapshots):
    """Select joined outcomes using their snapshot's sport."""
    sports = {str(snapshot['game_id']): snapshot['sport'] for snapshot in snapshots}
    by_sport = actuals['game_id'].map(sports)
    if actuals.empty or by_sport.dropna().empty:
        return actuals.iloc[:0]
    return pd.concat([recorded_workload_rows(rows, sport) for sport, rows in actuals.groupby(by_sport)])
