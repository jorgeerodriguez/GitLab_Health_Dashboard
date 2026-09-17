#!/usr/bin/env python3
"""
gitlab_sme_finder.py - Identify Subject Matter Experts (SMEs) from GitLab activity.
 
How it works
------------
1. COLLECT  - Pulls contribution signals from the GitLab REST API (v4) for a group
              or a list of projects and caches them as JSON:
                * commits (with lines changed, and optionally file paths)
                * merged merge requests authored (+ labels, optionally file paths)
                * MR reviews: approvals and non-trivial review comments
                * issues closed / resolved (assignees)
                * CODEOWNERS entries
                * project languages and topics
2. SCORE    - Each signal gets a weight and an exponential recency decay
              (default half-life 180 days), so recent, deep, reviewed work
              counts more than old drive-by commits. Scores are aggregated per
              person for: project, topic (dirs, languages, labels, project topics).
3. RECOMMEND- Ask "who is the SME for project X / topic Y?" and get a ranked list
              with an explanation of *why* each person is recommended, plus a
              bus-factor warning when knowledge is concentrated in one person.
 
Usage
-----
  export GITLAB_URL=https://gitlab.example.com
  export GITLAB_TOKEN=glpat-xxxx            # read_api scope is enough
 
  # 1) collect (cache to JSON)
  python gitlab_sme_finder.py collect --group platform-eng --since-days 365 --deep
  python gitlab_sme_finder.py collect --projects 123,infra/terraform-gcp
 
  # 2) query SMEs
  python gitlab_sme_finder.py sme --project infra/terraform-gcp
  python gitlab_sme_finder.py sme --topic terraform --topic gke --top 5
  python gitlab_sme_finder.py sme --keyword "helm" --format markdown
 
  # 3) full report of every project + topic
  python gitlab_sme_finder.py report --out sme_report.md
  python gitlab_sme_finder.py report --format csv --out sme_report.csv
 
  # try it without GitLab
  python gitlab_sme_finder.py demo
 
Requires: Python 3.9+, requests
"""
from __future__ import annotations
 
import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import quote
 
from dotenv import load_dotenv
import gitlab
from gitlab.exceptions import GitlabError
from gitlab_orion.client import DEFAULT_GROUP, get_group, list_group_projects, get_gitlab_client, test_connection

load_dotenv()

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
 
# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
 
DEFAULT_WEIGHTS = {
    "commit": 1.0,          # per commit
    "commit_lines": 0.002,  # per line added+deleted (capped per commit)
    "mr_merged": 5.0,       # authored and merged MR
    "mr_review": 3.0,       # approved someone else's MR
    "mr_comment": 0.5,      # review comment on someone else's MR (capped per MR)
    "issue_closed": 2.0,    # assignee on a closed issue
    "codeowner": 10.0,      # listed in CODEOWNERS (no decay)
}
 
BOT_PATTERNS = re.compile(
    r"(bot|renovate|dependabot|gitlab-ci|project_\d+_bot|group_\d+_bot|ghost|automation)",
    re.I,
)
 
EXT_LANG = {
    ".py": "python", ".go": "go", ".tf": "terraform", ".tfvars": "terraform",
    ".hcl": "terraform", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".java": "java", ".kt": "kotlin", ".rb": "ruby",
    ".rs": "rust", ".cs": "csharp", ".sh": "shell", ".bash": "shell",
    ".sql": "sql", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".md": "docs", ".rego": "opa", ".php": "php", ".scala": "scala",
    ".ipynb": "jupyter", "Dockerfile": "docker", ".gitlab-ci.yml": "gitlab-ci",
}
 
