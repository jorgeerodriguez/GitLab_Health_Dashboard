"""
GitLab IAM Report
=================
Finds every merge request that changed GCP IAM in the Terragrunt repos under
one or more GitLab groups, reads each MR's diff, and extracts the individual
grant changes (role, member, added/removed) plus per-MR delivery metrics
(lead time, review comments, closed without merging, Jira key).

Outputs (under reports/iam/, which is git-ignored):
  iam_grants_<date>.csv   one row per role/member change found in a diff
  iam_mrs_<date>.csv      one row per IAM merge request, with friction metrics
  iam_dashboard.html      self-contained HTML dashboard built from the two CSVs
  iam_diff_cache.json     diffs of merged/closed MRs, so re-runs are fast

Usage:
  python GitLab_IAM_Report.py                          # audacy-inc/gcp, this year
  python GitLab_IAM_Report.py --groups audacy-inc/gcp --since 2026-01-01
  python GitLab_IAM_Report.py --refresh-cache          # ignore cached diffs
  python GitLab_IAM_Report.py --dashboard-only         # rebuild the HTML from the latest CSVs

The parser reads diff text, not Terraform state: it sees what an MR changed,
not what is applied. A member added inside a long role list whose role line is
outside the diff's context is reported with role "(outside diff context)".
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from gitlab_pulse.iam_dashboard import build_iam_dashboard_html
from gitlab_pulse.client import _call_with_retries, get_gitlab_client, get_group, list_group_projects, test_connection

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_GROUPS = ["audacy-inc/gcp"]
OUT_DIR = Path("reports/iam")
CACHE_FILE = OUT_DIR / "iam_diff_cache.json"
DASHBOARD_FILE = OUT_DIR / "iam_dashboard.html"
WORKERS = 8

# A file is IAM-relevant if its path or its changed lines match these.
IAM_PATH_RE = re.compile(r"(^|/)iam(/|$)|[-_]iam\b|bindings\.ya?ml$|service-accounts?/|workload-identity", re.I)
IAM_LINE_RE = re.compile(r"roles/|google_\w+_iam_|(user|group|serviceAccount|principalSet|principal|domain):", re.I)

ROLE_RE = re.compile(r"((?:roles|(?:projects|organizations)/[^\"\s/]+/roles)/[\w.]+)")
MEMBER_RE = re.compile(
    r"((?:deleted:)?(?:user|group|serviceAccount|domain|principalSet|principal):[^\"\s,\]]+)"
)
SA_EMAIL_RE = re.compile(r"([\w.${}-]+@[\w.${}-]+\.iam\.gserviceaccount\.com)")
DEP_REF_RE = re.compile(r"dependency\.([\w-]+)\.outputs\.(\w+)")
# "<member>" = [ "roles/a", "roles/b" ]  (member -> roles map, e.g. landing zone)
MEMBER_KEY_RE = re.compile(r"^\s*\"" + MEMBER_RE.pattern + r"\"\s*=\s*\[")
# name = "sa-x" / names = ["sa-x"] in a terraform-google-service-accounts unit
SA_NAME_RE = re.compile(r"^\s*names?\s*=\s*\[?\s*\"([a-z][\w-]{4,})\"")
BLOCK_END_RE = re.compile(r"^\s*[\]}]\s*,?\s*$")
JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
ENV_TOKENS = ("dev", "qa", "uat", "stg", "prod", "nonprod", "shd", "shared", "ops", "sandbox")

# Rough risk tiers to spot grants that could be auto-approved vs need review.
HIGH_RISK_RE = re.compile(
    r"roles/(owner|editor|iam\.|resourcemanager\.\w*(iam)?admin|.*\.admin$|"
    r"iam\.serviceAccount(User|TokenCreator|KeyAdmin)|iam\.workloadIdentityUser)",
    re.I,
)
LOW_RISK_RE = re.compile(r"[./](viewer|reader|browser|.*[Vv]iewer|metadataViewer|dataViewer|subscriber)$")


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

def member_type(member: str) -> str:
    if member.startswith("dependency."):
        return "terraform_reference"
    kind, _, ident = member.partition(":")
    kind = kind.replace("deleted:", "")
    if kind == "serviceAccount":
        if re.search(r"^service-\d+@|gcp-sa-|@cloudservices|developer\.gserviceaccount", ident):
            return "google_service_agent"
        return "service_account"
    if kind in ("principal", "principalSet"):
        return "workload_identity"
    return kind  # user | group | domain


def risk_tier(role: str) -> str:
    if LOW_RISK_RE.search(role):  # before HIGH: roles/iam.serviceAccountViewer is a viewer
        return "low"
    if HIGH_RISK_RE.search(role):
        return "high"
    return "medium"


def parse_iam_diff(diff: str) -> list[dict]:
    """Return grant-change events found in one file's unified diff.

    Tracks the most recent role seen (on context or changed lines) so that a
    bare `+ "group:x@y",` line is attributed to the role block it sits in, and
    the most recent member key so roles listed under a member get that member.
    """
    events, role_lines = [], []
    current_role = current_member = None
    mock_depth = 0
    new_sa = None
    if "service-accounts" in diff:
        names = [m.group(1) for l in diff.splitlines() if (m := SA_NAME_RE.match(l[1:]))]
        new_sa = f"serviceAccount:{names[0]} (created in this unit)" if names else None

    for raw in diff.splitlines():
        if raw.startswith("@@"):
            current_role = current_member = None
            mock_depth = 0
            continue
        if not raw or raw[0] not in " +-":
            continue
        sign, line = raw[0], raw[1:]

        # Skip terragrunt mock_outputs blocks: they hold fake identities.
        if mock_depth or "mock_outputs" in line and "{" in line:
            mock_depth += line.count("{") - line.count("}")
            mock_depth = max(mock_depth, 0)
            continue

        key = MEMBER_KEY_RE.match(line)
        if key:  # opens a member -> roles block; the key line itself is not a grant
            current_member, current_role = key.group(1), None
            continue

        roles = ROLE_RE.findall(line)
        if roles:
            current_role = roles[-1]

        members = MEMBER_RE.findall(line)
        if not members:
            members = [f"serviceAccount:{e}" for e in SA_EMAIL_RE.findall(line)]
        if not members and current_role:
            members = [f"dependency.{d}.{o}" for d, o in DEP_REF_RE.findall(line)]

        if sign in "+-":
            action = "added" if sign == "+" else "removed"
            for m in members:
                events.append({"action": action, "role": current_role or "(outside diff context)", "member": m})
            if roles and not members:
                owner = current_member or new_sa
                if owner:
                    events.extend({"action": action, "role": r, "member": owner} for r in roles)
                else:
                    role_lines.extend({"action": action, "role": r} for r in roles)

        if BLOCK_END_RE.match(line):
            current_role = current_member = None

    # Role lines whose members appear in this diff are already covered; keep
    # the rest (a role added to a principal defined elsewhere in the file).
    covered = {(e["action"], e["role"]) for e in events}
    for rl in role_lines:
        if (rl["action"], rl["role"]) not in covered:
            events.append({**rl, "member": "(not in diff)"})
            covered.add((rl["action"], rl["role"]))

    # A removed and re-added identical grant is an edit (comma, indent, move),
    # not an access change.
    net = []
    for e in events:
        opposite = {**e, "action": "removed" if e["action"] == "added" else "added"}
        if opposite in net:
            net.remove(opposite)
        else:
            net.append(e)
    return net


def is_iam_file(change: dict) -> bool:
    path = change.get("new_path") or change.get("old_path") or ""
    changed = "\n".join(l for l in change.get("diff", "").splitlines() if l[:1] in "+-")
    return bool(IAM_PATH_RE.search(path) or IAM_LINE_RE.search(changed))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def repo_env(repo: str) -> str:
    tokens = repo.lower().split("-")
    for t in reversed(tokens):
        if t in ENV_TOKENS:
            return t
    return "n/a"


def load_cache(refresh: bool) -> dict:
    if refresh or not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_iam_changes(gl, project_id: int, iid: int) -> list[dict]:
    """Return only the IAM-relevant file diffs of one MR."""
    diffs = _call_with_retries(
        gl.http_list, f"/projects/{project_id}/merge_requests/{iid}/diffs", get_all=True
    )
    return [
        {"path": d.get("new_path") or d.get("old_path"), "diff": d.get("diff", ""),
         "too_large": bool(d.get("too_large") or d.get("collapsed"))}
        for d in diffs if is_iam_file(d)
    ]


def collect(gl, group_paths: list[str], since: str, refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    cache = load_cache(refresh)
    projects = []
    for gp in group_paths:
        projects += list_group_projects(gl, get_group(gl, gp.strip("/")))
    print(f"Listing merge requests created since {since} in {len(projects)} projects...")

    def list_mrs(p):
        proj = gl.projects.get(p.id, lazy=True)
        mrs = _call_with_retries(proj.mergerequests.list, created_after=since, state="all", get_all=True)
        return p, mrs

    all_mrs = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for fut in as_completed(pool.submit(list_mrs, p) for p in projects):
            p, mrs = fut.result()
            all_mrs += [(p, mr) for mr in mrs]
    print(f"Found {len(all_mrs)} merge requests. Reading diffs for IAM changes...")

    def changes_for(p, mr):
        key = f"{p.id}:{mr.iid}"
        if key in cache:
            return key, cache[key]
        return key, fetch_iam_changes(gl, p.id, mr.iid)

    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(changes_for, p, mr): (p, mr) for p, mr in all_mrs}
        for n, fut in enumerate(as_completed(futs), 1):
            key, changes = fut.result()
            results[key] = changes
            if futs[fut][1].state in ("merged", "closed"):
                cache[key] = changes
            if n % 250 == 0:
                print(f"  {n}/{len(all_mrs)} MRs read")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache))

    grant_rows, mr_rows = [], []
    for p, mr in all_mrs:
        changes = results.get(f"{p.id}:{mr.iid}") or []
        if not changes:
            continue
        repo = p.path_with_namespace
        jira = JIRA_RE.findall(f"{mr.title} {mr.source_branch}".upper())
        created = pd.Timestamp(mr.created_at)
        merged = pd.Timestamp(mr.merged_at) if mr.merged_at else None
        events = [dict(e, file=c["path"]) for c in changes for e in parse_iam_diff(c["diff"])]

        mr_rows.append({
            "repo": repo.split("/")[-1],
            "repo_path": repo,
            "env": repo_env(repo.split("/")[-1]),
            "iid": mr.iid,
            "title": mr.title,
            "state": mr.state,
            "draft": getattr(mr, "draft", False),
            "author": mr.author["username"],
            "merged_by": (mr.merged_by or {}).get("username", "") if hasattr(mr, "merged_by") else "",
            "labels": ";".join(mr.labels),
            "jira_key": jira[0] if jira else "",
            "created_at": created,
            "merged_at": merged,
            "lead_time_hours": round((merged - created).total_seconds() / 3600, 1) if merged is not None else None,
            "review_comments": mr.user_notes_count,
            "iam_files": len(changes),
            "grant_changes": len(events),
            "has_unparsed_large_diff": any(c["too_large"] for c in changes),
            "url": mr.web_url,
        })
        for e in events:
            grant_rows.append({
                "repo": repo.split("/")[-1],
                "env": repo_env(repo.split("/")[-1]),
                "iid": mr.iid,
                "jira_key": jira[0] if jira else "",
                "state": mr.state,
                "created_at": created,
                "merged_at": merged,
                "author": mr.author["username"],
                "action": e["action"],
                "role": e["role"],
                "risk_tier": risk_tier(e["role"]) if e["role"].startswith(("roles/", "projects/", "organizations/")) else "unknown",
                "member": e["member"],
                "member_type": member_type(e["member"]) if ":" in e["member"] or e["member"].startswith("dependency.") else "unknown",
                "file": e["file"],
                "url": mr.web_url,
            })

    return pd.DataFrame(mr_rows), pd.DataFrame(grant_rows)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(mrs: pd.DataFrame, grants: pd.DataFrame) -> None:
    if mrs.empty:
        print("No IAM merge requests found.")
        return
    merged = mrs[mrs.state == "merged"]
    print("\n=== IAM merge requests ===")
    print(f"Total: {len(mrs)}  " + "  ".join(f"{k}: {v}" for k, v in mrs.state.value_counts().items()))
    print(f"Closed without merging: {(mrs.state == 'closed').mean():.0%}")
    if not merged.empty:
        lt = merged.lead_time_hours
        print(f"Lead time to merge (hours): median {lt.median():.1f}, p75 {lt.quantile(.75):.1f}, p90 {lt.quantile(.9):.1f}")
    print(f"MRs with a large diff GitLab would not return: {mrs.has_unparsed_large_diff.sum()}")

    print("\nTop repos by IAM MRs:")
    by_repo = mrs.groupby("repo").agg(mrs=("iid", "count"), median_lead_h=("lead_time_hours", "median"))
    print(by_repo.sort_values("mrs", ascending=False).head(15).to_string())

    multi_env = mrs[mrs.jira_key != ""].groupby("jira_key").repo.nunique()
    print(f"\nJira keys whose IAM change spans more than one repo/env: {(multi_env > 1).sum()} "
          f"of {len(multi_env)}")

    if grants.empty:
        return
    added = grants[grants.action == "added"]
    print(f"\n=== Grant changes ===\nTotal: {len(grants)}  added: {len(added)}  removed: {(grants.action == 'removed').sum()}")
    print(f"Role outside diff context: {(grants.role == '(outside diff context)').sum()}")
    print("\nMember types (added):")
    print(added.member_type.value_counts().to_string())
    print("\nRisk tiers (added):")
    print(added.risk_tier.value_counts().to_string())
    print("\nMost-granted roles:")
    print(added.role.value_counts().head(20).to_string())
    combo = added.groupby(["role", "member_type"]).size().sort_values(ascending=False).head(15)
    print("\nMost repeated role + member type (self-service candidates):")
    print(combo.to_string())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report GCP IAM changes made through GitLab merge requests.")
    parser.add_argument("--groups", default=",".join(DEFAULT_GROUPS), help="comma-separated group paths")
    parser.add_argument("--since", default=f"{datetime.now(timezone.utc).year}-01-01", help="YYYY-MM-DD, MR creation date")
    parser.add_argument("--refresh-cache", action="store_true", help="re-read diffs instead of using the cache")
    parser.add_argument("--dashboard-only", action="store_true",
                        help="skip GitLab; rebuild the HTML dashboard from the latest CSVs in reports/iam/")
    args = parser.parse_args(argv)

    if args.dashboard_only:
        latest = sorted(OUT_DIR.glob("iam_mrs_*.csv"))
        if not latest:
            sys.exit(f"No iam_mrs_*.csv in {OUT_DIR}. Run without --dashboard-only first.")
        mrs_path = latest[-1]
        grants_path = mrs_path.with_name(mrs_path.name.replace("iam_mrs_", "iam_grants_"))
        print(f"Building dashboard from {mrs_path} and {grants_path}")
        build_iam_dashboard_html(pd.read_csv(mrs_path), pd.read_csv(grants_path), DASHBOARD_FILE,
                                 args.groups, args.since)
        return

    print("GitLab Pulse IAM report is running...")
    if not test_connection():
        sys.exit("GitLab connection test failed.")

    gl = get_gitlab_client()
    since = f"{args.since}T00:00:00Z"
    mrs, grants = collect(gl, [g for g in args.groups.split(",") if g], since, args.refresh_cache)

    stamp = datetime.now().strftime("%Y-%m-%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mrs_path, grants_path = OUT_DIR / f"iam_mrs_{stamp}.csv", OUT_DIR / f"iam_grants_{stamp}.csv"
    for df in (mrs, grants):
        for col in ("created_at", "merged_at"):
            if col in df:
                df[col] = pd.to_datetime(df[col], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    mrs.to_csv(mrs_path, index=False)
    grants.to_csv(grants_path, index=False)
    print_summary(mrs, grants)
    print(f"\nWrote {mrs_path} ({len(mrs)} rows) and {grants_path} ({len(grants)} rows)")
    build_iam_dashboard_html(mrs, grants, DASHBOARD_FILE, args.groups, args.since)


if __name__ == "__main__":
    main()
