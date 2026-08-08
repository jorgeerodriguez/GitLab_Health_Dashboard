# GitLab Orion

A CI/CD + repo health monitor for a GitLab group: pipeline health, job/stage
failure attribution, runner health, MR/merge health, and repo hygiene — all
landing in tidy pandas DataFrames, persisted as a local history so trends
are real (not synthesized), and rolled up into a self-contained HTML
dashboard for leadership.

## What it does

Each run:

1. Authenticates to GitLab with a Personal Access Token.
2. Lists every active (non-archived) project under a group, including subgroups.
3. For every project, concurrently collects:
   - **Pipeline health** — status, success rate, and the latest run's
     duration/coverage, over the most recent `sample_size` pipelines.
   - **Failure rate by branch** — the same pipeline sample, grouped by ref.
   - **Job/stage detail** — which CI stage (and which job within it) fails
     most, plus average duration and queue time per stage.
   - **MR/merge health** — open MRs, flagged stale (no update in
     `stale_mr_days`) or blocked from merging.
   - **Repo hygiene** — stale branches (no commit in `stale_branch_days`)
     and protected-branch policy drift on the default branch.
4. Once, at the group level: **runner health** (online/offline/stale),
   with per-runner average job queue time.
5. Appends this run's snapshot to a local Parquet history store, so success
   rate / duration / stale-MR trends build up as you run Orion repeatedly.
6. Writes CSV + JSON reports per domain and builds `reports/dashboard.html`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in your GitLab Personal Access Token:

```
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=your-personal-access-token-here
```

To create a token: on GitLab, go to **Avatar → Edit profile → Access Tokens**
(`https://gitlab.com/-/user_settings/personal_access_tokens`). Give it a name,
an expiration date, and the `read_api` scope (`read_repository` too if you'll
need repo contents later). The token needs at least **Reporter** access on
the group to list its runners; **Developer**+ gets you MR/merge-status
detail. Runner listing at the *project* level (needed to catch on-prem
runners registered directly to a project rather than the group — see
`runners.py`) requires **Maintainer**+ on that project; projects where the
token falls short are skipped and counted in the console output rather than
erroring out. Copy the value — GitLab only shows it once.

## Configuration

Set via environment variables (in `.env` or the shell):

| Variable | Default | Purpose |
|---|---|---|
| `GITLAB_URL` | `https://gitlab.com` | GitLab API base URL |
| `GITLAB_TOKEN` | — | Personal Access Token (required) |
| `GITLAB_GROUP` | `audacy-inc` | Group path to scan for projects |
| `GITLAB_INCLUDE_SHARED_RUNNERS` | `false` | Include GitLab-hosted shared runners (`runner_type=instance_type`) in runner health, alongside on-prem/self-managed ones |

Everything else is a Python constant, kept next to the domain it configures
so it's easy to find and edit:

| Constant | File | Default | Meaning |
|---|---|---|---|
| `UNHEALTHY_SUCCESS_RATE_THRESHOLD` | `gitlab_orion/pipelines.py` | `70.0` | Below this % success rate, a project is "unhealthy" |
| `STALE_MR_DAYS` | `gitlab_orion/merge_requests.py` | `14` | Open MR with no update in this many days is "stale" |
| `MR_SAMPLE_SIZE` | `gitlab_orion/merge_requests.py` | `50` | Max open MRs sampled per project (oldest-updated first) |
| `STALE_BRANCH_DAYS` | `gitlab_orion/repo_hygiene.py` | `90` | Branch with no commit in this many days is "stale" |
| `BRANCH_PROTECTION_POLICY` | `gitlab_orion/repo_hygiene.py` | protected, no force-push, code-owner approval required | The policy each project's default branch is compared against — GitLab has no native "policy compliance" concept, so this is a local standard you should tune to your org's actual rules |

`generate_reports()` in `GitLab_Orion.py` also takes `sample_size`,
`job_sample_size`, `max_workers`, and `recent_days` as keyword args if you
want to call it directly instead of via `main()`.

## Usage

```bash
.venv/bin/python GitLab_Orion.py
```

This will verify the connection, scan `GITLAB_GROUP`, print console
summaries, and write to `reports/`:

- `pipelines.csv` / `.json` — every project's pipeline health (see schema below).
- `pipelines_by_branch.csv` / `.json` — pipeline success/failure counts per branch.
- `jobs.csv` / `.json` — failure rate, worst job, avg duration/queue time per stage.
- `runners.csv` / `.json` — one row per group runner.
- `merge_requests.csv` / `.json` — one row per open MR.
- `branches.csv` / `.json` — one row per non-default branch.
- `protection_drift.csv` / `.json` — one row per project's default-branch policy check.
- `history/*.parquet` — the accumulated time-series for each domain above
  (append-only, deduped; this is what powers the trend chart).
- `dashboard.html` — **open this in a browser.** Self-contained (charts are
  base64-embedded PNGs, no internet needed), safe to email or drop in a
  shared drive.

> Previous versions of this script wrote `pipeline_health_detail.csv` /
> `pipeline_health_unhealthy*.csv`. Those filenames are gone — the pipeline
> report is now just `pipelines.csv`, and "unhealthy" is a `category`
> column you filter on (still shown as its own table in the console output).
> Old files from prior runs aren't deleted automatically; remove them from
> `reports/` whenever you like.

Run it on a schedule (cron, CI pipeline schedule, etc.) to build up real
trend history — a single run only ever has one data point.

### DataFrame schemas

