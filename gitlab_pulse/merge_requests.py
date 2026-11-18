"""MR/merge health: stale open MRs and MRs blocked from merging."""

import pandas as pd

from .client import RETRYABLE_EXCEPTIONS, _call_with_retries

MERGEABLE_STATUS = "mergeable"
STALE_MR_DAYS = 14
MR_SAMPLE_SIZE = 50


def fetch_merge_request_metrics(project, now, stale_days: int = STALE_MR_DAYS, sample_size: int = MR_SAMPLE_SIZE) -> list[dict]:
    """Return one row per open MR, oldest-updated first (capped at sample_size).

    Sorting by updated_at ascending means that when a project has more open
    MRs than `sample_size`, the ones that get dropped are the freshest —
    the stale/blocked ones we actually care about survive the cap.
    """
    try:
        mrs = _call_with_retries(
            project.mergerequests.list,
            state="opened",
            order_by="updated_at",
            sort="asc",
            per_page=sample_size,
            get_all=False,
        )
    except RETRYABLE_EXCEPTIONS:
        return []

    rows = []
    for mr in mrs:
        updated_at = pd.to_datetime(mr.updated_at, utc=True)
        age_days = (now - updated_at).days
        draft = bool(getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False))
        detailed_status = getattr(mr, "detailed_merge_status", None)
        is_blocked = detailed_status is not None and detailed_status != MERGEABLE_STATUS
        author = getattr(mr, "author", None) or {}

        rows.append({
            "mr_iid": mr.iid,
            "title": mr.title,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "author": author.get("username"),
            "created_at": pd.to_datetime(mr.created_at, utc=True),
            "updated_at": updated_at,
            "age_days": age_days,
            "draft": draft,
            "detailed_merge_status": detailed_status,
            "is_blocked": is_blocked,
            "is_stale": (not draft) and age_days >= stale_days,
            "web_url": mr.web_url,
        })
    return rows
