"""Runner health: online/offline status and a job-queue-time proxy.

Uses `group.runners.list()` rather than `gl.runners_all` — the latter is
instance-admin-only and 403s for a normal PAT. GitLab has no direct "queue
time per runner" metric, so it's proxied as the average `queued_duration`
of jobs that ran on each runner (from the raw job rows `jobs.py` collects
across all projects).
"""

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries


def fetch_runner_metrics(gl, group, raw_job_rows: list[dict]) -> list[dict]:
    """Return one row per runner available to the group, with an avg queue time."""
    try:
        runners = _call_with_retries(group.runners.list, get_all=True)
    except RETRYABLE_EXCEPTIONS as exc:
        print(f"Could not list group runners: {exc}")
        return []

    queue_by_runner = _avg_queued_duration_by_runner(raw_job_rows)

    rows = []
    for runner in runners:
        contacted_at = None
        tags = None
        try:
            detail = _call_with_retries(gl.runners.get, runner.id)
            contacted_at = detail.contacted_at
            tags = ", ".join(detail.tag_list) if detail.tag_list else None
        except RETRYABLE_EXCEPTIONS:
            pass

        rows.append({
            "runner_id": runner.id,
            "description": getattr(runner, "description", None),
            "runner_type": getattr(runner, "runner_type", None),
            "status": getattr(runner, "status", None),
            "paused": getattr(runner, "paused", None),
            "contacted_at": contacted_at,
            "tags": tags,
            "avg_queued_duration_seconds": queue_by_runner.get(runner.id),
        })
    return rows


def _avg_queued_duration_by_runner(raw_job_rows: list[dict]) -> dict:
    totals: dict[int, list[float]] = {}
    for row in raw_job_rows:
        runner_id = row.get("runner_id")
        queued = row.get("queued_duration")
        if runner_id is None or queued is None:
            continue
        totals.setdefault(runner_id, []).append(queued)
    return {
        runner_id: round(sum(values) / len(values), 1)
        for runner_id, values in totals.items()
    }
