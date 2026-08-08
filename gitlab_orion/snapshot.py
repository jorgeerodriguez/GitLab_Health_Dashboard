"""Per-project data collection and assembly into the 7 tidy DataFrames.

One worker task per project gathers pipelines + jobs + MRs + repo hygiene
together (four collectors, but still just one pass over the project list),
instead of four separate full scans across every project. Runners are
collected once, outside this per-project loop -- but that collection
(see runners.py) still sweeps every project's runner list under its own
thread pool, since group-level runner listing alone can miss project-
registered runners.
"""

import pandas as pd

from .jobs import fetch_job_metrics
from .merge_requests import fetch_merge_request_metrics
from .pipelines import classify_health, fetch_pipeline_metrics
from .repo_hygiene import fetch_branch_metrics


def collect_project_snapshot(
    gl,
    group_project,
    now,
    sample_size: int = 20,
    job_sample_size: int = 20,
    mr_sample_size: int = 50,
    stale_mr_days: int = 14,
    stale_branch_days: int = 90,
) -> dict:
    """Fetch every per-project metric domain for one project."""
    project = gl.projects.get(group_project.id, lazy=True)

    pipeline_row, branch_pipeline_rows = fetch_pipeline_metrics(project, sample_size)
    stage_rows, raw_job_rows = fetch_job_metrics(project, job_sample_size)
    mr_rows = fetch_merge_request_metrics(project, now, stale_mr_days, mr_sample_size)
    hygiene_branch_rows, protection_row = fetch_branch_metrics(project, now, stale_branch_days)

    return {
        "pipeline_row": pipeline_row,
        "branch_pipeline_rows": branch_pipeline_rows,
        "stage_rows": stage_rows,
        "raw_job_rows": raw_job_rows,
        "mr_rows": mr_rows,
        "hygiene_branch_rows": hygiene_branch_rows,
        "protection_row": protection_row,
    }


def build_snapshot_dataframes(
    group_projects: list,
    results_by_id: dict,
    runner_rows: list[dict],
    snapshot_at,
) -> dict[str, pd.DataFrame]:
    """Flatten per-project collection results + runner rows into 7 tidy DataFrames."""
    pipeline_records = []
    branch_pipeline_records = []
    job_records = []
    mr_records = []
    branch_records = []
    protection_records = []

    for project in sorted(group_projects, key=lambda p: p.path_with_namespace):
        result = results_by_id[project.id]
        project_path = project.path_with_namespace
        web_url = project.web_url

        pipeline_row = result["pipeline_row"]
        category, reason = classify_health(pipeline_row)
        pipeline_records.append({
            "project_path": project_path,
            "web_url": web_url,
            "category": category,
            "reason": reason,
            "snapshot_at": snapshot_at,
            **pipeline_row,
        })

        for row in result["branch_pipeline_rows"]:
            branch_pipeline_records.append({"project_path": project_path, "snapshot_at": snapshot_at, **row})

        for row in result["stage_rows"]:
            job_records.append({"project_path": project_path, "snapshot_at": snapshot_at, **row})

        for row in result["mr_rows"]:
            mr_records.append({"project_path": project_path, "snapshot_at": snapshot_at, **row})

        for row in result["hygiene_branch_rows"]:
            branch_records.append({"project_path": project_path, "snapshot_at": snapshot_at, **row})

        protection_records.append({
            "project_path": project_path,
            "snapshot_at": snapshot_at,
            **result["protection_row"],
        })

    runner_records = [{"snapshot_at": snapshot_at, **row} for row in runner_rows]

    return {
        "pipelines": pd.DataFrame(pipeline_records),
        "pipelines_by_branch": pd.DataFrame(branch_pipeline_records),
        "jobs": pd.DataFrame(job_records),
        "runners": pd.DataFrame(runner_records),
        "merge_requests": pd.DataFrame(mr_records),
        "branches": pd.DataFrame(branch_records),
        "protection_drift": pd.DataFrame(protection_records),
    }
