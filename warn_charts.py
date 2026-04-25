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
from datetime import datetime, timezone
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
    df["employees"] = (
        pd.to_numeric(df["employees"], errors="coerce").fillna(0).astype(int)
    )

    # Clean company names (group Amazon LAX 35 -> Amazon)
    def clean_company(name: str) -> str:
        name = str(name).strip()
        # Common cleanup patterns: "Amazon LAX 35", "Company, Inc. (12345)"
        # 1. Remove trailing codes like LAX, SFO, SNA, SJC + numbers
        name = re.split(
            r"\s+(LAX|SFO|SNA|SJC|OAK|SAN|BUR|LGB|SMF|ONT|SCK|FAT|MRY|SBS|SCZ)\s*\d*",
            name,
            flags=re.I,
        )[0]
        # 2. Remove trailing parentheticals or numbers in parentheses
        name = re.sub(r"\s+\(\d+\)\s*$", "", name).strip()
        # 3. Specific manual groupings if needed
        if "Amazon" in name:
            return "Amazon"
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
        "<b>"
        + df_plot["company"]
        + "</b><br>"
        + "County: "
        + df_plot["county"]
        + "<br>"
        + "Effective Date: "
        + df_plot["date_str"]
        + "<br>"
        + "Employees Affected: "
        + df_plot["employees"].map("{:,}".format)
        + "<br>"
        + "City: "
        + df_plot["city"]
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
        labels={
            "effective_date": "Effective Date",
            "employees": "Employees Affected",
            "county": "County",
        },
    )
    # Use custom_data to override the px default hover which can be flaky
    fig.update_traces(
        hovertemplate="%{customdata[0]}<extra></extra>",
        marker=dict(opacity=0.75, line=dict(width=0.5, color="white")),
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
    monthly = df_m.groupby("month")["employees"].sum().reset_index()
    monthly["month_str"] = monthly["month"].astype(str)
    monthly["employees"] = monthly["employees"].astype(int)

    x_vals = monthly["month_str"].tolist()
    y_vals = monthly["employees"].tolist()
    ma3 = monthly["employees"].rolling(3, min_periods=1).mean().tolist()

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=x_vals,
            y=y_vals,
            name="Employees Affected",
            marker_color=ACCENT,
            marker_line_width=0,
            hovertemplate="<b>%{x}</b><br>Employees: %{y:,}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_vals,
            y=ma3,
            name="3-Month MA",
            line=dict(color=ACCENT2, width=2, dash="dash"),
            hovertemplate="<b>3-Month MA</b><br>%{x}: %{y:,.0f}<extra></extra>",
        )
    )

    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text="<b>Monthly Layoffs — Total Employees Affected</b>", font_size=18
        ),
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

    fig.add_trace(
        go.Scatter(
            x=daily["date"],
            y=daily["employees"],
            name="Daily",
            fill="tozeroy",
            fillcolor=f"rgba(88, 166, 255, 0.15)",
            line=dict(color=ACCENT, width=1),
            hovertemplate="<b>%{x|%b %d, %Y}</b><br>Daily: %{y:,}<extra></extra>",
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=daily["date"],
            y=daily["rolling30"],
            name="30-Day Rolling Avg",
            line=dict(color=ACCENT2, width=2.5),
            hovertemplate="<b>30-Day Avg</b><br>%{x|%b %d}: %{y:,.0f}<extra></extra>",
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=daily["date"],
            y=daily["cumulative"],
            name="Cumulative Total",
            line=dict(color=ACCENT3, width=1.5, dash="dot"),
            hovertemplate="<b>Cumulative</b><br>%{x|%b %d}: %{y:,}<extra></extra>",
        ),
        secondary_y=True,
    )

    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text="<b>Layoff Trend — Daily, 30-Day Average & Cumulative</b>",
            font_size=18,
        ),
        yaxis_title="Employees / Day",
        yaxis2_title="Cumulative Employees",
    )
    fig.update_yaxes(gridcolor=GRID_COLOR, secondary_y=True)
    _save_chart(fig, "3_rolling_trend", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 4 — Top-N companies (horizontal bar)
# ---------------------------------------------------------------------------


def chart_top_companies(
    df: pd.DataFrame, top_n: int = 25, save_png: bool = True
) -> go.Figure:
    log.info(f"Chart 4: Top-{top_n} companies …")
    top = (
        df.groupby("company_clean")["employees"]
        .sum()
        .nlargest(top_n)
        .reset_index()
        .sort_values("employees")
        .reset_index(drop=True)
    ).rename(columns={"company_clean": "company"})

    # Colour gradient
    colors = px.colors.sequential.Blues[3:]
    color_range = len(colors)
    n = len(top)
    color_list = [colors[int(i / max(n - 1, 1) * (color_range - 1))] for i in range(n)]

    emp_vals = top["employees"].tolist()
    company_vals = top["company"].tolist()
    text_vals = [f"{v:,}" for v in emp_vals]

    fig = go.Figure(
        go.Bar(
            x=emp_vals,
            y=company_vals,
            orientation="h",
            marker_color=color_list,
            text=text_vals,
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Total Employees: %{x:,}<extra></extra>",
        )
    )
    _apply_theme(fig, margin=dict(l=250, r=80, t=70, b=50))
    fig.update_layout(
        title=dict(
            text=f"<b>Top {top_n} Filtered Companies by Total Employees Affected</b>",
            font_size=18,
        ),
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
        fig.update_layout(
            title="County heatmap: no county data available", **BASE_LAYOUT
        )
        _save_chart(fig, "5_county_heatmap", save_png)
        return fig

    df_c["month"] = df_c["effective_date"].dt.to_period("M").astype(str)
    pivot = df_c.pivot_table(
        index="county", columns="month", values="employees", aggfunc="sum", fill_value=0
    )

    # Keep top 20 counties by total
    top_counties = pivot.sum(axis=1).nlargest(20).index
    pivot = pivot.loc[top_counties]

    fig = go.Figure(
        go.Heatmap(
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
        )
    )
    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text="<b>County × Month Heatmap — Employees Affected</b>", font_size=18
        ),
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

    agg = df_t.groupby(["layoff_type", "company"])["employees"].sum().reset_index()
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
        fig.update_layout(
            title=dict(
                text="<b>Year-over-Year: No historical data yet</b>", font_size=18
            )
        )
        _save_chart(fig, "7_yoy_bar", save_png)
        return fig

    # Separate live (xlsx) vs incomplete PDF years
    pdf_years = [s for s in yearly_summary if s["source"] == "pdf"]
    live_years = [s for s in yearly_summary if s["source"] == "xlsx"]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # PDF bars — muted/gray to signal incompleteness
    if pdf_years:
        fig.add_trace(
            go.Bar(
                x=[s["label"] for s in pdf_years],
                y=[s["employees"] for s in pdf_years],
                name="Employees (PDF sample — incomplete)",
                marker_color="rgba(139,148,158,0.4)",
                marker_line_color="rgba(139,148,158,0.7)",
                marker_line_width=1,
                hovertemplate=(
                    "<b>%{x}</b><br>Employees (partial PDF): %{y:,}"
                    "<br><i>⚠ Incomplete — EDD PDFs capture only a fraction of annual notices</i>"
                    "<extra></extra>"
                ),
            ),
            secondary_y=False,
        )

    # Live XLSX bar — bright accent
    if live_years:
        fig.add_trace(
            go.Bar(
                x=[s["label"] for s in live_years],
                y=[s["employees"] for s in live_years],
                name="Employees Affected (Live XLSX)",
                marker_color=ACCENT,
                marker_line_width=0,
                hovertemplate="<b>%{x}</b><br>Employees: %{y:,}<extra></extra>",
            ),
            secondary_y=False,
        )

    # Notice count line — live only (PDF counts are meaningless)
    if live_years:
        fig.add_trace(
            go.Scatter(
                x=[s["label"] for s in live_years],
                y=[s["records"] for s in live_years],
                name="Notice Count (Live)",
                line=dict(color=ACCENT3, width=2.5),
                marker=dict(size=10),
                hovertemplate="<b>%{x}</b><br>Notices: %{y:,}<extra></extra>",
            ),
            secondary_y=True,
        )

    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text="<b>Year-over-Year — Employees Affected & Notice Count (2014–Present)</b>",
            font_size=18,
        ),
        xaxis_title="Fiscal Year",
        yaxis_title="Employees Affected",
        yaxis2_title="Number of Notices",
        barmode="overlay",
        showlegend=True,
        annotations=[
            dict(
                text=(
                    "⚠ Gray bars = partial PDF extracts (EDD PDFs capture only ~3–5% of actual annual notices). "
                    "Only FY 2025-26 (blue) reflects complete data."
                ),
                xref="paper", yref="paper",
                x=0, y=-0.22, showarrow=False,
                font=dict(size=11, color="#8b949e"),
                align="left",
            )
        ],
        margin=dict(l=60, r=30, t=70, b=100),
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
        fig.update_layout(
            title=dict(
                text="<b>Multi-Year Trend: No historical data yet</b>", font_size=18
            )
        )
        _save_chart(fig, "8_multiyear_trend", save_png)
        return fig

    df_all = pd.DataFrame(records_all)
    df_all["effective_date"] = pd.to_datetime(df_all["effective_date"], errors="coerce")
    df_all["employees"] = (
        pd.to_numeric(df_all["employees"], errors="coerce").fillna(0).astype(int)
    )
    df_all = df_all.dropna(subset=["effective_date"])

    # Normalise to month-of-year so we can compare across years
    df_all["month_of_year"] = df_all["effective_date"].dt.month
    df_all["year"] = df_all["effective_date"].dt.year
    month_pivot = (
        df_all.groupby(["year", "month_of_year"])["employees"].sum().reset_index()
    )

    month_labels = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]

    color_seq = px.colors.qualitative.Plotly
    fig = go.Figure()
    all_years = sorted(month_pivot["year"].unique())
    for i, yr in enumerate(all_years):
        sub = month_pivot[month_pivot["year"] == yr].sort_values("month_of_year")
        is_current = yr == max(all_years)
        fig.add_trace(
            go.Scatter(
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
            )
        )

    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text="<b>Multi-Year Monthly Comparison — Employees Affected by Month</b>",
            font_size=18,
        ),
        xaxis_title="Month",
        yaxis_title="Employees Affected",
        showlegend=True,
    )
    _save_chart(fig, "8_multiyear_trend", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 9 — Industry breakdown (bar chart from XLSX Related Industry field)
# ---------------------------------------------------------------------------


def chart_industry_breakdown(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 9: Industry breakdown …")
    df_i = df.copy()

    # Use XLSX industry field when available; otherwise infer from company name
    INDUSTRY_KEYWORDS = {
        "Technology": ["amazon", "google", "meta", "apple", "intel", "ibm", "microsoft",
                       "nvidia", "cisco", "oracle", "salesforce", "tech", "software",
                       "semiconductor", "ai ", " ai,", "data", "digital", "cyber"],
        "Healthcare": ["hospital", "health", "medical", "pharma", "clinic", "care",
                       "biotech", "bio ", "therapeutics", "labs", "laboratory"],
        "Retail": ["retail", "store", "market", "shop", "walmart", "target", "costco",
                   "grocery", "food", "restaurant", "cafe", "hotel", "hilton"],
        "Manufacturing": ["manufactur", "factory", "plant", "production", "assembly",
                          "aerospace", "defense", "automotive", "auto ", "motor"],
        "Finance": ["bank", "financial", "insurance", "capital", "fund", "invest",
                    "mortgage", "credit", "loan"],
        "Education": ["university", "college", "school", "education", "academic",
                      "institute", "learning"],
        "Government/Non-profit": ["county", "city of", "state of", "public", "nonprofit",
                                  "foundation", "agency"],
    }

    def classify(row):
        # Prefer XLSX industry field
        ind = str(row.get("industry", "")).strip()
        if ind and ind not in ("nan", ""):
            # Map EDD industry codes to readable names
            ind_lower = ind.lower()
            if any(k in ind_lower for k in ["tech", "information", "software", "computer"]):
                return "Technology"
            if any(k in ind_lower for k in ["health", "hospital", "medical", "pharma"]):
                return "Healthcare"
            if any(k in ind_lower for k in ["retail", "food service", "restaurant", "hotel", "accommodation"]):
                return "Retail"
            if any(k in ind_lower for k in ["manufactur", "aerospace", "defense"]):
                return "Manufacturing"
            if any(k in ind_lower for k in ["finance", "bank", "insurance", "real estate"]):
                return "Finance"
            if any(k in ind_lower for k in ["education", "school", "university", "college"]):
                return "Education"
            if any(k in ind_lower for k in ["government", "public admin", "nonprofit"]):
                return "Government/Non-profit"
            return ind[:40]  # Use XLSX value as-is if not mapped
        # Fall back to company-name inference
        name = str(row.get("company_clean", row.get("company", ""))).lower()
        for sector, keywords in INDUSTRY_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                return sector
        return "Other"

    df_i["sector"] = df_i.apply(classify, axis=1)
    agg = df_i.groupby("sector")["employees"].sum().sort_values(ascending=False)
    total = agg.sum()

    sectors = agg.index.tolist()
    values = agg.values.tolist()
    pcts = [v / total * 100 for v in values]

    SECTOR_COLORS = {
        "Technology": "#4C8EDA",
        "Healthcare": "#5CB85C",
        "Retail": "#F0AD4E",
        "Manufacturing": "#9B59B6",
        "Finance": "#E74C3C",
        "Education": "#1ABC9C",
        "Government/Non-profit": "#95A5A6",
        "Other": "#607080",
    }
    color_list = [SECTOR_COLORS.get(s, ACCENT) for s in sectors]

    fig = go.Figure(
        go.Bar(
            x=sectors,
            y=values,
            marker_color=color_list,
            text=[f"{v:,}<br>({p:.1f}%)" for v, p in zip(values, pcts)],
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>Employees: %{y:,}<extra></extra>",
        )
    )
    _apply_theme(fig, margin=dict(l=60, r=30, t=120, b=160))
    fig.update_layout(
        title=dict(text="<b>Employees Affected by Industry Sector</b>", font_size=18),
        xaxis_title="Industry Sector",
        yaxis_title="Total Employees Affected",
        height=560,
        yaxis=dict(range=[0, max(values) * 1.25]),
    )
    fig.update_xaxes(tickangle=-35)
    _save_chart(fig, "9_industry_breakdown", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 10 — Notice lead time histogram
# ---------------------------------------------------------------------------


def chart_lead_time_histogram(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 10: Lead time histogram …")
    df_lt = df.dropna(subset=["notice_date", "effective_date"]).copy()
    df_lt["lead_days"] = (
        df_lt["effective_date"] - df_lt["notice_date"]
    ).dt.days
    df_lt = df_lt[(df_lt["lead_days"] >= 0) & (df_lt["lead_days"] <= 365)]

    if df_lt.empty:
        fig = go.Figure()
        _apply_theme(fig)
        fig.update_layout(title="Lead Time: insufficient date data", **BASE_LAYOUT)
        _save_chart(fig, "10_lead_time", save_png)
        return fig

    median_days = int(df_lt["lead_days"].median())
    mean_days = df_lt["lead_days"].mean()
    pct_compliant = (df_lt["lead_days"] >= 60).mean() * 100

    fig = go.Figure(
        go.Histogram(
            x=df_lt["lead_days"],
            nbinsx=40,
            marker_color=ACCENT,
            marker_line_color=ACCENT2,
            marker_line_width=0.5,
            hovertemplate="Lead time: %{x} days<br>Count: %{y}<extra></extra>",
        )
    )
    # 60-day compliance line
    fig.add_vline(
        x=60, line_dash="dash", line_color=ACCENT3, line_width=2,
        annotation_text="60-day WARN requirement",
        annotation_position="top right",
        annotation_font_color=ACCENT3,
    )
    fig.add_vline(
        x=median_days, line_dash="dot", line_color="#aaaaaa", line_width=1.5,
        annotation_text=f"Median: {median_days}d",
        annotation_position="top left",
        annotation_font_color="#aaaaaa",
    )
    _apply_theme(fig)
    fig.update_layout(
        title=dict(
            text=f"<b>Notice Lead Time Distribution — {pct_compliant:.0f}% Filed ≥60 Days in Advance</b>",
            font_size=18,
        ),
        xaxis_title="Days from Notice Date to Effective Date",
        yaxis_title="Number of Notices",
        height=450,
    )
    _save_chart(fig, "10_lead_time", save_png)
    return fig


# ---------------------------------------------------------------------------
# Chart 11 — County comparison bar (top 10 counties)
# ---------------------------------------------------------------------------


def chart_county_bar(df: pd.DataFrame, save_png: bool = True) -> go.Figure:
    log.info("Chart 11: County bar …")
    df_c = df[df["county"].str.strip() != ""].copy()

    if df_c.empty:
        fig = go.Figure()
        _apply_theme(fig)
        fig.update_layout(title="County data not available", **BASE_LAYOUT)
        _save_chart(fig, "11_county_bar", save_png)
        return fig

    # Shorten "Los Angeles County" → "Los Angeles"
    df_c["county_short"] = df_c["county"].str.replace(
        r"\s*(County|Parish)\s*$", "", regex=True
    ).str.strip()

    agg = (
        df_c.groupby("county_short")
        .agg(employees=("employees", "sum"), notices=("company", "count"))
        .nlargest(10, "employees")
        .sort_values("employees")
        .reset_index()
    )

    fig = go.Figure(
        go.Bar(
            x=agg["employees"].tolist(),
            y=agg["county_short"].tolist(),
            orientation="h",
            marker_color=ACCENT,
            text=[f"{v:,}" for v in agg["employees"].tolist()],
            textposition="outside",
            customdata=agg["notices"].tolist(),
            hovertemplate=(
                "<b>%{y}</b><br>Employees: %{x:,}<br>Notices: %{customdata}<extra></extra>"
            ),
        )
    )
    _apply_theme(fig, margin=dict(l=130, r=80, t=70, b=50))
    fig.update_layout(
        title=dict(text="<b>Top 10 Counties by Employees Affected</b>", font_size=18),
        xaxis_title="Total Employees Affected",
        yaxis_title="",
        height=460,
    )
    _save_chart(fig, "11_county_bar", save_png)
    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHART_META = [
    {
        "id": "1_timeline_scatter",
        "title": "Layoff Timeline",
        "desc": "Employees affected by effective date, sized and coloured by county.",
    },
    {
        "id": "2_monthly_bar",
        "title": "Monthly Totals",
        "desc": "Total employees laid off per month with 3-month moving average.",
    },
    {
        "id": "3_rolling_trend",
        "title": "Rolling Trend",
        "desc": "Daily layoffs, 30-day rolling average, and cumulative total.",
    },
    {
        "id": "4_top_companies",
        "title": "Top Companies",
        "desc": "Top 25 companies by cumulative employees affected.",
    },
    {
        "id": "5_county_heatmap",
        "title": "County Heatmap",
        "desc": "Layoffs by county and month — heat intensity = employees.",
    },
    {
        "id": "6_treemap",
        "title": "Treemap",
        "desc": "Proportional breakdown by company and layoff type.",
    },
    {
        "id": "7_yoy_bar",
        "title": "Year-over-Year",
        "desc": "Annual employees affected and notice count from 2014 to present.",
    },
    {
        "id": "8_multiyear_trend",
        "title": "Multi-Year Trend",
        "desc": "Monthly layoff pattern overlaid across all years for seasonal comparison.",
    },
    {
        "id": "9_industry_breakdown",
        "title": "Industry Breakdown",
        "desc": "Employees affected by industry sector.",
    },
    {
        "id": "10_lead_time",
        "title": "Notice Lead Time",
        "desc": "Distribution of days from notice filing to effective date vs 60-day WARN requirement.",
    },
    {
        "id": "11_county_bar",
        "title": "Top Counties",
        "desc": "Top 10 counties by total employees affected.",
    },
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
        log.info(
            f"Historical data: {len(all_records):,} records across {len(yearly_summary)} years"
        )
    else:
        log.info("No historical data yet — run warn_history.py to generate it")

    def _call(fn, df, sp):
        """Unified call that always passes (df, save_png)."""
        return fn(df, sp)

    chart_fns = [
        chart_timeline_scatter,  # 1
        chart_monthly_bar,  # 2
        chart_rolling_trend,  # 3
        lambda d, sp: chart_top_companies(d, save_png=sp),  # 4
        chart_county_heatmap,  # 5
        chart_treemap,  # 6
        lambda d, sp: chart_yoy_bar(yearly_summary, save_png=sp),  # 7
        lambda d, sp: chart_multiyear_trend(all_records, save_png=sp),  # 8
        chart_industry_breakdown,  # 9
        chart_lead_time_histogram,  # 10
        chart_county_bar,  # 11
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
        "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
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
