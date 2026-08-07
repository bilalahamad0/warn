"""
warn_publish.py
---------------
Full pipeline runner:
  1. warn_sources  — download + parse + detect changes, per state
  2. warn_diff     — generate diff report
  3. warn_history  — historical PDFs; then aggregate the national dataset
  4. warn_charts   — generate the Plotly chart fragments
  5. build sites   — warn_site_us → docs/ (the US dashboard, at the site root)
                     build_site   → docs/ca/ (the California dashboard)
                     Only the root build is fatal; see step 5 in run().
  6. git_push      — commit + push to GitHub (requires GH_REPO_TOKEN env var)

This module owns the California page. The site root belongs to warn_site_us.

Usage:
    python3 warn_publish.py               # full run
    python3 warn_publish.py --no-push     # build only, skip git push
    python3 warn_publish.py --force       # force re-download even if unchanged
    python3 warn_publish.py --digest      # force last month's US digest
    python3 warn_publish.py --no-digest   # skip the monthly digest step
"""

import json
import logging
import argparse
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import warn_monitor
import warn_diff
import warn_charts
import warn_datasets
import warn_notify
import warn_history
import warn_sources
import warn_site_us
import warn_urls
from warn_sources import aggregate as warn_aggregate

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "docs"
CHARTS_DIR = OUTPUT_DIR / "charts"
DATA_DIR = BASE_DIR / "data"

# California's own page. The site root belongs to the national dashboard
# (warn_site_us) — this module used to own it, back when California was the
# only jurisdiction the project covered. Chart fragments stay at the shared
# docs/charts/ because they are inlined at build time, never linked, so they
# carry no URL coupling to either page's depth.
CA_DIR = OUTPUT_DIR / "ca"
SITE_DATA = CA_DIR / "data.json"
INDEX_HTML = CA_DIR / "index.html"

# Landing page for the signed unsubscribe links carried by every subscriber
# email (warn_subscribers.unsubscribe_url). Rebuilt every run alongside the
# dashboards so the link in an alert always resolves to a live page.
UNSUBSCRIBE_HTML = OUTPUT_DIR / "unsubscribe.html"

# The dashboard reads the cumulative store (union of every notice ever
# observed) so notices dropped by a later EDD re-export stay visible. It
# falls back to the latest download if the cumulative store is absent.
CUMULATIVE_FILE = DATA_DIR / "warn_cumulative.json"
LATEST_FILE = DATA_DIR / "warn_latest.json"

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


# Month abbreviations shared with the client JS so the server-rendered summary
# timeline and the client recompute format dates identically.
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_human_date(iso: str) -> str:
    """Format an ISO ``YYYY-MM-DD`` string as e.g. ``Jul 4, 2026``."""
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
    except Exception:
        return str(iso)


def _dashboard_payload() -> dict:
    """The dataset that backs the California dashboard.

    Delegates to warn_datasets so this page and warn_charts read the identical
    records — they used to resolve the file independently, which is how the
    California dashboard came to under-report early 2025 while the US dashboard
    counted it correctly. Returns ``{}`` rather than raising when there is no
    dataset at all, since callers each have their own empty-state rendering.
    """
    try:
        return warn_datasets.load_ca_dashboard()
    except FileNotFoundError:
        return {}


def _dashboard_records() -> list:
    return _dashboard_payload().get("records", [])


def _strip_county(name) -> str:
    """Normalise a county label to match the notices table (drop the suffix)."""
    return str(name or "").replace(" County", "").replace(" Parish", "").strip()


def _compute_kpis(records: list = None, date_from: str = None, date_to: str = None) -> dict:
    """Compute the summary KPI metrics over an optional notice-date window.

    When ``date_from``/``date_to`` (ISO ``YYYY-MM-DD``) are supplied, only
    notices whose ``notice_date`` falls within the inclusive window are counted
    — this backs the dashboard's default "current calendar year" summary view.
    With no window every record is counted.

    ``records`` may be passed in to avoid re-reading the dataset; otherwise the
    cumulative dashboard store is loaded.
    """
    defaults = {
        "count": 0,
        "employees_total": 0,
        "avg_lead_days": "N/A",
        "largest_company": "N/A",
        "largest_employees": "N/A",
        "top_county": "N/A",
        "top_county_employees": "N/A",
    }
    if records is None:
        records = _dashboard_records()
        if not records:
            return defaults

    if date_from or date_to:
        lo = date_from or "0000-01-01"
        hi = date_to or "9999-12-31"
        records = [
            r for r in records
            if lo <= str(r.get("notice_date") or "")[:10] <= hi
        ]

    if not records:
        return defaults

    # Average notice lead time
    lead_times = []
    for r in records:
        nd = str(r.get("notice_date") or "")[:10]
        ed = str(r.get("effective_date") or "")[:10]
        if len(nd) == 10 and len(ed) == 10:
            try:
                n = datetime.strptime(nd, "%Y-%m-%d").date()
                e = datetime.strptime(ed, "%Y-%m-%d").date()
                diff = (e - n).days
                if 0 < diff < 730:
                    lead_times.append(diff)
            except ValueError:
                pass
    # Round half-up (int(x + 0.5)) to match the client's JS Math.round, so the
    # server-rendered value and the client recompute never disagree by a day.
    avg_lead = (
        f"{int(sum(lead_times) / len(lead_times) + 0.5)}d" if lead_times else "N/A"
    )

    # Largest single layoff. The tiebreak — most employees, then latest notice,
    # then company name — is order-independent so it matches the client, which
    # iterates the notices table rather than the raw JSON order.
    largest = max(
        records,
        key=lambda r: (
            r.get("employees", 0) or 0,
            str(r.get("notice_date") or "")[:10],
            str(r.get("company") or ""),
        ),
        default={},
    )

    # Top county by employees (suffix stripped so it matches the notices table);
    # ties broken by county name so the server and client agree.
    county_totals: dict = {}
    for r in records:
        county = _strip_county(r.get("county"))
        if county:
            county_totals[county] = county_totals.get(county, 0) + (r.get("employees") or 0)
    top_county = (
        max(county_totals, key=lambda k: (county_totals[k], k))
        if county_totals else "N/A"
    )

    return {
        "count": len(records),
        "employees_total": sum(r.get("employees") or 0 for r in records),
        "avg_lead_days": avg_lead,
        "largest_company": largest.get("company", "N/A") or "N/A",
        "largest_employees": _format_number(largest.get("employees", 0) or 0),
        "top_county": top_county,
        "top_county_employees": _format_number(county_totals.get(top_county, 0)),
    }


