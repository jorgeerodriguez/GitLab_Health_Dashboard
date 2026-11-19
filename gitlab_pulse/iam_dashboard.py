"""Self-contained HTML dashboard for the GCP IAM report.

Built from the two CSVs GitLab_IAM_Report.py writes (one row per IAM merge
request, one row per grant change). Reuses the Pulse dashboard's matplotlib
helpers and palette so both dashboards read as one system: one hue (BLUE)
for magnitude, status colors only for risk tier, which is genuine state and
always shown with a text label.
"""

import html
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Patch

from gitlab_pulse.dashboard import (
    BLUE, MUTED_INK, PRIMARY_INK, STATUS,
    _chart_section, _empty_chart, _fig_to_data_uri, _kpi_tile, _style_axes,
)

RISK_COLOR = {"high": STATUS["critical"], "medium": STATUS["warning"], "low": STATUS["good"], "unknown": MUTED_INK}
RISK_ORDER = ["high", "medium", "low", "unknown"]
UNPARSED = ("(outside diff context)", "(not in diff)")
LEAD_BUCKETS = [(0, 1, "< 1 h"), (1, 4, "1–4 h"), (4, 24, "4–24 h"), (24, 72, "1–3 days"),
                (72, 168, "3–7 days"), (168, float("inf"), "> 7 days")]


def _prepare(mrs: pd.DataFrame, grants: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    mrs, grants = mrs.copy(), grants.copy()
    for df in (mrs, grants):
        for col in ("created_at", "merged_at"):
            if col in df:
                df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    grants["member"] = grants["member"].fillna("")
    return mrs, grants


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _value_labels_h(ax, bars, values, fmt="{:,}"):
    for bar, value in zip(bars, values):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, "  " + fmt.format(value),
                va="center", color=PRIMARY_INK, fontsize=10)


def monthly_trend_chart(mrs: pd.DataFrame) -> str:
    merged = mrs.dropna(subset=["merged_at"])
    if merged.empty:
        return _empty_chart("No merged IAM merge requests.")
    counts = merged["merged_at"].dt.strftime("%Y-%m").value_counts().sort_index()
    labels = [datetime.strptime(m, "%Y-%m").strftime("%b") for m in counts.index]

    fig, ax = plt.subplots(figsize=(13, 4.2))
    bars = ax.bar(labels, counts.values, color=BLUE, width=0.6, zorder=3)
    if counts.index[-1] == datetime.now(timezone.utc).strftime("%Y-%m"):
        bars[-1].set_alpha(0.45)  # partial month reads lighter, and the label says so
        ax.annotate("month to date", (bars[-1].get_x() + bars[-1].get_width() / 2, counts.values[-1]),
                    textcoords="offset points", xytext=(0, 18), ha="center", color=MUTED_INK, fontsize=9)
    for bar, value in zip(bars, counts.values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value}",
                ha="center", va="bottom", color=PRIMARY_INK, fontsize=10)
    ax.set_ylabel("Merged IAM MRs")
    _style_axes(ax)
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color="#e1e0d9", linewidth=1, zorder=0)
    return _fig_to_data_uri(fig)


def lead_time_chart(mrs: pd.DataFrame) -> str:
    lt = mrs["lead_time_hours"].dropna()
    if lt.empty:
        return _empty_chart("No merged IAM merge requests.")
    counts = [((lt >= lo) & (lt < hi)).sum() for lo, hi, _ in LEAD_BUCKETS]
    labels = [label for _, _, label in LEAD_BUCKETS]

    fig, ax = plt.subplots(figsize=(13, 3.8))
    bars = ax.bar(labels, counts, color=BLUE, width=0.6, zorder=3)
    total = sum(counts)
    for bar, value in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value}  ({value / total:.0%})",
                ha="center", va="bottom", color=PRIMARY_INK, fontsize=10)
    ax.set_ylabel("Merged IAM MRs")
    ax.set_xlabel("Time from MR opened to merged")
    _style_axes(ax)
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color="#e1e0d9", linewidth=1, zorder=0)
    return _fig_to_data_uri(fig)


