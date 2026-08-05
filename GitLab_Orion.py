#############
# GitLab Orion #
#############
"""CLI entrypoint. Domain logic lives in the gitlab_orion/ package:

- client.py            auth, retries, project/group listing
- pipelines.py          pipeline health (status, duration, branch failure rate)
- jobs.py               job/stage-level failure attribution
- merge_requests.py     MR/merge health (stale, blocked)
- repo_hygiene.py       stale branches, protected-branch policy drift
- runners.py            runner health (group-level)
- snapshot.py           per-project collection + DataFrame assembly
- history.py            Parquet snapshot store for real trend data across runs
- reports.py            CSV/JSON writers
- dashboard.py          self-contained HTML dashboard for leadership
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from gitlab_orion.client import DEFAULT_GROUP, get_group, list_group_projects, get_gitlab_client, test_connection
from gitlab_orion.dashboard import build_dashboard_html
from gitlab_orion.history import append_snapshot
from gitlab_orion.merge_requests import MR_SAMPLE_SIZE, STALE_MR_DAYS
from gitlab_orion.pipelines import filter_recent_unhealthy
from gitlab_orion.repo_hygiene import STALE_BRANCH_DAYS
from gitlab_orion.reports import write_reports
from gitlab_orion.runners import fetch_runner_metrics
from gitlab_orion.snapshot import build_snapshot_dataframes, collect_project_snapshot

load_dotenv()

REPORTS_DIR = Path(__file__).parent / "reports"
HISTORY_DIR = REPORTS_DIR / "history"
JOB_SAMPLE_SIZE = 20


def build_health_snapshot(
    group_path: str = DEFAULT_GROUP,
    sample_size: int = 20,
    job_sample_size: int = JOB_SAMPLE_SIZE,
    mr_sample_size: int = MR_SAMPLE_SIZE,
    stale_mr_days: int = STALE_MR_DAYS,
    stale_branch_days: int = STALE_BRANCH_DAYS,
    max_workers: int = 10,
) -> tuple[dict, "pd.Timestamp"]:
    """Collect every domain for every active project in a group as tidy DataFrames.

    One worker per project gathers pipelines + jobs + MRs + repo hygiene
    together; runners are collected once at the group level. Returns
    (dataframes_by_name, snapshot_at).
    """
    print(f"Building health snapshot for group '{group_path}' (sample_size={sample_size}, max_workers={max_workers})...")
    gl = get_gitlab_client()
    gl.auth()

    now = pd.Timestamp.now(tz="UTC")
    group = get_group(gl, group_path)
    group_projects = list_group_projects(gl, group)
    print(f"Found {len(group_projects)} active project(s) under '{group_path}'.")

    results_by_id = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_project = {
            pool.submit(
                collect_project_snapshot, gl, project, now,
                sample_size, job_sample_size, mr_sample_size, stale_mr_days, stale_branch_days,
            ): project
            for project in group_projects
        }
        for future in as_completed(future_to_project):
            project = future_to_project[future]
            results_by_id[project.id] = future.result()

    raw_job_rows = [row for result in results_by_id.values() for row in result["raw_job_rows"]]
    runner_rows = fetch_runner_metrics(gl, group, raw_job_rows)

    dataframes = build_snapshot_dataframes(group_projects, results_by_id, runner_rows, snapshot_at=now)
    return dataframes, now


def _console_table(df: pd.DataFrame) -> pd.DataFrame:
    view = df[["project_path", "last_status", "success_rate", "last_run_at", "reason"]].copy()
    view = view.sort_values("success_rate", na_position="first").reset_index(drop=True)
    view["last_status"] = view["last_status"].fillna("-")
    view["success_rate"] = view["success_rate"].map(lambda v: "n/a" if pd.isna(v) else f"{v}%")
    view["last_run_at"] = view["last_run_at"].map(lambda v: "-" if pd.isna(v) else v.strftime("%Y-%m-%d %H:%M UTC"))
    return view.rename(columns={
        "project_path": "Project", "last_status": "Last Status",
        "success_rate": "Success Rate", "last_run_at": "Last Run", "reason": "Reason",
    })


def print_unhealthy_table(df: pd.DataFrame, total_count: int, label: str = "unhealthy repo(s)"):
    """Display a slice of the pipelines DataFrame as a console table."""
    if df.empty:
        print(f"No {label} found.")
        return
    view = _console_table(df)
    with pd.option_context("display.max_rows", None, "display.max_colwidth", None, "display.width", None):
        print(view.to_string(index=False))
    print(f"\n{len(view)} {label} out of {total_count} total.")


def print_domain_summary(dataframes: dict):
    """One-line console rollups for the domains beyond pipeline health."""
    mrs = dataframes["merge_requests"]
    stale_mrs = int(mrs["is_stale"].sum()) if not mrs.empty else 0
    blocked_mrs = int(mrs["is_blocked"].sum()) if not mrs.empty else 0
    print(f"\nOpen MRs: {len(mrs)} ({stale_mrs} stale, {blocked_mrs} blocked)")

    runners = dataframes["runners"]
    offline = int(runners["status"].isin(["offline", "stale"]).sum()) if not runners.empty else 0
    print(f"Runners: {len(runners)} total, {offline} offline/stale")

    branches = dataframes["branches"]
    stale_branches = int(branches["is_stale"].sum()) if not branches.empty else 0
    print(f"Stale branches (90+ days): {stale_branches}")

    drift = dataframes["protection_drift"]
    violations = int(drift["violations"].notna().sum()) if not drift.empty else 0
    print(f"Branch-protection policy violations: {violations}")


def generate_reports(
    group_path: str = DEFAULT_GROUP,
    sample_size: int = 20,
    job_sample_size: int = JOB_SAMPLE_SIZE,
    mr_sample_size: int = MR_SAMPLE_SIZE,
    stale_mr_days: int = STALE_MR_DAYS,
    stale_branch_days: int = STALE_BRANCH_DAYS,
    max_workers: int = 10,
    recent_days: int = 7,
):
    """Collect all domains, persist history, write reports, build the dashboard."""
    dataframes, snapshot_at = build_health_snapshot(
        group_path, sample_size, job_sample_size, mr_sample_size,
        stale_mr_days, stale_branch_days, max_workers,
    )

    history = {}
    for name, df in dataframes.items():
        write_reports(df, REPORTS_DIR, name)
        history[name] = append_snapshot(df, name, HISTORY_DIR)

    pipelines_df = dataframes["pipelines"]
    unhealthy_df = pipelines_df[pipelines_df["category"] == "unhealthy"]
    recent_unhealthy_df = filter_recent_unhealthy(pipelines_df, days=recent_days, now=snapshot_at)

    print()
    print_unhealthy_table(unhealthy_df, total_count=len(pipelines_df))
    print(f"\nUnhealthy repos with a pipeline run in the last {recent_days} day(s):")
    print_unhealthy_table(recent_unhealthy_df, total_count=len(pipelines_df),
                           label=f"unhealthy repo(s) in the last {recent_days} days")

    print_domain_summary(dataframes)

    build_dashboard_html(
        latest=dataframes, history=history,
        out_path=REPORTS_DIR / "dashboard.html", group_path=group_path, snapshot_at=snapshot_at,
    )


def main():
    print("GitLab Orion is running...")
    ok = test_connection()
    if not ok:
        sys.exit(1)

    print()
    generate_reports()


if __name__ == "__main__":
    main()
