#############
# GitLab Orion #
#############

import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gitlab
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_GROUP = os.environ.get("GITLAB_GROUP", "audacy-inc")
UNHEALTHY_SUCCESS_RATE_THRESHOLD = 70.0
REPORTS_DIR = Path(__file__).parent / "reports"

DETAIL_FIELDS = [
    "path",
    "web_url",
    "category",
    "reason",
    "sampled",
    "success_rate",
    "last_status",
    "last_run_at",
    "error",
]


def get_gitlab_client() -> gitlab.Gitlab:
    """Build an authenticated python-gitlab client from env vars.

    Requires GITLAB_TOKEN. GITLAB_URL defaults to https://gitlab.com.
    """
    print("Connecting to GitLab...")
    url = os.environ.get("GITLAB_URL", "https://gitlab.com")
    token = os.environ.get("GITLAB_TOKEN")

    if not token:
        raise RuntimeError(
            "GITLAB_TOKEN is not set. Add it to your .env file "
            "(see .env.example)."
        )

    return gitlab.Gitlab(url=url, private_token=token)


def test_connection() -> bool:
    """Authenticate against GitLab and print the resulting user identity."""
    print("Testing GitLab connection...")
    gl = get_gitlab_client()

    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        return False
    except gitlab.exceptions.GitlabError as exc:
        print(f"Could not reach GitLab: {exc}")
        return False

    user = gl.user
    print("Connected to GitLab successfully.")
    print(f"  URL:      {gl.url}")
    print(f"  User:     {user.username} ({user.name})")
    print(f"  User ID:  {user.id}")
    return True


RETRYABLE_EXCEPTIONS = (gitlab.exceptions.GitlabError, requests.exceptions.RequestException)


def _call_with_retries(fn, *args, retries=3, backoff=2.0, **kwargs):
    """Retry a GitLab API call on transient network/API errors with backoff."""
    #print(f"Calling {fn.__name__} with retries={retries}, backoff={backoff}s...")
    last_exc = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def list_group_projects(gl: gitlab.Gitlab, group_path: str = DEFAULT_GROUP, archived: bool = False):
    """Return all active (non-archived) projects under a group, including subgroups."""
    print(f"Listing projects under group '{group_path}' (archived={archived})...")
    group = gl.groups.get(group_path)
    return _call_with_retries(
        group.projects.list, all=True, include_subgroups=True, archived=archived
    )


def get_pipeline_health(gl: gitlab.Gitlab, project_id: int, sample_size: int = 20) -> dict:
    """Summarize recent pipeline health for a single project.

    Looks at the most recent `sample_size` pipelines and reports success
    rate and the latest status.
    """
    #print(f"Fetching pipeline health for project ID {project_id} (sample_size={sample_size})...")
    project = gl.projects.get(project_id, lazy=True)

    try:
        pipelines = _call_with_retries(
            project.pipelines.list,
            order_by="id",
            sort="desc",
            per_page=sample_size,
            get_all=False,
        )
    except RETRYABLE_EXCEPTIONS as exc:
        return {
            "sampled": 0,
            "success_rate": None,
            "last_status": None,
            "last_run_at": None,
            "error": str(exc),
        }

    if not pipelines:
        return {
            "sampled": 0,
            "success_rate": None,
            "last_status": None,
            "last_run_at": None,
            "error": None,
        }

    successes = sum(1 for p in pipelines if p.status == "success")

    return {
        "sampled": len(pipelines),
        "success_rate": round(successes / len(pipelines) * 100, 1),
        "last_status": pipelines[0].status,
        "last_run_at": pd.to_datetime(pipelines[0].created_at, utc=True),
        "error": None,
    }


def classify_health(health: dict) -> tuple[str, str]:
    """Categorize a project's pipeline health as healthy/unhealthy/no_pipelines."""
    if health["error"]:
        return "unhealthy", f"API error: {health['error']}"
    if health["sampled"] == 0:
        return "no_pipelines", "no pipelines found"
    if health["last_status"] == "failed":
        return "unhealthy", "last pipeline run failed"
    if health["success_rate"] is not None and health["success_rate"] < UNHEALTHY_SUCCESS_RATE_THRESHOLD:
        return "unhealthy", f"success rate {health['success_rate']}% below {UNHEALTHY_SUCCESS_RATE_THRESHOLD}% threshold"
    return "healthy", ""


def is_within_days(last_run_at, days: int = 7, now: datetime | None = None) -> bool:
    """Return True if `last_run_at` (tz-aware Timestamp) falls within the past `days` days of `now`."""
    if last_run_at is None or pd.isna(last_run_at):
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    return last_run_at >= cutoff


def filter_unhealthy_recent(records: list[dict], days: int = 7, now: datetime | None = None) -> list[dict]:
    """Return unhealthy records whose last pipeline ran within the past `days` days."""
    return [
        r for r in records
        if r["category"] == "unhealthy" and is_within_days(r["last_run_at"], days, now)
    ]