def top_repos_chart(mrs: pd.DataFrame, n: int = 15) -> str:
    if mrs.empty:
        return _empty_chart("No IAM merge requests.")
    counts = mrs["repo"].value_counts().head(n).sort_values()
    fig, ax = plt.subplots(figsize=(13, max(3, 0.42 * len(counts))))
    bars = ax.barh(counts.index, counts.values, color=BLUE, height=0.6, zorder=3)
    _value_labels_h(ax, bars, counts.values)
    ax.set_xlabel("IAM merge requests")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def top_roles_chart(grants: pd.DataFrame, n: int = 20) -> str:
    added = grants[(grants["action"] == "added") & ~grants["role"].isin(UNPARSED)]
    if added.empty:
        return _empty_chart("No parsed role grants.")
    top = added.groupby("role").agg(grants=("role", "size"), tier=("risk_tier", "first"))
    top = top.sort_values("grants").tail(n)

    fig, ax = plt.subplots(figsize=(13, max(3, 0.42 * len(top))))
    bars = ax.barh(top.index, top["grants"], color=[RISK_COLOR.get(t, MUTED_INK) for t in top["tier"]],
                   height=0.6, zorder=3)
    _value_labels_h(ax, bars, top["grants"])
    ax.set_xlabel("Grants added")
    present = [t for t in RISK_ORDER if t in set(top["tier"])]
    ax.legend(handles=[Patch(color=RISK_COLOR[t], label=f"{t} risk") for t in present],
              loc="lower right", frameon=False, fontsize=10)
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def risk_tier_chart(grants: pd.DataFrame) -> str:
    added = grants[grants["action"] == "added"]
    if added.empty:
        return _empty_chart("No grants added.")
    counts = added["risk_tier"].value_counts().reindex(RISK_ORDER).dropna().astype(int)

    fig, ax = plt.subplots(figsize=(13, 2.8))
    bars = ax.barh([f"{t} risk" for t in counts.index][::-1], counts.values[::-1],
                   color=[RISK_COLOR[t] for t in counts.index][::-1], height=0.6, zorder=3)
    total = counts.sum()
    for bar, value in zip(bars, counts.values[::-1]):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  {value:,}  ({value / total:.0%})",
                va="center", color=PRIMARY_INK, fontsize=10)
    ax.set_xlabel("Grants added")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def member_type_chart(grants: pd.DataFrame) -> str:
    added = grants[grants["action"] == "added"]
    if added.empty:
        return _empty_chart("No grants added.")
    labels = {
        "service_account": "Service account", "group": "Google group", "unknown": "Not readable from diff",
        "google_service_agent": "Google service agent", "workload_identity": "Workload identity",
        "user": "Individual user", "terraform_reference": "Terraform output reference", "domain": "Domain",
    }
    counts = added["member_type"].value_counts().sort_values()
    fig, ax = plt.subplots(figsize=(13, max(2.8, 0.42 * len(counts))))
    bars = ax.barh([labels.get(k, k) for k in counts.index], counts.values, color=BLUE, height=0.6, zorder=3)
    _value_labels_h(ax, bars, counts.values)
    ax.set_xlabel("Grants added")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def author_chart(mrs: pd.DataFrame, n: int = 10) -> str:
    if mrs.empty:
        return _empty_chart("No IAM merge requests.")
    counts = mrs["author"].value_counts()
    top = counts.head(n).sort_values()
    total = counts.sum()
    fig, ax = plt.subplots(figsize=(13, max(3, 0.42 * len(top))))
    bars = ax.barh(top.index, top.values, color=BLUE, height=0.6, zorder=3)
    for bar, value in zip(bars, top.values):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  {value}  ({value / total:.0%})",
                va="center", color=PRIMARY_INK, fontsize=10)
    ax.set_xlabel("IAM merge requests authored")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _cell(value, link: str | None = None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return ""
    if isinstance(value, pd.Timestamp):
        value = value.strftime("%Y-%m-%d")
    text = html.escape(str(value))
    return f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{text}</a>' if link else text


def _table(df: pd.DataFrame, columns: dict[str, str], empty_message: str,
           link_col: str | None = None, max_rows: int = 30, wrap: tuple[str, ...] = ()) -> str:
    """columns maps dataframe column -> header text; link_col's cell links to the row's url.

    Cells stay on one line except the long-text columns named in wrap.
    """
    if df.empty:
        return f'<p class="empty-note">{html.escape(empty_message)}</p>'
    header = "".join(f"<th>{html.escape(h)}</th>" for h in columns.values())
    rows = []
    for _, row in df.head(max_rows).iterrows():
        cells = "".join(
            f"<td{' class=\"wrap\"' if c in wrap else ''}>{_cell(row[c], row.get('url') if c == link_col else None)}</td>"
            for c in columns
        )
        rows.append(f"<tr>{cells}</tr>")
    more = (f'<p class="empty-note">Showing {max_rows} of {len(df):,} rows. The full list is in the CSV.</p>'
            if len(df) > max_rows else "")
    return f'<table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>{more}'


def self_service_table(grants: pd.DataFrame) -> str:
    added = grants[(grants["action"] == "added") & ~grants["role"].isin(UNPARSED)
                   & (grants["member_type"] != "unknown")]
    combo = (added.groupby(["role", "member_type"])
             .agg(grants=("role", "size"), mrs=("url", "nunique"), repos=("repo", "nunique"),
                  risk_tier=("risk_tier", "first"))
             .reset_index().sort_values("grants", ascending=False))
    return _table(combo, {"role": "Role", "member_type": "Granted to", "risk_tier": "Risk",
                          "grants": "Grants", "mrs": "MRs", "repos": "Repos"},
                  "No repeated grants found.", max_rows=20)


def high_risk_table(grants: pd.DataFrame) -> str:
    rows = grants[(grants["action"] == "added") & (grants["risk_tier"] == "high")]
    rows = rows.drop_duplicates(["url", "role", "member"]).sort_values("created_at", ascending=False)
    return _table(rows, {"created_at": "Opened", "repo": "Repo", "iid": "MR", "role": "Role",
                         "member": "Member", "state": "State"},
                  "No high-risk grants added.", link_col="iid", max_rows=25, wrap=("member",))


def user_grants_table(grants: pd.DataFrame) -> str:
    rows = grants[(grants["action"] == "added") & (grants["member_type"] == "user")]
    rows = rows.drop_duplicates(["url", "role", "member"]).sort_values("created_at", ascending=False)
    return _table(rows, {"created_at": "Opened", "repo": "Repo", "iid": "MR", "role": "Role",
                         "member": "User", "state": "State"},
                  "No grants to individual users.", link_col="iid", max_rows=60, wrap=("member",))


def multi_repo_table(mrs: pd.DataFrame) -> str:
    keyed = mrs.dropna(subset=["jira_key"])
    agg = (keyed.groupby("jira_key")
           .agg(repos=("repo", "nunique"), mrs=("iid", "size"),
                repo_list=("repo", lambda s: ", ".join(sorted(set(s)))))
           .reset_index())
    agg = agg[agg["repos"] > 1].sort_values(["repos", "mrs"], ascending=False)
    return _table(agg, {"jira_key": "Ticket", "repos": "Repos", "mrs": "MRs", "repo_list": "Where"},
                  "No ticket needed IAM changes in more than one repo.", max_rows=20, wrap=("repo_list",))


def closed_table(mrs: pd.DataFrame) -> str:
    closed = mrs[mrs["state"] == "closed"].sort_values("created_at", ascending=False)
    return _table(closed, {"created_at": "Opened", "repo": "Repo", "iid": "MR", "title": "Title",
                           "author": "Author", "review_comments": "Comments"},
                  "No IAM merge requests were closed without merging.", link_col="iid", max_rows=25,
                  wrap=("title",))


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _kpis(mrs: pd.DataFrame, grants: pd.DataFrame) -> str:
    merged = mrs[mrs["state"] == "merged"]
    added = grants[grants["action"] == "added"]
    lt = merged["lead_time_hours"].dropna()
    users = int((added["member_type"] == "user").sum())
    tiles = [
        ("IAM merge requests", f"{len(mrs):,}", "muted"),
        ("Grants added", f"{len(added):,}", "muted"),
        ("Grants removed", f"{(grants['action'] == 'removed').sum():,}", "muted"),
        ("Median time to merge", f"{lt.median():.1f} h" if not lt.empty else "–", "muted"),
        ("90th percentile time to merge", f"{lt.quantile(.9):.0f} h" if not lt.empty else "–", "muted"),
        ("Closed without merging", f"{(mrs['state'] == 'closed').mean():.0%}" if len(mrs) else "–", "muted"),
        ("Merged with no review comments",
         f"{(merged['review_comments'] == 0).mean():.0%}" if len(merged) else "–", "muted"),
        ("High-risk grants added", f"{(added['risk_tier'] == 'high').sum():,}", "muted"),
        ("Grants to individual users", f"{users:,}", "warning" if users else "good"),
    ]
    return "\n".join(_kpi_tile(label, value, status) for label, value, status in tiles)


def _coverage_note(grants: pd.DataFrame) -> str:
    added = grants[grants["action"] == "added"]
    if added.empty:
        return ""
    no_role = (added["role"] == "(outside diff context)").mean()
    no_member = (added["member_type"] == "unknown").mean()
    return (f"{no_role:.0%} of added grants have no readable role and {no_member:.0%} have no readable "
            "member, because the role or member line falls outside what the diff shows or is defined in a "
            "variable. Those grants count in the totals but not in the role and member charts.")


def build_iam_dashboard_html(mrs: pd.DataFrame, grants: pd.DataFrame, out_path: Path,
                             groups: str, since: str) -> None:
    """Assemble the IAM dashboard and write it to out_path."""
    mrs, grants = _prepare(mrs, grants)

    charts = "\n".join([
        _chart_section("trend", "IAM merge requests merged per month", monthly_trend_chart(mrs),
                       "Merged IAM merge requests per month"),
        _chart_section("lead", "How long IAM merge requests take to merge", lead_time_chart(mrs),
                       "Distribution of time from MR opened to merged"),
        _chart_section("roles", "Most-granted roles, colored by risk tier", top_roles_chart(grants),
                       "Most frequently granted roles"),
        _chart_section("risk", "Risk tier of grants added", risk_tier_chart(grants),
                       "Grants added by risk tier"),
        _chart_section("members", "Who receives access", member_type_chart(grants),
                       "Grants added by member type"),
        _chart_section("repos", "Repos with the most IAM merge requests", top_repos_chart(mrs),
                       "IAM merge requests per repo"),
        _chart_section("authors", "Who does the IAM work", author_chart(mrs),
                       "IAM merge requests per author"),
    ])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_TEMPLATE.substitute(
        groups=html.escape(groups),
        since=html.escape(since),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        kpi_html=_kpis(mrs, grants),
        charts_html=charts,
        self_service_table=self_service_table(grants),
        multi_repo_table=multi_repo_table(mrs),
        high_risk_table=high_risk_table(grants),
        user_grants_table=user_grants_table(grants),
        closed_table=closed_table(mrs),
        coverage_note=html.escape(_coverage_note(grants)),
    ))
    print(f"Wrote IAM dashboard to {out_path}")


