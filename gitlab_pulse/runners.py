"""Runner health: online/offline status and a job-queue-time proxy.

Avoids `gl.runners_all` (`/runners/all`) entirely -- that endpoint is
instance-admin-only, and this org is on GitLab SaaS (gitlab.com), where
nobody outside GitLab staff ever holds that role. GitLab has no direct
"queue time per runner" metric either, so it's proxied as the average
`queued_duration` of jobs that ran on each runner (from the raw job rows
`jobs.py` collects across all projects).

This org runs on GitLab SaaS but executes jobs on its own self-managed,
on-prem runners rather than GitLab's shared/hosted fleet. Two runner-listing
endpoints exist, with different scope and different permission floors:

- `group.runners.list()` (`GET /groups/:id/runners`) -- runners registered
  to the group, its ancestor groups, and any GitLab-hosted shared runner
  made available to it. Requires Owner/Auditor on the group, which most
  scanning tokens won't have (Reporter+ is the norm here) -- expect a 403.
- `project.runners.list()` (`GET /projects/:id/runners`) -- everything the
  group call would return, *plus* runners registered directly to that
  project (`runner_type` == "project_type"). Requires Maintainer+ on the
  project. Since it's a superset, this is the one that actually matters;
  the group call is attempted for completeness but its failure is expected
  and non-fatal.

So this module sweeps `project.runners.list()` across every project in the
group and merges the results with whatever the group call managed to add
(deduped by runner_id). Projects where the token lacks Maintainer+ are
skipped and counted, not retried into the ground.

GitLab-hosted shared runners (`runner_type` == "instance_type") are
excluded from the merged result by default -- an `instance_type` row going
offline is GitLab's outage, not ours. Set `GITLAB_INCLUDE_SHARED_RUNNERS=true`
to include them anyway (e.g. to confirm SaaS shared runners are reachable).
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import gitlab

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries

INCLUDE_SHARED_RUNNERS = os.environ.get("GITLAB_INCLUDE_SHARED_RUNNERS", "false").lower() == "true"


def fetch_runner_metrics(gl, group, group_projects: list, raw_job_rows: list[dict], max_workers: int = 10) -> list[dict]:
    """Return one row per on-prem runner visible at the group or project level.

    Merges `group.runners.list()` with a per-project `project.runners.list()`
    sweep -- see module docstring for why the group call alone can miss
    project-registered runners. GitLab-hosted shared runners are dropped by
    default before the per-runner detail call, both because they aren't
    infrastructure we operate and to save an API call per shared runner.
    """
    runners_by_id: dict[int, object] = {}

    try:
        for runner in _call_with_retries(group.runners.list, get_all=True):
            runners_by_id[runner.id] = runner
    except gitlab.exceptions.GitlabListError as exc:
        if exc.response_code == 403:
            # Expected for a Reporter-level token -- group.runners.list() needs
            # Owner/Auditor on the group. Not a problem: the per-project sweep
            # below already includes runners inherited from ancestor groups.
            print("Group-level runner listing needs Owner/Auditor on the group (403) "
                  "-- continuing with the per-project sweep, which covers the same runners.")
        else:
            print(f"Could not list group runners: {exc}")
    except RETRYABLE_EXCEPTIONS as exc:
        print(f"Could not list group runners: {exc}")

    def _project_runners(group_project):
        project = gl.projects.get(group_project.id, lazy=True)
        return _call_with_retries(project.runners.list, get_all=True)

    denied, other_errors = 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_project = {pool.submit(_project_runners, p): p for p in group_projects}
        for future in as_completed(future_to_project):
            try:
                for runner in future.result():
                    runners_by_id[runner.id] = runner
            except gitlab.exceptions.GitlabListError as exc:
                if exc.response_code == 403:
                    denied += 1
                else:
                    other_errors += 1
            except RETRYABLE_EXCEPTIONS:
                other_errors += 1

    if denied:
        print(f"Skipped project-level runner listing for {denied} project(s): token lacks Maintainer+ role there.")
    if other_errors:
        print(f"Could not list runners for {other_errors} project(s) (transient error).")

    runners = list(runners_by_id.values())

    if not INCLUDE_SHARED_RUNNERS:
        before = len(runners)
        runners = [r for r in runners if getattr(r, "runner_type", None) != "instance_type"]
        excluded = before - len(runners)
        if excluded:
            print(
                f"Excluding {excluded} GitLab-hosted shared runner(s) (instance_type); "
                "set GITLAB_INCLUDE_SHARED_RUNNERS=true to include them."
            )

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
            "is_shared": getattr(runner, "is_shared", None),
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
