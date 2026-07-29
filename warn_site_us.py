"""
warn_site_us.py
---------------
Builds the standalone US-wide dashboard at docs/us/ (GitHub Pages: /us/).

Completely separate surface from the original California dashboard
(docs/index.html), which stays untouched by design. Everything here is driven
by data/warn_national.json — the unified multi-state dataset produced by
warn_sources.aggregate — so new states appear automatically as their sources
come online.

Usage:
    python3 warn_site_us.py          # build docs/us/ from current national data
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # SIGNUP_ENDPOINT lives in .env locally, in CI vars on Actions.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass

import pandas as pd
import plotly.graph_objects as go

import warn_charts
from warn_charts import ACCENT, ACCENT2, ACCENT3, _apply_theme

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
NATIONAL_FILE = DATA_DIR / "warn_national.json"
US_DIR = BASE_DIR / "docs" / "us"
CHARTS_DIR = BASE_DIR / "docs" / "charts"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("warn_site_us")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _load_national(national_file: Optional[Path] = None) -> dict:
    path = national_file if national_file is not None else NATIONAL_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run warn_sources.aggregate.build_national() first."
        )
    return json.loads(path.read_text())


def _to_frame(payload: dict) -> pd.DataFrame:
    df = pd.DataFrame(payload.get("records", []))
    for col in ("notice_date", "effective_date"):
        df[col] = pd.to_datetime(df.get(col), errors="coerce")
    df["employees"] = (
        pd.to_numeric(df.get("employees"), errors="coerce").fillna(0).astype(int)
    )
    df["state"] = df.get("state", "").astype(str).str.upper()
    # Best available event date: notice date, else effective date.
    df["event_date"] = df["notice_date"].fillna(df["effective_date"])
    return df


def _fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------


def compute_us_kpis(payload: dict) -> dict:
    """Cross-state KPIs for the current calendar year plus all-time totals."""
    df = _to_frame(payload)
    year = datetime.now(timezone.utc).year
    ydf = df[df["event_date"].dt.year == year]

    largest = {"company": "—", "state": "", "employees": 0}
    if len(ydf) and ydf["employees"].max() > 0:
        row = ydf.loc[ydf["employees"].idxmax()]
        largest = {
            "company": str(row.get("company", "—")),
            "state": str(row.get("state", "")),
            "employees": int(row["employees"]),
        }

    top_state = {"state": "—", "employees": 0}
    if len(ydf):
        by_state = ydf.groupby("state")["employees"].sum().sort_values(
            ascending=False
        )
        if len(by_state):
            top_state = {
                "state": str(by_state.index[0]),
                "employees": int(by_state.iloc[0]),
            }

    return {
        "year": year,
        "states_live": payload.get("states_live", 0),
        "year_notices": int(len(ydf)),
        "year_employees": int(ydf["employees"].sum()) if len(ydf) else 0,
        "total_notices": payload.get("total_records", len(df)),
        "total_employees": payload.get("total_employees", int(df["employees"].sum())),
        "largest": largest,
        "top_state": top_state,
        "last_updated": payload.get("last_updated", ""),
    }


# ---------------------------------------------------------------------------
# Charts (saved as div fragments into docs/charts/us_*.html)
# ---------------------------------------------------------------------------


def chart_us_monthly(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """National employees affected per month (last 24 months) + notice count."""
    recent = df[df["event_date"].notna()].copy()
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=24)
    recent = recent[recent["event_date"] >= cutoff]
    monthly = (
        recent.set_index("event_date")
        .resample("MS")
        .agg(employees=("employees", "sum"), notices=("employees", "size"))
        .reset_index()
    )

    def _compact(v: int) -> str:
        return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:,.0f}"

    fig = go.Figure()
    fig.add_bar(
        x=monthly["event_date"],
        y=monthly["employees"],
        name="Employees affected",
        marker_color=ACCENT,
        text=[_compact(v) for v in monthly["employees"]],
        textposition="outside",
        textfont=dict(size=9, color="#8b949e"),
        cliponaxis=False,
        hovertemplate="%{x|%b %Y}<br>%{y:,} employees<extra></extra>",
    )
    fig.add_scatter(
        x=monthly["event_date"],
        y=monthly["notices"],
        name="Notices",
        yaxis="y2",
        mode="lines+markers",
        line=dict(color=ACCENT2, width=2),
        hovertemplate="%{x|%b %Y}<br>%{y:,} notices<extra></extra>",
    )
    fig.update_layout(
        yaxis=dict(title="Employees"),
        yaxis2=dict(title="Notices", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.08),
        barmode="overlay",
    )
    # Mark the current month and shade months beyond it: states publish
    # future effective dates, so bars to the right are scheduled layoffs,
    # not observed history.
    now_month = pd.Timestamp.now().to_period("M").to_timestamp()
    x_end = monthly["event_date"].max()
    if pd.notna(x_end) and x_end >= now_month:
        fig.add_vrect(
            x0=now_month, x1=x_end + pd.DateOffset(months=1),
            fillcolor="rgba(247,129,102,0.07)", line_width=0,
        )
    fig.add_vline(x=now_month, line_dash="dot", line_color=ACCENT2, line_width=1.5)
    fig.add_annotation(
        x=now_month, y=1.04, yref="paper", xanchor="left",
        text="current month → future-dated", showarrow=False,
        font=dict(size=11, color=ACCENT2),
    )
    fig = _apply_theme(fig)
    warn_charts._save_chart(fig, "us_monthly", save_png)
    return fig


def _year_frames(df: pd.DataFrame, max_years: int = 6) -> list:
    """(label, frame) pairs: each recent year, newest first, then all time."""
    years = sorted(
        {int(y) for y in df["event_date"].dropna().dt.year.unique()},
        reverse=True,
    )[:max_years]
    frames = [(str(y), df[df["event_date"].dt.year == y]) for y in years]
    frames.append(("All time", df))
    return frames


def _year_menu(fig: go.Figure, labels: list) -> go.Figure:
    """One-visible-trace-at-a-time dropdown over per-year traces."""
    n = len(labels)
    fig.update_layout(
        updatemenus=[
            dict(
                buttons=[
                    dict(label=lbl, method="update",
                         args=[{"visible": [j == i for j in range(n)]}])
                    for i, lbl in enumerate(labels)
                ],
                direction="down",
                x=0.01, y=1.12, xanchor="left", yanchor="top",
                bgcolor="#161b22", bordercolor="#21262d",
                font=dict(color="#e6edf3"),
            )
        ]
    )
    return fig


# Qualitative palette for multi-line comparisons, tuned for the dark theme.
LINE_COLORS = [
    "#58a6ff", "#f78166", "#3fb950", "#d29922", "#bc8cff",
    "#39c5cf", "#f85149", "#7ce38b", "#ffa657", "#79c0ff",
]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def chart_us_monthly_years(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """Seasonal comparison: one line per year, employees by calendar month.

    A dropdown switches between monthly totals and cumulative year-to-date
    lines (each month adding onto the last), for comparing how years built
    up over time.
    """
    dated = df[df["event_date"].notna()].copy()
    dated["year"] = dated["event_date"].dt.year
    dated["month"] = dated["event_date"].dt.month
    years = sorted(sorted(dated["year"].unique(), reverse=True)[:6])
    now = pd.Timestamp.now()

    fig = go.Figure()
    for cumulative in (False, True):
        for i, year in enumerate(years):
            ydf = dated[dated["year"] == year]
            by_month = ydf.groupby("month")["employees"].sum()
            # Months not yet reached in the current year stay blank, not zero.
            last_month = now.month if year == now.year else 12
            y_vals, running = [], 0
            for m in range(1, 13):
                if m > last_month:
                    y_vals.append(None)
                    continue
                running += int(by_month.get(m, 0))
                y_vals.append(running if cumulative else int(by_month.get(m, 0)))
            is_current = year == now.year
            suffix = " cumulative" if cumulative else ""
            fig.add_scatter(
                x=_MONTHS, y=y_vals, name=str(year),
                legendgroup=str(year),
                visible=cumulative,
                mode="lines+markers",
                line=dict(
                    color=LINE_COLORS[i % len(LINE_COLORS)],
                    width=3.5 if is_current else 1.8,
                ),
                marker=dict(size=7 if is_current else 5),
                hovertemplate=("%{x} " + str(year) + "<br>%{y:,} employees"
                               + suffix + "<extra></extra>"),
            )

    n = len(years)
    fig.update_layout(
        yaxis_title="Employees affected",
        legend=dict(orientation="h", y=1.08),
        updatemenus=[dict(
            type="buttons",
            buttons=[
                dict(label="Cumulative (year to date)", method="update",
                     args=[{"visible": [False] * n + [True] * n}]),
                dict(label="Monthly totals", method="update",
                     args=[{"visible": [True] * n + [False] * n}]),
            ],
            active=0,
            direction="right",
            x=0.01, y=1.32, xanchor="left", yanchor="top",
            bgcolor="#161b22", bordercolor="#21262d",
            font=dict(color="#e6edf3"),
        )],
        annotations=[dict(
            text=f"{now.strftime('%b %Y')} is in progress",
            x=0.99, y=0.02, xref="paper", yref="paper",
            xanchor="right", yanchor="bottom",
            showarrow=False, font=dict(size=11, color="#8b949e"),
        )],
    )
    fig = _apply_theme(fig, margin=dict(l=60, r=30, t=95, b=60))
    warn_charts._save_chart(fig, "us_monthly_years", save_png)
    return fig


def chart_us_states_years(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """Top states compared across years — one line per state.

    Years outside a state's coverage window render as gaps, never as
    misleading zeros. Legend entries toggle lines on/off.
    """
    dated = df[df["event_date"].notna()].copy()
    dated["year"] = dated["event_date"].dt.year
    years = sorted(dated["year"].unique(), reverse=True)[:10]
    years = sorted(years)
    window = dated[dated["year"].isin(years)]

    top_states = (
        window.groupby("state")["employees"].sum()
        .sort_values(ascending=False).head(10).index.tolist()
    )
    coverage = dated.groupby("state")["year"].agg(["min", "max"])

    fig = go.Figure()
    for i, st in enumerate(top_states):
        sdf = window[window["state"] == st]
        by_year = sdf.groupby("year")["employees"].sum()
        lo, hi = coverage.loc[st, "min"], coverage.loc[st, "max"]
        y_vals = [
            int(by_year.get(y, 0)) if lo <= y <= hi else None
            for y in years
        ]
        fig.add_scatter(
            x=years, y=y_vals, name=st,
            mode="lines+markers",
            connectgaps=False,
            line=dict(color=LINE_COLORS[i % len(LINE_COLORS)], width=2),
            marker=dict(size=6),
            hovertemplate="<b>" + st + "</b> %{x}<br>%{y:,} employees"
                          "<extra></extra>",
        )
    fig.update_layout(
        yaxis_title="Employees affected",
        xaxis=dict(dtick=1),
        legend=dict(orientation="h", y=1.12),
        height=520,
        annotations=[dict(
            text="top 10 states by volume — click legend entries to "
                 "toggle; gaps = outside that state's coverage",
            x=0.99, y=0.02, xref="paper", yref="paper",
            xanchor="right", yanchor="bottom",
            showarrow=False, font=dict(size=11, color="#8b949e"),
        )],
    )
    fig = _apply_theme(fig)
    warn_charts._save_chart(fig, "us_states_years", save_png)
    return fig


def chart_us_top_states(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """States ranked by employees affected — dropdown over each recent year.

    Every bar carries a visible value label ("n/r" when a state's feed
    publishes no headcounts), and hover works row-wide so zero-length bars
    still respond.
    """

    def ranked(frame: pd.DataFrame) -> pd.DataFrame:
        # Every state with reported headcounts in the window; states whose
        # feeds published no counts for it are omitted (footnote says so)
        # rather than shown as zero-length "n/r" bars.
        r = (
            frame.groupby("state")
            .agg(employees=("employees", "sum"), notices=("employees", "size"))
            .sort_values("employees", ascending=True)
            .reset_index()
        )
        return r[r["employees"] > 0]

    frames = [(label, ranked(frame)) for label, frame in _year_frames(df)]
    fig = go.Figure()
    max_rows = 0
    for i, (label, r) in enumerate(frames):
        max_rows = max(max_rows, len(r))
        fig.add_bar(
            x=r["employees"], y=r["state"], orientation="h",
            name=label, marker_color=ACCENT3,
            customdata=r["notices"], visible=(i == 0),
            text=[f"{v:,.0f}" for v in r["employees"]],
            textposition="outside",
            textfont=dict(size=10, color="#8b949e"),
            hovertemplate=("<b>%{y}</b><br>%{x:,} employees"
                           "<br>%{customdata:,} notices<extra>%{fullData.name}"
                           "</extra>"),
        )
    fig.update_layout(
        xaxis_title="Employees affected", yaxis_title="",
        showlegend=False,
        hovermode="y unified",
        # Tall enough that ~47 horizontal bars stay readable.
        height=max(600, 22 * max_rows + 160),
        annotations=[dict(
            text=("states with notices but no reported headcounts are "
                  "omitted — the map shows their notice counts"),
            x=0.99, y=0.0, xref="paper", yref="paper",
            xanchor="right", yanchor="bottom",
            showarrow=False, font=dict(size=11, color="#8b949e"),
        )],
    )
    fig = _year_menu(_apply_theme(fig), [label for label, _ in frames])
    warn_charts._save_chart(fig, "us_top_states", save_png)
    return fig


def chart_us_top_companies(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """Top 20 employers by employees affected — dropdown over each recent year."""

    def ranked(frame: pd.DataFrame) -> pd.DataFrame:
        work = frame[frame["employees"] > 0].copy()
        # Merge trivial name variants ("Meta Platforms, Inc." vs "…, Inc"):
        # group on a normalized key, display the most common original form.
        work["norm"] = (
            work["company"].astype(str).str.strip()
            .str.replace(r"\s+", " ", regex=True)
            .str.rstrip(".,")
            .str.casefold()
        )
        top = (
            work.groupby("norm")
            .agg(
                employees=("employees", "sum"),
                company=("company", lambda s: s.value_counts().idxmax()),
            )
            .sort_values("employees", ascending=True)
            .tail(20)
            .reset_index(drop=True)
        )
        # Axis labels: ellipsized for width, made unique with invisible
        # hair-spaces so plotly never merges two companies into one row.
        labels, seen = [], set()
        for name in top["company"]:
            lbl = name if len(name) <= 32 else name[:31] + "…"
            while lbl in seen:
                lbl += " "
            seen.add(lbl)
            labels.append(lbl)
        top["label"] = labels
        return top

    frames = [(label, ranked(frame)) for label, frame in _year_frames(df)]
    fig = go.Figure()
    for i, (label, t) in enumerate(frames):
        fig.add_bar(
            x=t["employees"], y=t["label"], orientation="h",
            name=label, marker_color=ACCENT, visible=(i == 0),
            customdata=t["company"],
            text=[f"{v:,.0f}" for v in t["employees"]],
            textposition="outside",
            textfont=dict(size=10, color="#8b949e"),
            cliponaxis=False,
            hovertemplate=("<b>%{customdata}</b><br>%{x:,} employees"
                           "<extra>%{fullData.name}</extra>"),
        )
    fig.update_layout(xaxis_title="Employees affected", yaxis_title="",
                      showlegend=False, hovermode="y unified")
    fig = _year_menu(
        _apply_theme(fig, margin=dict(l=10, r=30, t=40, b=60)),
        [label for label, _ in frames],
    )
    fig.update_yaxes(automargin=True)
    warn_charts._save_chart(fig, "us_top_companies", save_png)
    return fig


# ---------------------------------------------------------------------------
# Recent notices table
# ---------------------------------------------------------------------------


PAGE_SIZE = 250


def _row_values(r) -> list:
    nd = (
        r["notice_date"].strftime("%Y-%m-%d")
        if pd.notna(r["notice_date"])
        else "—"
    )
    ed = (
        r["effective_date"].strftime("%Y-%m-%d")
        if pd.notna(r["effective_date"])
        else "—"
    )
    emp = _fmt(r["employees"]) if r["employees"] else "—"
    place = str(r.get("city") or r.get("county") or "")
    return [str(r["state"]), str(r.get("company", ""))[:60], place[:30], nd, ed, emp]


def _recent_sorted(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["event_date"].notna()].sort_values("event_date", ascending=False)


def _recent_rows(df: pd.DataFrame, limit: int = PAGE_SIZE) -> str:
    rows = []
    for _, r in _recent_sorted(df).head(limit).iterrows():
        st, co, place, nd, ed, emp = _row_values(r)
        rows.append(
            f'<tr data-state="{st}"><td class="st">{st}</td>'
            f"<td>{co}</td><td>{place}</td><td>{nd}</td><td>{ed}</td>"
            f'<td class="num">{emp}</td></tr>'
        )
    return "\n".join(rows)


def _write_pages(df: pd.DataFrame, out_dir: Path) -> dict:
    """Chunk the dataset into static page-file sets the table fetches lazily.

    One set for everything (``pages/all/``) plus one per state
    (``pages/IL/`` …), so selecting a state pages through that state's full
    record list — every page full — instead of sieving global pages.
    ~250 rows per file (~30 KB) keeps the main page light while making every
    record browsable without ever loading the whole dataset at once.

    Returns {set_name: page_count} for the client-side pager.
    """
    import shutil

    recent = _recent_sorted(df)
    pages_dir = out_dir / "pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)  # drop stale chunks from previous layouts

    def write_set(name: str, frame: pd.DataFrame) -> int:
        total = -(-len(frame) // PAGE_SIZE) if len(frame) else 0
        set_dir = pages_dir / name
        set_dir.mkdir(parents=True, exist_ok=True)
        for i in range(total):
            chunk = frame.iloc[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
            payload = {
                "page": i + 1,
                "total_pages": total,
                "rows": [_row_values(r) for _, r in chunk.iterrows()],
            }
            (set_dir / f"{i + 1}.json").write_text(json.dumps(payload))
        return total

    counts = {"all": write_set("all", recent)}
    for st, frame in recent.groupby("state"):
        if isinstance(st, str) and len(st) == 2:
            counts[st] = write_set(st, frame)
    return counts


# ---------------------------------------------------------------------------
# Lazy search index
# ---------------------------------------------------------------------------

SEARCH_INDEX_NAME = "search_index.json"

# Field separator inside an index row. Only `company` is allowed to contain it
# (a handful of real filings do — "Bed Bath & Beyond | Buy Buy Baby Inc | …");
# every other field is sanitised at build time so the client can locate the
# trailing four fields by scanning back from the end of the string. Nothing is
# dropped or invented: company text is stored verbatim.
INDEX_SEP = "|"


def _index_row(r) -> str:
    """One record as a compact delimited string: ST|Company|Place|ND|ED|Emp.

    Dates lose their dashes (``20260706``) and headcounts their separators;
    the client re-formats both, so the rendered table is byte-identical to
    what the pre-chunked page files show. Empty means "not reported" and
    renders as an em dash, never a guessed value.
    """
    st, co, place, nd, ed, emp = _row_values(r)
    nd = "" if nd == "—" else nd.replace("-", "")
    ed = "" if ed == "—" else ed.replace("-", "")
    emp = "" if emp == "—" else emp.replace(",", "")
    fields = [st, co, place, nd, ed, emp]
    return INDEX_SEP.join(
        f if i == 1 else f.replace(INDEX_SEP, "/") for i, f in enumerate(fields)
    )


def _write_search_index(df: pd.DataFrame, out_dir: Path) -> dict:
    """Write the single-file search index the table loads on first keystroke.

    Rows are in the same order as ``pages/all/`` so a search result and a
    normal page show the same record in the same form. The file is *not*
    referenced by any tag on the page — it is fetched only when the visitor
    actually types — so the no-search path costs exactly what it did before.

    Returns ``{"records": n, "bytes": n}`` for logging.
    """
    recent = _recent_sorted(df)
    rows = [_index_row(r) for _, r in recent.iterrows()]
    payload = {
        "page_size": PAGE_SIZE,
        "total": len(rows),
        "rows": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SEARCH_INDEX_NAME
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text)
    return {"records": len(rows), "bytes": len(text.encode("utf-8"))}


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

US_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>US WARN Layoff Tracker</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js"></script>
<style>
/* Mobile-first: base styles target phones; the media query below layers on
   the desktop layout. */
:root {{ --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
         --muted:#8b949e; --accent:#58a6ff; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ -webkit-text-size-adjust:100%; }}
body {{ background:var(--bg); color:var(--text);
        font:14px/1.5 Inter,system-ui,sans-serif; }}
a {{ color:var(--accent); text-decoration:none; }}
header {{ position:sticky; top:0; z-index:10; background:rgba(13,17,23,.97);
          border-bottom:1px solid var(--border); padding:10px 14px; }}
header h1 {{ font-size:17px; display:inline; margin-right:8px; }}
header .sub {{ display:block; color:var(--muted); font-size:12px;
               margin-top:2px; }}
header .right {{ display:block; font-size:12px; color:var(--muted);
                 margin-top:4px; }}
main {{ max-width:1200px; margin:0 auto; padding:12px; }}
.badge {{ display:inline-block; background:#1f6feb33; color:var(--accent);
          border:1px solid #1f6feb66; border-radius:12px; padding:1px 10px;
          font-size:12px; vertical-align:middle; }}
.kpis {{ display:grid; grid-template-columns:repeat(2,1fr);
         gap:10px; margin:14px 0 20px; }}
.kpi {{ background:var(--card); border:1px solid var(--border);
        border-radius:10px; padding:12px 14px; }}
.kpi .label {{ font-size:10px; letter-spacing:.06em; text-transform:uppercase;
               color:var(--muted); }}
.kpi .value {{ font-size:21px; font-weight:700; margin-top:4px;
               overflow-wrap:anywhere; }}
.kpi .note {{ font-size:11px; color:var(--muted); }}
section {{ background:var(--card); border:1px solid var(--border);
           border-radius:12px; padding:12px; margin-bottom:16px; }}
section h2 {{ font-size:15px; margin-bottom:4px; }}
section .desc {{ color:var(--muted); font-size:12.5px; margin-bottom:10px; }}
.chart {{ width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px;
         min-width:560px; }}
th,td {{ text-align:left; padding:6px 8px;
         border-bottom:1px solid var(--border); }}
th {{ color:var(--muted); font-weight:600; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.st {{ color:var(--accent); font-weight:600; }}
select, input[type=search], input[type=number] {{
  background:var(--card); color:var(--text); font-size:14px;
  border:1px solid var(--border); border-radius:6px; padding:8px 10px;
  width:100%; margin:4px 0; }}
select option:disabled {{ color:#555c66; }}
.pgbtn {{ background:var(--card); color:var(--accent);
          border:1px solid var(--border); border-radius:6px;
          padding:10px 16px; cursor:pointer; min-height:42px;
          font-size:14px; }}
.pgbtn:hover {{ border-color:var(--accent); }}
.pager {{ display:flex; gap:8px; align-items:center; margin-top:12px;
          flex-wrap:wrap; }}
.pager input {{ width:80px; margin:0; }}
.tabs {{ display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; }}
.tab {{ background:var(--card); border:1px solid var(--border);
        color:var(--muted); border-radius:6px; padding:8px 16px;
        cursor:pointer; font-size:13.5px; }}
.tab.active {{ color:var(--accent); border-color:var(--accent); }}
.pane {{ display:none; }}
.pane.active {{ display:block; }}
footer {{ color:var(--muted); font-size:12px; text-align:center;
          padding:20px 14px; }}

/* Email signup — full-width stacked controls on phones. */
.sub-form {{ display:grid; gap:10px; }}
.sub-row {{ display:grid; gap:10px; }}
.sub-form input[type=text], .sub-form input[type=email] {{
  background:var(--bg); color:var(--text); font-size:14px;
  border:1px solid var(--border); border-radius:6px; padding:11px 12px;
  width:100%; }}
.sub-form input[type=text]:focus, .sub-form input[type=email]:focus {{
  border-color:var(--accent); outline:none; }}
.sub-hp {{ position:absolute; left:-9999px; width:1px; height:1px;
           opacity:0; }}
.sub-states {{ border:1px solid var(--border); border-radius:8px;
               padding:10px 12px 12px; }}
.sub-states legend {{ font-size:12px; color:var(--muted); padding:0 6px; }}
.sub-actions {{ display:flex; gap:8px; margin-top:6px; }}
.sub-mini {{ background:var(--card); color:var(--accent);
             border:1px solid var(--border); border-radius:6px;
             padding:6px 12px; font-size:12px; cursor:pointer; }}
.sub-mini:hover {{ border-color:var(--accent); }}
.sub-grid {{ display:grid; gap:6px; margin-top:8px; max-height:200px;
             overflow-y:auto; -webkit-overflow-scrolling:touch;
             grid-template-columns:repeat(auto-fill,minmax(66px,1fr)); }}
.sub-st {{ display:flex; align-items:center; gap:6px; font-size:12.5px;
           background:var(--bg); border:1px solid var(--border);
           border-radius:6px; padding:8px; cursor:pointer;
           min-height:38px; }}
.sub-st input, .sub-digest input {{ accent-color:var(--accent);
                                    width:15px; height:15px; }}
.sub-digest {{ display:flex; gap:9px; align-items:flex-start;
               background:var(--bg); border:1px solid var(--border);
               border-radius:8px; padding:12px; font-size:13px;
               cursor:pointer; }}
.sub-digest span {{ display:block; color:var(--muted); font-size:11.5px;
                    margin-top:2px; }}
.sub-btn {{ background:var(--accent); color:#0d1117; border:0;
            border-radius:8px; padding:12px 18px; font-size:14.5px;
            font-weight:600; cursor:pointer; width:100%; min-height:44px; }}
.sub-btn:disabled {{ opacity:.6; cursor:not-allowed; }}
.sub-msg {{ font-size:12.5px; min-height:1.2em; color:var(--muted); }}
.sub-msg.ok {{ color:#3fb950; }}
.sub-msg.err {{ color:#f78166; }}

@media (min-width: 720px) {{
  body {{ font-size:15px; }}
  header {{ padding:14px 24px; display:flex; align-items:baseline;
            gap:14px; flex-wrap:wrap; }}
  header h1 {{ font-size:20px; display:block; margin-right:0; }}
  header .sub {{ display:inline; margin-top:0; font-size:13px; }}
  header .right {{ display:block; margin-left:auto; margin-top:0;
                   font-size:13px; }}
  main {{ padding:24px; }}
  .kpis {{ grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
           gap:14px; }}
  .kpi .value {{ font-size:26px; }}
  section {{ padding:18px; margin-bottom:24px; }}
  table {{ font-size:13.5px; }}
  th,td {{ padding:7px 10px; }}
  select, input[type=search], input[type=number] {{
    width:auto; margin:0; padding:5px 8px; display:inline-block; }}
  .pgbtn {{ padding:5px 12px; min-height:0; }}
  .sub-row {{ grid-template-columns:1fr 1fr; }}
  .sub-btn {{ width:auto; justify-self:start; }}
  .sub-grid {{ grid-template-columns:repeat(auto-fill,minmax(72px,1fr));
               max-height:none; }}
}}
</style>
</head>
<body>
<header>
  <h1>🇺🇸 US WARN Layoff Tracker</h1>
  <span class="badge">{live_badge}</span>
  <span class="sub">unified WARN notices, updated twice daily</span>
  <span class="right">Updated {updated} ·
    <a href="../">California dashboard</a> ·
    <a href="data.json">API</a> ·
    <a href="https://github.com/bilalahamad0/warn">GitHub</a></span>
</header>
<main>
  <div class="kpis">
    <div class="kpi"><div class="label">{year} Notices</div>
      <div class="value">{year_notices}</div>
      <div class="note">across {live_short}</div></div>
    <div class="kpi"><div class="label">{year} Employees</div>
      <div class="value">{year_employees}</div>
      <div class="note">affected this year</div></div>
    <div class="kpi"><div class="label">Largest {year} Layoff</div>
      <div class="value" style="font-size:17px">{largest_company}</div>
      <div class="note">{largest_employees} employees · {largest_state}</div></div>
    <div class="kpi"><div class="label">Top State {year}</div>
      <div class="value">{top_state}</div>
      <div class="note">{top_state_employees} employees</div></div>
    <div class="kpi"><div class="label">All-Time Records</div>
      <div class="value">{total_notices}</div>
      <div class="note">{total_employees} employees</div></div>
  </div>

  <section>
    <h2>WARN activity by state</h2>
    <div class="desc">Toggle the metric, pick a year; hover a state
      for details. States light up as their sources come online.</div>
    <div class="chart">{map_div}</div>
  </section>

  <section>
    <h2>National monthly trend</h2>
    <div class="tabs">
      <button class="tab active" data-pane="pane-timeline">Timeline</button>
      <button class="tab" data-pane="pane-byyear">By year</button>
    </div>
    <div id="pane-timeline" class="pane active">
      <div class="desc">Employees affected per month across all live
        states, last 24 months.</div>
      <div class="chart">{monthly_div}</div>
    </div>
    <div id="pane-byyear" class="pane">
      <div class="desc">The same calendar months overlaid across recent
        years — the current year draws thicker. Switch the dropdown to
        cumulative year-to-date lines to compare how each year built up.</div>
      <div class="chart">{monthly_years_div}</div>
    </div>
  </section>

  <section>
    <h2>States ranked</h2>
    <div class="tabs">
      <button class="tab active" data-pane="pane-ranking">Ranking</button>
      <button class="tab" data-pane="pane-overyears">Over the years</button>
    </div>
    <div id="pane-ranking" class="pane active">
      <div class="desc">Employees affected per state — pick any recent
        year or all time from the dropdown. Coverage depth varies by
        state; states without reported headcounts for the window are
        omitted.</div>
      <div class="chart">{states_div}</div>
    </div>
    <div id="pane-overyears" class="pane">
      <div class="desc">Year-by-year employees affected for the top 10
        states — click legend entries to add or remove states from the
        comparison.</div>
      <div class="chart">{states_years_div}</div>
    </div>
  </section>

  <section>
    <h2>Top employers nationally</h2>
    <div class="desc">Employees affected by employer — pick any recent
      year or all time from the dropdown.</div>
    <div class="chart">{companies_div}</div>
  </section>

  <section>
    <h2>All notices</h2>
    <div class="desc">Every record, newest first, {page_size} per page —
      pages load on demand so the site stays fast. Picking a state pages
      through that state's full history. Bulk access:
      <a href="data.json">data.json</a>.
      Searching a company scans every record in the dataset, not just the
      page on screen — results page the same way.
      Filter: <select id="stfilter"><option value="">All states</option>
      {state_options}
      {unavailable_options}</select>
      <input id="cofilter" type="search" placeholder="Search company…">
      <span id="filternote" style="color:var(--muted);font-size:12px"></span>
    </div>
    <div class="pager">
      <button class="pgbtn pgprev">← Prev</button>
      <span class="pginfo">Page 1 of {total_pages}</span>
      <button class="pgbtn pgnext">Next →</button>
      <span style="color:var(--muted);font-size:13px">Jump to:</span>
      <input class="pgjump" type="number" min="1" max="{total_pages}" value="1">
    </div>
    <div style="overflow-x:auto"><table id="recent">
      <thead><tr><th>State</th><th>Company</th><th>Location</th>
        <th>Notice</th><th>Effective</th><th>Employees</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table></div>
    <div class="pager">
      <button class="pgbtn pgprev">← Prev</button>
      <span class="pginfo">Page 1 of {total_pages}</span>
      <button class="pgbtn pgnext">Next →</button>
      <span style="color:var(--muted);font-size:13px">Jump to:</span>
      <input class="pgjump" type="number" min="1" max="{total_pages}" value="1">
    </div>
  </section>

  <section id="subscribe">
    <h2>📬 Get WARN alerts by email</h2>
    <div class="desc">Pick the states you want per-notice alerts for. We
      email you when our twice-daily check finds new notices there.</div>
    <form class="sub-form" id="subscribe-form" novalidate>
      <div class="sub-row">
        <input type="text" id="sub-name" placeholder="Your name"
               autocomplete="name" aria-label="Your name">
        <input type="email" id="sub-email" placeholder="you@example.com"
               autocomplete="email" aria-label="Email address" required>
      </div>
      <input type="text" id="sub-company-hp" class="sub-hp" tabindex="-1"
             autocomplete="off" aria-hidden="true">
      <fieldset class="sub-states">
        <legend>Alert me about these states</legend>
        <div class="sub-actions">
          <button type="button" class="sub-mini" id="sub-all">Select all</button>
          <button type="button" class="sub-mini" id="sub-none">Clear</button>
        </div>
        <div class="sub-grid">{state_checkboxes}</div>
      </fieldset>
      <label class="sub-digest">
        <input type="checkbox" id="sub-digest">
        <span style="color:var(--text);font-size:13px">Monthly summary of
          the whole US
          <span>One email a month covering every live state — separate
            from the per-state alerts above.</span></span>
      </label>
      <button type="submit" class="sub-btn" id="sub-submit">Subscribe</button>
      <div class="sub-msg" id="subscribe-msg" role="status"
           aria-live="polite"></div>
    </form>
  </section>
</main>
<footer>
  Data: official state workforce-agency WARN publications, unified by this
  project. California keeps its dedicated dashboard <a href="../">here</a>.
  Some states publish limited fields or shallow history; blocked or
  non-publishing states are documented in
  <a href="https://github.com/bilalahamad0/warn/blob/main/EXPANSION_RESEARCH.md">
  EXPANSION_RESEARCH.md</a>.
</footer>
<script>
var PAGE_COUNTS = {page_counts};
var PAGE_SIZE = {page_size};
var SEARCH_INDEX_URL = '{search_index_name}';
var currentSet = 'all';
var currentPage = 1;

// Search state. INDEX stays null until the visitor actually types, so the
// browse-only path never downloads it.
var INDEX = null;        // {{ rows: [...], key: [...], st: [...] }}
var indexPromise = null;
var query = '';          // active search term, lowercased
var matches = null;      // row indexes matching query + state, or null

var noteEl = document.getElementById('filternote');
var stfilterEl = document.getElementById('stfilter');
var cofilterEl = document.getElementById('cofilter');

function setNote(text) {{ noteEl.textContent = text; }}

// ── Index row decoding ──────────────────────────────────────────────
// "ST|Company|Place|20260706|20260904|160". Company may itself contain the
// separator, so the trailing four fields are located from the end.
function rowCuts(s) {{
  var e = s.length, cuts = [];
  for (var k = 0; k < 4; k++) {{
    e = s.lastIndexOf('|', e - 1);
    cuts.unshift(e);
  }}
  return cuts;
}}
function companyOf(s) {{
  return s.slice(s.indexOf('|') + 1, rowCuts(s)[0]);
}}
function stateOf(s) {{ return s.slice(0, s.indexOf('|')); }}
function fmtDate(v) {{
  return v ? v.slice(0, 4) + '-' + v.slice(4, 6) + '-' + v.slice(6, 8) : '—';
}}
function fmtNum(v) {{
  return v ? Number(v).toLocaleString('en-US') : '—';
}}
function decodeRow(s) {{
  var c = rowCuts(s);
  return [
    s.slice(0, s.indexOf('|')),
    s.slice(s.indexOf('|') + 1, c[0]),
    s.slice(c[0] + 1, c[1]),
    fmtDate(s.slice(c[1] + 1, c[2])),
    fmtDate(s.slice(c[2] + 1, c[3])),
    fmtNum(s.slice(c[3] + 1))
  ];
}}

function loadIndex() {{
  if (indexPromise) return indexPromise;
  setNote('loading search index…');
  indexPromise = fetch(SEARCH_INDEX_URL)
    .then(function (r) {{ return r.json(); }})
    .then(function (d) {{
      var rows = d.rows || [];
      INDEX = {{
        rows: rows,
        key: rows.map(function (s) {{ return companyOf(s).toLowerCase(); }}),
        st: rows.map(stateOf)
      }};
      return INDEX;
    }})
    .catch(function () {{
      indexPromise = null;
      setNote('search unavailable right now — try again in a moment');
      return null;
    }});
  return indexPromise;
}}

function computeMatches() {{
  matches = [];
  if (!INDEX || !query) return;
  var st = currentSet === 'all' ? '' : currentSet;
  for (var i = 0; i < INDEX.key.length; i++) {{
    if (st && INDEX.st[i] !== st) continue;
    if (INDEX.key[i].indexOf(query) !== -1) matches.push(i);
  }}
}}

function searchPages() {{
  return matches ? Math.ceil(matches.length / PAGE_SIZE) : 0;
}}

// ── Rendering ───────────────────────────────────────────────────────
function renderRows(rows) {{
  var tbody = document.querySelector('#recent tbody');
  tbody.textContent = '';
  rows.forEach(function (r) {{
    var tr = document.createElement('tr');
    tr.dataset.state = r[0];
    r.forEach(function (v, i) {{
      var td = document.createElement('td');
      td.textContent = v;
      if (i === 0) td.className = 'st';
      if (i === 5) td.className = 'num';
      tr.appendChild(td);
    }});
    tbody.appendChild(tr);
  }});
}}

function setPagerText(text) {{
  document.querySelectorAll('.pginfo').forEach(function (el) {{
    el.textContent = text;
  }});
}}

function syncJump(page, total) {{
  document.querySelectorAll('.pgjump').forEach(function (jump) {{
    jump.value = page;
    jump.max = Math.max(1, total);
  }});
}}

function scopeLabel() {{
  return currentSet === 'all' ? '' : ' — ' + currentSet + ' only';
}}

function gotoSearchPage(n) {{
  var total = searchPages();
  var label = scopeLabel();
  var hits = matches.length.toLocaleString();
  if (!total) {{
    currentPage = 1;
    renderRows([]);
    setPagerText('No matches' + label);
    syncJump(1, 1);
    setNote('0 records match "' + query + '"' + label);
    return;
  }}
  n = Math.max(1, Math.min(total, n));
  currentPage = n;
  var slice = matches.slice((n - 1) * PAGE_SIZE, n * PAGE_SIZE);
  renderRows(slice.map(function (i) {{ return decodeRow(INDEX.rows[i]); }}));
  setPagerText('Page ' + n + ' of ' + total + label);
  syncJump(n, total);
  setNote(hits + ' matching record(s) for "' + query + '"' + label
          + ' — across all pages');
}}

function gotoBrowsePage(n) {{
  var total = PAGE_COUNTS[currentSet] || 0;
  var label = scopeLabel();
  if (!total) {{
    renderRows([]);
    setPagerText('No dated records' + label);
    syncJump(1, 1);
    return;
  }}
  n = Math.max(1, Math.min(total, n));
  fetch('pages/' + currentSet + '/' + n + '.json')
    .then(function (r) {{ return r.json(); }})
    .then(function (p) {{
      currentPage = p.page;
      renderRows(p.rows);
      setPagerText('Page ' + p.page + ' of ' + p.total_pages + label);
      syncJump(p.page, p.total_pages);
    }});
}}

function gotoPage(n) {{
  if (query && matches) {{ gotoSearchPage(n); }} else {{ gotoBrowsePage(n); }}
}}

function refreshSearch() {{
  if (!query) {{
    matches = null;
    setNote('');
    gotoBrowsePage(1);
    return;
  }}
  loadIndex().then(function (idx) {{
    if (!idx || !query) return;
    computeMatches();
    gotoSearchPage(1);
  }});
}}

document.querySelectorAll('.pgprev').forEach(function (b) {{
  b.addEventListener('click', function () {{ gotoPage(currentPage - 1); }});
}});
document.querySelectorAll('.pgnext').forEach(function (b) {{
  b.addEventListener('click', function () {{ gotoPage(currentPage + 1); }});
}});
document.querySelectorAll('.pgjump').forEach(function (j) {{
  j.addEventListener('change', function () {{
    gotoPage(parseInt(this.value, 10) || 1);
  }});
}});
stfilterEl.addEventListener('change', function () {{
  currentSet = this.value || 'all';
  if (query) {{ refreshSearch(); }} else {{ gotoBrowsePage(1); }}
}});

var searchTimer = null;
cofilterEl.addEventListener('input', function () {{
  var next = this.value.trim().toLowerCase();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function () {{
    if (next === query) return;
    query = next;
    refreshSearch();
  }}, 200);
}});

// ── Email signup ────────────────────────────────────────────────────
var SIGNUP_ENDPOINT = "{signup_endpoint}";
var DIGEST_CODE = 'US';   // sentinel for the whole-US monthly summary
var subForm = document.getElementById('subscribe-form');
var subMsg = document.getElementById('subscribe-msg');
var subName = document.getElementById('sub-name');
var subEmail = document.getElementById('sub-email');
var subBtn = document.getElementById('sub-submit');
var subHp = document.getElementById('sub-company-hp');
var subDigest = document.getElementById('sub-digest');

function setSubMsg(text, kind) {{
  if (!subMsg) return;
  subMsg.textContent = text;
  subMsg.className = 'sub-msg' + (kind ? ' ' + kind : '');
}}
function validEmail(v) {{ return /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v); }}

// "CA,NY", "CA,US" or "US" — state codes first, digest sentinel last.
function selectedStates() {{
  var out = [];
  document.querySelectorAll('.sub-state:checked').forEach(function (c) {{
    out.push(c.value);
  }});
  if (subDigest && subDigest.checked) out.push(DIGEST_CODE);
  return out;
}}

function setAllStates(on) {{
  document.querySelectorAll('.sub-state').forEach(function (c) {{
    c.checked = on;
  }});
}}
var subAll = document.getElementById('sub-all');
var subNone = document.getElementById('sub-none');
if (subAll) {{
  subAll.addEventListener('click', function () {{ setAllStates(true); }});
}}
if (subNone) {{
  subNone.addEventListener('click', function () {{ setAllStates(false); }});
}}

if (subForm) {{
  subForm.addEventListener('submit', function (ev) {{
    ev.preventDefault();
    if (subHp && subHp.value) return;   // honeypot: silently drop bots
    var nm = ((subName && subName.value) || '').trim();
    var em = ((subEmail && subEmail.value) || '').trim();
    if (!validEmail(em)) {{
      setSubMsg('Please enter a valid email address.', 'err');
      return;
    }}
    var states = selectedStates();
    if (!states.length) {{
      setSubMsg('Pick at least one state, or the monthly US summary.', 'err');
      return;
    }}
    if (!SIGNUP_ENDPOINT) {{
      setSubMsg("Signups aren't configured yet — check back soon.", 'err');
      return;
    }}
    if (subBtn) {{ subBtn.disabled = true; subBtn.textContent = 'Subscribing…'; }}
    setSubMsg('');
    fetch(SIGNUP_ENDPOINT, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'text/plain;charset=utf-8' }},
      body: JSON.stringify({{
        name: nm, email: em, states: states.join(','), source: 'us-dashboard'
      }})
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d && d.ok) {{
          setSubMsg(d.duplicate
            ? "You're already subscribed — preferences updated."
            : "You're in! Watch your inbox for new WARN alerts.", 'ok');
          subForm.reset();
        }} else {{
          setSubMsg('Something went wrong. Please try again later.', 'err');
        }}
      }})
      .catch(function () {{
        setSubMsg('Network error. Please try again later.', 'err');
      }})
      .finally(function () {{
        if (subBtn) {{
          subBtn.disabled = false;
          subBtn.textContent = 'Subscribe';
        }}
      }});
  }});
}}

// Tab switching: charts rendered while hidden need a resize kick once shown.
document.querySelectorAll('.tab').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    var section = btn.closest('section');
    section.querySelectorAll('.tab').forEach(function (b) {{
      b.classList.toggle('active', b === btn);
    }});
    section.querySelectorAll('.pane').forEach(function (p) {{
      p.classList.toggle('active', p.id === btn.dataset.pane);
    }});
    var pane = document.getElementById(btn.dataset.pane);
    pane.querySelectorAll('.plotly-graph-div').forEach(function (g) {{
      if (window.Plotly) {{ Plotly.Plots.resize(g); }}
    }});
  }});
}});
</script>
</body>
</html>
"""