# string.Template ($var) for the same reason as the Pulse dashboard: the CSS
# and JS are full of literal braces.
_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GCP IAM Report</title>
<style>
  :root {
    color-scheme: light dark;
    --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e;
    --muted: #898781; --border: rgba(11,11,11,0.10); --link: #2a78d6;
  }
  @media (prefers-color-scheme: dark) {
    :root { --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink-2: #c3c2b7;
            --border: rgba(255,255,255,0.10); --link: #6da7ec; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  @media (max-width: 640px) { body { padding: 16px; } }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .subtitle { color: var(--ink-2); margin: 0 0 28px; font-size: 14px; }
  .kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 32px; }
  .kpi-tile {
    background: var(--surface); border: 1px solid var(--border); border-left: 4px solid;
    border-radius: 8px; padding: 14px 16px;
  }
  .kpi-label { font-size: 12px; color: var(--ink-2); margin-bottom: 6px; }
  .kpi-value { font-size: 26px; font-weight: 600; color: var(--ink); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; overflow-x: auto; }
  .card img { max-width: 100%; height: auto; display: block; }
  .chart-section { margin-bottom: 24px; }
  .chart-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .chart-header h2 { font-size: 15px; margin: 0; color: var(--ink); }
  .copy-btn {
    flex: none; font-size: 12px; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--ink-2); cursor: pointer; white-space: nowrap;
  }
  .copy-btn:hover { color: var(--ink); border-color: var(--ink-2); }
  .copy-btn.is-copied { color: #0ca30c; border-color: #0ca30c; }
  .copy-btn.is-failed { color: #d03b3b; border-color: #d03b3b; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  td { white-space: nowrap; }
  td.wrap { white-space: normal; overflow-wrap: anywhere; min-width: 240px; }
  th { color: var(--ink-2); font-weight: 600; }
  a { color: var(--link); }
  .empty-note { color: var(--muted); font-size: 13px; }
  section { margin-bottom: 32px; }
  section > h2 { font-size: 16px; margin: 0 0 4px; }
  .why { color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }
  .notes { color: var(--ink-2); font-size: 13px; line-height: 1.5; }
  .notes li { margin-bottom: 6px; }
</style>
</head>
<body>
  <h1>GCP IAM Report</h1>
  <p class="subtitle">IAM changes made through merge requests in $groups, created since $since. Generated $generated_at.</p>

  <div class="kpi-row">
$kpi_html
  </div>

$charts_html

  <section>
    <h2>Self-service candidates</h2>
    <p class="why">The role and member-type combinations granted most often. Frequent, low-risk combinations are the best candidates for a request form or automatic approval.</p>
    <div class="card">$self_service_table</div>
  </section>

  <section>
    <h2>Tickets that needed IAM changes in more than one repo</h2>
    <p class="why">Usually the same grant made in nonprod and then prod. One request could update every environment.</p>
    <div class="card">$multi_repo_table</div>
  </section>

  <section>
    <h2>Recent high-risk grants</h2>
    <p class="why">Admin, owner/editor, IAM and service-account impersonation roles. These should keep a human review.</p>
    <div class="card">$high_risk_table</div>
  </section>

  <section>
    <h2>Grants to individual users</h2>
    <p class="why">Access given to a person directly instead of through a group.</p>
    <div class="card">$user_grants_table</div>
  </section>

  <section>
    <h2>Closed without merging</h2>
    <p class="why">Work that was started and abandoned or redone. The titles show where requests go wrong.</p>
    <div class="card">$closed_table</div>
  </section>

  <section>
    <h2>About this data</h2>
    <ul class="notes">
      <li>Counts come from reading each merge request's diff, so they show what was changed, not what is applied in GCP today.</li>
      <li>$coverage_note</li>
      <li>Time to merge starts when the MR is opened. Time spent before that, for example in Jira, is not included.</li>
      <li>Risk tiers are a simple rule based on the role name, not a security policy.</li>
    </ul>
  </section>

<script>
async function copyChartImage(btn, imgId) {
  const img = document.getElementById(imgId);
  const original = btn.textContent;
  btn.classList.remove('is-copied', 'is-failed');
  try {
    const response = await fetch(img.src);
    const blob = await response.blob();
    if (!navigator.clipboard || !window.ClipboardItem) {
      throw new Error('Clipboard API unavailable');
    }
    await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
    btn.textContent = 'Copied!';
    btn.classList.add('is-copied');
  } catch (err) {
    btn.textContent = 'Right-click image \\u2192 Copy';
    btn.classList.add('is-failed');
  }
  setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove('is-copied', 'is-failed');
  }, 2000);
}
</script>
</body>
</html>
""")
