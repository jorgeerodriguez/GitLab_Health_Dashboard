#!/usr/bin/env python3
"""Interactive Service Account dashboard backed by sa_inventory/findings.csv.

Run:
	streamlit run Service_Accounts_Dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st  # pyright: ignore[reportMissingImports]


DATA_PATH = Path(__file__).resolve().parent / "sa_inventory" / "findings.csv"
PRESETS_PATH = Path(__file__).resolve().parent / "sa_inventory" / "team_filter_presets.json"

DISPLAY_COLUMNS = [
	"identity",
	"identity_type",
	"category",
	"severity",
	"scope",
	"scope_path",
	"location",
	"web_url",
]

FILTER_KEYS = {
	"scopes": "filter_scopes",
	"categories": "filter_categories",
	"severity": "filter_severity",
	"roots": "filter_roots",
	"scope_path_query": "filter_scope_path_query",
	"location_query": "filter_location_query",
	"identity_query": "filter_identity_query",
	"team_preset": "filter_team_preset",
	"team_preset_loaded": "filter_team_preset_loaded",
}


@st.cache_data(show_spinner=False)
def load_findings(csv_path: Path) -> pd.DataFrame:
	if not csv_path.exists():
		raise FileNotFoundError(f"CSV file not found: {csv_path}")

	df = pd.read_csv(csv_path)

	required = {
		"category",
		"identity_type",
		"identity",
		"scope",
		"scope_path",
		"location",
		"severity",
		"web_url",
	}
	missing = required - set(df.columns)
	if missing:
		missing_cols = ", ".join(sorted(missing))
		raise ValueError(f"Missing required columns in findings.csv: {missing_cols}")

	df = df.copy()
	for col in required:
		df[col] = df[col].fillna("").astype(str)

	df["scope_path_root"] = df["scope_path"].str.split("/").str[:2].str.join("/")
	return df


def load_presets(presets_path: Path) -> dict[str, dict]:
	if not presets_path.exists():
		return {}

	try:
		with presets_path.open("r", encoding="utf-8") as f:
			data = json.load(f)
	except (json.JSONDecodeError, OSError):
		return {}

	if not isinstance(data, dict):
		return {}

	cleaned: dict[str, dict] = {}
	for name, payload in data.items():
		if isinstance(name, str) and isinstance(payload, dict):
			cleaned[name] = payload
	return cleaned


def save_presets(presets_path: Path, presets: dict[str, dict]) -> None:
	presets_path.parent.mkdir(parents=True, exist_ok=True)
	with presets_path.open("w", encoding="utf-8") as f:
		json.dump(presets, f, indent=2, sort_keys=True)


def _list_from_preset(value: object) -> list[str]:
	if not isinstance(value, list):
		return []
	return [str(v) for v in value if str(v)]


def _str_from_preset(value: object) -> str:
	return str(value) if isinstance(value, str) else ""


def apply_team_preset(preset: dict) -> None:
	st.session_state[FILTER_KEYS["scopes"]] = _list_from_preset(preset.get("scopes"))
	st.session_state[FILTER_KEYS["categories"]] = _list_from_preset(
		preset.get("categories")
	)
	st.session_state[FILTER_KEYS["severity"]] = _list_from_preset(
		preset.get("severity")
	)
	st.session_state[FILTER_KEYS["roots"]] = _list_from_preset(preset.get("roots"))
	st.session_state[FILTER_KEYS["scope_path_query"]] = _str_from_preset(
		preset.get("scope_path_query")
	)
	st.session_state[FILTER_KEYS["location_query"]] = _str_from_preset(
		preset.get("location_query")
	)
	st.session_state[FILTER_KEYS["identity_query"]] = _str_from_preset(
		preset.get("identity_query")
	)


def render_html_table(df: pd.DataFrame, *, max_rows: int | None = None) -> None:
	if df.empty:
		st.info("No data to show.")
		return

	show_df = df.head(max_rows) if max_rows else df
	html = show_df.to_html(index=False, escape=False)
	st.markdown(
		"""
		<style>
		table {
			width: 100%;
			border-collapse: collapse;
			font-size: 0.9rem;
		}
		th, td {
			border: 1px solid #d0d7de;
			padding: 6px 8px;
			text-align: left;
			vertical-align: top;
		}
		th {
			background: #f6f8fa;
			font-weight: 600;
		}
		</style>
		""",
		unsafe_allow_html=True,
	)
	st.markdown(html, unsafe_allow_html=True)


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
	st.sidebar.header("Filters")

	scope_choices = sorted(x for x in df["scope"].dropna().unique() if x)
	category_choices = sorted(x for x in df["category"].dropna().unique() if x)
	severity_order = ["critical", "high", "warn", "info"]
	severity_found = [x for x in severity_order if x in set(df["severity"].str.lower())]
	root_choices = sorted(x for x in df["scope_path_root"].dropna().unique() if x)
	presets = load_presets(PRESETS_PATH)
	preset_names = sorted(presets.keys())

	if FILTER_KEYS["scopes"] not in st.session_state:
		st.session_state[FILTER_KEYS["scopes"]] = scope_choices
	if FILTER_KEYS["categories"] not in st.session_state:
		st.session_state[FILTER_KEYS["categories"]] = category_choices
	if FILTER_KEYS["severity"] not in st.session_state:
		st.session_state[FILTER_KEYS["severity"]] = severity_found
	if FILTER_KEYS["roots"] not in st.session_state:
		st.session_state[FILTER_KEYS["roots"]] = []
	if FILTER_KEYS["scope_path_query"] not in st.session_state:
		st.session_state[FILTER_KEYS["scope_path_query"]] = ""
	if FILTER_KEYS["location_query"] not in st.session_state:
		st.session_state[FILTER_KEYS["location_query"]] = ""
	if FILTER_KEYS["identity_query"] not in st.session_state:
		st.session_state[FILTER_KEYS["identity_query"]] = ""
	if FILTER_KEYS["team_preset"] not in st.session_state:
		st.session_state[FILTER_KEYS["team_preset"]] = "(none)"
	if FILTER_KEYS["team_preset_loaded"] not in st.session_state:
		st.session_state[FILTER_KEYS["team_preset_loaded"]] = "(none)"

	# Keep state valid if source data changed.
	st.session_state[FILTER_KEYS["scopes"]] = [
		x for x in st.session_state[FILTER_KEYS["scopes"]] if x in scope_choices
	]
	st.session_state[FILTER_KEYS["categories"]] = [
		x for x in st.session_state[FILTER_KEYS["categories"]] if x in category_choices
	]
	st.session_state[FILTER_KEYS["severity"]] = [
		x for x in st.session_state[FILTER_KEYS["severity"]] if x in severity_found
	]
	st.session_state[FILTER_KEYS["roots"]] = [
		x for x in st.session_state[FILTER_KEYS["roots"]] if x in root_choices
	]

	selected_preset = st.sidebar.selectbox(
		"Team preset",
		options=["(none)"] + preset_names,
		key=FILTER_KEYS["team_preset"],
		help="Choose a saved team preset. It applies automatically.",
	)

	# Auto-apply when the selected preset changes.
	if selected_preset != st.session_state[FILTER_KEYS["team_preset_loaded"]]:
		if selected_preset != "(none)":
			apply_team_preset(presets.get(selected_preset, {}))
		st.session_state[FILTER_KEYS["team_preset_loaded"]] = selected_preset
		st.rerun()

	selected_scopes = st.sidebar.multiselect(
		"Scope",
		scope_choices,
		key=FILTER_KEYS["scopes"],
	)
	selected_categories = st.sidebar.multiselect(
		"Category",
		category_choices,
		key=FILTER_KEYS["categories"],
	)
	selected_severity = st.sidebar.multiselect(
		"Severity",
		severity_found,
		key=FILTER_KEYS["severity"],
	)
	selected_roots = st.sidebar.multiselect(
		"Scope root",
		root_choices,
		key=FILTER_KEYS["roots"],
	)

	scope_path_query = st.sidebar.text_input(
		"scope_path contains",
		placeholder="ex: audacy-inc/devops",
		key=FILTER_KEYS["scope_path_query"],
	)
	location_query = st.sidebar.text_input(
		"location contains",
		placeholder="ex: ci variable, terragrunt.hcl",
		key=FILTER_KEYS["location_query"],
	)
	identity_query = st.sidebar.text_input(
		"identity contains",
		placeholder="ex: sa-tf-admin",
		key=FILTER_KEYS["identity_query"],
	)

	preset_name_input = st.sidebar.text_input(
		"Preset name",
		placeholder="ex: DevOps Team",
		help="Save current filters for a team.",
	)
	manage_cols = st.sidebar.columns(2)
	if manage_cols[0].button("Save preset"):
		name = preset_name_input.strip()
		if not name:
			st.sidebar.warning("Enter a preset name before saving.")
		else:
			presets[name] = {
				"scopes": selected_scopes,
				"categories": selected_categories,
				"severity": selected_severity,
				"roots": selected_roots,
				"scope_path_query": scope_path_query,
				"location_query": location_query,
				"identity_query": identity_query,
			}
			save_presets(PRESETS_PATH, presets)
			st.session_state[FILTER_KEYS["team_preset"]] = name
			st.session_state[FILTER_KEYS["team_preset_loaded"]] = name
			st.sidebar.success(f"Saved preset: {name}")

	if manage_cols[1].button("Delete preset"):
		name = selected_preset
		if name == "(none)":
			st.sidebar.warning("Choose a preset to delete.")
		elif name in presets:
			del presets[name]
			save_presets(PRESETS_PATH, presets)
			st.session_state[FILTER_KEYS["team_preset"]] = "(none)"
			st.session_state[FILTER_KEYS["team_preset_loaded"]] = "(none)"
			st.sidebar.success(f"Deleted preset: {name}")
			st.rerun()

	filtered = df.copy()
	if selected_scopes:
		filtered = filtered[filtered["scope"].isin(selected_scopes)]
	if selected_categories:
		filtered = filtered[filtered["category"].isin(selected_categories)]
	if selected_severity:
		filtered = filtered[filtered["severity"].str.lower().isin(selected_severity)]
	if selected_roots:
		filtered = filtered[filtered["scope_path_root"].isin(selected_roots)]

	if scope_path_query:
		filtered = filtered[
			filtered["scope_path"].str.contains(scope_path_query, case=False, na=False)
		]
	if location_query:
		filtered = filtered[
			filtered["location"].str.contains(location_query, case=False, na=False)
		]
	if identity_query:
		filtered = filtered[
			filtered["identity"].str.contains(identity_query, case=False, na=False)
		]

	return filtered


def render_summary(source_df: pd.DataFrame, filtered_df: pd.DataFrame) -> None:
	total = len(source_df)
	showing = len(filtered_df)
	unique_identities = filtered_df["identity"].nunique()
	unique_scope_paths = filtered_df["scope_path"].nunique()

	c1, c2, c3, c4 = st.columns(4)
	c1.metric("Rows (filtered)", f"{showing:,}")
	c2.metric("Rows (all)", f"{total:,}")
	c3.metric("Unique identities", f"{unique_identities:,}")
	c4.metric("Unique scope paths", f"{unique_scope_paths:,}")


def render_top_tables(filtered_df: pd.DataFrame) -> None:
	left, right = st.columns(2)

	with left:
		st.subheader("Top scope_path")
		top_scope = (
			filtered_df.groupby("scope_path", as_index=False)
			.size()
			.sort_values("size", ascending=False)
			.rename(columns={"size": "count"})
			.head(15)
		)
		render_html_table(top_scope, max_rows=15)

	with right:
		st.subheader("Top location")
		top_location = (
			filtered_df.groupby("location", as_index=False)
			.size()
			.sort_values("size", ascending=False)
			.rename(columns={"size": "count"})
			.head(15)
		)
		render_html_table(top_location, max_rows=15)


def render_results(filtered_df: pd.DataFrame) -> None:
	st.subheader("Matching service accounts")

	if filtered_df.empty:
		st.warning("No rows match your filters. Try widening location/scope_path filters.")
		return

	visible_columns = [c for c in DISPLAY_COLUMNS if c in filtered_df.columns]
	view_df = filtered_df.loc[:, visible_columns].sort_values(
		by=["severity", "scope_path", "identity"], ascending=[True, True, True]
	)
	export_df = view_df.copy()

	if "web_url" in view_df.columns:
		view_df = view_df.copy()
		view_df["web_url"] = view_df["web_url"].apply(
			lambda url: f'<a href="{url}" target="_blank">Open</a>' if url else ""
		)

	render_html_table(view_df)

	csv_bytes = export_df.to_csv(index=False).encode("utf-8")
	st.download_button(
		label="Download filtered results as CSV",
		data=csv_bytes,
		file_name="service_accounts_filtered.csv",
		mime="text/csv",
	)


def main() -> None:
	st.set_page_config(
		page_title="Service Accounts Dashboard",
		page_icon=":mag:",
		layout="wide",
	)
	st.title("Service Accounts Dashboard")
	st.caption("Find service accounts quickly by location and scope_path.")

	try:
		df = load_findings(DATA_PATH)
	except (FileNotFoundError, ValueError) as exc:
		st.error(str(exc))
		st.stop()

	filtered = apply_filters(df)
	render_summary(df, filtered)
	render_top_tables(filtered)
	render_results(filtered)


if __name__ == "__main__":
	main()