def _chart_div(name: str) -> str:
    path = CHARTS_DIR / f"{name}.html"
    if path.exists():
        return path.read_text()
    return f'<div>Chart {name} not available</div>'


def build_us_site(
    national_file: Optional[Path] = None, out_dir: Optional[Path] = None
) -> Path:
    """Build docs/us/index.html + docs/us/data.json from the national dataset."""
    out_dir = out_dir if out_dir is not None else US_DIR
    payload = _load_national(national_file)
    df = _to_frame(payload)
    kpis = compute_us_kpis(payload)

    # Charts (fragments in docs/charts/; the US map is chart 12 from warn_charts)
    chart_us_monthly(df)
    chart_us_monthly_years(df)
    chart_us_top_states(df)
    chart_us_states_years(df)
    chart_us_top_companies(df)

    codes = sorted(c for c in df["state"].unique() if len(c) == 2)
    state_options = "\n".join(f'<option value="{c}">{c}</option>' for c in codes)
    # Jurisdictions with no public data appear greyed-out and unselectable,
    # so their absence reads as deliberate rather than an oversight.
    unavailable_options = "\n".join(
        f"<option disabled>{code} — no public data</option>"
        for code in sorted(warn_charts.UNAVAILABLE_STATES)
        if code not in codes
    )

    # Checkbox per live state, none pre-selected: subscribers opt in.
    state_checkboxes = "\n".join(
        f'<label class="sub-st"><input type="checkbox" class="sub-state" '
        f'value="{c}">{c}</label>'
        for c in codes
    )
    # Public endpoint, injected at build time. Unset → the form says so.
    signup_endpoint = os.getenv("SIGNUP_ENDPOINT", "").strip()

    page_counts = _write_pages(df, out_dir)
    index_stats = _write_search_index(df, out_dir)
    total_pages = page_counts["all"]
    updated = str(kpis["last_updated"])[:10]
    n_live = kpis["states_live"]
    has_dc = "DC" in payload.get("states", {})
    live_short = f"{n_live - 1} states + DC" if has_dc else f"{n_live} states"
    live_badge = f"{live_short} live"
    html = US_TEMPLATE.format(
        states_live=kpis["states_live"],
        live_badge=live_badge,
        live_short=live_short,
        updated=updated,
        year=kpis["year"],
        year_notices=_fmt(kpis["year_notices"]),
        year_employees=_fmt(kpis["year_employees"]),
        largest_company=kpis["largest"]["company"][:38],
        largest_employees=_fmt(kpis["largest"]["employees"]),
        largest_state=kpis["largest"]["state"],
        top_state=kpis["top_state"]["state"],
        top_state_employees=_fmt(kpis["top_state"]["employees"]),
        total_notices=_fmt(kpis["total_notices"]),
        total_employees=_fmt(kpis["total_employees"]),
        map_div=_chart_div("12_us_map"),
        monthly_div=_chart_div("us_monthly"),
        monthly_years_div=_chart_div("us_monthly_years"),
        states_div=_chart_div("us_top_states"),
        states_years_div=_chart_div("us_states_years"),
        companies_div=_chart_div("us_top_companies"),
        state_options=state_options,
        unavailable_options=unavailable_options,
        state_checkboxes=state_checkboxes,
        signup_endpoint=signup_endpoint,
        search_index_name=SEARCH_INDEX_NAME,
        recent_rows=_recent_rows(df),
        page_size=PAGE_SIZE,
        total_pages=total_pages,
        page_counts=json.dumps(page_counts),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(html)
    (out_dir / "data.json").write_text(json.dumps(payload, default=str))
    log.info(
        f"US dashboard built: {index} "
        f"({kpis['states_live']} states, {kpis['total_notices']} records)"
    )
    log.info(
        f"Search index: {index_stats['records']:,} rows, "
        f"{index_stats['bytes'] / 1e6:.2f} MB (fetched only on first search)"
    )
    return index


if __name__ == "__main__":
    build_us_site()