Every table shares a `snapshot_at` column (one UTC timestamp per run) so
history rows concatenate cleanly for trend charts.

**`pipelines`** — one row per project

| Field | Meaning |
|---|---|
| `project_path` | Project path with namespace (e.g. `audacy-inc/cobra/radio-api`) |
| `web_url` | Link to the project on GitLab |
| `category` | `healthy`, `unhealthy`, or `no_pipelines` |
| `reason` | Why it was classified that way (blank for healthy) |
| `sampled` | Number of pipelines sampled |
| `success_rate` | % of sampled pipelines that succeeded |
| `last_status` | Status of the most recent pipeline |
| `last_run_at` | Timestamp of the most recent pipeline (ISO 8601, UTC) |
| `last_duration_seconds` | Duration of the most recent pipeline (one extra API call — the list endpoint doesn't return duration) |
| `last_queued_duration_seconds` | Time the most recent pipeline waited before starting |
| `coverage` | Test coverage % reported by the most recent pipeline, if any |
| `error` | API error message, if the project's pipelines couldn't be fetched |

**`pipelines_by_branch`** — one row per (project, branch) within the sample: `ref`, `sampled`, `successes`, `failures`, `success_rate`.

**`jobs`** — one row per (project, stage) within the sample: `stage`, `sampled`, `failures`, `failure_rate`, `top_failing_job`, `avg_duration_seconds`, `avg_queued_duration_seconds`.

**`runners`** — one row per on-prem group runner (GitLab-hosted shared runners are excluded by default; see `GITLAB_INCLUDE_SHARED_RUNNERS`): `runner_id`, `description`, `runner_type` (`group_type`/`project_type`, or `instance_type` if shared runners are included), `is_shared`, `status` (`online`/`offline`/`stale`/`never_contacted`), `paused`, `contacted_at`, `tags`, `avg_queued_duration_seconds` (proxy for queue time, averaged from jobs that ran on it).

**`merge_requests`** — one row per open MR: `mr_iid`, `title`, `source_branch`, `target_branch`, `author`, `created_at`, `updated_at`, `age_days`, `draft`, `detailed_merge_status`, `is_blocked` (anything but `mergeable`), `is_stale`, `web_url`.

**`branches`** — one row per non-default branch: `branch`, `last_commit_at`, `age_days`, `is_stale`, `merged`, `protected`.

**`protection_drift`** — one row per project: `default_branch`, `is_protected`, `allow_force_push`, `code_owner_approval_required`, `violations` (comma-joined list of the policy fields that don't match `BRANCH_PROTECTION_POLICY`).

## Filtering by date

Every datetime column (`last_run_at`, `updated_at`, `contacted_at`,
`snapshot_at`, ...) is a real timezone-aware `pandas.Timestamp` (UTC), not
a string — parsed as soon as it comes back from GitLab, so it can be
compared/filtered directly, and only rendered to an ISO 8601 string at
write time (`reports.py`). `gitlab_orion/pipelines.py` has
`filter_recent_unhealthy(pipelines_df, days=7, now=None)`, used by
`GitLab_Orion.py` to print/report the last-7-days unhealthy subset
alongside the full unhealthy table.

## Architecture

```
gitlab_orion/
    client.py            auth, retries, group/project listing
    pipelines.py         pipeline health + failure rate by branch
    jobs.py               job/stage-level failure attribution
    merge_requests.py     MR/merge health
    repo_hygiene.py       stale branches + protected-branch drift
    runners.py            group runner health (queue time proxied from jobs.py)
    snapshot.py           per-project worker + DataFrame assembly
    history.py            Parquet snapshot store (reports/history/)
    reports.py            CSV/JSON writers
    dashboard.py          matplotlib charts -> self-contained HTML
GitLab_Orion.py           CLI entrypoint / orchestrator
```

`GitLab_Orion.py` still runs one `ThreadPoolExecutor` task per project —
that task now gathers pipelines + jobs + MRs + repo hygiene together
(4 collectors per project, called from `snapshot.collect_project_snapshot`)
rather than four separate full passes over every project. Runners are
group-scoped and collected once, outside that loop.

## Notes

- **API cost**: roughly 5 calls per project per run (pipeline list + 1
  detail call, jobs list, branches list, protected-branches list, MRs
  list), plus one detail call per group runner. For ~190 projects that's
  ~1,000 calls/run — tune `sample_size` / `job_sample_size` /
  `mr_sample_size` / `max_workers` in `generate_reports()` if that's too
  slow or you're hitting rate limits.
- **Runner listing requires only Reporter+ on the group** — it
  deliberately uses `group.runners.list()`, not `gl.runners_all` (which is
  instance-admin-only and will 403 for a normal PAT).
- **`BRANCH_PROTECTION_POLICY` is a local standard, not a GitLab concept**
  — GitLab doesn't have a "compliance" API; drift detection just diffs
  each project's default-branch protection against the constant in
  `repo_hygiene.py`. Edit it to match your org's actual policy.
- Pipeline duration is only fetched for the *latest* pipeline per project
  (the list endpoint doesn't include it, and fetching it for every sampled
  pipeline would multiply the API cost) — duration *trend* comes from the
  history store across repeated runs, not from one big historical pull.
- There is no official or third-party "Orion" MCP server for GitLab — this
  project talks to GitLab directly via its REST API through the
  [python-gitlab](https://python-gitlab.readthedocs.io/) SDK.
- Charts in `dashboard.html` are matplotlib PNGs embedded as base64 data
  URIs — no network/CDN dependency, safe to open offline.
