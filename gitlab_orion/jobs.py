"""Job-level detail: which stage/job fails most, and per-job duration/queue time.

`project.jobs.list()` returns jobs across a project's recent pipelines in a
single call (cheaper than iterating `pipeline.jobs.list()` per sampled
pipeline), including duration, queued_duration, and the runner that ran it.
"""

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries


def fetch_job_metrics(project, sample_size: int = 20) -> tuple[list[dict], list[dict]]:
    """Return (stage_rows, raw_job_rows) for a project's most recent jobs.

    stage_rows: one row per stage — failure rate, worst-offending job name,
    average duration/queue time (feeds the jobs_df / "which stage fails
    most" question).

    raw_job_rows: minimal per-job records (runner_id, queued_duration) used
    downstream to compute avg queue time per runner — kept separate from
    stage_rows so callers that only want the runner join don't need to
    re-derive it from aggregated stage data.
    """
    try:
        jobs = _call_with_retries(
            project.jobs.list, per_page=sample_size, get_all=False
        )
    except RETRYABLE_EXCEPTIONS:
        return [], []

    by_stage: dict[str, dict] = {}
    raw_job_rows = []

    for job in jobs:
        stage = job.stage or "(unknown)"
        bucket = by_stage.setdefault(stage, {
            "sampled": 0,
            "failures": 0,
            "failures_by_job": {},
            "durations": [],
            "queued_durations": [],
        })
        bucket["sampled"] += 1
        if job.status == "failed":
            bucket["failures"] += 1
            bucket["failures_by_job"][job.name] = bucket["failures_by_job"].get(job.name, 0) + 1
        if job.duration is not None:
            bucket["durations"].append(job.duration)
        if job.queued_duration is not None:
            bucket["queued_durations"].append(job.queued_duration)

        runner = getattr(job, "runner", None)
        raw_job_rows.append({
            "runner_id": runner.get("id") if runner else None,
            "queued_duration": job.queued_duration,
        })

    stage_rows = []
    for stage, bucket in by_stage.items():
        top_failing_job = None
        if bucket["failures_by_job"]:
            top_failing_job = max(bucket["failures_by_job"], key=bucket["failures_by_job"].get)
        stage_rows.append({
            "stage": stage,
            "sampled": bucket["sampled"],
            "failures": bucket["failures"],
            "failure_rate": round(bucket["failures"] / bucket["sampled"] * 100, 1),
            "top_failing_job": top_failing_job,
            "avg_duration_seconds": _mean(bucket["durations"]),
            "avg_queued_duration_seconds": _mean(bucket["queued_durations"]),
        })

    return stage_rows, raw_job_rows


def _mean(values: list[float]):
    return round(sum(values) / len(values), 1) if values else None