def build_health_records(group_path: str = DEFAULT_GROUP, sample_size: int = 20, max_workers: int = 10) -> list[dict]:
    """Gather pipeline health for every active project in a group as flat records."""
    print(f"Building pipeline health records for group '{group_path}' (sample_size={sample_size}, max_workers={max_workers})...")
    gl = get_gitlab_client()
    gl.auth()

    projects = list_group_projects(gl, group_path)
    print(f"Found {len(projects)} active project(s) under '{group_path}'.")

    health_by_id = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_project = {
            pool.submit(get_pipeline_health, gl, project.id, sample_size): project
            for project in projects
        }
        for future in as_completed(future_to_project):
            project = future_to_project[future]
            health_by_id[project.id] = future.result()

    records = []
    for project in projects:
        health = health_by_id[project.id]
        category, reason = classify_health(health)
        records.append(
            {
                "path": project.path_with_namespace,
                "web_url": project.web_url,
                "category": category,
                "reason": reason,
                **health,
            }
        )

    return sorted(records, key=lambda r: r["path"])


def _for_output(records: list[dict]) -> list[dict]:
    """Copy records with datetime fields (e.g. last_run_at) rendered as ISO 8601 strings."""
    prepared = []
    for record in records:
        last_run_at = record.get("last_run_at")
        prepared.append({
            **record,
            "last_run_at": None if last_run_at is None or pd.isna(last_run_at) else last_run_at.isoformat(),
        })
    return prepared


def write_csv_report(records: list[dict], out_path: Path):
    print(f"Writing CSV report to {out_path}...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        for record in _for_output(records):
            writer.writerow({field: record.get(field) for field in DETAIL_FIELDS})


def write_json_report(records: list[dict], out_path: Path):
    print(f"Writing JSON report to {out_path}...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(_for_output(records), f, indent=2)


def build_unhealthy_dataframe(records: list[dict]) -> pd.DataFrame:
    """Build a DataFrame of unhealthy repos, worst success rate first."""
    print("Building unhealthy repos DataFrame...")  
    df = pd.DataFrame(records, columns=["path", "last_status", "success_rate", "last_run_at", "reason"])
    df = df.sort_values("success_rate", na_position="first").reset_index(drop=True)
    df["last_status"] = df["last_status"].fillna("-")
    df["success_rate"] = df["success_rate"].map(
        lambda v: "n/a" if pd.isna(v) else f"{v}%"
    )
    df["last_run_at"] = df["last_run_at"].map(
        lambda v: "-" if pd.isna(v) else v.strftime("%Y-%m-%d %H:%M UTC")
    )
    return df.rename(
        columns={
            "path": "Project",
            "last_status": "Last Status",
            "success_rate": "Success Rate",
            "last_run_at": "Last Run",
            "reason": "Reason",
        }
    )


def print_unhealthy_table(records: list[dict], total_count: int):
    """Display the unhealthy repos as a pandas DataFrame."""
    print("Printing unhealthy repos table...")
    if not records:
        print("No unhealthy repos found.")
        return

    df = build_unhealthy_dataframe(records)
    with pd.option_context(
        "display.max_rows", None, "display.max_colwidth", None, "display.width", None
    ):
        print(df.to_string(index=False))
    print(f"\n{len(df)} unhealthy repo(s) out of {total_count} total.")


def generate_reports(group_path: str = DEFAULT_GROUP, sample_size: int = 20, max_workers: int = 10, recent_days: int = 7):
    """Build detail + unhealthy CSV/JSON reports and print the unhealthy table."""
    print(f"Generating reports for group '{group_path}' (sample_size={sample_size}, max_workers={max_workers})...")
    records = build_health_records(group_path, sample_size, max_workers)
    unhealthy_records = [r for r in records if r["category"] == "unhealthy"]
    recent_unhealthy_records = filter_unhealthy_recent(records, days=recent_days)

    write_csv_report(records, REPORTS_DIR / "pipeline_health_detail.csv")
    write_json_report(records, REPORTS_DIR / "pipeline_health_detail.json")
    write_csv_report(unhealthy_records, REPORTS_DIR / "pipeline_health_unhealthy.csv")
    write_json_report(unhealthy_records, REPORTS_DIR / "pipeline_health_unhealthy.json")
    write_csv_report(recent_unhealthy_records, REPORTS_DIR / f"pipeline_health_unhealthy_last_{recent_days}_days.csv")
    write_json_report(recent_unhealthy_records, REPORTS_DIR / f"pipeline_health_unhealthy_last_{recent_days}_days.json")

    #print(f"\nWrote detail report ({len(records)} rows) to {REPORTS_DIR / 'pipeline_health_detail.csv'} / .json")
    #print(f"Wrote unhealthy report ({len(unhealthy_records)} rows) to {REPORTS_DIR / 'pipeline_health_unhealthy.csv'} / .json\n")

    print_unhealthy_table(unhealthy_records, total_count=len(records))
    print(f"\nUnhealthy repos with a pipeline run in the last {recent_days} day(s):")
    print_unhealthy_table(recent_unhealthy_records, total_count=len(records))


def main():
    print("GitLab Orion is running...")
    ok = test_connection()
    if not ok:
        sys.exit(1)

    print()
    generate_reports()


if __name__ == "__main__":
    main()
