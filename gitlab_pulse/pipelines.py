"""Pipeline health: per-project status/duration, and failure rate by branch."""

import pandas as pd

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries

UNHEALTHY_SUCCESS_RATE_THRESHOLD = 70.0


def fetch_pipeline_metrics(project, sample_size: int = 20) -> tuple[dict, list[dict]]:
    """Summarize recent pipeline health for a single project.

    Looks at the most recent `sample_size` pipelines (one list call) and
    reports success rate, latest status, and duration for the latest run
    (one extra detail call — the list endpoint doesn't include duration).
    Also returns a failure-rate-by-branch breakdown computed for free from
    the already-fetched pipeline list.

    Returns (pipeline_row, branch_rows).
    """
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
            "last_duration_seconds": None,
            "last_queued_duration_seconds": None,
            "coverage": None,
            "error": str(exc),
        }, []

    if not pipelines:
        return {
            "sampled": 0,
            "success_rate": None,
            "last_status": None,
            "last_run_at": None,
            "last_duration_seconds": None,
            "last_queued_duration_seconds": None,
            "coverage": None,
            "error": None,
        }, []

    successes = sum(1 for p in pipelines if p.status == "success")

    # Duration/coverage only come back from the single-pipeline detail
    # endpoint, not the list — fetch it just for the latest pipeline so
    # this stays a fixed, small number of extra calls per project. Failure
    # here shouldn't sink the whole row; the rest of the metrics still hold.
    last_duration_seconds = None
    last_queued_duration_seconds = None
    coverage = None
    try:
        latest = _call_with_retries(project.pipelines.get, pipelines[0].id)
        last_duration_seconds = latest.duration
        last_queued_duration_seconds = latest.queued_duration
        coverage = latest.coverage
    except RETRYABLE_EXCEPTIONS:
        pass

    branch_rows = _branch_breakdown(pipelines)

    pipeline_row = {
        "sampled": len(pipelines),
        "success_rate": round(successes / len(pipelines) * 100, 1),
        "last_status": pipelines[0].status,
        "last_run_at": pd.to_datetime(pipelines[0].created_at, utc=True),
        "last_duration_seconds": last_duration_seconds,
        "last_queued_duration_seconds": last_queued_duration_seconds,
        "coverage": coverage,
        "error": None,
    }
    return pipeline_row, branch_rows


def _branch_breakdown(pipelines) -> list[dict]:
    """Group an already-fetched pipeline sample by ref (branch/tag)."""
    by_ref: dict[str, dict] = {}
    for p in pipelines:
        ref = p.ref or "(unknown)"
        bucket = by_ref.setdefault(ref, {"sampled": 0, "successes": 0, "failures": 0})
        bucket["sampled"] += 1
        if p.status == "success":
            bucket["successes"] += 1
        elif p.status == "failed":
            bucket["failures"] += 1

    rows = []
    for ref, bucket in by_ref.items():
        rows.append({
            "ref": ref,
            "sampled": bucket["sampled"],
            "successes": bucket["successes"],
            "failures": bucket["failures"],
            "success_rate": round(bucket["successes"] / bucket["sampled"] * 100, 1),
        })
    return rows


def filter_recent_unhealthy(pipelines_df: pd.DataFrame, days: int = 7, now=None) -> pd.DataFrame:
    """Return unhealthy rows whose last pipeline ran within the past `days` days."""
    if pipelines_df.empty:
        return pipelines_df
    now = now or pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(days=days)
    mask = (
        (pipelines_df["category"] == "unhealthy")
        & pipelines_df["last_run_at"].notna()
        & (pipelines_df["last_run_at"] >= cutoff)
    )
    return pipelines_df[mask]


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
