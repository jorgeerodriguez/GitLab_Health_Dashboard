"""Self-contained HTML dashboard for leadership.

Charts are rendered with matplotlib (Agg backend) and embedded as base64
PNGs, so the resulting HTML file opens standalone in any browser with no
network/CDN dependency. Colors follow a validated categorical/status
palette (see the `dataviz` skill): a single hue for magnitude charts, fixed
status colors (good/warning/serious/critical) only for genuine state
(runner status, KPI accents) — never a rainbow, never color standing in
for a legend a reader has to guess at.
"""

import base64
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

# --- palette (validated reference instance; see dataviz skill palette.md) ---
SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED_INK = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

BLUE = "#2a78d6"
ORANGE = "#eb6834"

STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "muted": MUTED_INK,
}
RUNNER_STATUS_COLOR = {
    "online": STATUS["good"],
    "offline": STATUS["critical"],
    "stale": STATUS["warning"],
    "never_contacted": STATUS["muted"],
}

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 11,
    "text.color": SECONDARY_INK,
    "axes.edgecolor": BASELINE,
    "axes.labelcolor": SECONDARY_INK,
    "xtick.color": MUTED_INK,
    "ytick.color": MUTED_INK,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
})


def _fig_to_data_uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _empty_chart(message: str) -> str:
    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.axis("off")
    ax.text(0.5, 0.5, message, ha="center", va="center", color=MUTED_INK, fontsize=11)
    return _fig_to_data_uri(fig)


def _style_axes(ax):
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis="x", color=GRID, linewidth=1, zorder=0)
    ax.set_axisbelow(True)


def success_rate_trend_chart(pipeline_history: pd.DataFrame) -> str:
    """Mean pipeline success rate over time — the real trend, built up run over run."""
    if pipeline_history.empty:
        return _empty_chart("No history yet — run Orion again to start a trend line.")

    trend = (
        pipeline_history.dropna(subset=["success_rate"])
        .groupby("snapshot_at")["success_rate"]
        .mean()
        .sort_index()
    )
    if trend.empty:
        return _empty_chart("No success-rate data recorded yet.")
    if len(trend) == 1:
        return _empty_chart(
            f"Only one snapshot so far ({trend.index[0]:%Y-%m-%d}) — "
            "run Orion again to start a trend line."
        )

    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(trend.index, trend.values, color=BLUE, linewidth=2, marker="o", markersize=8,
            markerfacecolor=BLUE, markeredgecolor=SURFACE, markeredgewidth=2)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Mean success rate (%)")
    last_x, last_y = trend.index[-1], trend.values[-1]
    ax.annotate(f"{last_y:.1f}%", (last_x, last_y), textcoords="offset points",
                xytext=(6, 6), color=PRIMARY_INK, fontsize=11, fontweight="bold")
    _style_axes(ax)
    ax.grid(axis="y", visible=False)
    fig.autofmt_xdate()
    return _fig_to_data_uri(fig)


def stage_failure_chart(jobs_df: pd.DataFrame) -> str:
    """Failure rate by CI stage, aggregated across all projects — worst first."""
    if jobs_df.empty:
        return _empty_chart("No job data collected.")

    agg = jobs_df.groupby("stage").agg(sampled=("sampled", "sum"), failures=("failures", "sum"))
    agg = agg[agg["sampled"] > 0]
    if agg.empty:
        return _empty_chart("No job data collected.")
    agg["failure_rate"] = (agg["failures"] / agg["sampled"] * 100).round(1)
    agg = agg.sort_values("failure_rate", ascending=True).tail(10)

    fig, ax = plt.subplots(figsize=(9, max(2.5, 0.4 * len(agg))))
    bars = ax.barh(agg.index, agg["failure_rate"], color=BLUE, height=0.6, zorder=3)
    for bar, value in zip(bars, agg["failure_rate"]):
        ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2, f"{value:.1f}%",
                va="center", color=PRIMARY_INK, fontsize=10)
    ax.set_xlabel("Failure rate (%)")
    ax.set_xlim(0, max(100, agg["failure_rate"].max() * 1.15))
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def runner_status_chart(runners_df: pd.DataFrame) -> str:
    """Runner status breakdown — colors map to actual state, not arbitrary series."""
    if runners_df.empty:
        return _empty_chart("No runners visible to this token/group.")

    counts = runners_df["status"].fillna("unknown").value_counts()
    colors = [RUNNER_STATUS_COLOR.get(status, MUTED_INK) for status in counts.index]

    fig, ax = plt.subplots(figsize=(6, 3.2))
    bars = ax.bar(counts.index, counts.values, color=colors, width=0.6, zorder=3)
    for bar, value in zip(bars, counts.values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.05, str(value),
                ha="center", color=PRIMARY_INK, fontsize=11, fontweight="bold")
    ax.set_ylabel("Runners")
    _style_axes(ax)
    ax.grid(axis="y", visible=False)
    return _fig_to_data_uri(fig)


