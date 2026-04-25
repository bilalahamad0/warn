"""
warn_publish.py
---------------
Full pipeline runner:
  1. warn_monitor  — download + parse + detect changes
  2. warn_diff     — generate diff report
  3. warn_charts   — generate 6 Plotly charts
  4. build_site    — assemble output/index.html
  5. git_push      — commit + push to GitHub (requires GITHUB_TOKEN env var)

Usage:
    python3 warn_publish.py               # full run
    python3 warn_publish.py --no-push     # build only, skip git push
    python3 warn_publish.py --force       # force re-download even if unchanged
"""

import json
import logging
import argparse
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import warn_monitor
import warn_diff
import warn_charts
import warn_notify
import warn_history

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "docs"
CHARTS_DIR = OUTPUT_DIR / "charts"
DATA_DIR = BASE_DIR / "data"
SITE_DATA = OUTPUT_DIR / "data.json"
INDEX_HTML = OUTPUT_DIR / "index.html"
TEMPLATE = BASE_DIR / "docs" / "index_template.html"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("warn_publish")


# ---------------------------------------------------------------------------
# Site builder
# ---------------------------------------------------------------------------


def _read_chart_div(chart_id: str) -> str:
    path = CHARTS_DIR / f"{chart_id}.html"
    if path.exists():
        return path.read_text()
    return f'<div class="chart-error">Chart {chart_id} not available</div>'


