"""
GitLab Skill-Profile Builder
=============================
Pulls commit, merge-request, and issue data from GitLab (project or group level),
extracts per-user activity against a configurable "skill taxonomy" (mapping file
paths / labels to topics like "Terraform/GCP", "GitLab CI", "Kubernetes", etc.),
and builds a user x topic skill matrix you can use to:
  1. Rank the most knowledgeable person for a given topic (ticket routing)
  2. Cluster users into skill archetypes (KMeans) for team analysis
 
Requirements:
    pip install requests pandas numpy scikit-learn

Usage:
    python gitlab_skill_profiler.py

Configure GITLAB_URL, GITLAB_TOKEN, and either PROJECT_IDS or GROUP_ID below,
plus the SKILL_TAXONOMY regex map to match your stack.
"""

import os
import re
import time
import math
from datetime import datetime, timezone
from collections import defaultdict
from urllib.parse import quote

import requests
import pandas as pd
import numpy as np

try:
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize
except ImportError:
    raise ImportError("scikit-learn is required. Install with: pip install scikit-learn")

from dotenv import load_dotenv
import gitlab
from gitlab.exceptions import GitlabError
from gitlab_orion.client import DEFAULT_GROUP, get_group, list_group_projects, get_gitlab_client, test_connection

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
 
GITLAB_URL = "https://gitlab.com"          # or your self-hosted instance
GITLAB_TOKEN = "YOUR_PERSONAL_ACCESS_TOKEN"  # needs api + read_repository scopes
GROUP_ID = None            # e.g. 123456 -> pulls all projects under a group
PROJECT_IDS = []           # or explicit project IDs, e.g. [111, 222]
SINCE_DAYS = 365           # how far back to look
HALF_LIFE_DAYS = 90        # recency decay half-life for weighting activity

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GROUP_ID = "audacy-inc/devops/"
PROJECT_IDS =[]

# Map topics -> regex patterns matched against file paths (case-insensitive).
# Extend this to match your actual repo layout / tech stack.
SKILL_TAXONOMY = {
    "Terraform/GCP": [r"\.tf$", r"terraform/", r"modules/gcp", r"\.tfvars$"],
    "Terraform/AWS": [r"terraform/aws", r"modules/aws"],
    "GitLab CI": [r"\.gitlab-ci\.yml$", r"ci/.*\.yml$", r"\.gitlab/"],
    "Kubernetes": [r"\.ya?ml$.*k8s", r"k8s/", r"helm/", r"charts/", r"kustomize/"],
    "GitOps": [r"argocd/", r"flux/", r"gitops/"],
    "Python": [r"\.py$"],
    "Go": [r"\.go$"],
    "Docker": [r"Dockerfile", r"docker-compose"],
    "Docs": [r"\.md$", r"docs/"],
}
COMPILED_TAXONOMY = {
    topic: [re.compile(p, re.IGNORECASE) for p in patterns]
    for topic, patterns in SKILL_TAXONOMY.items()
}
 
# Weight given to different kinds of evidence when building the skill score.
WEIGHTS = {
    "commit": 1.0,
    "mr_authored": 1.5,   # you wrote and got a change merged
    "mr_approved": 2.5,   # you reviewed and vouched for it — strong expertise signal
    "issue_resolved": 2.0,
}
 
 
# ---------------------------------------------------------------------------
# GitLab API client
# ---------------------------------------------------------------------------
 
class GitLabClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.headers = {"PRIVATE-TOKEN": token}
 
    def _get(self, endpoint, params=None):
        resp = requests.get(f"{self.base_url}{endpoint}", headers=self.headers, params=params)
        resp.raise_for_status()
        return resp
 
    def _paginate(self, endpoint, params=None):
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        out = []
        while True:
            params["page"] = page
            try:
                resp = self._get(endpoint, params)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (404, 403):
                    # Empty repository or a disabled feature (repo/issues/MRs) on
                    # this particular project -- skip it, don't abort the whole run.
                    print(f"  Skipping {endpoint}: HTTP {status}")
                    break
                raise
            data = resp.json()
            if not data:
                break
            out.extend(data)
            next_page = resp.headers.get("x-next-page")
            if not next_page:
                break
            page = int(next_page)
            time.sleep(0.05)  # gentle on rate limits
        return out
 
    def get_group_projects(self, group_id):
        encoded_group_id = quote(str(group_id).strip("/"), safe="")
        return self._paginate(f"/groups/{encoded_group_id}/projects", {"include_subgroups": "true", "archived": "false"})
 
    def get_commits(self, project_id, since_iso):
        return self._paginate(
            f"/projects/{project_id}/repository/commits",
            {"with_stats": "true", "since": since_iso},
        )
 
    def get_commit_diff(self, project_id, sha):
        try:
            return self._get(f"/projects/{project_id}/repository/commits/{sha}/diff").json()
        except requests.HTTPError:
            return []
 
    def get_merge_requests(self, project_id, since_iso):
        return self._paginate(
            f"/projects/{project_id}/merge_requests",
            {"state": "merged", "updated_after": since_iso},
        )
 
    def get_mr_approvals(self, project_id, mr_iid):
        try:
            return self._get(f"/projects/{project_id}/merge_requests/{mr_iid}/approvals").json()
        except requests.HTTPError:
            return {}
 
    def get_mr_changes(self, project_id, mr_iid):
        try:
            return self._get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes").json()
        except requests.HTTPError:
            return {}
 
    def get_issues(self, project_id, since_iso):
        return self._paginate(
            f"/projects/{project_id}/issues",
            {"state": "closed", "updated_after": since_iso},
        )
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def match_topics(file_path):
    """Return the list of taxonomy topics a given file path matches."""
    hits = []
    for topic, patterns in COMPILED_TAXONOMY.items():
        if any(p.search(file_path) for p in patterns):
            hits.append(topic)
    return hits
 
 
def recency_weight(event_date, now, half_life_days):
    """Exponential decay: activity gets less weight the older it is."""
    age_days = (now - event_date).days
    if age_days < 0:
        age_days = 0
    return 0.5 ** (age_days / half_life_days)
 
 
def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
 
 
# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------
 
def extract_project_features(client, project_id, since_iso, now, scores):
    """
    Populates `scores[user][topic] += weighted_value` in place, from:
      - commits (weighted by files changed matching each topic)
      - merge requests (author gets credit; approvers get stronger credit)
      - issues (assignee gets credit per label-derived topic)
    """
    # --- Commits ---
    commits = client.get_commits(project_id, since_iso)
    for c in commits:
        author = c.get("author_name") or c.get("author_email")
        if not author:
            continue
        event_date = parse_dt(c["created_at"])
        w = recency_weight(event_date, now, HALF_LIFE_DAYS) * WEIGHTS["commit"]
        diff = client.get_commit_diff(project_id, c["id"])
        topics_seen = set()
        for d in diff:
            for path in filter(None, [d.get("new_path"), d.get("old_path")]):
                topics_seen.update(match_topics(path))
        for topic in topics_seen:
            scores[author][topic] += w
 
    # --- Merge requests (authored + approved) ---
    mrs = client.get_merge_requests(project_id, since_iso)
    for mr in mrs:
        merged_at = mr.get("merged_at") or mr.get("updated_at")
        if not merged_at:
            continue
        event_date = parse_dt(merged_at)
        decay = recency_weight(event_date, now, HALF_LIFE_DAYS)
 
        changes = client.get_mr_changes(project_id, mr["iid"])
        topics_seen = set()
        for ch in changes.get("changes", []):
            for path in filter(None, [ch.get("new_path"), ch.get("old_path")]):
                topics_seen.update(match_topics(path))
        if not topics_seen:
            continue
 
        author = mr.get("author", {}).get("name")
        if author:
            for topic in topics_seen:
                scores[author][topic] += decay * WEIGHTS["mr_authored"]
 
        approvals = client.get_mr_approvals(project_id, mr["iid"])
        for approver in approvals.get("approved_by", []):
            name = approver.get("user", {}).get("name")
            if not name:
                continue
            for topic in topics_seen:
                scores[name][topic] += decay * WEIGHTS["mr_approved"]
 
    # --- Issues (assignee gets credit for label-based topics) ---
    issues = client.get_issues(project_id, since_iso)
    for issue in issues:
        closed_at = issue.get("closed_at") or issue.get("updated_at")
        if not closed_at:
            continue
        event_date = parse_dt(closed_at)
        decay = recency_weight(event_date, now, HALF_LIFE_DAYS)
 
        # Map labels to topics directly if label name matches a taxonomy key
        # (case-insensitive substring match), e.g. label "terraform" -> "Terraform/GCP"
        label_topics = set()
        for label in issue.get("labels", []):
            for topic in SKILL_TAXONOMY:
                if topic.split("/")[0].lower() in label.lower():
                    label_topics.add(topic)
        if not label_topics:
            continue
 
        for assignee in issue.get("assignees", []):
            name = assignee.get("name")
            if not name:
                continue
            for topic in label_topics:
                scores[name][topic] += decay * WEIGHTS["issue_resolved"]
 
 
