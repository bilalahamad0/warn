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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    fig = go.Figure()
    fig.add_bar(
        x=monthly["event_date"],
        y=monthly["employees"],
        name="Employees affected",
        marker_color=ACCENT,
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


def _year_window(df: pd.DataFrame) -> pd.DataFrame:
    """Rows whose event date falls in the current calendar year."""
    year = datetime.now(timezone.utc).year
    return df[df["event_date"].dt.year == year]


def _year_toggle(fig: go.Figure, year_label: str) -> go.Figure:
    """Two-trace figures: current year (default) vs all time."""
    fig.update_layout(
        updatemenus=[
            dict(
                buttons=[
                    dict(label=year_label, method="update",
                         args=[{"visible": [True, False]}]),
                    dict(label="All time", method="update",
                         args=[{"visible": [False, True]}]),
                ],
                direction="down",
                x=0.99, y=1.12, xanchor="right", yanchor="top",
                bgcolor="#161b22", bordercolor="#21262d",
                font=dict(color="#e6edf3"),
            )
        ]
    )
    return fig


def chart_us_top_states(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """States ranked by employees affected — current year default, all-time toggle."""
    year = datetime.now(timezone.utc).year

    def ranked(frame: pd.DataFrame) -> pd.DataFrame:
        # Every live state, not a top-N: the whole point of the chart is
        # comparing all of them at once.
        return (
            frame.groupby("state")
            .agg(employees=("employees", "sum"), notices=("employees", "size"))
            .sort_values("employees", ascending=True)
            .reset_index()
        )

    fig = go.Figure()
    max_rows = 0
    for frame, visible in ((_year_window(df), True), (df, False)):
        r = ranked(frame)
        max_rows = max(max_rows, len(r))
        fig.add_bar(
            x=r["employees"], y=r["state"], orientation="h",
            marker_color=ACCENT3, customdata=r["notices"], visible=visible,
            hovertemplate=("<b>%{y}</b><br>%{x:,} employees"
                           "<br>%{customdata:,} notices<extra></extra>"),
        )
    fig.update_layout(
        xaxis_title="Employees affected", yaxis_title="",
        showlegend=False,
        # Tall enough that ~47 horizontal bars stay readable.
        height=max(600, 22 * max_rows + 160),
    )
    fig = _year_toggle(_apply_theme(fig), str(year))
    warn_charts._save_chart(fig, "us_top_states", save_png)
    return fig


def chart_us_top_companies(df: pd.DataFrame, save_png: bool = False) -> go.Figure:
    """Top 20 employers by employees affected — current year default."""
    year = datetime.now(timezone.utc).year

    def ranked(frame: pd.DataFrame) -> pd.DataFrame:
        top = (
            frame.groupby("company")["employees"]
            .sum()
            .sort_values(ascending=True)
            .tail(20)
            .reset_index()
        )
        top["label"] = top["company"].str.slice(0, 40)
        return top

    fig = go.Figure()
    for frame, visible in ((_year_window(df), True), (df, False)):
        t = ranked(frame)
        fig.add_bar(
            x=t["employees"], y=t["label"], orientation="h",
            marker_color=ACCENT, visible=visible,
            hovertemplate="<b>%{y}</b><br>%{x:,} employees<extra></extra>",
        )
    fig.update_layout(xaxis_title="Employees affected", yaxis_title="",
                      showlegend=False)
    fig = _year_toggle(
        _apply_theme(fig, margin=dict(l=220, r=30, t=40, b=60)), str(year)
    )
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


def _write_pages(df: pd.DataFrame, out_dir: Path) -> int:
    """Chunk the full dataset into static page files the table fetches lazily.

    ~250 rows per page (~30 KB each) keeps the main page light while making
    every record browsable without ever loading the whole dataset at once.
    """
    recent = _recent_sorted(df)
    total_pages = max(1, -(-len(recent) // PAGE_SIZE))
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for i in range(total_pages):
        chunk = recent.iloc[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        payload = {
            "page": i + 1,
            "total_pages": total_pages,
            "rows": [_row_values(r) for _, r in chunk.iterrows()],
        }
        (pages_dir / f"{i + 1}.json").write_text(json.dumps(payload))
    return total_pages


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
:root {{ --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
         --muted:#8b949e; --accent:#58a6ff; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text);
        font:15px/1.5 Inter,system-ui,sans-serif; }}
a {{ color:var(--accent); text-decoration:none; }}
header {{ position:sticky; top:0; z-index:10; background:rgba(13,17,23,.95);
          border-bottom:1px solid var(--border); padding:14px 24px;
          display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }}
header h1 {{ font-size:20px; }}
header .sub {{ color:var(--muted); font-size:13px; }}
header .right {{ margin-left:auto; font-size:13px; color:var(--muted); }}
main {{ max-width:1200px; margin:0 auto; padding:24px; }}
.badge {{ display:inline-block; background:#1f6feb33; color:var(--accent);
          border:1px solid #1f6feb66; border-radius:12px; padding:1px 10px;
          font-size:12px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
         gap:14px; margin:18px 0 26px; }}
.kpi {{ background:var(--card); border:1px solid var(--border);
        border-radius:10px; padding:14px 16px; }}
.kpi .label {{ font-size:11px; letter-spacing:.06em; text-transform:uppercase;
               color:var(--muted); }}
.kpi .value {{ font-size:26px; font-weight:700; margin-top:4px; }}
.kpi .note {{ font-size:12px; color:var(--muted); }}
section {{ background:var(--card); border:1px solid var(--border);
           border-radius:12px; padding:18px; margin-bottom:24px; }}
section h2 {{ font-size:15px; margin-bottom:4px; }}
section .desc {{ color:var(--muted); font-size:13px; margin-bottom:10px; }}
.chart {{ width:100%; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
th,td {{ text-align:left; padding:7px 10px;
         border-bottom:1px solid var(--border); }}
th {{ color:var(--muted); font-weight:600; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.st {{ color:var(--accent); font-weight:600; }}
select {{ background:var(--card); color:var(--text);
          border:1px solid var(--border); border-radius:6px; padding:5px 8px; }}
select option:disabled {{ color:#555c66; }}
.pgbtn {{ background:var(--card); color:var(--accent);
          border:1px solid var(--border); border-radius:6px;
          padding:5px 12px; cursor:pointer; }}
.pgbtn:hover {{ border-color:var(--accent); }}
footer {{ color:var(--muted); font-size:12.5px; text-align:center;
          padding:26px; }}
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
      <div class="note">across {states_live} states</div></div>
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
    <div class="desc">Pick a metric and year from the dropdown; hover a state
      for details. States light up as their sources come online.</div>
    <div class="chart">{map_div}</div>
  </section>

  <section>
    <h2>National monthly trend</h2>
    <div class="desc">Employees affected per month across all live states,
      last 24 months.</div>
    <div class="chart">{monthly_div}</div>
  </section>

  <section>
    <h2>States ranked</h2>
    <div class="desc">Employees affected per state — current year by
      default; switch to all time with the dropdown. Coverage depth varies
      by state.</div>
    <div class="chart">{states_div}</div>
  </section>

  <section>
    <h2>Top employers nationally</h2>
    <div class="desc">Employees affected by employer — current year by
      default; all time via the dropdown.</div>
    <div class="chart">{companies_div}</div>
  </section>

  <section>
    <h2>All notices</h2>
    <div class="desc">Every record, newest first, {page_size} per page —
      pages load on demand so the site stays fast. Bulk access:
      <a href="data.json">data.json</a>.
      Filter: <select id="stfilter"><option value="">All states</option>
      {state_options}
      {unavailable_options}</select>
      <input id="cofilter" type="search" placeholder="Search company…"
        style="background:var(--card);color:var(--text);
               border:1px solid var(--border);border-radius:6px;
               padding:5px 8px;margin-left:8px">
      <span id="filternote" style="color:var(--muted);font-size:12px"></span>
    </div>
    <div style="overflow-x:auto"><table id="recent">
      <thead><tr><th>State</th><th>Company</th><th>Location</th>
        <th>Notice</th><th>Effective</th><th>Employees</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table></div>
    <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
      <button id="prevpg" class="pgbtn">← Prev</button>
      <span id="pginfo">Page 1 of {total_pages}</span>
      <button id="nextpg" class="pgbtn">Next →</button>
      <span style="color:var(--muted);font-size:13px">Jump to:</span>
      <input id="pgjump" type="number" min="1" max="{total_pages}" value="1"
        style="width:70px;background:var(--card);color:var(--text);
               border:1px solid var(--border);border-radius:6px;
               padding:5px 8px">
    </div>
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
var TOTAL_PAGES = {total_pages};
var currentPage = 1;

function applyFilters() {{
  var st = document.getElementById('stfilter').value;
  var q = document.getElementById('cofilter').value.toLowerCase();
  var shown = 0;
  document.querySelectorAll('#recent tbody tr').forEach(function (tr) {{
    var stOk = !st || tr.dataset.state === st;
    var qOk = !q || tr.cells[1].textContent.toLowerCase().indexOf(q) !== -1;
    var on = stOk && qOk;
    tr.style.display = on ? '' : 'none';
    if (on) shown++;
  }});
  document.getElementById('filternote').textContent =
    (st || q) ? shown + ' match(es) on this page — filters apply per page' : '';
}}

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
  applyFilters();
}}

function gotoPage(n) {{
  n = Math.max(1, Math.min(TOTAL_PAGES, n));
  fetch('pages/' + n + '.json')
    .then(function (r) {{ return r.json(); }})
    .then(function (p) {{
      currentPage = p.page;
      renderRows(p.rows);
      document.getElementById('pginfo').textContent =
        'Page ' + p.page + ' of ' + p.total_pages;
      document.getElementById('pgjump').value = p.page;
    }});
}}

document.getElementById('prevpg').addEventListener('click', function () {{
  gotoPage(currentPage - 1);
}});
document.getElementById('nextpg').addEventListener('click', function () {{
  gotoPage(currentPage + 1);
}});
document.getElementById('pgjump').addEventListener('change', function () {{
  gotoPage(parseInt(this.value, 10) || 1);
}});
document.getElementById('stfilter').addEventListener('change', applyFilters);
document.getElementById('cofilter').addEventListener('input', applyFilters);
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
    chart_us_top_states(df)
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

    total_pages = _write_pages(df, out_dir)
    updated = str(kpis["last_updated"])[:10]
    n_live = kpis["states_live"]
    live_badge = (
        f"{n_live - 1} states + DC live"
        if "DC" in payload.get("states", {})
        else f"{n_live} states live"
    )
    html = US_TEMPLATE.format(
        states_live=kpis["states_live"],
        live_badge=live_badge,
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
        states_div=_chart_div("us_top_states"),
        companies_div=_chart_div("us_top_companies"),
        state_options=state_options,
        unavailable_options=unavailable_options,
        recent_rows=_recent_rows(df),
        page_size=PAGE_SIZE,
        total_pages=total_pages,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(html)
    (out_dir / "data.json").write_text(json.dumps(payload, default=str))
    log.info(
        f"US dashboard built: {index} "
        f"({kpis['states_live']} states, {kpis['total_notices']} records)"
    )
    return index


if __name__ == "__main__":
    build_us_site()
