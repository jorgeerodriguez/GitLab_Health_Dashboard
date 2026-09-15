#!/usr/bin/env python3
"""
GitLab Service Account Inventory
================================
Goes through every GitLab project and group your token can see and lists every
non-human identity it finds. It looks in two places:
 
  A. GitLab's own identities (from the API)
     - Service account and bot users (instance and group level)
     - Project and group access tokens (each one is backed by a bot user)
     - Deploy tokens and deploy keys
     - Personal access tokens that belong to bot users (admin only)
     - CI/CD variables that hold or point to cloud identities
       (only the identity is recorded, never the secret value)
 
  B. Cloud and platform identities mentioned in code (default branch only)
     - GCP service account emails and JSON key files committed to a repo
     - GKE Workload Identity annotations
     - AWS IAM role and user ARNs, plus access key IDs (redacted)
     - Kubernetes ServiceAccount manifests and serviceAccountName references
     - Terraform resources that create service accounts or roles
     - gcloud activate-service-account / key-file logins in CI
 
Requirements:  pip install "python-gitlab>=4.4"
Token scopes:  read_api + read_repository
               (an admin token also collects the instance-wide items)
 
Usage:
  export GITLAB_URL=https://gitlab.example.com
  export GITLAB_TOKEN=glpat-xxxxxxxx
  python gitlab_sa_inventory.py --out ./sa_inventory --workers 8
  python gitlab_sa_inventory.py --group platform-eng          # one group tree only
  python gitlab_sa_inventory.py --no-code-scan                 # API items only (fast)
"""
from __future__ import annotations
 
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import tarfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
 
from dotenv import load_dotenv
import gitlab
from gitlab.exceptions import GitlabError

from gitlab_orion.client import DEFAULT_GROUP, get_group, list_group_projects, get_gitlab_client, test_connection


load_dotenv()
 
log = logging.getLogger("sa-inventory")
 
SEVERITY_RANK = {"info": 0, "warn": 1, "high": 2, "critical": 3}
 
 
# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    category: str            # gitlab_native | cloud_reference | secret_exposure
    identity_type: str       # gcp_service_account, project_access_token, ...
    identity: str            # SA email / token name / ARN; never a secret value
    scope: str               # instance | group | project
    scope_path: str          # full path of the group or project
    location: str = ""       # file:line, CI variable key, settings page, ...
    severity: str = "info"   # info | warn | high | critical
    details: dict = field(default_factory=dict)
    web_url: str = ""
 
 
# --------------------------------------------------------------------------- #
# Detection patterns
# --------------------------------------------------------------------------- #
# (identity_type, regex, severity). If the regex has a capture group, group(1)
# is the identity; otherwise the whole match is.
PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("gcp_service_account",
     re.compile(r"\b[a-z0-9][a-z0-9-]{0,62}@[a-z0-9-]+\.iam\.gserviceaccount\.com\b", re.I), "info"),
    ("gcp_default_service_account",
     re.compile(r"\b(?:\d+-compute@developer|[a-z0-9-]+@appspot)\.gserviceaccount\.com\b", re.I), "warn"),
    ("gke_workload_identity",
     re.compile(r"iam\.gke\.io/gcp-service-account[\"']?\s*[:=]\s*[\"']?([\w.@-]+)"), "info"),
    ("aws_iam_role",
     re.compile(r"\barn:aws[\w-]*:iam::\d{12}:role/[\w+=,.@/-]+"), "info"),
    ("aws_iam_user",
     re.compile(r"\barn:aws[\w-]*:iam::\d{12}:user/[\w+=,.@/-]+"), "warn"),
    ("aws_access_key_id",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high"),
    ("azure_client_id",
     re.compile(r"\b(?:AZURE|ARM)_CLIENT_ID[\"']?\s*[:=]\s*[\"']?([0-9a-f-]{36})", re.I), "info"),
    ("k8s_service_account_ref",
     re.compile(r"\bserviceAccountName\s*:\s*[\"']?([\w.-]+)"), "info"),
    ("tf_google_service_account",
     re.compile(r'resource\s+"google_service_account"\s+"([\w-]+)"'), "info"),
    ("tf_aws_iam_principal",
     re.compile(r'resource\s+"aws_iam_(?:role|user)"\s+"([\w-]+)"'), "info"),
    ("tf_kubernetes_service_account",
     re.compile(r'resource\s+"kubernetes_service_account(?:_v1)?"\s+"([\w-]+)"'), "info"),
    ("gcloud_key_file_auth",
     re.compile(r"gcloud\s+auth\s+activate-service-account[^\n\"']{0,160}"), "warn"),
]
 
