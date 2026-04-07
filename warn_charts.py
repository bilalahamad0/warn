"""
warn_charts.py
--------------
Generates 6 interactive Plotly charts from parsed WARN data and exports
them as self-contained HTML divs + static PNGs (requires kaleido).

Usage:
    python3 warn_charts.py               # generate all charts
    python3 warn_charts.py --no-png      # HTML only (skips kaleido)
"""

import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
import re

import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import plotly.io as pio

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "docs"
CHARTS_DIR = OUTPUT_DIR / "charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

LATEST_FILE = DATA_DIR / "warn_latest.json"
CHART_MANIFEST = DATA_DIR / "charts_manifest.json"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("warn_charts")

# ---------------------------------------------------------------------------
# Plotly theme
# ---------------------------------------------------------------------------

DARK_BG = "#0d1117"
CARD_BG = "#161b22"
ACCENT = "#58a6ff"
ACCENT2 = "#f78166"
ACCENT3 = "#3fb950"
ACCENT4 = "#d29922"
TEXT_COLOR = "#e6edf3"
GRID_COLOR = "#21262d"

BASE_LAYOUT = dict(
    paper_bgcolor=DARK_BG,
    plot_bgcolor=CARD_BG,
    font=dict(color=TEXT_COLOR, family="Inter, system-ui, sans-serif"),
    margin=dict(l=60, r=30, t=70, b=60),
    hoverlabel=dict(bgcolor=CARD_BG, font_color=TEXT_COLOR),
)

# Axis defaults applied via update_xaxes/update_yaxes to avoid kwarg conflicts
AXIS_DEFAULTS = dict(gridcolor=GRID_COLOR, zerolinecolor=GRID_COLOR, showgrid=True)
LEGEND_DEFAULTS = dict(bgcolor=CARD_BG, bordercolor=GRID_COLOR, borderwidth=1)


def _apply_theme(fig: go.Figure, margin: dict = None) -> go.Figure:
    """Apply the standard dark theme to any figure."""
    layout_kwargs = {**BASE_LAYOUT}
    if margin:
        layout_kwargs["margin"] = margin
    layout_kwargs["legend"] = LEGEND_DEFAULTS
    fig.update_layout(**layout_kwargs)
    fig.update_xaxes(**AXIS_DEFAULTS)
    fig.update_yaxes(**AXIS_DEFAULTS)
    return fig

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data() -> pd.DataFrame:
    if not LATEST_FILE.exists():
        raise FileNotFoundError(f"Run warn_monitor.py first — {LATEST_FILE} not found.")
    payload = json.loads(LATEST_FILE.read_text())
    df = pd.DataFrame(payload["records"])
    df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")
    df["notice_date"] = pd.to_datetime(df.get("notice_date"), errors="coerce")
    df["employees"] = pd.to_numeric(df["employees"], errors="coerce").fillna(0).astype(int)

    # Clean company names (group Amazon LAX 35 -> Amazon)
    def clean_company(name: str) -> str:
        name = str(name).strip()
        # Common cleanup patterns: "Amazon LAX 35", "Company, Inc. (12345)"
        # 1. Remove trailing codes like LAX, SFO, SNA, SJC + numbers
        name = re.split(r'\s+(LAX|SFO|SNA|SJC|OAK|SAN|BUR|LGB|SMF|ONT|SCK|FAT|MRY|SBS|SCZ)\s*\d*', name, flags=re.I)[0]
        # 2. Remove trailing parentheticals or numbers in parentheses
        name = re.sub(r'\s+\(\d+\)\s*$', '', name).strip()
        # 3. Specific manual groupings if needed
        if "Amazon" in name: return "Amazon"
        return name

    df["company_clean"] = df["company"].apply(clean_company)
    return df, payload


def _save_chart(fig: go.Figure, name: str, save_png: bool = True) -> str:
    html_path = CHARTS_DIR / f"{name}.html"
    div_str = fig.to_html(full_html=False, include_plotlyjs=False)
    html_path.write_text(div_str)
    if save_png:
        try:
            png_path = CHARTS_DIR / f"{name}.png"
            fig.write_image(str(png_path), width=1200, height=600, scale=2)
            log.info(f"  → PNG: {png_path}")
        except Exception as e:
            log.warning(f"  PNG export failed (kaleido?): {e}")
    log.info(f"  → HTML div: {html_path}")
    return str(html_path)