# ---------------------------------------------------------------------------
# Build skill matrix
# ---------------------------------------------------------------------------
 
def build_skill_matrix(client, project_ids, since_days):
    now = datetime.now(timezone.utc)
    since_iso = (now - pd.Timedelta(days=since_days)).isoformat()
 
    scores = defaultdict(lambda: defaultdict(float))
    for pid in project_ids:
        print(f"Processing project {pid} ...")
        try:
            extract_project_features(client, pid, since_iso, now, scores)
        except requests.HTTPError as exc:
            print(f"  Skipping project {pid}: {exc}")
 
    df = pd.DataFrame(scores).T.fillna(0.0)
    df = df.reindex(sorted(df.columns), axis=1)
    return df
 
 
def cluster_users(skill_df, n_clusters=4):
    """KMeans over L2-normalized skill vectors to find skill archetypes."""
    if skill_df.empty or skill_df.shape[0] < n_clusters:
        return skill_df.assign(cluster=-1)
    normalized = normalize(skill_df.values)
    km = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    labels = km.fit_predict(normalized)
    result = skill_df.copy()
    result["cluster"] = labels
    return result
 
 
def recommend_owner(skill_df, topic, top_n=3):
    """Return the top_n most knowledgeable people for a given topic."""
    if topic not in skill_df.columns:
        return []
    ranked = skill_df[topic].sort_values(ascending=False)
    return list(ranked.head(top_n).items())
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    client = GitLabClient(GITLAB_URL, GITLAB_TOKEN)
 
    project_ids = list(PROJECT_IDS)
    if GROUP_ID:
        projects = client.get_group_projects(GROUP_ID)
        project_ids += [p["id"] for p in projects]
 
    if not project_ids:
        raise SystemExit("Set PROJECT_IDS or GROUP_ID in the config section.")
 
    skill_df = build_skill_matrix(client, project_ids, SINCE_DAYS)
    skill_df.to_csv("skill_matrix.csv")
    print("\nSkill matrix (raw scores):")
    print(skill_df)
 
    clustered = cluster_users(skill_df, n_clusters=min(4, max(1, len(skill_df) // 2)))
    clustered.to_csv("skill_clusters.csv")
    print("\nUser clusters:")
    print(clustered["cluster"])
 
    print("\nExample routing recommendation for 'Terraform/GCP':")
    for name, score in recommend_owner(skill_df, "Terraform/GCP"):
        print(f"  {name}: {score:.2f}")
 