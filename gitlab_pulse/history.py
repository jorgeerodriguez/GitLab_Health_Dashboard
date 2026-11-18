"""Snapshot persistence: accumulate each run's DataFrames into a local
Parquet time-series store, so trend charts (success rate over time,
duration drift, stale-MR count over time) reflect real calendar history
across repeated runs instead of a single point-in-time sample.
"""

from pathlib import Path

import pandas as pd


def _history_path(history_dir: Path, name: str) -> Path:
    return history_dir / f"{name}.parquet"


def append_snapshot(df: pd.DataFrame, name: str, history_dir: Path) -> pd.DataFrame:
    """Append `df` (one run's rows, tagged with snapshot_at) to the history
    store for `name`, and return the full accumulated history.

    Safe to call with an empty df (e.g. no unhealthy branches this run) —
    an empty snapshot still gets recorded so trend counts don't silently
    skip a data point.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    path = _history_path(history_dir, name)

    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, df], ignore_index=True) if not df.empty else existing
    else:
        combined = df

    combined = combined.drop_duplicates().reset_index(drop=True)
    if not combined.empty:
        combined.to_parquet(path, index=False)
    return combined


def load_history(name: str, history_dir: Path) -> pd.DataFrame:
    """Return the full accumulated history for `name`, or an empty DataFrame."""
    path = _history_path(history_dir, name)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)