GCP_KEY_JSON = re.compile(r'"type"\s*:\s*"service_account"')
GCP_KEY_EMAIL = re.compile(r'"client_email"\s*:\s*"([^"]+)"')
YAML_DOC_SPLIT = re.compile(r"^---\s*$", re.M)
K8S_KIND_SA = re.compile(r"^kind:\s*[\"']?ServiceAccount[\"']?\s*$", re.M)
K8S_META_NAME = re.compile(r"^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*[\"']?([\w.-]+)", re.M)
K8S_META_NS = re.compile(r"^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+namespace:\s*[\"']?([\w.-]+)", re.M)
 
# Variable names that suggest a credential even when the value doesn't match a pattern
CREDENTIAL_VAR_NAME = re.compile(
    r"(SERVICE_ACCOUNT|_SA_KEY|SA_JSON|GCP_CREDENTIALS|GOOGLE_APPLICATION_CREDENTIALS|"
    r"GOOGLE_CREDENTIALS|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|ROLE_ARN|KUBE_?CONFIG|"
    r"CLIENT_ID|CLIENT_SECRET|_TOKEN$|_API_KEY$)", re.I)
 
BOT_USERNAME = re.compile(r"^(?:project|group)_\d+_bot|^service_account|_bot$|^bot[-_]", re.I)
 
SKIP_DIRS = {".git", "node_modules", "vendor", ".terraform", "dist", "build", "__pycache__", ".venv"}
MAX_FILE_BYTES = 1_000_000
 
 
# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def redact(value: str) -> str:
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"
 
 
def pick(obj, *attrs) -> dict:
    return {a: getattr(obj, a, None) for a in attrs if getattr(obj, a, None) is not None}
 
 
def safe(fn, what: str, where: str):
    """Call an API list function. On 401/403/404 (no permission or not supported), return []."""
    try:
        return fn()
    except GitlabError as e:
        log.debug("skip %s on %s (%s)", what, where, getattr(e, "response_code", e))
        return []
 
 
def token_severity(t) -> str:
    if getattr(t, "revoked", False) or getattr(t, "active", True) is False:
        return "info"
    scopes = set(getattr(t, "scopes", None) or [])
    if scopes & {"api", "write_repository", "sudo", "admin_mode"} and (getattr(t, "access_level", 0) or 0) >= 40:
        return "high"
    if not getattr(t, "expires_at", None):
        return "warn"
    return "info"
 
 
# --------------------------------------------------------------------------- #
# Text scanning (repo files and CI variable values)
# --------------------------------------------------------------------------- #
def scan_text(text: str, scope: str, scope_path: str, location: str, base_url: str = "") -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
 
    # Committed / stored GCP JSON key: the most serious finding
    if GCP_KEY_JSON.search(text):
        m = GCP_KEY_EMAIL.search(text)
        out.append(Finding("secret_exposure", "gcp_service_account_key", m.group(1) if m else "unknown",
                           scope, scope_path, location, "critical", web_url=base_url))
        if m:
            seen.add(("gcp_service_account", m.group(1).lower()))
 
    # Kubernetes ServiceAccount manifests
    if location.split(":")[0].endswith((".yaml", ".yml")) and K8S_KIND_SA.search(text):
        for doc in YAML_DOC_SPLIT.split(text):
            if K8S_KIND_SA.search(doc) and (n := K8S_META_NAME.search(doc)):
                ns = K8S_META_NS.search(doc)
                ident = f"{ns.group(1) if ns else 'default'}/{n.group(1)}"
                out.append(Finding("cloud_reference", "k8s_service_account", ident,
                                   scope, scope_path, location, "info", web_url=base_url))
 
    for lineno, line in enumerate(text.splitlines(), 1):
        for itype, rx, sev in PATTERNS:
            for m in rx.finditer(line):
                ident = (m.group(1) if rx.groups else m.group(0)).strip()
                if itype == "aws_access_key_id":
                    ident = redact(ident)
                key = (itype, ident.lower())
                if key in seen:      # one finding per identity per file/variable
                    continue
                seen.add(key)
                loc = f"{location}:{lineno}" if base_url else location
                url = f"{base_url}#L{lineno}" if base_url else ""
                out.append(Finding("cloud_reference", itype, ident, scope, scope_path, loc, sev, web_url=url))
    return out
 
 