PATH_HINTS = {  # path fragments -> topic
    "helm": "helm", "charts": "helm", "k8s": "kubernetes", "kubernetes": "kubernetes",
    "manifests": "kubernetes", "kustomize": "kubernetes", "argocd": "gitops",
    "flux": "gitops", "terraform": "terraform", "modules": "terraform",
    "ansible": "ansible", ".gitlab": "gitlab-ci", "ci": "ci-cd", "docker": "docker",
    "monitoring": "observability", "grafana": "observability",
    "prometheus": "observability", "alerts": "observability", "iam": "security",
    "security": "security", "network": "networking", "vpc": "networking",
    "gke": "gke", "eks": "eks", "bigquery": "bigquery", "api": "api",
    "frontend": "frontend", "ui": "frontend", "tests": "testing", "test": "testing",
    "migrations": "database", "db": "database",
}
 
 
@dataclass
class Contribution:
    person: str               # canonical identity (username or email)
    display_name: str
    project: str              # path_with_namespace
    kind: str                 # commit | mr_merged | mr_review | mr_comment | issue_closed | codeowner
    timestamp: str            # ISO8601
    weight_units: float = 1.0 # e.g. lines changed for commit_lines
    topics: list[str] = field(default_factory=list)
    ref: str = ""             # sha / MR iid / issue iid for traceability
 
 
# --------------------------------------------------------------------------- #
# GitLab API client
# --------------------------------------------------------------------------- #
 
class GitLabClient:
    def __init__(self, url: str, token: str, verify: bool | str = True, timeout: int = 30):
        if requests is None:
            sys.exit("The 'requests' package is required: pip install requests")
        self.base = url.rstrip("/") + "/api/v4"
        self.s = requests.Session()
        self.s.headers.update({"PRIVATE-TOKEN": token, "User-Agent": "gitlab-sme-finder"})
        self.s.verify = verify
        self.timeout = timeout
 
    def _request(self, path: str, params: dict | None = None, raw: bool = False):
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(6):
            r = self.s.get(url, params=params, timeout=self.timeout)
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        r.raise_for_status()
 
    def get(self, path: str, params: dict | None = None):
        r = self._request(path, params)
        return None if r is None else r.json()
 
    def get_raw(self, path: str, params: dict | None = None) -> str | None:
        r = self._request(path, params)
        return None if r is None else r.text
 
    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        params["pagination"] = "keyset" if params.get("order_by") == "id" else None
        params = {k: v for k, v in params.items() if v is not None}
        url, count = path, 0
        while url:
            r = self._request(url, params)
            if r is None:
                return
            for item in r.json():
                yield item
                count += 1
                if limit and count >= limit:
                    return
            nxt = r.links.get("next", {}).get("url")
            if nxt:
                url, params = nxt, None
            else:
                page = r.headers.get("X-Next-Page")
                if page:
                    params = dict(params or {}, page=page)
                else:
                    url = None
 
 
# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
 
def topics_from_paths(paths: Iterable[str]) -> set[str]:
    topics: set[str] = set()
    for p in paths:
        name = os.path.basename(p)
        if name in EXT_LANG:
            topics.add(EXT_LANG[name])
        if p.endswith(".gitlab-ci.yml"):
            topics.add("gitlab-ci")
        ext = os.path.splitext(name)[1].lower()
        if ext in EXT_LANG:
            topics.add(EXT_LANG[ext])
        parts = [x.lower() for x in p.split("/")[:-1]]
        if parts:
            topics.add(f"dir:{parts[0]}")
        for part in parts:
            if part in PATH_HINTS:
                topics.add(PATH_HINTS[part])
    return topics
 
 
class IdentityResolver:
    """Merges commit emails/names with GitLab usernames. Optional alias file:
    {"jorge": ["jorge@corp.com", "Jorge Rodriguez", "jrodriguez@users.noreply.gitlab.com"]}"""
 
    def __init__(self, alias_file: str | None = None):
        self.alias: dict[str, str] = {}
        self.names: dict[str, str] = {}
        if alias_file and Path(alias_file).exists():
            for canonical, keys in json.loads(Path(alias_file).read_text()).items():
                for k in keys + [canonical]:
                    self.alias[k.strip().lower()] = canonical
 
    def learn(self, username: str, name: str | None = None, email: str | None = None):
        for k in (username, name, email):
            if k:
                self.alias.setdefault(k.strip().lower(), username)
        if name:
            self.names[username] = name
 
    def resolve(self, username=None, name=None, email=None) -> str:
        for k in (username, email, name):
            if k and k.strip().lower() in self.alias:
                return self.alias[k.strip().lower()]
        if email and email.endswith("users.noreply.gitlab.com"):
            m = re.match(r"(?:\d+-)?(.+)@users\.noreply", email)
            if m:
                return m.group(1)
        return username or (email or name or "unknown").lower()
 
 