def mr_age_histogram(mr_df: pd.DataFrame) -> str:
    """Distribution of how long open MRs have sat since their last update."""
    if mr_df.empty:
        return _empty_chart("No open merge requests.")

    fig, ax = plt.subplots(figsize=(9, 3))
    ax.hist(mr_df["age_days"], bins=min(20, max(5, mr_df["age_days"].nunique())),
            color=BLUE, edgecolor=SURFACE, linewidth=1, zorder=3)
    ax.set_xlabel("Days since last update")
    ax.set_ylabel("Open MRs")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def stale_branch_chart(branches_df: pd.DataFrame) -> str:
    """Stale (non-default, no recent commit) branch count, worst projects first."""
    if branches_df.empty:
        return _empty_chart("No branch data collected.")

    stale = branches_df[branches_df["is_stale"] == True]  # noqa: E712
    if stale.empty:
        return _empty_chart("No stale branches found.")

    counts = stale.groupby("project_path").size().sort_values(ascending=True).tail(15)
    fig, ax = plt.subplots(figsize=(9, max(2.5, 0.35 * len(counts))))
    bars = ax.barh(counts.index, counts.values, color=ORANGE, height=0.6, zorder=3)
    for bar, value in zip(bars, counts.values):
        ax.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height() / 2, str(value),
                va="center", color=PRIMARY_INK, fontsize=10)
    ax.set_xlabel("Stale branches")
    _style_axes(ax)
    return _fig_to_data_uri(fig)


def _kpi_tile(label: str, value: str, status: str) -> str:
    color = STATUS.get(status, MUTED_INK)
    return f"""
    <div class="kpi-tile" style="border-left-color: {color}">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value">{value}</div>
    </div>"""


def _status_for(count: int, warn_at: int = 1) -> str:
    return "good" if count < warn_at else ("warning" if count < warn_at * 5 else "critical")


def _build_kpi_tiles(latest: dict) -> str:
    pipelines = latest["pipelines"]
    total_projects = len(pipelines)
    healthy_pct = (
        round((pipelines["category"] == "healthy").sum() / total_projects * 100, 1)
        if total_projects else 0.0
    )
    unhealthy_count = int((pipelines["category"] == "unhealthy").sum())
    stale_mr_count = int(latest["merge_requests"]["is_stale"].sum()) if not latest["merge_requests"].empty else 0
    runners_df = latest["runners"]
    offline_runners = int(runners_df["status"].isin(["offline", "stale"]).sum()) if not runners_df.empty else 0
    stale_branches = int(latest["branches"]["is_stale"].sum()) if not latest["branches"].empty else 0
    violations = int(latest["protection_drift"]["violations"].notna().sum()) if not latest["protection_drift"].empty else 0

    tiles = [
        ("Projects scanned", str(total_projects), "muted"),
        ("Healthy pipelines", f"{healthy_pct}%", "good" if healthy_pct >= 80 else "warning" if healthy_pct >= 50 else "critical"),
        ("Unhealthy projects", str(unhealthy_count), _status_for(unhealthy_count, warn_at=1)),
        ("Stale MRs (14+ days)", str(stale_mr_count), _status_for(stale_mr_count, warn_at=1)),
        ("Offline/stale runners", str(offline_runners), _status_for(offline_runners, warn_at=1)),
        ("Stale branches", str(stale_branches), _status_for(stale_branches, warn_at=5)),
        ("Branch-protection violations", str(violations), _status_for(violations, warn_at=1)),
    ]
    return "\n".join(_kpi_tile(label, value, status) for label, value, status in tiles)