def _build_recent_table(new_keys: list = None) -> tuple:
    """Build the full notices table HTML and the filter-controls HTML.

    Returns (controls_html, table_html, bottom_controls_html, total_count).
    Loads ALL records, sorted newest-first by notice date.
    Pagination + per-column filters are wired client-side.
    """
    if new_keys is None:
        new_keys = []

    records = _dashboard_records()
    if not records:
        return ("", "<p style='color:var(--muted)'>No data available.</p>", "", 0)
    
    def get_key(r):
        return f"{r.get('company','')}__{r.get('effective_date','')}__{r.get('employees','')}"
        
    for r in records:
        r["_is_new"] = get_key(r) in new_keys
        
    sorted_recs = sorted(
        records,
        key=lambda r: (r.get("_is_new", False), str(r.get("notice_date") or "")),
        reverse=True,
    )

    counties = sorted({
        str(r.get("county") or "").replace(" County", "").replace(" Parish", "").strip()
        for r in records
        if r.get("county")
    })
    industries = sorted({str(r.get("industry") or "").strip() for r in records if r.get("industry")})
    types = sorted({str(r.get("layoff_type") or "").strip() for r in records if r.get("layoff_type")})

    # Default date window: from the earliest notice in the dataset through today.
    notice_dates = [str(r.get("notice_date") or "")[:10] for r in records if r.get("notice_date")]
    date_from_default = min(notice_dates) if notice_dates else ""
    date_to_default = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _opt(values):
        return "".join(f'<option value="{v}">{v}</option>' for v in values if v)

    def _pager_bar(include_size: bool) -> str:
        size_html = (
            """
      <div class="pager-left">
        <label class="pager-label">Show
          <select class="js-page-size">
            <option value="50" selected>50</option>
            <option value="100">100</option>
            <option value="150">150</option>
            <option value="200">200</option>
            <option value="0">All</option>
          </select>
          per page
        </label>
      </div>"""
            if include_size
            else ""
        )
        return f"""
    <div class="table-controls">{size_html}
      <div class="table-count js-table-count"></div>
      <div class="pager-right">
        <button type="button" class="pager-btn js-page-prev">‹ Prev</button>
        <span class="pager-info js-page-info">Page 1</span>
        <button type="button" class="pager-btn js-page-next">Next ›</button>
      </div>
    </div>"""

    controls_html = f"""
    <div class="filter-row">
      <input type="search" id="filter-company" class="filter-input" placeholder="Company, e.g. meta, linkedin" autocomplete="off"/>
      <select id="filter-county" class="filter-input">
        <option value="">All counties</option>{_opt(counties)}
      </select>
      <select id="filter-industry" class="filter-input">
        <option value="">All industries</option>{_opt(industries)}
      </select>
      <select id="filter-type" class="filter-input">
        <option value="">All types</option>{_opt(types)}
      </select>
      <input type="date" id="filter-date-from" class="filter-input" title="Notice date from" value="{date_from_default}" data-default="{date_from_default}" />
      <input type="date" id="filter-date-to" class="filter-input" title="Notice date to" value="{date_to_default}" data-default="{date_to_default}" />
      <button type="button" id="filter-reset" class="filter-reset">Reset</button>
    </div>{_pager_bar(include_size=False)}"""

    bottom_controls_html = _pager_bar(include_size=True)

    rows = []
    for r in sorted_recs:
        company = str(r.get("company") or "").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        county = str(r.get("county") or "").replace(" County", "").replace(" Parish", "").strip().replace('"', "&quot;")
        employees = r.get("employees", 0)
        emp_str = _format_number(employees)
        notice = str(r.get("notice_date") or "")[:10]
        effective = str(r.get("effective_date") or "")[:10]
        layoff_type = str(r.get("layoff_type") or "").replace('"', "&quot;")
        industry = str(r.get("industry") or "").replace('"', "&quot;")
        
        company_display = company
        if r.get("_is_new"):
            company_display = f'<span class="badge-new" style="margin-right:6px">NEW</span>{company}'

        rows.append(
            f'<tr data-company="{company.lower()}" data-company-name="{company}" '
            f'data-county="{county}" '
            f'data-industry="{industry}" data-type="{layoff_type}" '
            f'data-notice="{notice}" data-effective="{effective}" data-employees="{employees}">'
            f"<td>{company_display}</td>"
            f"<td>{county}</td>"
            f"<td class='num'>{emp_str}</td>"
            f"<td>{notice}</td>"
            f"<td>{effective}</td>"
            f"<td>{layoff_type}</td>"
            f"<td>{industry}</td>"
            f"</tr>"
        )

    table_html = (
        '<table id="notices-table" class="notices-table">'
        '<thead><tr>'
        '<th data-key="company">Company</th>'
        '<th data-key="county">County</th>'
        '<th class="num" data-key="employees">Employees</th>'
        '<th data-key="notice">Notice Date</th>'
        '<th data-key="effective">Effective Date</th>'
        '<th data-key="type">Type</th>'
        '<th data-key="industry">Industry</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )
    return (controls_html, table_html, bottom_controls_html, len(sorted_recs))


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


def _build_coverage_note(payload: dict, records: list) -> str:
    """The banner stating what span of California filings this page covers.

    Dates come from the records themselves, so the note cannot go stale. When
    the payload carries no ``coverage_start`` the derivation fell back to the
    raw EDD store (see warn_datasets.load_ca_dashboard) and the page is
    under-reporting early 2025 — say so rather than showing a confident total
    that quietly disagrees with the US dashboard.
    """
    if not records:
        return ""

    dates = sorted(str(r.get("notice_date") or "")[:10]
                   for r in records if r.get("notice_date"))
    if not dates:
        return ""
    span = f"{_fmt_human_date(dates[0])} – {_fmt_human_date(dates[-1])}"
    count = _format_number(len(records))

    if payload.get("coverage_start"):
        return (
            '<div class="coverage-note">'
            '<span aria-hidden="true">📌</span>'
            f'<span>Covers <strong>{count} California notices</strong> filed '
            f'{span} — the span for which every field on this page '
            '(industry, county, layoff type) is complete. Earlier California '
            'filings, back to 2008, are counted on the '
            '<a href="../">US dashboard</a>.</span>'
            '</div>'
        )
    return (
        '<div class="coverage-note degraded">'
        '<span aria-hidden="true">⚠️</span>'
        f'<span>Showing <strong>{count} California notices</strong> filed '
        f'{span} from the live EDD feed only. The national dataset was '
        'unavailable at build time, so notices filed before the feed began '
        'are missing and these totals will read lower than the '
        '<a href="../">US dashboard</a>\'s for California.</span>'
        '</div>'
    )


def build_site(manifest: dict, monitor_result: dict,
               out_dir: Path = None) -> str:
    """Build the California dashboard by embedding Plotly divs.

    ``out_dir`` defaults to ``docs/ca/``. It stays a keyword with a default so
    the pipeline's ``build_site(manifest, monitor_result)`` call is unchanged;
    tests pass a tmp dir to check the page's ``../`` asset paths resolve.
    """
    out_dir = Path(out_dir) if out_dir is not None else CA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Building {out_dir.name}/index.html …")

    meta_by_id = {cm["id"]: cm for cm in warn_charts.CHART_META}
    chart_divs = {cm["id"]: _read_chart_div(cm["id"]) for cm in warn_charts.CHART_META}

    diff = monitor_result.get("diff", {})
    new_count = diff.get("new_count", 0)
    new_employees = diff.get("total_employees_new", 0)

    # Headline totals/date-range come from the same derived California dataset
    # the table and the charts read (warn_datasets), so every number on the page
    # describes one record set — and the same one the US dashboard counts for
    # California.
    dash = _dashboard_payload()
    records = dash.get("records", [])
    last_updated = (dash.get("last_updated") or manifest.get("last_updated", ""))[:10]

    # Default summary view: the current calendar year, Jan 1 → today. The KPI
    # cards render these values server-side (so the page is correct without JS),
    # and a client-side "Date Range" selector recomputes them for All time /
    # prior years. See the KPI selector block in the page script.
    today = datetime.now(timezone.utc).date()
    cur_year = today.year
    year_start = f"{cur_year}-01-01"
    year_end = today.strftime("%Y-%m-%d")

    kpis = _compute_kpis(records, year_start, year_end)
    total_records = _format_number(kpis["count"])
    total_employees = _format_number(kpis["employees_total"])

    # Date-range <select> options: current year (YTD) first, then All time,
    # then every prior year present in the data, newest first.
    data_years = sorted({
        str(r.get("notice_date") or "")[:4]
        for r in records
        if str(r.get("notice_date") or "")[:4].isdigit()
    })
    range_opts = [
        f'<option value="ytd" selected>{cur_year} (Year to date)</option>',
        '<option value="all">All time</option>',
    ]
    range_opts += [
        f'<option value="{y}">{y}</option>'
        for y in sorted((y for y in data_years if y != str(cur_year)), reverse=True)
    ]
    kpi_range_options = "\n        ".join(range_opts)
    kpi_range_span = f"{year_start} → {year_end}"

    # Plain-language timeline for the summary banner (default: current year).
    summary_scope = "current year to date"
    summary_dates = f"{_fmt_human_date(year_start)} – {_fmt_human_date(year_end)}"

    new_banner = ""
    if new_count > 0:
        new_banner = (
            f'<div class="new-banner">'
            f'<span class="badge-new">NEW</span>'
            f'<strong>{new_count} new WARN notice{"s" if new_count > 1 else ""}</strong>'
            f' affecting <strong>{_format_number(new_employees)} employees</strong>'
            f" since last check.</div>"
        )

    coverage_note = _build_coverage_note(dash, records)

    # Section: Impact — California only. The national choropleth used to lead
    # this section, which made the first thing a visitor saw on the California
    # page a 47-state map indistinguishable from the US dashboard's. The map
    # now lives at the site root, where it is the point rather than a detour.
    impact_tabs, impact_panes = _build_chart_tabs_panes(
        ["9_industry_breakdown", "4_top_companies", "11_county_bar"],
        chart_divs, meta_by_id,
    )
    # Section: Trends
    trend_tabs, trend_panes = _build_chart_tabs_panes(
        ["1_timeline_scatter", "2_monthly_bar", "3_rolling_trend", "7_yoy_bar", "8_multiyear_trend"],
        chart_divs, meta_by_id,
    )
    # Section: Details (Treemap first, then Lead Time, then County Heatmap)
    detail_tabs, detail_panes = _build_chart_tabs_panes(
        ["6_treemap", "10_lead_time", "5_county_heatmap"],
        chart_divs, meta_by_id,
    )

    recent_controls, recent_table, recent_bottom_controls, recent_total = _build_recent_table(diff.get("new_keys", []))

    signup_endpoint = os.getenv("SIGNUP_ENDPOINT", "").strip()

    html = SITE_HTML_TEMPLATE.format(
        signup_endpoint=signup_endpoint,
        total_records=total_records,
        total_employees=total_employees,
        last_updated=last_updated,
        kpi_range_options=kpi_range_options,
        kpi_range_span=kpi_range_span,
        summary_scope=summary_scope,
        summary_dates=summary_dates,
        coverage_note=coverage_note,
        meta_description=(
            "Live California WARN layoff notices from the Employment "
            "Development Department, with charts, filters and a free JSON API."
        ),
        ca_url=warn_urls.CA_DASHBOARD_URL,
        og_image=warn_urls.OG_IMAGE_URL,
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
        recent_controls=recent_controls,
        recent_table=recent_table,
        recent_table_controls=recent_bottom_controls,
        recent_total=recent_total,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    index_html = out_dir / "index.html"
    index_html.write_text(html, encoding="utf-8")

    # Publish the derived California dataset as this page's API. Written from
    # the in-memory payload rather than copied from data/, because the records
    # the page shows are derived (national CA slice, coverage-window trimmed,
    # schema-normalised) and no file on disk holds exactly them.
    if dash:
        (out_dir / "data.json").write_text(
            json.dumps(dash, indent=2, default=str), encoding="utf-8"
        )

    log.info(f"Site built → {INDEX_HTML}")
    return str(index_html)


def build_unsubscribe_page() -> None:
    """Write docs/unsubscribe.html via warn_unsubscribe.

    A seam (like ``_build_digest_payload``): warn_unsubscribe is imported at
    call time so this module still imports on a checkout that predates it, and
    tests can patch the whole step. The caller wraps this in try/except —
    losing the page must never fail a run that produced good data, though it
    does mean the links already mailed out land on a stale page until the next
    successful build.
    """
    import warn_unsubscribe

    warn_unsubscribe.build_unsubscribe_page()


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
    add_paths = [
        "add",
        "data/",
        "docs/",
        "file.xlsx",
        "requirements.txt",
        "warn_monitor.py",
        "warn_charts.py",
        "warn_diff.py",
        "warn_publish.py",
        "warn_site_us.py",
        "warn_sources/",
        "README.md",
    ]
    # Stage the unsubscribe page by name as well as via docs/, so the page the
    # mailed-out links point at can never be left behind. Only when it exists:
    # `git add` fails the *whole* invocation on a pathspec that matches
    # nothing, which would strand every other change above.
    if UNSUBSCRIBE_HTML.exists():
        add_paths.append("docs/unsubscribe.html")
    run_git(add_paths)

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
        log.error("✗ Push failed — check GH_REPO_TOKEN and repo permissions.")
    return push_ok


# ---------------------------------------------------------------------------
# HTML Template (inline to keep single-file deployment)
# ---------------------------------------------------------------------------

SITE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>California WARN Layoff Tracker — live EDD notices, charts and API</title>
  <meta name="description" content="{meta_description}" />
  <link rel="canonical" href="{ca_url}" />
  <!-- Icons live at the site root and are shared with the US dashboard: one
       byte-identical set, one cache entry, nothing to keep in sync. The
       manifest is the deliberate exception — start_url and scope resolve
       against the *manifest* URL, so a shared one would make an installed
       California PWA open the US dashboard. -->
  <link rel="apple-touch-icon" sizes="180x180" href="../apple-touch-icon.png" />
  <link rel="icon" type="image/png" sizes="32x32" href="../favicon-32x32.png" />
  <link rel="icon" type="image/png" sizes="16x16" href="../favicon-16x16.png" />
  <link rel="icon" href="../favicon.ico" sizes="any" />
  <link rel="icon" type="image/svg+xml" href="../icon.svg" />
  <link rel="manifest" href="site.webmanifest" />
  <meta name="theme-color" content="#0d1117" />
  <meta name="apple-mobile-web-app-title" content="CA Layoffs" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="WARN Layoff Tracker" />
  <meta property="og:title" content="California WARN Layoff Tracker" />
  <meta property="og:description" content="{meta_description}" />
  <meta property="og:url" content="{ca_url}" />
  <meta property="og:image" content="{og_image}" />
  <meta name="twitter:card" content="summary_large_image" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <!-- Must match the plotly.py major that generates the chart divs: plotly.py 6
       emits base64 "bdata" arrays that plotly.js 2.x cannot decode. -->
  <script src="https://cdn.plot.ly/plotly-3.5.0.min.js"></script>
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

    /* ── Subscribe card ── */
    .subscribe-card {{
      background: linear-gradient(90deg, rgba(88,166,255,0.10), rgba(188,140,255,0.06));
      border: 1px solid rgba(88,166,255,0.25);
      border-radius: 14px;
      padding: 1.1rem 1.4rem;
      margin-bottom: 1.5rem;
      display: flex; align-items: center; justify-content: space-between;
      gap: 0.75rem 1.5rem; flex-wrap: wrap;
      backdrop-filter: blur(8px);
    }}
    .subscribe-text {{ flex: 1 1 280px; min-width: 220px; }}
    .subscribe-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 0.2rem; }}
    .subscribe-sub {{ font-size: 0.8rem; color: var(--muted); }}
    .subscribe-form {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }}
    .subscribe-input {{
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      border-radius: 8px; color: var(--text);
      font-family: inherit; font-size: 0.85rem;
      padding: 0.5rem 0.75rem; outline: none;
      transition: border-color 0.2s; min-width: 0;
    }}
    .subscribe-input:focus {{ border-color: var(--accent); }}
    .subscribe-input::placeholder {{ color: var(--muted); }}
    #sub-name {{ width: 150px; }}
    #sub-email {{ width: 210px; }}
    .subscribe-hp {{ position: absolute; left: -9999px; width: 1px; height: 1px; opacity: 0; }}
    .subscribe-btn {{
      background: linear-gradient(135deg, var(--accent), #388bfd);
      border: none; color: #fff; font-family: inherit;
      font-size: 0.85rem; font-weight: 600;
      padding: 0.5rem 1.25rem; border-radius: 8px; cursor: pointer;
      transition: opacity 0.15s, transform 0.15s; white-space: nowrap;
    }}
    .subscribe-btn:hover:not(:disabled) {{ opacity: 0.92; transform: translateY(-1px); }}
    .subscribe-btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    .subscribe-msg {{ font-size: 0.8rem; flex-basis: 100%; min-height: 1.1em; }}
    .subscribe-msg.ok {{ color: var(--accent3); }}
    .subscribe-msg.err {{ color: var(--accent2); }}
    @media (max-width: 560px) {{
      .subscribe-form {{ width: 100%; }}
      #sub-name, #sub-email {{ flex: 1 1 100%; width: 100%; }}
      .subscribe-btn {{ flex: 1 1 100%; }}
    }}

    /* ── Summary timeline banner ── */
    .summary-range-banner {{
      display: flex; align-items: center; gap: 0.55rem;
      background: rgba(88,166,255,0.08);
      border: 1px solid rgba(88,166,255,0.25);
      border-radius: 10px;
      padding: 0.6rem 0.9rem;
      margin-bottom: 1rem;
      font-size: 0.9rem; color: var(--text);
    }}
    .summary-range-banner .srb-icon {{ font-size: 1.05rem; line-height: 1; }}
    .summary-range-banner strong {{ color: var(--accent); font-weight: 600; }}
    .summary-range-banner .srb-dates {{
      color: var(--muted); font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    @media (max-width: 560px) {{
      .summary-range-banner {{ font-size: 0.82rem; align-items: flex-start; }}
      .summary-range-banner .srb-dates {{ white-space: normal; }}
    }}

    /* States what span of California filings this page actually covers, so the
       totals here can be reconciled against the national dashboard's. */
    .coverage-note {{
      display: flex; align-items: flex-start; gap: 0.55rem;
      background: var(--card);
      border: 1px solid var(--border);
      border-left: 3px solid var(--accent6);
      border-radius: 10px;
      padding: 0.6rem 0.9rem;
      margin-bottom: 1.25rem;
      font-size: 0.84rem; color: var(--muted); line-height: 1.5;
    }}
    .coverage-note strong {{ color: var(--text); font-weight: 600; }}
    .coverage-note a {{ color: var(--accent); }}
    .coverage-note.degraded {{ border-left-color: var(--accent4); }}

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
    .kpi-card-range {{ display: flex; flex-direction: column; }}
    .kpi-range-select {{
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-family: inherit;
      font-size: 0.95rem;
      font-weight: 600;
      padding: 0.32rem 0.5rem;
      margin-top: 0.1rem;
      outline: none;
      cursor: pointer;
      width: 100%;
      max-width: 100%;
      transition: border-color 0.2s;
    }}
    .kpi-range-select:focus {{ border-color: var(--accent); }}
    .kpi-range-select option {{ background: var(--bg, #0d1117); color: var(--text); }}

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
    .filter-row {{
      display: grid;
      grid-template-columns: 2fr 1.3fr 1.6fr 1fr 1fr 1fr auto;
      gap: 0.5rem;
      margin-bottom: 0.85rem;
    }}
    @media (max-width: 900px) {{
      .filter-row {{ grid-template-columns: 1fr 1fr; }}
    }}
    .filter-input {{
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-family: inherit;
      font-size: 0.8rem;
      padding: 0.42rem 0.65rem;
      outline: none;
      transition: border-color 0.2s;
      min-width: 0;
    }}
    .filter-input:focus {{ border-color: var(--accent); }}
    .filter-input::placeholder {{ color: var(--muted); }}
    .filter-reset {{
      background: rgba(247,129,102,0.1);
      border: 1px solid rgba(247,129,102,0.4);
      color: var(--accent2);
      border-radius: 8px;
      font-family: inherit;
      font-size: 0.78rem;
      padding: 0.42rem 0.85rem;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .filter-reset:hover {{ background: rgba(247,129,102,0.2); }}
    .table-controls {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 0.85rem; gap: 0.75rem; flex-wrap: wrap;
      padding: 0.5rem 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
    }}
    .pager-label {{ font-size: 0.78rem; color: var(--muted); display: inline-flex; align-items: center; gap: 0.4rem; }}
    .pager-label select {{
      background: rgba(255,255,255,0.05); border: 1px solid var(--border);
      color: var(--text); border-radius: 6px; padding: 0.2rem 0.4rem;
      font-family: inherit; font-size: 0.78rem;
    }}
    .pager-right {{ display: flex; align-items: center; gap: 0.5rem; }}
    .pager-info {{ font-size: 0.78rem; color: var(--muted); min-width: 95px; text-align: center; }}
    .pager-btn {{
      background: rgba(88,166,255,0.08);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      font-family: inherit; font-size: 0.78rem;
      padding: 0.3rem 0.7rem; cursor: pointer;
      transition: all 0.15s;
    }}
    .pager-btn:hover:not(:disabled) {{ border-color: var(--accent); color: var(--accent); }}
    .pager-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
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
    .visitor-counter {{ white-space: nowrap; }}
    .visitor-counter strong {{ color: var(--accent); font-variant-numeric: tabular-nums; }}

    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(6px); }}
      to {{ opacity: 1; transform: none; }}
    }}

    @media (max-width: 640px) {{
      main {{ padding: 1rem; }}
      .kpi-value {{ font-size: 1.45rem; }}
      h1 {{ font-size: 1.1rem; }}
    }}
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="brand">
      <div class="brand-icon">📋</div>
      <div>
        <h1>California Live Layoff Monitoring Dashboard</h1>
        <div class="subtitle">Employment Development Department · part of the US WARN Layoff Tracker</div>
      </div>
    </div>
    <div class="header-right">
      <div class="header-meta">
        Updated: <strong>{last_updated}</strong><br/>
        <a href="../">🇺🇸 All 50 states</a>
        &nbsp;·&nbsp;
        <a href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn" target="_blank" rel="noopener">CA EDD WARN</a>
        &nbsp;·&nbsp;
        <a href="https://github.com/bilalahamad0/warn" target="_blank" rel="noopener">GitHub</a>
      </div>
    </div>
  </div>
</header>

<main>
  {new_banner}

  <!-- Email signup -->
  <section class="subscribe-card" id="subscribe">
    <div class="subscribe-text">
      <h2 class="subscribe-title">📬 Get layoff alerts in your inbox</h2>
      <p class="subscribe-sub">New California WARN notices, delivered straight to your inbox when our twice-daily check finds them.</p>
      <p class="subscribe-sub">Adds California to your alerts — any other
        states you picked on the <a href="../#subscribe">US dashboard</a> stay
        as they are. Change or cancel anything from the link in every email.</p>
    </div>
    <form class="subscribe-form" id="subscribe-form" novalidate>
      <input type="text" id="sub-name" class="subscribe-input" placeholder="Your name" autocomplete="name" aria-label="Your name" />
      <input type="email" id="sub-email" class="subscribe-input" placeholder="you@example.com" autocomplete="email" aria-label="Email address" required />
      <input type="text" id="sub-company-hp" class="subscribe-hp" tabindex="-1" autocomplete="off" aria-hidden="true" />
      <button type="submit" class="subscribe-btn" id="sub-submit">Subscribe</button>
    </form>
    <div class="subscribe-msg" id="subscribe-msg" role="status" aria-live="polite"></div>
  </section>

  <!-- Summary timeline banner -->
  <div class="summary-range-banner" id="summary-range-banner">
    <span class="srb-icon" aria-hidden="true">📅</span>
    <span class="srb-text">Summary shows <strong id="srb-scope">{summary_scope}</strong>
      <span class="srb-dates" id="srb-dates">{summary_dates}</span></span>
  </div>

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">WARN Notices</div>
      <div class="kpi-value" id="kpi-notices">{total_records}</div>
      <div class="kpi-sub">Unique filings</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Employees Affected</div>
      <div class="kpi-value" id="kpi-employees">{total_employees}</div>
      <div class="kpi-sub">Total affected</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Lead Time</div>
      <div class="kpi-value" id="kpi-lead">{avg_lead_days}</div>
      <div class="kpi-sub">Notice → effective date</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Largest Layoff</div>
      <div class="kpi-value sm" id="kpi-largest-co">{largest_company}</div>
      <div class="kpi-sub"><span id="kpi-largest-emp">{largest_employees}</span> employees</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Top County</div>
      <div class="kpi-value sm" id="kpi-county">{top_county}</div>
      <div class="kpi-sub"><span id="kpi-county-emp">{top_county_employees}</span> employees</div>
    </div>
    <div class="kpi-card kpi-card-range">
      <div class="kpi-label"><label for="kpi-range">Date Range</label></div>
      <select id="kpi-range" class="kpi-range-select" aria-label="Summary date range">
        {kpi_range_options}
      </select>
      <div class="kpi-sub" id="kpi-range-span">{kpi_range_span}</div>
    </div>
  </div>

  {coverage_note}

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
      <span class="section-tag">{recent_total} total</span>
    </div>
    {recent_controls}
    <div class="table-wrap">
      {recent_table}
    </div>
    {recent_table_controls}
  </div>
</main>

<footer>
  Built by <a href="https://bilalahamad.com" target="_blank">bilalahamad.com</a> ·
  Data: <a href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn" target="_blank">CA EDD</a> ·
  <a href="../architecture.html">How it works</a> ·
  Generated {generated_at}
  <span id="visitor-counter" class="visitor-counter" hidden> · 👁 <strong id="visitor-count">—</strong> visitors</span>
</footer>

<script>
(function () {{
  // ── Plotly resize helper ──
  function resizeChartsIn(container) {{
    if (!container || !window.Plotly) return;
    container.querySelectorAll('.plotly-graph-div').forEach(div => {{
      try {{ Plotly.Plots.resize(div); }} catch (e) {{}}
    }});
  }}

  // ── Tab switching (scoped per section) ──
  document.querySelectorAll('.chart-tabs').forEach(tabGroup => {{
    tabGroup.querySelectorAll('.chart-tab').forEach(btn => {{
      btn.addEventListener('click', () => {{
        tabGroup.querySelectorAll('.chart-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const target = document.getElementById(btn.dataset.target);
        if (!target) return;
        target.parentElement.querySelectorAll('.chart-pane').forEach(p => p.classList.remove('active'));
        target.classList.add('active');
        setTimeout(() => resizeChartsIn(target), 60);
      }});
    }});
  }});

  // After load, force a resize on all visible charts (handles initial render in hidden tabs)
  window.addEventListener('load', () => {{
    setTimeout(() => {{
      document.querySelectorAll('.chart-pane').forEach(p => resizeChartsIn(p));
    }}, 200);
  }});

  // ── Email signup ──
  var SIGNUP_ENDPOINT = "{signup_endpoint}";
  var subForm = document.getElementById('subscribe-form');
  var subMsg = document.getElementById('subscribe-msg');
  var subName = document.getElementById('sub-name');
  var subEmail = document.getElementById('sub-email');
  var subBtn = document.getElementById('sub-submit');
  var subHp = document.getElementById('sub-company-hp');

  function setSubMsg(text, kind) {{
    if (!subMsg) return;
    subMsg.textContent = text;
    subMsg.className = 'subscribe-msg' + (kind ? ' ' + kind : '');
  }}
  function validEmail(v) {{ return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v); }}

  if (subForm) {{
    subForm.addEventListener('submit', function (ev) {{
      ev.preventDefault();
      if (subHp && subHp.value) return;  // honeypot: silently drop bots
      var nm = ((subName && subName.value) || '').trim();
      var em = ((subEmail && subEmail.value) || '').trim();
      if (!validEmail(em)) {{ setSubMsg('Please enter a valid email address.', 'err'); return; }}
      if (!SIGNUP_ENDPOINT) {{ setSubMsg("Signups aren't configured yet — check back soon.", 'err'); return; }}
      if (subBtn) {{ subBtn.disabled = true; subBtn.textContent = 'Subscribing…'; }}
      setSubMsg('');
      fetch(SIGNUP_ENDPOINT, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'text/plain;charset=utf-8' }},
        // 'CA' is sent explicitly rather than leaning on the Apps Script's
        // DEFAULT_STATES fallback, so the payload says what this form means.
        // Signup is additive server-side: any other states this address
        // already subscribed to are kept.
        body: JSON.stringify({{
          name: nm, email: em, states: 'CA', source: 'dashboard'
        }})
      }})
        .then(function (r) {{ return r.json(); }})
        .then(function (d) {{
          if (d && d.ok) {{
            setSubMsg(d.duplicate
              ? (d.updated
                  ? "California added — your other alerts are unchanged."
                  : "You're already subscribed to California alerts.")
              : "You're in! Watch your inbox for new WARN alerts.", 'ok');
            subForm.reset();
          }} else {{
            setSubMsg('Something went wrong. Please try again later.', 'err');
          }}
        }})
        .catch(function () {{ setSubMsg('Network error. Please try again later.', 'err'); }})
        .finally(function () {{ if (subBtn) {{ subBtn.disabled = false; subBtn.textContent = 'Subscribe'; }} }});
    }});
  }}

  // ── Visitor counter (counts each browser once via localStorage) ──
  (function () {{
    var wrap = document.getElementById('visitor-counter');
    var out = document.getElementById('visitor-count');
    if (!wrap || !out || !SIGNUP_ENDPOINT) return;
    var KEY = 'warn_visitor_counted';
    var firstVisit = false;
    try {{ firstVisit = !localStorage.getItem(KEY); }} catch (_e) {{ firstVisit = false; }}
    var action = firstVisit ? 'hit' : 'views';
    var sep = SIGNUP_ENDPOINT.indexOf('?') > -1 ? '&' : '?';
    fetch(SIGNUP_ENDPOINT + sep + 'action=' + action)
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (!d || typeof d.count !== 'number') return;
        out.textContent = d.count.toLocaleString();
        wrap.hidden = false;
        if (firstVisit) {{ try {{ localStorage.setItem(KEY, '1'); }} catch (_e) {{}} }}
      }})
      .catch(function () {{ /* leave the counter hidden on failure */ }});
  }})();

  // ── KPI date-range selector (Summary metrics by period) ──
  (function () {{
    const sel = document.getElementById('kpi-range');
    const kt = document.getElementById('notices-table');
    if (!sel || !kt || !kt.tBodies.length) return;

    const recs = [...kt.tBodies[0].rows].map(r => ({{
      company: r.dataset.companyName || '',
      county: r.dataset.county || '',
      employees: parseInt(r.dataset.employees, 10) || 0,
      notice: (r.dataset.notice || '').slice(0, 10),
      effective: (r.dataset.effective || '').slice(0, 10),
    }}));

    const el = {{
      notices: document.getElementById('kpi-notices'),
      employees: document.getElementById('kpi-employees'),
      lead: document.getElementById('kpi-lead'),
      largestCo: document.getElementById('kpi-largest-co'),
      largestEmp: document.getElementById('kpi-largest-emp'),
      county: document.getElementById('kpi-county'),
      countyEmp: document.getElementById('kpi-county-emp'),
      span: document.getElementById('kpi-range-span'),
      srbScope: document.getElementById('srb-scope'),
      srbDates: document.getElementById('srb-dates'),
    }};
    const fmt = n => (Number(n) || 0).toLocaleString('en-US');
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    // Format 'YYYY-MM-DD' as 'Jul 4, 2026' — matches the server's _fmt_human_date.
    const fmtDate = iso => {{
      const p = String(iso).split('-');
      return p.length === 3 ? MONTHS[(+p[1]) - 1] + ' ' + (+p[2]) + ', ' + p[0] : String(iso);
    }};

    function windowFor(val) {{
      const now = new Date();
      const today = now.toISOString().slice(0, 10);
      if (val === 'all') {{
        const ds = recs.map(r => r.notice).filter(Boolean).sort();
        const earliest = ds.length ? ds[0] : today;
        return {{ from: earliest, to: '9999-12-31', span: earliest + ' → ' + today,
                 scope: 'all available data', lo: earliest, hi: today }};
      }}
      if (val === 'ytd') {{
        // Year from the same UTC basis as `today` (matches the server's UTC
        // build) — mixing local getFullYear() with a UTC upper bound inverts
        // the window across the New-Year boundary for users east of UTC.
        const y = today.slice(0, 4);
        return {{ from: y + '-01-01', to: today, span: y + '-01-01 → ' + today,
                 scope: 'current year to date', lo: y + '-01-01', hi: today }};
      }}
      return {{ from: val + '-01-01', to: val + '-12-31', span: val + '-01-01 → ' + val + '-12-31',
               scope: 'calendar year ' + val, lo: val + '-01-01', hi: val + '-12-31' }};
    }}

    function apply() {{
      const w = windowFor(sel.value);
      const inRange = recs.filter(r => r.notice && r.notice >= w.from && r.notice <= w.to);

      let employees = 0, leadSum = 0, leadN = 0, largest = null;
      const ct = Object.create(null);
      for (const r of inRange) {{
        employees += r.employees;
        if (r.notice.length === 10 && r.effective.length === 10) {{
          const d = (Date.parse(r.effective) - Date.parse(r.notice)) / 86400000;
          if (d > 0 && d < 730) {{ leadSum += d; leadN += 1; }}
        }}
        // Tiebreak mirrors the server: most employees, then latest notice,
        // then company name — so the picked layoff is order-independent.
        if (!largest
            || r.employees > largest.employees
            || (r.employees === largest.employees && r.notice > largest.notice)
            || (r.employees === largest.employees && r.notice === largest.notice
                && r.company > largest.company)) {{
          largest = r;
        }}
        if (r.county) ct[r.county] = (ct[r.county] || 0) + r.employees;
      }}

      let topCounty = null, topVal = -1;
      for (const c in ct) {{
        if (ct[c] > topVal || (ct[c] === topVal && c > topCounty)) {{
          topVal = ct[c]; topCounty = c;
        }}
      }}

      if (el.notices) el.notices.textContent = fmt(inRange.length);
      if (el.employees) el.employees.textContent = fmt(employees);
      if (el.lead) el.lead.textContent = leadN ? Math.round(leadSum / leadN) + 'd' : 'N/A';
      if (el.largestCo) el.largestCo.textContent = largest ? (largest.company || 'N/A') : 'N/A';
      if (el.largestEmp) el.largestEmp.textContent = largest ? fmt(largest.employees) : 'N/A';
      if (el.county) el.county.textContent = topCounty || 'N/A';
      if (el.countyEmp) el.countyEmp.textContent = topCounty ? fmt(topVal) : 'N/A';
      if (el.span) el.span.textContent = w.span;
      if (el.srbScope) el.srbScope.textContent = w.scope;
      if (el.srbDates) el.srbDates.textContent = fmtDate(w.lo) + ' – ' + fmtDate(w.hi);
    }}

    sel.addEventListener('change', apply);
    apply();
  }})();

  // ── Notices table: pagination + filter + sort ──
  const table = document.getElementById('notices-table');
  if (!table) return;

  const tbody = table.querySelector('tbody');
  const allRows = [...tbody.querySelectorAll('tr')];
  let filteredRows = allRows.slice();
  let currentPage = 1;
  let pageSize = 50;
  let sortCol = -1, sortAsc = true;

  const fCompany = document.getElementById('filter-company');
  const fCounty = document.getElementById('filter-county');
  const fIndustry = document.getElementById('filter-industry');
  const fType = document.getElementById('filter-type');
  const fFrom = document.getElementById('filter-date-from');
  const fTo = document.getElementById('filter-date-to');
  const resetBtn = document.getElementById('filter-reset');
  const pageSizeEls = [...document.querySelectorAll('.js-page-size')];
  const prevBtns = [...document.querySelectorAll('.js-page-prev')];
  const nextBtns = [...document.querySelectorAll('.js-page-next')];
  const pageInfos = [...document.querySelectorAll('.js-page-info')];
  const countEls = [...document.querySelectorAll('.js-table-count')];

  function applyFilters() {{
    const qCompany = (fCompany?.value || '').trim().toLowerCase();
    const companyTerms = qCompany.split(',').map(t => t.trim()).filter(Boolean);
    const qCounty = fCounty?.value || '';
    const qIndustry = fIndustry?.value || '';
    const qType = fType?.value || '';
    const qFrom = fFrom?.value || '';
    const qTo = fTo?.value || '';

    filteredRows = allRows.filter(row => {{
      const d = row.dataset;
      if (companyTerms.length && !companyTerms.some(t => d.company.includes(t))) return false;
      if (qCounty && d.county !== qCounty) return false;
      if (qIndustry && d.industry !== qIndustry) return false;
      if (qType && d.type !== qType) return false;
      if (qFrom && d.notice && d.notice < qFrom) return false;
      if (qTo && d.notice && d.notice > qTo) return false;
      return true;
    }});
    currentPage = 1;
    render();
  }}

  function render() {{
    // Hide every row, then show the current page slice
    allRows.forEach(r => r.style.display = 'none');
    const totalFiltered = filteredRows.length;
    const totalAll = allRows.length;
    const size = pageSize === 0 ? totalFiltered : pageSize;
    const totalPages = size === 0 ? 1 : Math.max(1, Math.ceil(totalFiltered / size));
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;
    const start = (currentPage - 1) * (pageSize === 0 ? totalFiltered : pageSize);
    const end = pageSize === 0 ? totalFiltered : start + pageSize;
    const slice = filteredRows.slice(start, end);
    slice.forEach(r => r.style.display = '');

    // Reorder DOM to match filtered+sorted order (within current page)
    slice.forEach(r => tbody.appendChild(r));

    const countText = totalFiltered === 0
      ? 'No matches'
      : totalFiltered === totalAll
        ? `${{totalAll.toLocaleString()}} notices`
        : `${{totalFiltered.toLocaleString()}} of ${{totalAll.toLocaleString()}} matched`;
    countEls.forEach(el => {{ el.textContent = countText; }});

    const showStart = totalFiltered === 0 ? 0 : start + 1;
    const showEnd = Math.min(end, totalFiltered);
    const infoText = pageSize === 0
      ? `Showing all ${{totalFiltered.toLocaleString()}}`
      : `${{showStart.toLocaleString()}}–${{showEnd.toLocaleString()}} / ${{totalFiltered.toLocaleString()}}`;
    pageInfos.forEach(el => {{ el.textContent = infoText; }});
    prevBtns.forEach(b => {{ b.disabled = currentPage <= 1; }});
    nextBtns.forEach(b => {{ b.disabled = currentPage >= totalPages; }});
  }}

  // Sortable headers
  table.querySelectorAll('th').forEach((th, ci) => {{
    th.innerHTML += ' <span class="sort-arrow">▲</span>';
    th.addEventListener('click', () => {{
      const asc = sortCol === ci ? !sortAsc : true;
      sortCol = ci; sortAsc = asc;
      table.querySelectorAll('th').forEach(h => h.classList.remove('sorted'));
      th.classList.add('sorted');
      th.querySelector('.sort-arrow').textContent = asc ? '▲' : '▼';
      filteredRows.sort((a, b) => {{
        const av = a.cells[ci]?.textContent.replace(/,/g, '') || '';
        const bv = b.cells[ci]?.textContent.replace(/,/g, '') || '';
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = !isNaN(an) && !isNaN(bn) ? an - bn : av.localeCompare(bv);
        return asc ? cmp : -cmp;
      }});
      render();
    }});
  }});

  // Wire filter inputs
  [fCompany, fCounty, fIndustry, fType, fFrom, fTo].forEach(el => {{
    if (el) el.addEventListener('input', applyFilters);
    if (el) el.addEventListener('change', applyFilters);
  }});
  if (resetBtn) {{
    resetBtn.addEventListener('click', () => {{
      [fCompany, fCounty, fIndustry, fType, fFrom, fTo].forEach(el => {{ if (el) el.value = el.dataset.default || ''; }});
      applyFilters();
    }});
  }}
  pageSizeEls.forEach(sel => {{
    sel.addEventListener('change', () => {{
      pageSize = parseInt(sel.value, 10) || 0;
      pageSizeEls.forEach(s => {{ s.value = sel.value; }});
      currentPage = 1;
      render();
    }});
  }});
  prevBtns.forEach(b => b.addEventListener('click', () => {{ currentPage--; render(); }}));
  nextBtns.forEach(b => b.addEventListener('click', () => {{ currentPage++; render(); }}));

  render();
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Monthly US digest
# ---------------------------------------------------------------------------