class Collector:
    def __init__(self, gl: GitLabClient, since_days: int, deep: bool,
                 max_items: int, resolver: IdentityResolver, verbose: bool = True):
        self.gl, self.deep, self.max_items, self.ids = gl, deep, max_items, resolver
        self.since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        self.verbose = verbose
        self.out: list[Contribution] = []
        self.projects: dict[str, dict] = {}
        self._commit_idents: list[tuple[Contribution, str, str]] = []
 
    def log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)
 
    def add(self, **kw):
        if BOT_PATTERNS.search(kw["person"]) or BOT_PATTERNS.search(kw.get("display_name", "")):
            return None
        c = Contribution(**kw)
        self.out.append(c)
        return c
 
    # -- project discovery --
    def list_projects(self, group: str | None, projects: list[str]) -> list[dict]:
        result = []
        if group:
            result += list(self.gl.paginate(
                f"/groups/{quote(group, safe='')}/projects",
                {"include_subgroups": "true", "archived": "false", "with_shared": "false"}))
        for p in projects:
            proj = self.gl.get(f"/projects/{quote(p, safe='')}")
            if proj:
                result.append(proj)
        return result
 
    def learn_members(self, pid: int):
        for m in self.gl.paginate(f"/projects/{pid}/members/all"):
            self.ids.learn(m["username"], m.get("name"), m.get("public_email") or m.get("email"))
 
    # -- signals --
    def collect_project(self, proj: dict):
        pid, path = proj["id"], proj["path_with_namespace"]
        self.log(f"→ {path}")
        langs = self.gl.get(f"/projects/{pid}/languages") or {}
        base_topics = {t.lower() for t in (proj.get("topics") or proj.get("tag_list") or [])}
        base_topics |= {l.lower() for l, pct in langs.items() if pct >= 10}
        self.projects[path] = {"id": pid, "name": proj.get("name"), "web_url": proj.get("web_url"),
                               "languages": langs, "topics": sorted(base_topics)}
        self.learn_members(pid)
        self._mrs(pid, path, base_topics)
        self._commits(pid, path, base_topics)
        self._issues(pid, path, base_topics)
        self._codeowners(pid, path, proj.get("default_branch") or "main")
 
    def _mrs(self, pid, path, base_topics):
        mrs = self.gl.paginate(f"/projects/{pid}/merge_requests",
                               {"state": "merged", "updated_after": self.since,
                                "order_by": "updated_at"}, limit=self.max_items)
        for mr in mrs:
            author = mr["author"]
            self.ids.learn(author["username"], author.get("name"))
            topics = set(base_topics) | {l.lower() for l in mr.get("labels", [])}
            if self.deep:
                diffs = self.gl.get(f"/projects/{pid}/merge_requests/{mr['iid']}/diffs",
                                    {"per_page": 100}) or []
                topics |= topics_from_paths(d.get("new_path") or d.get("old_path") for d in diffs)
            ts = mr.get("merged_at") or mr["updated_at"]
            self.add(person=author["username"], display_name=author.get("name", ""),
                     project=path, kind="mr_merged", timestamp=ts,
                     topics=sorted(topics), ref=f"!{mr['iid']}")
            # approvals
            appr = self.gl.get(f"/projects/{pid}/merge_requests/{mr['iid']}/approvals") or {}
            for a in appr.get("approved_by", []):
                u = a["user"]
                if u["username"] != author["username"]:
                    self.ids.learn(u["username"], u.get("name"))
                    self.add(person=u["username"], display_name=u.get("name", ""), project=path,
                             kind="mr_review", timestamp=ts, topics=sorted(topics), ref=f"!{mr['iid']}")
            # review comments (non-system, not by author), capped per reviewer per MR
            per_reviewer: dict[str, int] = defaultdict(int)
            for n in self.gl.paginate(f"/projects/{pid}/merge_requests/{mr['iid']}/notes",
                                      {"sort": "asc"}, limit=200):
                u = n.get("author") or {}
                if n.get("system") or u.get("username") == author["username"]:
                    continue
                if len((n.get("body") or "").strip()) < 15 or per_reviewer[u["username"]] >= 5:
                    continue
                per_reviewer[u["username"]] += 1
                self.add(person=u["username"], display_name=u.get("name", ""), project=path,
                         kind="mr_comment", timestamp=n["created_at"], topics=sorted(topics),
                         ref=f"!{mr['iid']}")
 
    def _commits(self, pid, path, base_topics):
        commits = self.gl.paginate(f"/projects/{pid}/repository/commits",
                                   {"since": self.since, "with_stats": "true", "all": "false"},
                                   limit=self.max_items)
        for c in commits:
            if len(c.get("parent_ids") or []) > 1:  # skip merge commits
                continue
            person = self.ids.resolve(name=c.get("author_name"), email=c.get("author_email"))
            topics = set(base_topics)
            if self.deep:
                diff = self.gl.get(f"/projects/{pid}/repository/commits/{c['id']}/diff",
                                   {"per_page": 100}) or []
                topics |= topics_from_paths(d.get("new_path") or d.get("old_path") for d in diff)
            stats = c.get("stats") or {}
            lines = min((stats.get("additions", 0) + stats.get("deletions", 0)), 1000)
            obj = self.add(person=person, display_name=c.get("author_name", ""), project=path,
                           kind="commit", timestamp=c.get("authored_date") or c["created_at"],
                           weight_units=lines, topics=sorted(topics), ref=c["short_id"])
            if obj:
                self._commit_idents.append((obj, c.get("author_name"), c.get("author_email")))
 
    def _issues(self, pid, path, base_topics):
        issues = self.gl.paginate(f"/projects/{pid}/issues",
                                  {"state": "closed", "updated_after": self.since}, limit=self.max_items)
        for i in issues:
            topics = sorted(set(base_topics) | {l.lower() for l in i.get("labels", [])})
            for u in i.get("assignees") or []:
                self.ids.learn(u["username"], u.get("name"))
                self.add(person=u["username"], display_name=u.get("name", ""), project=path,
                         kind="issue_closed", timestamp=i.get("closed_at") or i["updated_at"],
                         topics=topics, ref=f"#{i['iid']}")
 
    def _codeowners(self, pid, path, ref):
        for loc in ("CODEOWNERS", ".gitlab/CODEOWNERS", "docs/CODEOWNERS"):
            raw = self.gl.get_raw(f"/projects/{pid}/repository/files/{quote(loc, safe='')}/raw", {"ref": ref})
            if not raw:
                continue
            for line in raw.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("["):
                    continue
                pattern, *owners = line.split()
                for o in owners:
                    if o.startswith("@") and "/" not in o:  # users only; groups skipped
                        uname = o[1:]
                        self.add(person=uname, display_name=self.ids.names.get(uname, ""),
                                 project=path, kind="codeowner",
                                 timestamp=datetime.now(timezone.utc).isoformat(),
                                 topics=sorted(topics_from_paths([pattern.lstrip("/") + "/x"])
                                               | {f"owns:{pattern}"}), ref=loc)
            return
 
    def finalize(self):
        # re-resolve commit identities now that usernames are known from MRs/members
        for obj, name, email in self._commit_idents:
            obj.person = self.ids.resolve(name=name, email=email)
        for c in self.out:
            if not c.display_name or c.display_name.lower() == c.person:
                c.display_name = self.ids.names.get(c.person, c.display_name or c.person)
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "since": self.since,
                "projects": self.projects, "contributions": [asdict(c) for c in self.out]}
 
 
# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
 
@dataclass
class PersonScore:
    person: str
    name: str
    score: float = 0.0
    counts: dict = field(default_factory=lambda: defaultdict(int))
    last_active: datetime | None = None
    projects: set = field(default_factory=set)
    topics: dict = field(default_factory=lambda: defaultdict(float))
    months: set = field(default_factory=set)
    owns: set = field(default_factory=set)
 
 
class SMEScorer:
    def __init__(self, data: dict, weights: dict | None = None, half_life_days: float = 180,
                 now: datetime | None = None):
        self.data = data
        self.w = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.half_life = half_life_days
        self.now = now or datetime.now(timezone.utc)
        self.contribs = [Contribution(**c) for c in data["contributions"]]
 
    def _decay(self, ts: datetime, kind: str) -> float:
        if kind == "codeowner":
            return 1.0
        age = max((self.now - ts).total_seconds() / 86400, 0)
        return 0.5 ** (age / self.half_life)
 
    def _value(self, c: Contribution, ts: datetime) -> float:
        base = self.w.get(c.kind, 0)
        if c.kind == "commit":
            base += self.w["commit_lines"] * c.weight_units
        return base * self._decay(ts, c.kind)
 
    @staticmethod
    def _parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
 
    def rank(self, projects: list[str] | None = None, topics: list[str] | None = None,
             keywords: list[str] | None = None, exclude: list[str] | None = None,
             min_score: float = 0.5) -> list[PersonScore]:
        projects = [p.lower() for p in projects or []]
        topics = [t.lower() for t in topics or []]
        keywords = [k.lower() for k in keywords or []]
        exclude = {e.lower() for e in exclude or []}
        people: dict[str, PersonScore] = {}
 
        for c in self.contribs:
            if c.person.lower() in exclude:
                continue
            proj_l = c.project.lower()
            if projects and not any(proj_l == p or proj_l.endswith("/" + p) or proj_l.startswith(p + "/")
                                    for p in projects):
                continue
            c_topics = [t.lower() for t in c.topics]
            relevance = 1.0
            if topics or keywords:
                hits = sum(1 for t in topics if t in c_topics or f"dir:{t}" in c_topics)
                hits += sum(1 for k in keywords if k in proj_l or any(k in t for t in c_topics))
                if hits == 0:
                    continue
                relevance = min(1.0, 0.6 + 0.4 * hits / max(len(topics) + len(keywords), 1))
            ts = self._parse(c.timestamp)
            ps = people.setdefault(c.person, PersonScore(c.person, c.display_name))
            if c.display_name and (not ps.name or ps.name == ps.person):
                ps.name = c.display_name
            v = self._value(c, ts) * relevance
            ps.score += v
            ps.counts[c.kind] += 1
            ps.projects.add(c.project)
            if c.kind != "codeowner":
                ps.months.add(ts.strftime("%Y-%m"))
                ps.last_active = max(ps.last_active or ts, ts)
            else:
                ps.owns |= {t[5:] for t in c.topics if t.startswith("owns:")}
            for t in c.topics:
                if not t.startswith("owns:"):
                    ps.topics[t] += v
 
        # consistency bonus: sustained involvement beats one-off bursts (up to +25%)
        for ps in people.values():
            ps.score *= 1 + min(len(ps.months), 12) / 48
        ranked = sorted((p for p in people.values() if p.score >= min_score),
                        key=lambda p: p.score, reverse=True)
        return ranked
 
    @staticmethod
    def bus_factor(ranked: list[PersonScore], threshold: float = 0.5) -> int:
        """Minimum number of people covering `threshold` of total expertise score."""
        total = sum(p.score for p in ranked) or 1
        acc = 0
        for i, p in enumerate(ranked, 1):
            acc += p.score
            if acc / total >= threshold:
                return i
        return len(ranked)
 
    def explain(self, ps: PersonScore) -> str:
        parts = []
        labels = {"mr_merged": "MRs merged", "commit": "commits", "mr_review": "MR approvals",
                  "mr_comment": "review comments", "issue_closed": "issues closed"}
        for k, lbl in labels.items():
            if ps.counts.get(k):
                parts.append(f"{ps.counts[k]} {lbl}")
        if ps.owns:
            parts.append("CODEOWNER of " + ", ".join(sorted(ps.owns)[:3]))
        if ps.last_active:
            parts.append(f"last active {(self.now - ps.last_active).days}d ago")
        parts.append(f"active {len(ps.months)} mo")
        return "; ".join(parts)
 
    @staticmethod
    def top_topics(ps: PersonScore, n: int = 5) -> list[str]:
        items = [(t, v) for t, v in ps.topics.items() if not t.startswith("dir:")]
        return [t for t, _ in sorted(items, key=lambda x: x[1], reverse=True)[:n]]
 
 
# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
 
def rows_for(scorer: SMEScorer, ranked: list[PersonScore], top: int, context: str = "") -> list[dict]:
    total = sum(p.score for p in ranked) or 1
    rows = []
    for i, p in enumerate(ranked[:top], 1):
        confidence = "high" if p.score / total >= 0.25 and len(p.months) >= 3 else \
                     "medium" if p.score / total >= 0.10 else "low"
        rows.append({
            "context": context, "rank": i, "username": p.person, "name": p.name,
            "score": round(p.score, 1), "share_pct": round(100 * p.score / total, 1),
            "confidence": confidence, "top_topics": ", ".join(scorer.top_topics(p)),
            "projects": len(p.projects), "why": scorer.explain(p),
        })
    return rows
 
 
def render(rows: list[dict], fmt: str, title: str = "", note: str = "") -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return buf.getvalue()
    cols = ["rank", "name", "username", "score", "share_pct", "confidence", "top_topics", "why"]
    if fmt == "markdown":
        out = [f"### {title}\n"] if title else []
        if note:
            out.append(f"> {note}\n")
        out.append("| " + " | ".join(cols) + " |")
        out.append("|" + "---|" * len(cols))
        for r in rows:
            out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
        return "\n".join(out) + "\n"
    # plain table
    short = ["rank", "name", "username", "score", "share_pct", "confidence", "top_topics"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in short}
    lines = [title, "=" * len(title)] if title else []
    lines.append("  ".join(c.upper().ljust(widths[c]) for c in short))
    for r in rows:
        lines.append("  ".join(str(r[c]).ljust(widths[c]) for c in short))
        lines.append("      ↳ " + r["why"])
    if note:
        lines.append(f"⚠  {note}")
    return "\n".join(lines) + "\n"
 
 