def _table_html(df: pd.DataFrame, columns: list[str], empty_message: str, max_rows: int = 25) -> str:
    if df.empty:
        return f'<p class="empty-note">{empty_message}</p>'
    subset = df[columns].head(max_rows)
    header = "".join(f"<th>{c}</th>" for c in columns)
    rows = "\n".join(
        "<tr>" + "".join(f"<td>{'' if pd.isna(v) else v}</td>" for v in row) + "</tr>"
        for row in subset.itertuples(index=False)
    )
    return f'<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>'


def build_dashboard_html(latest: dict, history: dict, out_path: Path, group_path: str, snapshot_at):
    """Assemble the full leadership dashboard and write it to out_path."""
    kpi_html = _build_kpi_tiles(latest)

    trend_chart = success_rate_trend_chart(history["pipelines"])
    stage_chart = stage_failure_chart(latest["jobs"])
    runner_chart = runner_status_chart(latest["runners"])
    mr_chart = mr_age_histogram(latest["merge_requests"])
    branch_chart = stale_branch_chart(latest["branches"])

    unhealthy = latest["pipelines"][latest["pipelines"]["category"] == "unhealthy"].sort_values("success_rate")
    unhealthy_table = _table_html(
        unhealthy, ["project_path", "last_status", "success_rate", "reason"],
        "No unhealthy projects.",
    )

    drift = latest["protection_drift"][latest["protection_drift"]["violations"].notna()]
    drift_table = _table_html(
        drift, ["project_path", "default_branch", "violations"],
        "No branch-protection policy violations found.",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_TEMPLATE.format(
        group_path=group_path,
        generated_at=snapshot_at.strftime("%Y-%m-%d %H:%M UTC"),
        kpi_html=kpi_html,
        trend_chart=trend_chart,
        stage_chart=stage_chart,
        runner_chart=runner_chart,
        mr_chart=mr_chart,
        branch_chart=branch_chart,
        unhealthy_table=unhealthy_table,
        drift_table=drift_table,
    ))
    print(f"Wrote dashboard to {out_path}")


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GitLab Orion — {group_path}</title>
<style>
  :root {{
    color-scheme: light dark;
    --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e;
    --muted: #898781; --border: rgba(11,11,11,0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink-2: #c3c2b7; --border: rgba(255,255,255,0.10); }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--ink-2); margin: 0 0 28px; font-size: 14px; }}
  .kpi-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 32px; }}
  .kpi-tile {{
    background: var(--surface); border: 1px solid var(--border); border-left: 4px solid;
    border-radius: 8px; padding: 14px 16px;
  }}
  .kpi-label {{ font-size: 12px; color: var(--ink-2); margin-bottom: 6px; }}
  .kpi-value {{ font-size: 26px; font-weight: 600; color: var(--ink); }}
  .charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; margin-bottom: 32px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; overflow-x: auto; }}
  .card h2 {{ font-size: 15px; margin: 0 0 12px; color: var(--ink); }}
  .card img {{ max-width: 100%; height: auto; display: block; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--ink-2); font-weight: 600; }}
  .empty-note {{ color: var(--muted); font-size: 13px; }}
  section {{ margin-bottom: 32px; }}
  section h2 {{ font-size: 16px; margin: 0 0 12px; }}
</style>
</head>
<body>
  <h1>GitLab Orion — {group_path}</h1>
  <p class="subtitle">Generated {generated_at}</p>

  <div class="kpi-row">
{kpi_html}
  </div>

  <div class="charts">
    <div class="card">
      <h2>Pipeline success rate over time</h2>
      <img src="{trend_chart}" alt="Pipeline success rate trend">
    </div>
    <div class="card">
      <h2>Failure rate by CI stage</h2>
      <img src="{stage_chart}" alt="Failure rate by stage">
    </div>
    <div class="card">
      <h2>Runner status</h2>
      <img src="{runner_chart}" alt="Runner status breakdown">
    </div>
    <div class="card">
      <h2>Open MR age</h2>
      <img src="{mr_chart}" alt="Open merge request age distribution">
    </div>
    <div class="card">
      <h2>Stale branches by project</h2>
      <img src="{branch_chart}" alt="Stale branch count by project">
    </div>
  </div>

  <section>
    <h2>Unhealthy projects</h2>
    <div class="card">{unhealthy_table}</div>
  </section>

  <section>
    <h2>Branch-protection policy violations</h2>
    <div class="card">{drift_table}</div>
  </section>
</body>
</html>
"""
