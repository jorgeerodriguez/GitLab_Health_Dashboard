"""Generic CSV/JSON report writers, shared by all 7 domain DataFrames.

Works directly against DataFrames (rather than list[dict]) so datetime
columns (last_run_at, updated_at, snapshot_at, ...) serialize consistently
via pandas itself instead of a hand-rolled per-field stringifier.
"""

from pathlib import Path

import pandas as pd


def write_csv_report(df: pd.DataFrame, out_path: Path):
    print(f"Writing CSV report to {out_path}...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)


def write_json_report(df: pd.DataFrame, out_path: Path):
    print(f"Writing JSON report to {out_path}...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(df.to_json(orient="records", date_format="iso", indent=2))


def write_reports(df: pd.DataFrame, reports_dir: Path, base_name: str):
    """Write both the CSV and JSON report for a domain DataFrame."""
    write_csv_report(df, reports_dir / f"{base_name}.csv")
    write_json_report(df, reports_dir / f"{base_name}.json")
