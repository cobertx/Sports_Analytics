"""Simple, auditable reference model for future forecasting work."""

import pandas as pd


def rolling_baseline(frame, metric, window=5):
    """Single-entity input only; shift the window to prevent target leakage.

    The next forecast is the last N observed appearances' mean. Missing stats
    are excluded, never filled with zero. Require a full window for evaluation.
    """
    if window < 2:
        raise ValueError("The rolling window must be at least 2 games.")
    if frame["entity_id"].nunique() > 1:
        raise ValueError("Select one team or player before forecasting.")
    history = frame.sort_values(["season", "event_date", "week", "game_id"], na_position="last").copy()
    history[metric] = pd.to_numeric(history[metric], errors="coerce")
    history = history.dropna(subset=[metric]).reset_index(drop=True)
    history["baseline"] = history[metric].shift(1).rolling(window, min_periods=window).mean()
    scored = history.dropna(subset=["baseline"])
    mae = float((scored[metric] - scored["baseline"]).abs().mean()) if not scored.empty else None
    prediction = float(history[metric].tail(window).mean()) if len(history) >= window else None
    return history, prediction, mae, len(scored)
