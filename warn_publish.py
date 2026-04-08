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
    """Read Plotly div from charts dir."""
    path = CHARTS_DIR / f"{chart_id}.html"
    if path.exists():
        return path.read_text()
    return f'<div class="chart-error">Chart {chart_id} not available</div>'


def _format_number(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_site(manifest: dict, monitor_result: dict) -> str:
    """Build the full index.html by embedding Plotly divs."""
    log.info("Building index.html …")

    chart_divs = {cm["id"]: _read_chart_div(cm["id"]) for cm in warn_charts.CHART_META}

    diff = monitor_result.get("diff", {})
    new_count = diff.get("new_count", 0)
    new_employees = diff.get("total_employees_new", 0)
    total_records = _format_number(manifest.get("total_records", 0))
    total_employees = _format_number(manifest.get("total_employees", 0))
    last_updated = manifest.get("last_updated", "")[:10]
    date_start = str(manifest.get("date_range_start", ""))[:10]
    date_end = str(manifest.get("date_range_end", ""))[:10]

    new_banner = ""
    if new_count > 0:
        new_banner = f"""
        <div class="new-banner">
          <span class="badge-new">NEW</span>
          <strong>{new_count} new WARN notice{"s" if new_count > 1 else ""}</strong>
          affecting <strong>{_format_number(new_employees)} employees</strong>
          since last check.
        </div>"""

    chart_tabs_html = ""
    chart_panes_html = ""
    for i, cm in enumerate(warn_charts.CHART_META):
        active_tab = "active" if i == 0 else ""
        active_pane = "active" if i == 0 else ""
        chart_tabs_html += (
            f'<button class="chart-tab {active_tab}" data-target="pane-{cm["id"]}">'
            f'{cm["title"]}</button>\n'
        )
        chart_panes_html += f"""
        <div class="chart-pane {active_pane}" id="pane-{cm['id']}">
          <p class="chart-desc">{cm['desc']}</p>
          <div class="chart-container">
            {chart_divs[cm['id']]}
          </div>
        </div>"""

    # Read the HTML template and inject
    # template_path = BASE_DIR / "docs" / "_template.html"
    html = SITE_HTML_TEMPLATE.format(
        total_records=total_records,
        total_employees=total_employees,
        last_updated=last_updated,
        date_start=date_start,
        date_end=date_end,
        new_banner=new_banner,
        chart_tabs=chart_tabs_html,
        chart_panes=chart_panes_html,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    INDEX_HTML.write_text(html, encoding="utf-8")

    # Copy data.json for public access
    if (DATA_DIR / "warn_latest.json").exists():
        shutil.copy(DATA_DIR / "warn_latest.json", SITE_DATA)

    log.info(f"Site built → {INDEX_HTML}")
    return str(INDEX_HTML)


# ---------------------------------------------------------------------------
# Git push
# ---------------------------------------------------------------------------


def git_commit_push(message: str = None) -> bool:
    """Stage changed files, commit, and push."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        # Try .env
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
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
      <meta
        name="description"
        content="Live monitoring and analysis of California WARN layoff notices from the Employment Development Department."
      />
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

    /* ── Background mesh ── */
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
      padding: 2rem 2rem 1rem;
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(8px);
      background: var(--glass);
      position: sticky; top: 0; z-index: 100;
    }}
    .header-inner {{
      max-width: 1400px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 1rem;
    }}
    .brand {{ display: flex; align-items: center; gap: 0.75rem; }}
    .brand-icon {{
      width: 40px; height: 40px; border-radius: 10px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      display: grid; place-items: center; font-size: 1.2rem;
    }}
    h1 {{ font-size: 1.4rem; font-weight: 700; }}
    .subtitle {{ font-size: 0.8rem; color: var(--muted); }}
    .header-meta {{ font-size: 0.78rem; color: var(--muted); text-align: right; }}
    .header-meta a {{ color: var(--accent); text-decoration: none; }}
    .header-meta a:hover {{ text-decoration: underline; }}

    /* ── Main layout ── */
    main {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}

    /* ── New banner ── */
    .new-banner {{
      background: linear-gradient(90deg, rgba(63,185,80,0.15), rgba(63,185,80,0.05));
      border: 1px solid rgba(63,185,80,0.3);
      border-radius: 10px; padding: 0.85rem 1.25rem;
      margin-bottom: 1.5rem;
      display: flex; align-items: center; gap: 0.75rem;
      animation: fadeIn 0.5s ease;
    }}
    .badge-new {{
      background: var(--accent3); color: #000;
      padding: 0.2rem 0.5rem; border-radius: 4px;
      font-size: 0.72rem; font-weight: 700; letter-spacing: 0.05em;
    }}

    /* ── KPI cards ── */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem; margin-bottom: 2rem;
    }}
    .kpi-card {{
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.2rem 1.4rem;
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
    .kpi-card:hover {{ transform: translateY(-2px); border-color: var(--accent); }}
    .kpi-label {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.4rem; }}
    .kpi-value {{ font-size: 2rem; font-weight: 700; line-height: 1; }}
    .kpi-sub {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.3rem; }}

    /* ── Charts section ── */
    .charts-section {{
      background: var(--glass);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(8px);
      margin-bottom: 2rem;
    }}
    .charts-section h2 {{
      font-size: 1rem; font-weight: 600;
      color: var(--muted); margin-bottom: 1rem;
      text-transform: uppercase; letter-spacing: 0.08em;
    }}
    .chart-tabs {{
      display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1.5rem;
      border-bottom: 1px solid var(--border); padding-bottom: 1rem;
    }}
    .chart-tab {{
      background: none; border: 1px solid var(--border);
      color: var(--muted); padding: 0.45rem 1rem;
      border-radius: 8px; cursor: pointer;
      font-size: 0.82rem; font-family: inherit;
      transition: all 0.2s;
    }}
    .chart-tab:hover {{ border-color: var(--accent); color: var(--text); }}
    .chart-tab.active {{
      background: rgba(88,166,255,0.12);
      border-color: var(--accent); color: var(--accent); font-weight: 500;
    }}
    .chart-pane {{ display: none; animation: fadeIn 0.3s ease; }}
    .chart-pane.active {{ display: block; }}
    .chart-desc {{ font-size: 0.82rem; color: var(--muted); margin-bottom: 1rem; }}
    .chart-container {{ width: 100%; min-height: 500px; }}
    .chart-container .plotly-graph-div {{ width: 100% !important; }}
    .chart-error {{
      background: rgba(247,129,102,0.1); border: 1px solid rgba(247,129,102,0.3);
      border-radius: 8px; padding: 1rem; color: var(--accent2); font-size: 0.85rem;
    }}

    /* ── Footer ── */
    footer {{
      border-top: 1px solid var(--border);
      padding: 1.5rem 2rem; text-align: center;
      font-size: 0.78rem; color: var(--muted);
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}

    /* ── Animations ── */
    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: none; }}
    }}

    /* ── Responsive ── */
    @media (max-width: 640px) {{
      main {{ padding: 1rem; }}
      .kpi-value {{ font-size: 1.5rem; }}
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
        <h1>California WARN Layoff Monitor</h1>
        <div class="subtitle">
          Employment Development Department · Real-time Tracking
        </div>
      </div>
    </div>
    <div class="header-meta">
      Last updated: <strong>{last_updated}</strong><br/>
      <a
        href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn"
        target="_blank"
        rel="noopener"
      >
        Source: CA EDD WARN
      </a>
    </div>
  </div>
</header>

<main>
  {new_banner}

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Total WARN Notices</div>
      <div class="kpi-value">{total_records}</div>
      <div class="kpi-sub">Unique filings</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Employees Affected</div>
      <div class="kpi-value">{total_employees}</div>
      <div class="kpi-sub">Cumulative total</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Date Range</div>
      <div class="kpi-value" style="font-size:1rem;padding-top:0.4rem">{date_start}</div>
      <div class="kpi-sub">through {date_end}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Data Source</div>
      <div class="kpi-value" style="font-size:1rem;padding-top:0.4rem">EDD</div>
      <div class="kpi-sub">Auto-updated twice daily</div>
    </div>
  </div>

  <div class="charts-section">
    <h2>📈 Interactive Charts</h2>
    <div class="chart-tabs">
      {chart_tabs}
    </div>
    {chart_panes}
  </div>
</main>

<footer>
  Built by <a href="https://bilalahamad.com" target="_blank">bilalahamad.com</a> ·
  Data from
  <a
    href="https://edd.ca.gov/en/jobs_and_training/layoff_services_warn"
    target="_blank"
  >
    California EDD
  </a>
  ·
  <a href="https://github.com/bilalahamad0/warn" target="_blank">GitHub</a> ·
  Generated {generated_at}
</footer>

<script>
  // Tab switching
  document.querySelectorAll('.chart-tab').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const target = btn.dataset.target;
      document.querySelectorAll('.chart-tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.chart-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(target).classList.add('active');
      // Trigger Plotly resize
      setTimeout(() => window.dispatchEvent(new Event('resize')), 50);
    }});
  }});
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