def _format_number(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _compute_kpis() -> dict:
    """Compute extra KPI metrics from warn_latest.json."""
    defaults = {
        "avg_lead_days": "N/A",
        "largest_company": "N/A",
        "largest_employees": "N/A",
        "top_county": "N/A",
        "top_county_employees": "N/A",
    }
    latest = DATA_DIR / "warn_latest.json"
    if not latest.exists():
        return defaults

    payload = json.loads(latest.read_text())
    records = payload.get("records", [])
    if not records:
        return defaults

    # Average notice lead time
    lead_times = []
    for r in records:
        nd = str(r.get("notice_date") or "")[:10]
        ed = str(r.get("effective_date") or "")[:10]
        if len(nd) == 10 and len(ed) == 10:
            try:
                from datetime import date as _date
                n = datetime.strptime(nd, "%Y-%m-%d").date()
                e = datetime.strptime(ed, "%Y-%m-%d").date()
                diff = (e - n).days
                if 0 < diff < 730:
                    lead_times.append(diff)
            except ValueError:
                pass
    avg_lead = f"{round(sum(lead_times) / len(lead_times))}d" if lead_times else "N/A"

    # Largest single layoff
    largest = max(records, key=lambda r: r.get("employees", 0), default={})

    # Top county by employees
    county_totals: dict = {}
    for r in records:
        county = str(r.get("county") or "").strip()
        if county:
            county_totals[county] = county_totals.get(county, 0) + (r.get("employees") or 0)
    top_county = max(county_totals, key=lambda k: county_totals[k]) if county_totals else "N/A"

    return {
        "avg_lead_days": avg_lead,
        "largest_company": largest.get("company", "N/A"),
        "largest_employees": _format_number(largest.get("employees", 0)),
        "top_county": top_county,
        "top_county_employees": _format_number(county_totals.get(top_county, 0)),
    }


def _build_recent_table() -> str:
    """Build HTML table of the 50 most recent WARN notices."""
    latest = DATA_DIR / "warn_latest.json"
    if not latest.exists():
        return "<p style='color:var(--muted)'>No data available.</p>"

    payload = json.loads(latest.read_text())
    records = payload.get("records", [])
    sorted_recs = sorted(
        records,
        key=lambda r: str(r.get("notice_date") or ""),
        reverse=True,
    )[:50]

    rows = ""
    for r in sorted_recs:
        company = str(r.get("company") or "").replace("<", "&lt;").replace(">", "&gt;")
        county = str(r.get("county") or "").replace(" County", "").replace(" Parish", "")
        employees = _format_number(r.get("employees", 0))
        notice = str(r.get("notice_date") or "")[:10]
        effective = str(r.get("effective_date") or "")[:10]
        layoff_type = str(r.get("layoff_type") or "")
        industry = str(r.get("industry") or "")
        rows += (
            f"<tr>"
            f"<td>{company}</td>"
            f"<td>{county}</td>"
            f"<td class='num'>{employees}</td>"
            f"<td>{notice}</td>"
            f"<td>{effective}</td>"
            f"<td>{layoff_type}</td>"
            f"<td>{industry}</td>"
            f"</tr>\n"
        )

    return f"""<table id="notices-table" class="notices-table">
      <thead><tr>
        <th>Company</th>
        <th>County</th>
        <th class="num">Employees</th>
        <th>Notice Date</th>
        <th>Effective Date</th>
        <th>Type</th>
        <th>Industry</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_chart_tabs_panes(chart_ids: list, chart_divs: dict, meta_by_id: dict) -> tuple:
    """Return (tabs_html, panes_html) for a given list of chart IDs."""
    tabs = ""
    panes = ""
    for i, cid in enumerate(chart_ids):
        cm = meta_by_id.get(cid, {"id": cid, "title": cid, "desc": ""})
        active = "active" if i == 0 else ""
        tabs += (
            f'<button class="chart-tab {active}" data-target="pane-{cid}">'
            f'{cm["title"]}</button>\n'
        )
        panes += (
            f'<div class="chart-pane {active}" id="pane-{cid}">'
            f'<p class="chart-desc">{cm["desc"]}</p>'
            f'<div class="chart-container">{chart_divs[cid]}</div>'
            f'</div>\n'
        )
    return tabs, panes


def build_site(manifest: dict, monitor_result: dict) -> str:
    """Build the full index.html by embedding Plotly divs."""
    log.info("Building index.html …")

    meta_by_id = {cm["id"]: cm for cm in warn_charts.CHART_META}
    chart_divs = {cm["id"]: _read_chart_div(cm["id"]) for cm in warn_charts.CHART_META}

    diff = monitor_result.get("diff", {})
    new_count = diff.get("new_count", 0)
    new_employees = diff.get("total_employees_new", 0)
    total_records = _format_number(manifest.get("total_records", 0))
    total_employees = _format_number(manifest.get("total_employees", 0))
    last_updated = manifest.get("last_updated", "")[:10]
    date_start = str(manifest.get("date_range_start", ""))[:10]
    date_end = str(manifest.get("date_range_end", ""))[:10]

    kpis = _compute_kpis()

    new_banner = ""
    if new_count > 0:
        new_banner = (
            f'<div class="new-banner">'
            f'<span class="badge-new">NEW</span>'
            f'<strong>{new_count} new WARN notice{"s" if new_count > 1 else ""}</strong>'
            f' affecting <strong>{_format_number(new_employees)} employees</strong>'
            f" since last check.</div>"
        )

    # Section: Impact
    impact_tabs, impact_panes = _build_chart_tabs_panes(
        ["9_industry_breakdown", "4_top_companies", "11_county_bar"],
        chart_divs, meta_by_id,
    )
    # Section: Trends
    trend_tabs, trend_panes = _build_chart_tabs_panes(
        ["1_timeline_scatter", "2_monthly_bar", "3_rolling_trend", "7_yoy_bar", "8_multiyear_trend"],
        chart_divs, meta_by_id,
    )
    # Section: Details
    detail_tabs, detail_panes = _build_chart_tabs_panes(
        ["10_lead_time", "5_county_heatmap", "6_treemap"],
        chart_divs, meta_by_id,
    )

    recent_table = _build_recent_table()

    html = SITE_HTML_TEMPLATE.format(
        total_records=total_records,
        total_employees=total_employees,
        last_updated=last_updated,
        date_start=date_start,
        date_end=date_end,
        new_banner=new_banner,
        avg_lead_days=kpis["avg_lead_days"],
        largest_company=kpis["largest_company"],
        largest_employees=kpis["largest_employees"],
        top_county=kpis["top_county"],
        top_county_employees=kpis["top_county_employees"],
        impact_tabs=impact_tabs,
        impact_panes=impact_panes,
        trend_tabs=trend_tabs,
        trend_panes=trend_panes,
        detail_tabs=detail_tabs,
        detail_panes=detail_panes,
        recent_table=recent_table,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    INDEX_HTML.write_text(html, encoding="utf-8")

    if (DATA_DIR / "warn_latest.json").exists():
        shutil.copy(DATA_DIR / "warn_latest.json", SITE_DATA)

    log.info(f"Site built → {INDEX_HTML}")
    return str(INDEX_HTML)


# ---------------------------------------------------------------------------
# Git push
# ---------------------------------------------------------------------------


def git_commit_push(message: str = None) -> bool:
    """Stage changed files, commit, and push."""
    token = os.getenv("GH_REPO_TOKEN")
    if not token:
        # Try .env
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GH_REPO_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("\"'")

    msg = (
        message
        or f"auto: WARN update {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

    def run_git(args):
        result = subprocess.run(
            ["git"] + args,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning(f"git {' '.join(args)} stderr: {result.stderr.strip()}")
        return result.returncode == 0

    log.info("Staging changes …")
    run_git(
        [
            "add",
            "data/",
            "docs/",
            "file.xlsx",
            "requirements.txt",
            "warn_monitor.py",
            "warn_charts.py",
            "warn_diff.py",
            "warn_publish.py",
            "README.md",
        ]
    )

    # Check if there's anything to commit
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    if not status_result.stdout.strip():
        log.info("Nothing to commit — working tree clean.")
        return True

    log.info(f"Committing: {msg}")
    ok = run_git(["commit", "-m", msg])
    if not ok:
        log.error("git commit failed.")
        return False

    log.info("Pushing to origin/main …")
    # Inject token if available
    if token:
        remote_url_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
        )
        original_url = remote_url_result.stdout.strip()
        if "github.com" in original_url and "https://" in original_url:
            auth_url = original_url.replace("https://", f"https://{token}@")
            subprocess.run(
                ["git", "remote", "set-url", "origin", auth_url],
                cwd=str(BASE_DIR),
                capture_output=True,
            )

    push_ok = run_git(["push", "origin", "main"])

    # Restore original URL if we modified it
    if token and "github.com" in original_url:
        subprocess.run(
            ["git", "remote", "set-url", "origin", original_url],
            cwd=str(BASE_DIR),
            capture_output=True,
        )

    if push_ok:
        log.info("✓ Pushed successfully.")
    else:
        log.error("✗ Push failed — check GITHUB_TOKEN and repo permissions.")
    return push_ok


# ---------------------------------------------------------------------------
# HTML Template (inline to keep single-file deployment)
# ---------------------------------------------------------------------------

SITE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>California WARN Layoff Monitor</title>
  <meta name="description" content="Live monitoring and analysis of California WARN layoff notices from the Employment Development Department." />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    :root {{
      --bg: #0d1117;
      --card: #161b22;
      --border: #21262d;
      --accent: #58a6ff;
      --accent2: #f78166;
      --accent3: #3fb950;
      --accent4: #d29922;
      --accent5: #bc8cff;
      --accent6: #39d0d8;
      --text: #e6edf3;
      --muted: #8b949e;
      --glass: rgba(22,27,34,0.7);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', system-ui, sans-serif;
      min-height: 100vh;
      overflow-x: hidden;
    }}
    body::before {{
      content: '';
      position: fixed; inset: 0; z-index: -1;
      background:
        radial-gradient(ellipse 80% 50% at 20% 0%, rgba(88,166,255,0.08) 0%, transparent 60%),
        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(247,129,102,0.06) 0%, transparent 60%),
        var(--bg);
    }}

    /* ── Header ── */
    header {{
      padding: 1.25rem 2rem;
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(8px);
      background: var(--glass);
      position: sticky; top: 0; z-index: 100;
    }}
    .header-inner {{
      max-width: 1400px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.75rem;
    }}
    .brand {{ display: flex; align-items: center; gap: 0.75rem; }}
    .brand-icon {{
      width: 38px; height: 38px; border-radius: 10px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      display: grid; place-items: center; font-size: 1.1rem; flex-shrink: 0;
    }}
    h1 {{ font-size: 1.3rem; font-weight: 700; }}
    .subtitle {{ font-size: 0.75rem; color: var(--muted); }}
    .header-right {{ display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap; }}
    .search-wrap {{ position: relative; }}
    .search-wrap input {{
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-family: inherit;
      font-size: 0.82rem;
      padding: 0.4rem 0.75rem 0.4rem 2rem;
      width: 220px;
      transition: border-color 0.2s;
      outline: none;
    }}
    .search-wrap input:focus {{ border-color: var(--accent); }}
    .search-wrap input::placeholder {{ color: var(--muted); }}
    .search-icon {{
      position: absolute; left: 0.55rem; top: 50%; transform: translateY(-50%);
      color: var(--muted); font-size: 0.85rem; pointer-events: none;
    }}
    .header-meta {{ font-size: 0.75rem; color: var(--muted); text-align: right; white-space: nowrap; }}
    .header-meta a {{ color: var(--accent); text-decoration: none; }}
    .header-meta a:hover {{ text-decoration: underline; }}

    /* ── Main ── */
    main {{ max-width: 1400px; margin: 0 auto; padding: 1.5rem 2rem; }}

    /* ── New banner ── */
    .new-banner {{
      background: linear-gradient(90deg, rgba(63,185,80,0.15), rgba(63,185,80,0.05));
      border: 1px solid rgba(63,185,80,0.3);
      border-radius: 10px; padding: 0.75rem 1.25rem;
      margin-bottom: 1.25rem;
      display: flex; align-items: center; gap: 0.75rem;
      animation: fadeIn 0.5s ease;
    }}
    .badge-new {{
      background: var(--accent3); color: #000;
      padding: 0.18rem 0.45rem; border-radius: 4px;
      font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em;
    }}

    /* ── KPI cards ── */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 1rem; margin-bottom: 1.75rem;
    }}
    .kpi-card {{
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.1rem 1.25rem;
      backdrop-filter: blur(8px);
      transition: transform 0.2s, border-color 0.2s;
      position: relative; overflow: hidden;
    }}
    .kpi-card::before {{
      content: '';
      position: absolute; top: 0; left: 0; right: 0; height: 2px;
    }}
    .kpi-card:nth-child(1)::before {{ background: var(--accent); }}
    .kpi-card:nth-child(2)::before {{ background: var(--accent2); }}
    .kpi-card:nth-child(3)::before {{ background: var(--accent3); }}
    .kpi-card:nth-child(4)::before {{ background: var(--accent4); }}
    .kpi-card:nth-child(5)::before {{ background: var(--accent5); }}
    .kpi-card:nth-child(6)::before {{ background: var(--accent6); }}
    .kpi-card:hover {{ transform: translateY(-2px); border-color: rgba(88,166,255,0.4); }}
    .kpi-label {{ font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.35rem; }}
    .kpi-value {{ font-size: 1.85rem; font-weight: 700; line-height: 1; }}
    .kpi-value.sm {{ font-size: 1rem; padding-top: 0.3rem; }}
    .kpi-sub {{ font-size: 0.72rem; color: var(--muted); margin-top: 0.3rem; }}

    /* ── Section cards ── */
    .section-card {{
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.4rem 1.5rem;
      backdrop-filter: blur(8px);
      margin-bottom: 1.5rem;
    }}
    .section-header {{
      display: flex; align-items: baseline; gap: 0.6rem;
      margin-bottom: 1.1rem;
    }}
    .section-header h2 {{
      font-size: 0.78rem; font-weight: 600;
      color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.1em;
    }}
    .section-tag {{
      font-size: 0.65rem; color: var(--muted);
      border: 1px solid var(--border); border-radius: 4px;
      padding: 0.1rem 0.4rem;
    }}

    /* ── Chart tabs ── */
    .chart-tabs {{
      display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 1.25rem;
      border-bottom: 1px solid var(--border); padding-bottom: 0.85rem;
    }}
    .chart-tab {{
      background: none; border: 1px solid var(--border);
      color: var(--muted); padding: 0.38rem 0.9rem;
      border-radius: 8px; cursor: pointer;
      font-size: 0.8rem; font-family: inherit;
      transition: all 0.18s;
    }}
    .chart-tab:hover {{ border-color: var(--accent); color: var(--text); }}
    .chart-tab.active {{
      background: rgba(88,166,255,0.12);
      border-color: var(--accent); color: var(--accent); font-weight: 500;
    }}
    .chart-pane {{ display: none; animation: fadeIn 0.25s ease; }}
    .chart-pane.active {{ display: block; }}
    .chart-desc {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 0.85rem; }}
    .chart-container {{ width: 100%; min-height: 480px; }}
    .chart-container .plotly-graph-div {{ width: 100% !important; }}
    .chart-error {{
      background: rgba(247,129,102,0.1); border: 1px solid rgba(247,129,102,0.3);
      border-radius: 8px; padding: 1rem; color: var(--accent2); font-size: 0.85rem;
    }}

    /* ── Notices table ── */
    .table-controls {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 0.85rem; gap: 0.75rem; flex-wrap: wrap;
    }}
    .table-count {{ font-size: 0.78rem; color: var(--muted); }}
    .notices-table {{
      width: 100%; border-collapse: collapse;
      font-size: 0.82rem;
    }}
    .notices-table th {{
      text-align: left; padding: 0.55rem 0.75rem;
      font-size: 0.7rem; font-weight: 600;
      color: var(--muted); text-transform: uppercase; letter-spacing: 0.07em;
      border-bottom: 1px solid var(--border);
      cursor: pointer; user-select: none; white-space: nowrap;
    }}
    .notices-table th:hover {{ color: var(--accent); }}
    .notices-table th .sort-arrow {{ margin-left: 0.25rem; opacity: 0.4; }}
    .notices-table th.sorted .sort-arrow {{ opacity: 1; color: var(--accent); }}
    .notices-table td {{
      padding: 0.5rem 0.75rem;
      border-bottom: 1px solid rgba(33,38,45,0.6);
      vertical-align: middle;
    }}
    .notices-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .notices-table tr:hover td {{ background: rgba(88,166,255,0.05); }}
    .notices-table tr.hidden {{ display: none; }}
    .table-wrap {{ overflow-x: auto; }}

    /* ── Footer ── */
    footer {{
      border-top: 1px solid var(--border);
      padding: 1.25rem 2rem; text-align: center;
      font-size: 0.75rem; color: var(--muted);
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}

    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(6px); }}
      to {{ opacity: 1; transform: none; }}
    }}

    @media (max-width: 640px) {{
      main {{ padding: 1rem; }}
      .kpi-value {{ font-size: 1.45rem; }}
      h1 {{ font-size: 1.1rem; }}
      .search-wrap input {{ width: 160px; }}
    }}
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="brand">
      <div class="brand-icon">📋</div>
      <div>
        <h1>California WARN Layoff Monitor</h1>
        <div class="subtitle">Employment Development Department · Real-time Tracking</div>
      </div>
    </div>
    <div class="header-right">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="search" id="global-search" placeholder="Search company or county…" autocomplete="off" />
      </div>
      <div class="header-meta">
        Updated: <strong>{last_updated}</strong><br/>
        <a href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn" target="_blank" rel="noopener">CA EDD WARN</a>
        &nbsp;·&nbsp;
        <a href="https://github.com/bilalahamad0/warn" target="_blank" rel="noopener">GitHub</a>
      </div>
    </div>
  </div>
</header>

<main>
  {new_banner}

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">WARN Notices</div>
      <div class="kpi-value">{total_records}</div>
      <div class="kpi-sub">Unique filings</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Employees Affected</div>
      <div class="kpi-value">{total_employees}</div>
      <div class="kpi-sub">Cumulative total</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Lead Time</div>
      <div class="kpi-value">{avg_lead_days}</div>
      <div class="kpi-sub">Notice → effective date</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Largest Layoff</div>
      <div class="kpi-value sm">{largest_company}</div>
      <div class="kpi-sub">{largest_employees} employees</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Top County</div>
      <div class="kpi-value sm">{top_county}</div>
      <div class="kpi-sub">{top_county_employees} employees</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Date Range</div>
      <div class="kpi-value sm">{date_start}</div>
      <div class="kpi-sub">through {date_end}</div>
    </div>
  </div>

  <!-- Section: IMPACT -->
  <div class="section-card">
    <div class="section-header">
      <h2>Impact</h2>
      <span class="section-tag">Who &amp; Where</span>
    </div>
    <div class="chart-tabs" data-section="impact">
      {impact_tabs}
    </div>
    {impact_panes}
  </div>

  <!-- Section: TRENDS -->
  <div class="section-card">
    <div class="section-header">
      <h2>Trends</h2>
      <span class="section-tag">Over Time</span>
    </div>
    <div class="chart-tabs" data-section="trends">
      {trend_tabs}
    </div>
    {trend_panes}
  </div>

  <!-- Section: DETAILS -->
  <div class="section-card">
    <div class="section-header">
      <h2>Details</h2>
      <span class="section-tag">Deep Dive</span>
    </div>
    <div class="chart-tabs" data-section="details">
      {detail_tabs}
    </div>
    {detail_panes}
  </div>

  <!-- Recent Notices Table -->
  <div class="section-card">
    <div class="section-header">
      <h2>Recent Notices</h2>
      <span class="section-tag">Last 50</span>
    </div>
    <div class="table-controls">
      <div class="table-count" id="table-count"></div>
    </div>
    <div class="table-wrap">
      {recent_table}
    </div>
  </div>
</main>

<footer>
  Built by <a href="https://bilalahamad.com" target="_blank">bilalahamad.com</a> ·
  Data: <a href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn" target="_blank">CA EDD</a> ·
  Generated {generated_at}
</footer>

<script>
(function () {{
  // ── Tab switching (scoped per section) ──
  document.querySelectorAll('.chart-tabs').forEach(tabGroup => {{
    tabGroup.querySelectorAll('.chart-tab').forEach(btn => {{
      btn.addEventListener('click', () => {{
        tabGroup.querySelectorAll('.chart-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const target = document.getElementById(btn.dataset.target);
        if (!target) return;
        // hide all panes that are siblings of the same parent section
        target.parentElement.querySelectorAll('.chart-pane').forEach(p => p.classList.remove('active'));
        target.classList.add('active');
        setTimeout(() => window.dispatchEvent(new Event('resize')), 50);
      }});
    }});
  }});

  // ── Table sort ──
  const table = document.getElementById('notices-table');
  if (table) {{
    let sortCol = -1, sortAsc = true;
    table.querySelectorAll('th').forEach((th, ci) => {{
      th.innerHTML += ' <span class="sort-arrow">▲</span>';
      th.addEventListener('click', () => {{
        const asc = sortCol === ci ? !sortAsc : true;
        sortCol = ci; sortAsc = asc;
        table.querySelectorAll('th').forEach(h => h.classList.remove('sorted'));
        th.classList.add('sorted');
        th.querySelector('.sort-arrow').textContent = asc ? '▲' : '▼';
        const tbody = table.querySelector('tbody');
        const rows = [...tbody.querySelectorAll('tr')];
        rows.sort((a, b) => {{
          const av = a.cells[ci]?.textContent.replace(/,/g,'') || '';
          const bv = b.cells[ci]?.textContent.replace(/,/g,'') || '';
          const an = parseFloat(av), bn = parseFloat(bv);
          const cmp = !isNaN(an) && !isNaN(bn) ? an - bn : av.localeCompare(bv);
          return asc ? cmp : -cmp;
        }});
        rows.forEach(r => tbody.appendChild(r));
      }});
    }});
  }}

  // ── Global search (filters table rows) ──
  const searchInput = document.getElementById('global-search');
  const countEl = document.getElementById('table-count');
  function updateCount() {{
    if (!table) return;
    const total = table.querySelectorAll('tbody tr').length;
    const visible = table.querySelectorAll('tbody tr:not(.hidden)').length;
    if (countEl) countEl.textContent = visible < total ? `${{visible}} of ${{total}} shown` : `${{total}} notices`;
  }}
  if (searchInput && table) {{
    searchInput.addEventListener('input', () => {{
      const q = searchInput.value.trim().toLowerCase();
      table.querySelectorAll('tbody tr').forEach(row => {{
        const text = row.textContent.toLowerCase();
        row.classList.toggle('hidden', q.length > 0 && !text.includes(q));
      }});
      updateCount();
    }});
    updateCount();
  }}
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(no_push: bool = False, force: bool = False, skip_history: bool = False):
    log.info("=" * 70)
    log.info(f"WARN Publisher — {datetime.now(timezone.utc).isoformat()}Z")
    log.info("=" * 70)

    # Step 1: Monitor
    log.info("Step 1/5: Running monitor …")
    monitor_result = warn_monitor.run(force=force)

    # Step 2: Diff report
    log.info("Step 2/5: Generating diff report …")
    try:
        warn_diff.generate_report()
    except Exception as e:
        log.warning(f"Diff report failed (non-fatal): {e}")

    # Step 3: Historical data (once per day, or on demand)
    if not skip_history:
        log.info("Step 3/5: Updating historical data …")
        try:
            warn_history.run(force=False)
        except Exception as e:
            log.warning(f"History update failed (non-fatal): {e}")
    else:
        log.info("Step 3/5: Skipping historical data (--skip-history).")

    # Step 4: Charts
    log.info("Step 4/5: Generating charts …")
    try:
        # chart_results = warn_charts.run(save_png=True)
        warn_charts.run(save_png=True)
        manifest = json.loads((DATA_DIR / "charts_manifest.json").read_text())
    except Exception as e:
        log.error(f"Chart generation failed: {e}")
        manifest = {
            "charts": [],
            "total_records": 0,
            "total_employees": 0,
            "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        }

    # Step 5: Build site
    log.info("Step 5/5: Building site …")
    build_site(manifest, monitor_result)

    # Notify on changes
    diff = monitor_result.get("diff", {})
    summary = monitor_result.get("summary", {})
    if diff.get("new_count", 0) > 0:
        try:
            warn_notify.notify_if_changes(diff, summary)
        except Exception as e:
            log.warning(f"Email notification failed (non-fatal): {e}")

    # Git push
    if not no_push:
        log.info("Git push …")
        git_commit_push()
    else:
        log.info("Skipping git push (--no-push).")

    log.info("✓ Publisher complete.")
    return monitor_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WARN Full Pipeline Publisher")
    parser.add_argument("--no-push", action="store_true", help="Skip git push")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    parser.add_argument(
        "--skip-history", action="store_true", help="Skip historical PDF update"
    )
    args = parser.parse_args()
    run(no_push=args.no_push, force=args.force, skip_history=args.skip_history)