def scan_repository(gl: gitlab.Gitlab, project, max_archive_mb: int) -> list[Finding]:
    ref = project.default_branch
    if not ref or getattr(project, "empty_repo", False):
        return []
    p = project.path_with_namespace
    try:
        resp = gl.http_get(f"/projects/{project.id}/repository/archive.tar.gz",
                           query_data={"sha": ref}, streamed=True, raw=True)
    except GitlabError as e:
        log.warning("archive download failed for %s: %s", p, e)
        return []
 
    buf, limit = io.BytesIO(), max_archive_mb * 1024 * 1024
    for chunk in resp.iter_content(chunk_size=256 * 1024):
        buf.write(chunk)
        if buf.tell() > limit:
            resp.close()
            log.warning("skipping code scan for %s: archive larger than %d MB", p, max_archive_mb)
            return [Finding("cloud_reference", "scan_skipped", f"archive>{max_archive_mb}MB",
                            "project", p, severity="warn", web_url=project.web_url)]
    buf.seek(0)
 
    findings: list[Finding] = []
    try:
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile() or member.size > MAX_FILE_BYTES:
                    continue
                rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
                if any(part in SKIP_DIRS for part in rel.split("/")):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                data = fh.read()
                if b"\x00" in data[:8192]:  # binary file
                    continue
                url = f"{project.web_url}/-/blob/{ref}/{rel}"
                findings.extend(scan_text(data.decode("utf-8", "ignore"), "project", p, rel, url))
    except tarfile.TarError as e:
        log.warning("could not read archive for %s: %s", p, e)
    return findings
 
 
def scan_variables(variables, scope: str, scope_path: str, settings_url: str) -> list[Finding]:
    out: list[Finding] = []
    for v in variables:
        value = getattr(v, "value", None) or ""   # hidden variables come back as null
        details = pick(v, "variable_type", "protected", "masked", "hidden", "raw", "environment_scope")
        loc = f"CI variable {v.key} (env={getattr(v, 'environment_scope', '*')})"
        hits = scan_text(value, scope, scope_path, loc) if value else []
        for h in hits:
            h.details = details
            h.web_url = settings_url
            if h.identity_type == "gcp_service_account_key":
                h.identity_type, h.severity = "gcp_sa_key_in_ci_variable", "high"
        out.extend(hits)
        if not hits and CREDENTIAL_VAR_NAME.search(v.key):
            sev = "warn" if not getattr(v, "masked", False) else "info"
            out.append(Finding("gitlab_native", "credential_ci_variable", v.key, scope, scope_path,
                               loc, sev, details, settings_url))
    return out
 
 
# --------------------------------------------------------------------------- #
# GitLab-native identities
# --------------------------------------------------------------------------- #
def inventory_project(gl, project_id: int, bot_user_ids: set[int], code_scan: bool, max_archive_mb: int):
    project = gl.projects.get(project_id)
    p, url = project.path_with_namespace, project.web_url
    out: list[Finding] = []
 
    for t in safe(lambda: project.access_tokens.list(get_all=True), "access tokens", p):
        out.append(Finding("gitlab_native", "project_access_token", t.name, "project", p,
                           f"bot user_id={getattr(t, 'user_id', '')}", token_severity(t),
                           pick(t, "id", "scopes", "access_level", "active", "revoked",
                                "expires_at", "last_used_at", "created_at"),
                           f"{url}/-/settings/access_tokens"))
 
    for d in safe(lambda: project.deploytokens.list(get_all=True), "deploy tokens", p):
        out.append(Finding("gitlab_native", "deploy_token", d.name or d.username, "project", p,
                           f"username={d.username}", "warn" if not getattr(d, "expires_at", None) else "info",
                           pick(d, "id", "username", "scopes", "expires_at", "revoked", "expired"),
                           f"{url}/-/settings/repository"))
 
    for k in safe(lambda: project.keys.list(get_all=True), "deploy keys", p):
        out.append(Finding("gitlab_native", "deploy_key", k.title, "project", p, "",
                           "warn" if getattr(k, "can_push", False) else "info",
                           pick(k, "id", "can_push", "created_at", "expires_at", "fingerprint_sha256"),
                           f"{url}/-/settings/repository"))
 
    for m in safe(lambda: project.members.list(get_all=True), "members", p):
        if m.id in bot_user_ids or BOT_USERNAME.search(m.username or ""):
            out.append(Finding("gitlab_native", "bot_member", m.username, "project", p,
                               "project member", "info",
                               pick(m, "id", "name", "access_level", "expires_at", "state"),
                               f"{url}/-/project_members"))
 
    out.extend(scan_variables(safe(lambda: project.variables.list(get_all=True), "variables", p),
                              "project", p, f"{url}/-/settings/ci_cd"))
 
    if code_scan and not getattr(project, "archived", False):
        out.extend(scan_repository(gl, project, max_archive_mb))
    return p, out
 
 
