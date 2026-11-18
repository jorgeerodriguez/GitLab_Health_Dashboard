"""Repo hygiene: stale branches and protected-branch policy drift.

GitLab has no built-in "policy compliance" concept, so drift is modeled
here as a comparison against a configurable policy for the *default*
branch — edit BRANCH_PROTECTION_POLICY below to match your org's actual
branch-protection standard.
"""

import pandas as pd

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries

STALE_BRANCH_DAYS = 90

BRANCH_PROTECTION_POLICY = {
    "is_protected": True,
    "allow_force_push": False,
    "code_owner_approval_required": True,
}


def fetch_branch_metrics(project, now, stale_days: int = STALE_BRANCH_DAYS) -> tuple[list[dict], dict]:
    """Return (branch_rows, protection_row) for a project.

    branch_rows covers every non-default branch (staleness by last commit).
    protection_row compares the default branch's protection settings
    against BRANCH_PROTECTION_POLICY.
    """
    try:
        branches = _call_with_retries(project.branches.list, get_all=True)
    except RETRYABLE_EXCEPTIONS:
        return [], {"default_branch": None, "is_protected": None, "allow_force_push": None,
                     "code_owner_approval_required": None, "violations": "branches unavailable"}

    default_branch_name = next((b.name for b in branches if getattr(b, "default", False)), None)

    branch_rows = []
    for b in branches:
        if getattr(b, "default", False):
            continue
        commit = getattr(b, "commit", None) or {}
        committed_date = commit.get("committed_date")
        last_commit_at = pd.to_datetime(committed_date, utc=True) if committed_date else None
        age_days = (now - last_commit_at).days if last_commit_at is not None else None

        branch_rows.append({
            "branch": b.name,
            "last_commit_at": last_commit_at,
            "age_days": age_days,
            "is_stale": age_days is not None and age_days >= stale_days,
            "merged": getattr(b, "merged", None),
            "protected": getattr(b, "protected", None),
        })

    protection_row = _protection_row(project, default_branch_name)
    return branch_rows, protection_row


def _protection_row(project, default_branch_name: str | None) -> dict:
    if default_branch_name is None:
        return {"default_branch": None, "is_protected": None, "allow_force_push": None,
                "code_owner_approval_required": None, "violations": "no default branch found"}

    try:
        protected = _call_with_retries(project.protectedbranches.list, get_all=True)
    except RETRYABLE_EXCEPTIONS:
        return {"default_branch": default_branch_name, "is_protected": None, "allow_force_push": None,
                "code_owner_approval_required": None, "violations": "protected-branch rules unavailable"}

    rule = next((p for p in protected if p.name == default_branch_name), None)

    is_protected = rule is not None
    allow_force_push = getattr(rule, "allow_force_push", None) if rule else None
    code_owner_approval_required = getattr(rule, "code_owner_approval_required", None) if rule else None

    actual = {
        "is_protected": is_protected,
        "allow_force_push": allow_force_push,
        "code_owner_approval_required": code_owner_approval_required,
    }
    violations = [
        rule_name for rule_name, expected in BRANCH_PROTECTION_POLICY.items()
        if actual[rule_name] != expected
    ]

    return {
        "default_branch": default_branch_name,
        "is_protected": is_protected,
        "allow_force_push": allow_force_push,
        "code_owner_approval_required": code_owner_approval_required,
        "violations": ", ".join(violations) if violations else None,
    }