# ---------------------------------------------------------------------------
# Chart 1 — Timeline scatter (employees vs effective_date, coloured by county)
# ---------------------------------------------------------------------------

def chart_timeline_scatter(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 1: Timeline scatter …")
    df_plot = df.dropna(subset=["effective_date"]).copy()

    # Pre-calculate hover content to ensure robust mapping
    df_plot["date_str"] = df_plot["effective_date"].dt.strftime("%b %d, %Y")
    df_plot["hover_text"] = (
        "<b>" + df_plot["company"] + "</b><br>" +
        "County: " + df_plot["county"] + "<br>" +
        "Effective Date: " + df_plot["date_str"] + "<br>" +
        "Employees Affected: " + df_plot["employees"].map("{:,}".format) + "<br>" +
        "City: " + df_plot["city"]
    )

    fig = px.scatter(
        df_plot,
        x="effective_date",
        y="employees",
        color="county" if df_plot["county"].str.strip().ne("").any() else "company",
        size="employees",
        size_max=40,
        hover_name="company",
        custom_data=["hover_text"],
        title="<b>WARN Notices — Employees by Effective Date</b>",
        labels={"effective_date": "Effective Date", "employees": "Employees Affected", "county": "County"},
    )
    # Use custom_data to override the px default hover which can be flaky
    fig.update_traces(
        hovertemplate="%{customdata[0]}<extra></extra>",
        marker=dict(opacity=0.75, line=dict(width=0.5, color="white"))
    )
    _apply_theme(fig)
    fig.update_layout(
        title_font_size=18,
        showlegend=True,
        xaxis_title="Effective Date",
        yaxis_title="Employees Affected",
    )
    _save_chart(fig, "1_timeline_scatter", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 2 — Monthly bar chart (total employees per month)
# ---------------------------------------------------------------------------

def chart_monthly_bar(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 2: Monthly bar chart …")
    df_m = df.dropna(subset=["effective_date"]).copy()
    df_m["month"] = df_m["effective_date"].dt.to_period("M")
    monthly = (
        df_m.groupby("month")["employees"]
        .sum()
        .reset_index()
    )
    monthly["month_str"] = monthly["month"].astype(str)
    monthly["employees"] = monthly["employees"].astype(int)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=monthly["month_str"],
        y=monthly["employees"],
        name="Employees Affected",
        marker_color=ACCENT,
        marker_line_width=0,
        hovertemplate="<b>%{x}</b><br>Employees: %{y:,}<extra></extra>",
    ))

    # Add 3-month moving average
    monthly["ma3"] = monthly["employees"].rolling(3, min_periods=1).mean()
    fig.add_trace(go.Scatter(
        x=monthly["month_str"],
        y=monthly["ma3"],
        name="3-Month MA",
        line=dict(color=ACCENT2, width=2, dash="dash"),
        hovertemplate="<b>3-Month MA</b><br>%{x}: %{y:,.0f}<extra></extra>",
    ))

    _apply_theme(fig)
    fig.update_layout(
        title=dict(text="<b>Monthly Layoffs — Total Employees Affected</b>", font_size=18),
        xaxis_title="Month",
        yaxis_title="Employees Affected",
        barmode="group",
    )
    fig.update_xaxes(tickangle=-45, gridcolor=GRID_COLOR)
    _save_chart(fig, "2_monthly_bar", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 3 — Rolling 30-day trend + cumulative
# ---------------------------------------------------------------------------

def chart_rolling_trend(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 3: Rolling trend …")
    df_d = df.dropna(subset=["effective_date"]).copy()
    daily = df_d.groupby("effective_date")["employees"].sum().reset_index()
    daily = daily.set_index("effective_date").sort_index()

    # Fill gaps in date range
    idx = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    daily = daily.reindex(idx, fill_value=0)
    daily["rolling30"] = daily["employees"].rolling(30, min_periods=1).mean()
    daily["cumulative"] = daily["employees"].cumsum()
    daily = daily.reset_index().rename(columns={"index": "date"})

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Scatter(
        x=daily["date"], y=daily["employees"],
        name="Daily",
        fill="tozeroy",
        fillcolor=f"rgba(88, 166, 255, 0.15)",
        line=dict(color=ACCENT, width=1),
        hovertemplate="<b>%{x|%b %d, %Y}</b><br>Daily: %{y:,}<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=daily["date"], y=daily["rolling30"],
        name="30-Day Rolling Avg",
        line=dict(color=ACCENT2, width=2.5),
        hovertemplate="<b>30-Day Avg</b><br>%{x|%b %d}: %{y:,.0f}<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=daily["date"], y=daily["cumulative"],
        name="Cumulative Total",
        line=dict(color=ACCENT3, width=1.5, dash="dot"),
        hovertemplate="<b>Cumulative</b><br>%{x|%b %d}: %{y:,}<extra></extra>",
    ), secondary_y=True)

    _apply_theme(fig)
    fig.update_layout(
        title=dict(text="<b>Layoff Trend — Daily, 30-Day Average & Cumulative</b>", font_size=18),
        yaxis_title="Employees / Day",
        yaxis2_title="Cumulative Employees",
    )
    fig.update_yaxes(gridcolor=GRID_COLOR, secondary_y=True)
    _save_chart(fig, "3_rolling_trend", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 4 — Top-N companies (horizontal bar)
# ---------------------------------------------------------------------------

def chart_top_companies(df: pd.DataFrame, top_n: int = 25, save_png: bool = True) -> go.Figure:
    log.info(f"Chart 4: Top-{top_n} companies …")
    top = (
        df.groupby("company_clean")["employees"]
        .sum()
        .nlargest(top_n)
        .reset_index()
        .sort_values("employees")
    ).rename(columns={"company_clean": "company"})

    # Colour gradient
    colors = px.colors.sequential.Blues[3:]
    color_range = len(colors)
    color_list = [colors[int(i / top_n * (color_range - 1))] for i in range(len(top))]

    fig = go.Figure(go.Bar(
        x=top["employees"],
        y=top["company"],
        orientation="h",
        marker_color=color_list,
        text=top["employees"].map("{:,}".format),
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Total Employees: %{x:,}<extra></extra>",
    ))
    _apply_theme(fig, margin=dict(l=250, r=80, t=70, b=50))
    fig.update_layout(
        title=dict(text=f"<b>Top {top_n} Filtered Companies by Total Employees Affected</b>", font_size=18),
        xaxis_title="Total Employees Affected",
        yaxis_title="",
        height=max(500, len(top) * 26),
    )
    fig.update_yaxes(tickfont=dict(size=11))
    _save_chart(fig, "4_top_companies", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 5 — County heatmap (month × county)
# ---------------------------------------------------------------------------

def chart_county_heatmap(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 5: County heatmap …")
    df_c = df.dropna(subset=["effective_date"]).copy()
    df_c = df_c[df_c["county"].str.strip() != ""]

    if df_c.empty:
        log.warning("No county data — skipping heatmap.")
        fig = go.Figure()
        fig.update_layout(title="County heatmap: no county data available", **BASE_LAYOUT)
        _save_chart(fig, "5_county_heatmap", save_png)
        return fig

    df_c["month"] = df_c["effective_date"].dt.to_period("M").astype(str)
    pivot = df_c.pivot_table(
        index="county", columns="month", values="employees",
        aggfunc="sum", fill_value=0
    )

    # Keep top 20 counties by total
    top_counties = pivot.sum(axis=1).nlargest(20).index
    pivot = pivot.loc[top_counties]

    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=list(pivot.columns),
        y=list(pivot.index),
        colorscale="Blues",
        hoverongaps=False,
        hovertemplate="County: <b>%{y}</b><br>Month: <b>%{x}</b><br>Employees: <b>%{z:,}</b><extra></extra>",
        colorbar=dict(
            title=dict(text="Employees", font=dict(color=TEXT_COLOR)),
            tickfont=dict(color=TEXT_COLOR),
        ),
    ))
    _apply_theme(fig)
    fig.update_layout(
        title=dict(text="<b>County × Month Heatmap — Employees Affected</b>", font_size=18),
        xaxis_title="Month",
        yaxis_title="County",
        height=max(500, len(pivot) * 28 + 120),
    )
    fig.update_xaxes(tickangle=-45)
    _save_chart(fig, "5_county_heatmap", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 6 — Treemap (company × layoff type, sized by employees)
# ---------------------------------------------------------------------------

def chart_treemap(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 6: Treemap …")
    df_t = df.copy()
    df_t["layoff_type"] = df_t.get("layoff_type", pd.Series(["Unknown"] * len(df_t)))
    df_t["layoff_type"] = df_t["layoff_type"].fillna("Unknown").replace("", "Unknown")

    agg = (
        df_t.groupby(["layoff_type", "company"])["employees"]
        .sum()
        .reset_index()
    )
    # Limit to top 80 companies for readability
    top80 = df_t.groupby("company")["employees"].sum().nlargest(80).index
    agg = agg[agg["company"].isin(top80)]

    fig = px.treemap(
        agg,
        path=["layoff_type", "company"],
        values="employees",
        color="employees",
        color_continuous_scale="Blues",
        hover_data={"employees": ":,"},
        title="<b>Layoff Treemap — Company Size by Employees Affected</b>",
    )
    fig.update_traces(
        textinfo="label+value",
        hovertemplate="<b>%{label}</b><br>Employees: %{value:,}<extra></extra>",
        textfont=dict(size=12),
        marker=dict(pad=dict(t=20)),
    )
    treemap_layout = {**BASE_LAYOUT}
    treemap_layout["margin"] = dict(l=10, r=10, t=70, b=10)
    fig.update_layout(
        **treemap_layout,
        coloraxis_colorbar=dict(
            title=dict(text="Employees", font=dict(color=TEXT_COLOR)),
            tickfont=dict(color=TEXT_COLOR),
        ),
    )
    _save_chart(fig, "6_treemap", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 7 — Year-over-year comparison bar (historical)
# ---------------------------------------------------------------------------

def chart_yoy_bar(yearly_summary: list, save_png: bool = True) -> go.Figure:
    log.info("Chart 7: Year-over-year bar …")
    if not yearly_summary:
        fig = go.Figure()
        _apply_theme(fig)
        fig.update_layout(title=dict(text="<b>Year-over-Year: No historical data yet</b>", font_size=18))
        _save_chart(fig, "7_yoy_bar", save_png)
        return fig

    years  = [s["label"]     for s in yearly_summary]
    emps   = [s["employees"] for s in yearly_summary]
    recs   = [s["records"]   for s in yearly_summary]
    colors = [ACCENT if s["source"] == "xlsx" else ACCENT2 for s in yearly_summary]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=years, y=emps, name="Employees Affected",
        marker_color=colors, marker_line_width=0,
        hovertemplate="<b>%{x}</b><br>Employees: %{y:,}<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=years, y=recs, name="Notice Count",
        line=dict(color=ACCENT3, width=2.5),
        marker=dict(size=7),
        hovertemplate="<b>%{x}</b><br>Notices: %{y:,}<extra></extra>",
    ), secondary_y=True)

    _apply_theme(fig)
    fig.update_layout(
        title=dict(text="<b>Year-over-Year — Employees Affected & Notice Count (2014–Present)</b>", font_size=18),
        xaxis_title="Fiscal Year",
        yaxis_title="Employees Affected",
        yaxis2_title="Number of Notices",
        showlegend=True,
    )
    fig.update_xaxes(tickangle=-30)
    fig.update_yaxes(gridcolor=GRID_COLOR, secondary_y=True)
    _save_chart(fig, "7_yoy_bar", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 8 — Multi-year trend line (monthly, all years overlaid)
# ---------------------------------------------------------------------------

def chart_multiyear_trend(records_all: list, save_png: bool = True) -> go.Figure:
    log.info("Chart 8: Multi-year trend …")
    if not records_all:
        fig = go.Figure()
        _apply_theme(fig)
        fig.update_layout(title=dict(text="<b>Multi-Year Trend: No historical data yet</b>", font_size=18))
        _save_chart(fig, "8_multiyear_trend", save_png)
        return fig

    df_all = pd.DataFrame(records_all)
    df_all["effective_date"] = pd.to_datetime(df_all["effective_date"], errors="coerce")
    df_all["employees"] = pd.to_numeric(df_all["employees"], errors="coerce").fillna(0).astype(int)
    df_all = df_all.dropna(subset=["effective_date"])

    # Normalise to month-of-year so we can compare across years
    df_all["month_of_year"] = df_all["effective_date"].dt.month
    df_all["year"] = df_all["effective_date"].dt.year
    month_pivot = (
        df_all.groupby(["year", "month_of_year"])["employees"]
        .sum().reset_index()
    )

    month_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                    "Jul","Aug","Sep","Oct","Nov","Dec"]

    color_seq = px.colors.qualitative.Plotly
    fig = go.Figure()
    all_years = sorted(month_pivot["year"].unique())
    for i, yr in enumerate(all_years):
        sub = month_pivot[month_pivot["year"] == yr].sort_values("month_of_year")
        is_current = (yr == max(all_years))
        fig.add_trace(go.Scatter(
            x=sub["month_of_year"].map(lambda m: month_labels[m - 1]),
            y=sub["employees"],
            name=str(yr),
            mode="lines+markers",
            line=dict(
                color=ACCENT if is_current else color_seq[i % len(color_seq)],
                width=3 if is_current else 1.5,
                dash="solid" if is_current else "dot",
            ),
            marker=dict(size=5 if is_current else 3),
            hovertemplate=f"<b>{yr}</b><br>%{{x}}: %{{y:,}} employees<extra></extra>",
        ))

    _apply_theme(fig)
    fig.update_layout(
        title=dict(text="<b>Multi-Year Monthly Comparison — Employees Affected by Month</b>", font_size=18),
        xaxis_title="Month",
        yaxis_title="Employees Affected",
        showlegend=True,
    )
    _save_chart(fig, "8_multiyear_trend", save_png)
    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHART_META = [
    {"id": "1_timeline_scatter", "title": "Layoff Timeline",
     "desc": "Employees affected by effective date, sized and coloured by county."},
    {"id": "2_monthly_bar",      "title": "Monthly Totals",
     "desc": "Total employees laid off per month with 3-month moving average."},
    {"id": "3_rolling_trend",    "title": "Rolling Trend",
     "desc": "Daily layoffs, 30-day rolling average, and cumulative total."},
    {"id": "4_top_companies",    "title": "Top Companies",
     "desc": "Top 25 companies by cumulative employees affected."},
    {"id": "5_county_heatmap",   "title": "County Heatmap",
     "desc": "Layoffs by county and month — heat intensity = employees."},
    {"id": "6_treemap",          "title": "Treemap",
     "desc": "Proportional breakdown by company and layoff type."},
    {"id": "7_yoy_bar",          "title": "Year-over-Year",
     "desc": "Annual employees affected and notice count from 2014 to present."},
    {"id": "8_multiyear_trend",  "title": "Multi-Year Trend",
     "desc": "Monthly layoff pattern overlaid across all years for seasonal comparison."},
]


def run(save_png: bool = True) -> list:
    df, payload = load_data()
    log.info(f"Loaded {len(df)} records for charting.")

    # Load historical data if available
    combined_file = BASE_DIR / "data" / "warn_all_years.json"
    yearly_summary = []
    all_records = []
    if combined_file.exists():
        combined = json.loads(combined_file.read_text())
        yearly_summary = combined.get("yearly_summary", [])
        all_records = combined.get("records", [])
        log.info(f"Historical data: {len(all_records):,} records across {len(yearly_summary)} years")
    else:
        log.info("No historical data yet — run warn_history.py to generate it")

    def _call(fn, df, sp):
        """Unified call that always passes (df, save_png)."""
        return fn(df, sp)

    chart_fns = [
        chart_timeline_scatter,                                        # 1
        chart_monthly_bar,                                             # 2
        chart_rolling_trend,                                           # 3
        lambda d, sp: chart_top_companies(d, save_png=sp),            # 4
        chart_county_heatmap,                                          # 5
        chart_treemap,                                                 # 6
        lambda d, sp: chart_yoy_bar(yearly_summary, save_png=sp),     # 7
        lambda d, sp: chart_multiyear_trend(all_records, save_png=sp),# 8
    ]

    results = []
    for fn, meta in zip(chart_fns, CHART_META):
        try:
            _call(fn, df, save_png)
            results.append({**meta, "status": "ok"})
        except Exception as e:
            log.error(f"Chart {meta['id']} failed: {e}")
            results.append({**meta, "status": "error", "error": str(e)})

    # Save manifest
    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "charts": results,
        "total_records": payload.get("total_records", 0),
        "total_employees": payload.get("total_employees", 0),
        "date_range_start": payload.get("date_range_start"),
        "date_range_end": payload.get("date_range_end"),
        "last_updated": payload.get("last_updated"),
    }
    CHART_MANIFEST.write_text(json.dumps(manifest, indent=2, default=str))
    log.info("Charts complete.")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate WARN charts")
    parser.add_argument("--no-png", action="store_true", help="Skip PNG export")
    args = parser.parse_args()
    results = run(save_png=not args.no_png)
    for r in results:
        status_icon = "✓" if r["status"] == "ok" else "✗"
        print(f"  {status_icon} {r['id']}: {r['status']}")