# Which months' digests have already gone out. The pipeline runs twice daily,
# so this ledger is what makes the digest exactly-once-per-month. Mirrors the
# notified/amended ledger discipline in warn_monitor: a period is recorded ONLY
# after a successful send, so a failed send simply retries on the next run.
DIGEST_LEDGER_NAME = "digest_sent.json"


def _digest_ledger_path() -> Path:
    """Resolved at call time so DATA_DIR stays patchable (tests, alt roots)."""
    return DATA_DIR / DIGEST_LEDGER_NAME


def _previous_month(today=None) -> str:
    """The just-completed calendar month as ``YYYY-MM`` (UTC)."""
    d = today or datetime.now(timezone.utc).date()
    last_of_prev = d.replace(day=1) - timedelta(days=1)
    return f"{last_of_prev.year:04d}-{last_of_prev.month:02d}"


def _load_digest_ledger() -> dict:
    path = _digest_ledger_path()
    if not path.exists():
        return {"sent": []}
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.warning(f"Digest ledger unreadable ({e}) — treating as empty.")
        return {"sent": []}
    if not isinstance(data, dict):
        return {"sent": []}
    data.setdefault("sent", [])
    return data


def _digest_already_sent(period: str) -> bool:
    return period in (_load_digest_ledger().get("sent") or [])