def bus_factor_note(scorer: SMEScorer, ranked: list[PersonScore]) -> str:
    if len(ranked) == 0:
        return "No matching activity found."
    bf = scorer.bus_factor(ranked)
    if bf == 1:
        return (f"Bus factor 1: {ranked[0].name} holds ≥50% of the expertise. "
                f"Consider pairing {ranked[1].name if len(ranked) > 1 else 'someone'} on future work.")
    return f"Bus factor {bf} (people covering 50% of expertise)."
 
 
# --------------------------------------------------------------------------- #
# Demo data
# --------------------------------------------------------------------------- #
 
def demo_data(seed: int = 7) -> dict:
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc)
    people = {
        "ana.torres": ("Ana Torres", {"infra/terraform-gcp": 0.6, "platform/helm-charts": 0.1}),
        "li.wei": ("Li Wei", {"platform/helm-charts": 0.5, "platform/argocd-apps": 0.4}),
        "marcus.j": ("Marcus Johnson", {"infra/terraform-gcp": 0.3, "tools/pipeline-health": 0.2}),
        "priya.n": ("Priya Nair", {"tools/pipeline-health": 0.7, "data/finance-bq-agent": 0.3}),
        "sam.k": ("Sam Kim", {"data/finance-bq-agent": 0.8}),
        "dana.o": ("Dana Okafor", {"platform/argocd-apps": 0.3, "infra/terraform-gcp": 0.1}),
    }
    proj_paths = {
        "infra/terraform-gcp": (["modules/gke/main.tf", "modules/vpc/network.tf", "envs/prod/main.tfvars",
                                 "modules/iam/roles.tf", ".gitlab-ci.yml"], ["terraform", "gcp"]),
        "platform/helm-charts": (["charts/api/values.yaml", "charts/api/templates/deploy.yaml",
                                  "charts/monitoring/prometheus.yaml"], ["helm", "kubernetes"]),
        "platform/argocd-apps": (["argocd/apps/prod.yaml", "kustomize/base/kustomization.yaml"],
                                 ["gitops", "kubernetes"]),
        "tools/pipeline-health": (["src/collector.py", "src/api/routes.py", "monitoring/grafana/dash.json",
                                   "tests/test_collector.py"], ["python", "observability"]),
        "data/finance-bq-agent": (["agent/tools.py", "sql/views/revenue.sql", "bigquery/schema.json"],
                                  ["python", "bigquery", "vertex-ai"]),
    }
    labels = ["bug", "feature", "security", "tech-debt", "performance"]
    out = []
    for user, (name, focus) in people.items():
        for proj, intensity in focus.items():
            files, base = proj_paths[proj]
            recency_bias = 60 if user != "dana.o" else 400  # Dana's work is older
            for _ in range(int(120 * intensity)):
                ts = now - timedelta(days=min(abs(rnd.gauss(recency_bias, 90)), 540))
                touched = rnd.sample(files, k=rnd.randint(1, len(files)))
                topics = sorted(set(base) | topics_from_paths(touched))
                r = rnd.random()
                kind = "commit" if r < 0.6 else "mr_merged" if r < 0.75 else \
                    "mr_review" if r < 0.88 else "mr_comment" if r < 0.95 else "issue_closed"
                if kind in ("mr_merged", "issue_closed"):
                    topics = sorted(set(topics) | {rnd.choice(labels)})
                out.append(asdict(Contribution(user, name, proj, kind, ts.isoformat(),
                                               rnd.randint(5, 400), topics, "demo")))
    out.append(asdict(Contribution("ana.torres", "Ana Torres", "infra/terraform-gcp", "codeowner",
                                   now.isoformat(), 1, ["terraform", "gke", "owns:/modules/gke/"], "CODEOWNERS")))
    out.append(asdict(Contribution("priya.n", "Priya Nair", "tools/pipeline-health", "codeowner",
                                   now.isoformat(), 1, ["python", "owns:/src/"], "CODEOWNERS")))
    return {"generated_at": now.isoformat(), "since": (now - timedelta(days=540)).isoformat(),
            "projects": {p: {"topics": t} for p, (_, t) in proj_paths.items()}, "contributions": out}
 
 
# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
 
def load_cache(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Cache {path} not found. Run 'collect' first (or 'demo').")
    return json.loads(p.read_text())
 
 
def emit(text: str, out: str | None):
    if out:
        Path(out).write_text(text)
        print(f"Wrote {out}", file=sys.stderr)
    else:
        print(text)
 
 
def cmd_collect(a):
    url, token = a.url or os.getenv("GITLAB_URL"), a.token or os.getenv("GITLAB_TOKEN")
    if not url or not token:
        sys.exit("Set GITLAB_URL and GITLAB_TOKEN (or pass --url/--token).")
    gl = GitLabClient(url, token, verify=os.getenv("REQUESTS_CA_BUNDLE", True))
    col = Collector(gl, a.since_days, a.deep, a.max_items, IdentityResolver(a.aliases))
    projects = col.list_projects(a.group, [p for p in (a.projects or "").split(",") if p])
    if not projects:
        sys.exit("No projects found - check --group/--projects and token permissions.")
    for proj in projects:
        try:
            col.collect_project(proj)
        except Exception as e:  # keep going on per-project failures
            print(f"  ! {proj.get('path_with_namespace')}: {e}", file=sys.stderr)
    data = col.finalize()
    Path(a.cache).write_text(json.dumps(data, indent=1))
    print(f"Collected {len(data['contributions'])} signals from {len(projects)} projects → {a.cache}",
          file=sys.stderr)
 
 
def make_scorer(a, data):
    weights = json.loads(Path(a.weights).read_text()) if a.weights else None
    return SMEScorer(data, weights, a.half_life)
 
 
def cmd_sme(a, data=None):
    data = data or load_cache(a.cache)
    scorer = make_scorer(a, data)
    ranked = scorer.rank(a.project, a.topic, a.keyword, a.exclude)
    ctx = " + ".join(filter(None, [", ".join(a.project or []), ", ".join(a.topic or []),
                                   ", ".join(a.keyword or [])])) or "all projects"
    rows = rows_for(scorer, ranked, a.top, ctx)
    emit(render(rows, a.format, f"SMEs for: {ctx}", bus_factor_note(scorer, ranked)), a.out)
 
 
def cmd_report(a, data=None):
    data = data or load_cache(a.cache)
    scorer = make_scorer(a, data)
    all_rows, chunks = [], []
    for proj in sorted(data["projects"]):
        ranked = scorer.rank(projects=[proj], exclude=a.exclude)
        rows = rows_for(scorer, ranked, a.top, f"project:{proj}")
        all_rows += rows
        chunks.append(render(rows, a.format, f"Project: {proj}", bus_factor_note(scorer, ranked)))
    topic_totals: dict[str, float] = defaultdict(float)
    for p in scorer.rank():
        for t, v in p.topics.items():
            if not t.startswith("dir:"):
                topic_totals[t] += v
    for topic, _ in sorted(topic_totals.items(), key=lambda x: -x[1])[: a.topics]:
        ranked = scorer.rank(topics=[topic], exclude=a.exclude)
        rows = rows_for(scorer, ranked, a.top, f"topic:{topic}")
        all_rows += rows
        chunks.append(render(rows, a.format, f"Topic: {topic}", bus_factor_note(scorer, ranked)))
    if a.format in ("csv", "json"):
        emit(render(all_rows, a.format), a.out)
    else:
        header = (f"# GitLab SME Report\n\nGenerated {datetime.now():%Y-%m-%d} · data since "
                  f"{data['since'][:10]} · half-life {a.half_life:g}d\n\n") if a.format == "markdown" else ""
        emit(header + "\n".join(chunks), a.out)
 
 
def build_parser():
    ap = argparse.ArgumentParser(description="Find GitLab SMEs from contribution history.")
    sub = ap.add_subparsers(dest="cmd", required=True)
 
    def common(p):
        p.add_argument("--cache", default="sme_cache.json", help="JSON cache from 'collect'")
        p.add_argument("--half-life", type=float, default=180, help="recency half-life in days")
        p.add_argument("--weights", help="JSON file overriding signal weights")
        p.add_argument("--exclude", action="append", help="username to exclude (repeatable)")
        p.add_argument("--top", type=int, default=5)
        p.add_argument("--format", choices=["table", "markdown", "csv", "json"], default="table")
        p.add_argument("--out", help="write output to file")
 
    c = sub.add_parser("collect", help="pull activity from GitLab into a cache")
    c.add_argument("--url"); c.add_argument("--token")
    c.add_argument("--group", help="group full path (includes subgroups)")
    c.add_argument("--projects", help="comma-separated project IDs or paths")
    c.add_argument("--since-days", type=int, default=365)
    c.add_argument("--max-items", type=int, default=2000, help="per project, per signal type")
    c.add_argument("--deep", action="store_true", help="fetch file paths per commit/MR (slower, richer topics)")
    c.add_argument("--aliases", help="JSON identity alias file")
    c.add_argument("--cache", default="sme_cache.json")
    c.set_defaults(func=cmd_collect)
 
    s = sub.add_parser("sme", help="rank SMEs for a project/topic/keyword")
    common(s)
    s.add_argument("--project", action="append", help="project path (repeatable)")
    s.add_argument("--topic", action="append", help="topic e.g. terraform, helm, python (repeatable)")
    s.add_argument("--keyword", action="append", help="free-text match on project/topic/label")
    s.set_defaults(func=cmd_sme)
 
    r = sub.add_parser("report", help="SME report for every project and top topics")
    common(r)
    r.add_argument("--topics", type=int, default=10, help="number of top topics to include")
    r.set_defaults(func=cmd_report)
 
    d = sub.add_parser("demo", help="run with synthetic data (no GitLab needed)")
    common(d)
    d.set_defaults(func=None)
    return ap
 
 
def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "demo":
        data = demo_data()
        Path(a.cache).write_text(json.dumps(data, indent=1))
        print(f"Demo cache written to {a.cache}\n", file=sys.stderr)
        for extra in ({"project": ["infra/terraform-gcp"], "topic": None, "keyword": None},
                      {"project": None, "topic": ["kubernetes"], "keyword": None},
                      {"project": None, "topic": None, "keyword": ["bigquery"]}):
            ns = argparse.Namespace(**vars(a), **extra)
            cmd_sme(ns, data)
        return
    a.func(a)
 
 
if __name__ == "__main__":
    main()
 