def inventory_group(gl, group) -> list[Finding]:
    g, url = group.full_path, group.web_url
    out: list[Finding] = []
 
    for t in safe(lambda: group.access_tokens.list(get_all=True), "group tokens", g):
        out.append(Finding("gitlab_native", "group_access_token", t.name, "group", g,
                           f"bot user_id={getattr(t, 'user_id', '')}", token_severity(t),
                           pick(t, "id", "scopes", "access_level", "active", "revoked",
                                "expires_at", "last_used_at", "created_at"),
                           f"{url}/-/settings/access_tokens"))
 
    for d in safe(lambda: group.deploytokens.list(get_all=True), "group deploy tokens", g):
        out.append(Finding("gitlab_native", "deploy_token", d.name or d.username, "group", g,
                           f"username={d.username}", "warn" if not getattr(d, "expires_at", None) else "info",
                           pick(d, "id", "username", "scopes", "expires_at", "revoked", "expired"),
                           f"{url}/-/settings/repository"))
 
    # Group service accounts exist on top-level groups (GitLab Premium/Ultimate)
    if not getattr(group, "parent_id", None):
        for sa in safe(lambda: gl.http_list(f"/groups/{group.id}/service_accounts", get_all=True),
                       "group service accounts", g):
            out.append(Finding("gitlab_native", "group_service_account", sa.get("username", ""), "group", g,
                               "", "info", {k: sa.get(k) for k in ("id", "name", "email") if sa.get(k)},
                               f"{url}/-/settings/service_accounts"))
 
    out.extend(scan_variables(safe(lambda: group.variables.list(get_all=True), "group variables", g),
                              "group", g, f"{url}/-/settings/ci_cd"))
    return out
 
 
def inventory_instance(gl) -> tuple[list[Finding], set[int]]:
    """Instance-wide items (needs an admin token)."""
    out: list[Finding] = []
    bot_ids: set[int] = set()
    host = gl.url
 
    for u in safe(lambda: gl.users.list(exclude_humans=True, get_all=True), "bot users", "instance"):
        bot_ids.add(u.id)
        out.append(Finding("gitlab_native", "bot_user", u.username, "instance", "instance", "", "info",
                           pick(u, "id", "name", "state", "bot", "created_at", "last_activity_on",
                                "last_sign_in_at"),
                           f"{host}/admin/users/{u.username}"))
 
    for sa in safe(lambda: gl.http_list("/service_accounts", get_all=True), "instance service accounts", "instance"):
        bot_ids.add(sa.get("id"))
        out.append(Finding("gitlab_native", "instance_service_account", sa.get("username", ""), "instance",
                           "instance", "", "info", {k: sa.get(k) for k in ("id", "name", "email") if sa.get(k)}))
 
    for t in safe(lambda: gl.personal_access_tokens.list(get_all=True), "personal access tokens", "instance"):
        if getattr(t, "user_id", None) in bot_ids:
            out.append(Finding("gitlab_native", "bot_personal_access_token", t.name, "instance", "instance",
                               f"user_id={t.user_id}", token_severity(t),
                               pick(t, "id", "scopes", "active", "revoked", "expires_at", "last_used_at",
                                    "created_at")))
 
    for d in safe(lambda: gl.deploytokens.list(get_all=True), "instance deploy tokens", "instance"):
        out.append(Finding("gitlab_native", "deploy_token", d.name or d.username, "instance", "instance",
                           f"username={d.username}", "info", pick(d, "id", "username", "scopes", "expires_at")))
 
    for k in safe(lambda: gl.deploykeys.list(get_all=True), "instance deploy keys", "instance"):
        projects = [p.get("path_with_namespace") for p in (getattr(k, "projects_with_write_access", None) or [])]
        out.append(Finding("gitlab_native", "deploy_key_instance", k.title, "instance", "instance", "",
                           "warn" if projects else "info",
                           {**pick(k, "id", "created_at", "fingerprint_sha256"),
                            "write_access_projects": projects}))
    return out, bot_ids
 
 
# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(findings: list[Finding], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    rows = [asdict(f) for f in findings]
 
    with open(os.path.join(out_dir, "findings.json"), "w") as fh:
        json.dump(rows, fh, indent=2, default=str)
 
    cols = list(Finding.__dataclass_fields__.keys())
    with open(os.path.join(out_dir, "findings.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "details": json.dumps(r["details"], default=str)})
 
    # One row per unique identity: where it is used and its worst severity
    summary: dict[tuple[str, str], dict] = defaultdict(lambda: {"scopes": set(), "severity": "info", "count": 0})
    for f in findings:
        s = summary[(f.identity_type, f.identity)]
        s["scopes"].add(f.scope_path)
        s["count"] += 1
        if SEVERITY_RANK[f.severity] > SEVERITY_RANK[s["severity"]]:
            s["severity"] = f.severity
    with open(os.path.join(out_dir, "identities_summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["identity_type", "identity", "max_severity", "occurrences", "used_in_count", "used_in"])
        for (itype, ident), s in sorted(summary.items(), key=lambda kv: (-SEVERITY_RANK[kv[1]["severity"]], kv[0])):
            w.writerow([itype, ident, s["severity"], s["count"], len(s["scopes"]), "; ".join(sorted(s["scopes"]))])
 
 
# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory service accounts across GitLab.")
    ap.add_argument("--url", default=os.getenv("GITLAB_URL", "https://gitlab.com"))
    ap.add_argument("--token", default=os.getenv("GITLAB_TOKEN"))
    ap.add_argument("--group", help="Limit to this group path (includes subgroups)")
    ap.add_argument("--out", default="./sa_inventory")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-code-scan", action="store_true", help="Skip repository file scanning")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--max-archive-mb", type=int, default=200)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
 
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.token:
        log.error("Set GITLAB_TOKEN or pass --token")
        return 2
 
    gl = gitlab.Gitlab(args.url, private_token=args.token, per_page=100,
                       retry_transient_errors=True, ssl_verify=os.getenv("REQUESTS_CA_BUNDLE", True))
    gl.auth()
    is_admin = bool(getattr(gl.user, "is_admin", False))
    log.info("Authenticated as %s (admin=%s) on %s", gl.user.username, is_admin, args.url)
    print("Authenticated successfully.")
 
    
    findings: list[Finding] = []
    bot_ids: set[int] = set()
    if is_admin and not args.group:
        inst, bot_ids = inventory_instance(gl)
        findings.extend(inst)
        log.info("Instance-level: %d findings, %d bot users", len(inst), len(bot_ids))
        print("Instance-level inventory complete.")
        print(inst)
        print(bot_ids)
    print(findings)
    
    # Groups
    archived_filter = {} if args.include_archived else {"archived": False}
    if args.group:
        root = gl.groups.get(args.group)
        groups = [root] + [gl.groups.get(sg.id) for sg in root.descendant_groups.list(iterator=True)]
        project_ids = [p.id for p in root.projects.list(include_subgroups=True, iterator=True, **archived_filter)]
    else:
        groups = list(gl.groups.list(iterator=True, all_available=is_admin))
        project_ids = [p.id for p in gl.projects.list(iterator=True, membership=not is_admin, **archived_filter)]
 
    for g in groups:
        findings.extend(inventory_group(gl, g))
        print("g")
    log.info("Scanned %d groups; scanning %d projects with %d workers", len(groups), len(project_ids), args.workers)
    #print(findings)
    
    # Projects (in parallel)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(inventory_project, gl, pid, bot_ids, not args.no_code_scan, args.max_archive_mb): pid
                   for pid in project_ids}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                path, res = fut.result()
                findings.extend(res)
                log.info("[%d/%d] %s: %d findings", i, len(project_ids), path, len(res))
            except Exception as e:  # keep going if one project fails
                log.error("[%d/%d] project %s failed: %s", i, len(project_ids), futures[fut], e)
 
    write_outputs(findings, args.out)
    by_sev = defaultdict(int)
    for f in findings:
        by_sev[f.severity] += 1
    log.info("Done. %d findings %s -> %s", len(findings), dict(by_sev), os.path.abspath(args.out))
    
    return 0
 
 
if __name__ == "__main__":
    print("GitLab Orion is running...")
    ok = test_connection()
    if not ok:
        sys.exit(1)

    print("GitLab Orion connection test passed.")
    sys.exit(main())
 