def _record_digest_sent(period: str) -> None:
    """Mark ``period`` delivered. Called only after a successful send."""
    data = _load_digest_ledger()
    sent = [p for p in (data.get("sent") or []) if p]
    if period not in sent:
        sent.append(period)
    data["sent"] = sorted(sent)
    data["last_sent"] = period
    data["last_sent_at"] = datetime.now(timezone.utc).isoformat() + "Z"
    path = _digest_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _build_digest_payload(period: str) -> dict:
    """Build the monthly digest for ``period`` (``YYYY-MM``) via warn_digest."""
    import warn_digest

    return warn_digest.build_monthly_digest(
        year=int(period[:4]), month=int(period[5:7])
    )


def maybe_send_monthly_digest(records=None, force: bool = False,
                              period: str = None) -> bool:
    """Send the whole-US monthly digest at most once per calendar month.

    Called on every pipeline run: it targets the just-completed month, so the
    first run of a new month delivers it and every later run that month is a
    ledger no-op. ``force`` re-sends regardless (manual ``--digest`` testing).
    """
    period = period or _previous_month()
    if _digest_already_sent(period) and not force:
        log.info(f"Monthly digest for {period} already sent — skipping.")
        return False

    log.info(f"Building monthly US digest for {period} …")
    digest = _build_digest_payload(period)
    if not digest:
        log.warning(f"No digest payload for {period} — nothing to send.")
        return False

    sent = warn_notify.send_monthly_digest(digest, records=records)
    if sent:
        _record_digest_sent(period)
        log.info(f"✓ Monthly digest for {period} sent.")
    else:
        log.warning(f"Monthly digest for {period} not sent — retrying next run.")
    return sent


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(no_push: bool = False, force: bool = False, skip_history: bool = False,
        send_digest: bool = True, force_digest: bool = False):
    log.info("=" * 70)
    log.info(f"WARN Publisher — {datetime.now(timezone.utc).isoformat()}Z")
    log.info("=" * 70)

    # Step 1: Monitor every registered state source (failure-isolated).
    # California's result doubles as the headline monitor_result the site
    # builder and notifier consume, exactly as before multi-state support.
    log.info("Step 1/5: Running state sources …")
    state_results = warn_sources.run_all(force=force)
    monitor_result = state_results.get("ca") or {"diff": {}, "summary": {}}
    monitor_result["states"] = {
        code: {k: v for k, v in res.items() if k in ("state", "file_changed", "error")}
        for code, res in state_results.items()
    }
    for code, res in state_results.items():
        if res.get("error"):
            log.warning(f"State source '{code}' failed (non-fatal): {res['error']}")

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

    # Step 3.5: National dataset (feeds the US map chart + cross-state stats).
    try:
        warn_aggregate.build_national()
    except Exception as e:
        log.warning(f"National aggregation failed (non-fatal): {e}")

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

    # Step 5: Build sites — the national dashboard at the site root, then
    # California at docs/ca/, then the unsubscribe landing page.
    #
    # Which build is allowed to fail is deliberate and it INVERTED when the
    # national dashboard took over the root. Whatever builds the root page must
    # be fatal: an unguarded failure exits non-zero, so git_commit_push is
    # skipped here and CI's `if: success()` commit step never runs, leaving the
    # last good page published. That guard used to sit on build_site because
    # California was the root. It now sits on build_us_site, and California —
    # a sub-page whose failure leaves a live, correct front page — is the
    # non-fatal one.
    log.info("Step 5/5: Building sites …")
    site_failures = []
    try:
        warn_site_us.build_us_site(out_dir=OUTPUT_DIR)
        warn_site_us.build_legacy_us_redirect(site_dir=OUTPUT_DIR)
    except Exception as e:
        site_failures.append(f"root US dashboard: {e}")
        log.error(f"ROOT dashboard build FAILED — nothing will be committed: {e}")

    try:
        build_site(manifest, monitor_result)
    except Exception as e:
        log.warning(f"California dashboard build failed (non-fatal): {e}")

    # The landing page for the signed unsubscribe links in every subscriber
    # email. Built before the sends below, so a link can never be mailed out
    # ahead of the page it points at.
    try:
        build_unsubscribe_page()
    except Exception as e:
        log.warning(f"Unsubscribe page build failed (non-fatal): {e}")

    # Subscriber preferences, fetched once for the whole run and threaded
    # through every send — a run that alerts on N states must not hit the
    # signup sheet N times.
    subscriber_records = warn_notify.load_subscriber_records()

    # Notify on changes, per state. Each alert reaches the operator plus only
    # the subscribers who asked for that state. Only mark notices as "alerted"
    # once the email actually sends — a failed send is then retried next run
    # instead of being lost. Each state's ledgers live with its source paths;
    # this is what stops feed version churn from re-alerting the same notices
    # on consecutive runs (see warn_monitor.detect_changes).
    for source in warn_sources.all_sources():
        res = state_results.get(source.code) or {}
        diff = res.get("diff", {})
        summary = res.get("summary", {})
        if diff.get("new_count", 0) > 0 or diff.get("amendment_count", 0) > 0:
            try:
                sent = warn_notify.notify_if_changes(
                    diff,
                    summary,
                    state=source.code.upper(),
                    records=subscriber_records,
                )
                if sent:
                    source.record_alerted(diff)
            except Exception as e:
                log.warning(
                    f"Email notification failed for {source.code.upper()} "
                    f"(non-fatal): {e}"
                )

    # Monthly whole-US digest — a ledger no-op except on the first run of a
    # new calendar month. Non-fatal: a digest problem must never fail a run
    # that already produced good data.
    if send_digest:
        try:
            maybe_send_monthly_digest(
                records=subscriber_records, force=force_digest
            )
        except Exception as e:
            log.warning(f"Monthly digest failed (non-fatal): {e}")
    else:
        log.info("Skipping monthly digest (--no-digest).")

    # Git push — never when the site root failed to build, or the commit would
    # publish a stale or half-built front page.
    if site_failures:
        log.error("Skipping git push — the site root was not rebuilt.")
    elif not no_push:
        log.info("Git push …")
        git_commit_push()
    else:
        log.info("Skipping git push (--no-push).")

    # Raised only here, after the notifications and the digest have gone out: a
    # chart hiccup in the national build should not cost a subscriber a
    # legitimate alert. The ledgers only record after a successful send, so a
    # send skipped by this raise is retried next run rather than lost.
    if site_failures:
        raise RuntimeError("site build failed: " + "; ".join(site_failures))

    log.info("✓ Publisher complete.")
    return monitor_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WARN Full Pipeline Publisher")
    parser.add_argument("--no-push", action="store_true", help="Skip git push")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    parser.add_argument(
        "--skip-history", action="store_true", help="Skip historical PDF update"
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Force build+send of last month's US digest (ignores the ledger)",
    )
    parser.add_argument(
        "--no-digest", action="store_true", help="Skip the monthly US digest step"
    )
    args = parser.parse_args()
    run(
        no_push=args.no_push,
        force=args.force,
        skip_history=args.skip_history,
        send_digest=not args.no_digest,
        force_digest=args.digest,
    )
