# 📋 US WARN Layoff Monitor

[![WARN Monitor](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml/badge.svg)](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml)
[![CodeQL](https://github.com/bilalahamad0/warn/actions/workflows/codeql.yml/badge.svg)](https://github.com/bilalahamad0/warn/actions/workflows/codeql.yml)
[![Auto-Update](https://img.shields.io/badge/updates-twice_daily-brightgreen)](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml)
[![Last Commit](https://img.shields.io/github/last-commit/bilalahamad0/warn)](https://github.com/bilalahamad0/warn/commits/main)
[![US Dashboard](https://img.shields.io/badge/US_dashboard-live-orange)](https://bilalahamad0.github.io/warn/)
[![California Dashboard](https://img.shields.io/badge/California_dashboard-live-orange)](https://bilalahamad0.github.io/warn/ca/)
[![Architecture](https://img.shields.io/badge/architecture-animated-blueviolet)](https://bilalahamad0.github.io/warn/architecture.html)
[![Data Source](https://img.shields.io/badge/source-CA_EDD-blue)](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#-license)

An automated end-to-end pipeline that monitors layoff notices (WARN Act filings) from 45 state workforce agencies plus DC — the 46 sources with `enabled = True` in the `warn_sources/` registry (count them with `len([s for s in warn_sources.SOURCES.values() if s.enabled])`; Missouri and Texas are fully implemented but disabled behind anti-bot walls). It parses historical records, detects changes, generates rich interactive charts, and publishes live dashboards with email alerts. California — the jurisdiction the project started with — keeps a dedicated dashboard with deeper per-notice detail.

---

## 🌐 Live Dashboards

**[→ US Dashboard](https://bilalahamad0.github.io/warn/)** · **[→ California Dashboard](https://bilalahamad0.github.io/warn/ca/)** · **[→ How it works (animated architecture)](https://bilalahamad0.github.io/warn/architecture.html)**

| URL | Serves |
|-----|--------|
| `/warn/` | US national dashboard — all live states, searchable across the whole dataset |
| `/warn/ca/` | California — EDD notices with industry, county and layoff-type filters |
| `/warn/us/` | Redirect to `/warn/` (the US dashboard's address before August 2026) |

The [architecture page](https://bilalahamad0.github.io/warn/architecture.html) is an interactive, animated walk-through of the system: the layered architecture, the 5-stage pipeline (with a play-through), the data-flow / change-detection logic, and the twice-daily CI workflow.

Or embed on any website:
```html
<!-- US dashboard, all states -->
<iframe
  src="https://bilalahamad0.github.io/warn/"
  width="100%" height="800"
  style="border:none;border-radius:12px;"
  title="US WARN Layoff Monitor"
></iframe>

<!-- California only -->
<iframe
  src="https://bilalahamad0.github.io/warn/ca/"
  width="100%" height="800"
  style="border:none;border-radius:12px;"
  title="California WARN Layoff Monitor"
></iframe>
```

---

## 📊 Charts Generated (12)

| # | Chart | Description |
|---|-------|-------------|
| 1 | **Timeline Scatter** | Employees affected by effective date, sized and coloured by county |
| 2 | **Monthly Totals** | Total employees per month with 3-month moving average |
| 3 | **Rolling Trend** | Daily layoffs, 30-day rolling average, and cumulative total |
| 4 | **Top 25 Companies** | Biggest layoffs by cumulative headcount |
| 5 | **County Heatmap** | County × Month heat-intensity matrix |
| 6 | **Treemap** | Proportional breakdown by company and layoff type |
| 7 | **Year-over-Year** | Employees and notices per calendar year (2014-present), derived from the national dataset; years with missing months are hatched, not hidden |
| 8 | **Multi-Year Trend** | Monthly layoff pattern overlaid across all years (2014-present) for seasonal comparison |
| 9 | **Industry Breakdown** | Employees affected by industry sector |
| 10 | **Notice Lead Time** | Days from notice filing to effective date vs the 60-day WARN requirement |
| 11 | **Top Counties** | Top 10 counties by total employees affected |
| 12 | **US Map** | Filterable state choropleth — WARN activity by state, with metric and year toggles |

---

## 🚀 Setup

### 1. Install dependencies
```bash
pip3 install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and add your secrets:
# GH_REPO_TOKEN=your_personal_access_token (repo write scope)
# GMAIL_USER=your_email@gmail.com
# GMAIL_APP_PASSWORD=your_16_char_google_app_password
# NOTIFY_EMAIL=recipient@example.com
# SIGNUP_ENDPOINT=...      (optional — enables the dashboard signup form)
# SUBSCRIBERS_TOKEN=...    (optional — lets the pipeline email subscribers)
```

### 3. Run manually
```bash
# Full pipeline: download → parse → history → diff → charts → build site → notified → push
python3 warn_publish.py

# Build only (no push)
python3 warn_publish.py --no-push

# Force re-download (ignore ETag cache)
python3 warn_publish.py --force

# Update historical data only (parses PDFs from 2014-2024)
python3 warn_history.py
```

### 4. Automated runs

**GitHub Actions (recommended).** The [`monitor.yml`](.github/workflows/monitor.yml)
workflow runs the full pipeline twice daily (00:00 and 12:00 UTC) and on demand
from the **Actions** tab. It runs the test suite, executes `warn_publish.py --no-push`,
then commits any data/chart changes as `"auto: WARN data update [skip ci]"`. Four
companion workflows keep the repo healthy:
[`tests.yml`](.github/workflows/tests.yml) (runs pytest on every pull request),
[`pages.yml`](.github/workflows/pages.yml) (deploys `docs/` to GitHub Pages — see
["Publish"](#5-publish-github-pages) below),
[`codeql.yml`](.github/workflows/codeql.yml) (weekly security scanning) and
[`update-ai-metrics.yml`](.github/workflows/update-ai-metrics.yml) (refreshes
`ai-metrics.json`).

Configure under **Settings ▸ Secrets and variables ▸ Actions**:

| Kind | Name | Purpose |
|------|------|---------|
| Secret | `GH_REPO_TOKEN` | Push the auto-update commits |
| Secret | `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL` | Send alert emails |
| Secret | `SUBSCRIBERS_TOKEN` | Read the subscriber list (matches Apps Script `LIST_TOKEN`) |
| Variable | `SIGNUP_ENDPOINT` | Apps Script `/exec` URL embedded in the signup form |

**Local cron (macOS launchd alternative).** Edit
[`automation/com.bilalahamad.warn.plist`](automation/com.bilalahamad.warn.plist)
first and replace `/ABSOLUTE/PATH/TO/warn` with your checkout path.

```bash
# Copy the launchd plist (runs at 6 AM + 6 PM daily)
cp automation/com.bilalahamad.warn.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bilalahamad.warn.plist

# To stop:
launchctl unload ~/Library/LaunchAgents/com.bilalahamad.warn.plist

# View logs
tail -f data/warn_cron.log data/warn_cron_err.log
```

### 5. Publish (GitHub Pages)

The dashboards are deployed by [`pages.yml`](.github/workflows/pages.yml), which
uploads `docs/` as the Pages artifact and deploys it with `actions/deploy-pages`.
For this to work, **Settings ▸ Pages ▸ Source** must be set to **GitHub Actions**
(not a branch). The workflow runs on three triggers:

- **push to `main` touching `docs/**`** — a human merge that changes the site;
- **`workflow_run`** after a successful "WARN Monitor & Dashboard Update" run —
  the pipeline's `[skip ci]` commits can never fire the push trigger, so this is
  how the twice-daily data updates reach the live site;
- **manually**: **Actions ▸ Deploy Pages ▸ Run workflow** (useful to redeploy
  after a hiccup).

Every pipeline run rewrites `docs/index.html` (US), `docs/ca/index.html`
(California) and both `data.json` files, so the live site follows `main`
automatically.

> **⚠️ API change (August 2026).** `/warn/data.json` used to serve California and now
> serves the national dataset — 47 jurisdictions, ~14 MB, each record carrying a
> `state` field. California moved to
> [`/warn/ca/data.json`](https://bilalahamad0.github.io/warn/ca/data.json). Both
> payloads carry a top-level `"scope"` (`"us"` or `"ca"`) so a client can tell which
> it received. GitHub Pages cannot redirect a JSON file, so this one could not be
> made backward compatible; `/warn/us/data.json` is still published as a
> byte-identical copy of the national payload for anything pinned to the old path.

---

## 📬 Email Signups (optional)

Both dashboards have a **name + email signup form**. Visitors who subscribe get
the same alert email the pipeline sends whenever new WARN notices appear, and the
signup count doubles as a simple user-base metric.

Signing up is **additive**: a returning address keeps every state it already
subscribed to and gains whatever was just picked. Neither form shows you your
current selection, so neither is allowed to replace it. To stop alerts for a
state, use the link at the bottom of any alert email — that page loads your
preferences, shows them, and saves exactly what you confirm.

Because the dashboard is a static GitHub Pages site, signups are collected by a
small **Google Apps Script Web App** that writes to a **Google Sheet** you own
(no third-party service, free). The pipeline reads that list (using a shared
token) and BCCs every subscriber.

**Setup (~10 min):**

1. Create a Google Sheet — this is your subscriber database.
2. In it, open **Extensions ▸ Apps Script**, paste [`automation/subscribe.gs`](automation/subscribe.gs).
3. **Project Settings ▸ Script properties** → add `LIST_TOKEN` = a long random string.
4. **Deploy ▸ New deployment ▸ Web app** → *Execute as: Me*, *Who has access: Anyone* → copy the `/exec` URL.
5. In your GitHub repo settings:
   - **Variables** → add `SIGNUP_ENDPOINT` = the `/exec` URL (public; embedded in the form).
   - **Secrets** → add `SUBSCRIBERS_TOKEN` = the same value as `LIST_TOKEN`.

On the next pipeline run the form goes live and subscribers start receiving alerts.
Until `SIGNUP_ENDPOINT` is set the form shows a "not configured yet" message, so
nothing breaks if you skip this. The subscriber list lives only in your Sheet;
emails are sent BCC so subscribers never see each other.

> **Note:** Gmail limits a single message to ~100 recipients (free) / ~500
> (Workspace) per day, so this suits a modest list. The pipeline batches BCCs to
> stay under the per-message cap.

---

## 🏗 Architecture

```
EDD WARN XLSX  ───► warn_monitor.py ──► data/warn_latest.json
    (ETag cache)          │                      │
                          ▼                      ▼
                  warn_history.py        warn_charts.py
                  (PDF 2014-2024)        (12 Plotly charts)
                          │                      │
                          ▼                      ▼
                  warn_diff.py           docs/charts/*.html
                  (change detect)                │
                          │                      │
                  data/diff_report.md            │
                          └──────┬───────────────┘
                                 ▼
                          warn_publish.py
                          (builds both sites + git push)
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
            docs/index.html  docs/ca/     warn_notify.py
            (US, site root)  index.html   (Email Alerts)
                             (California)
```

### Data files
| File | Description |
|------|-------------|
| `data/warn_latest.json` | Latest parsed active WARN data |
| `data/warn_all_years.json` | Unified historical + live dataset (2014-present) |
| `data/warn_snapshot.json` | Previous run snapshot (for diffing) |
| `data/meta.json` | ETag, hash, last-checked timestamp |
| `data/changelog.jsonl` | Append-only record of every change detected |
| `data/diff_report.md` | Human-readable summary of the latest change |
| `data/charts_manifest.json` | Chart metadata + dataset summary the dashboard reads |
| `data/warn_national.json` | Unified multi-state dataset; also the source the California page is derived from |
| `docs/index.html` | Published US dashboard (site root) |
| `docs/data.json` | Public JSON API — national, `"scope": "us"` |
| `docs/search_index.json` | Compact row index, fetched only on the first search keystroke |
| `docs/pages/<ST>/N.json` | Paged table shards the US dashboard fetches lazily |
| `docs/ca/index.html` | Published California dashboard |
| `docs/ca/data.json` | Public JSON API — California, `"scope": "ca"` |
| `docs/us/index.html` | Redirect stub to the site root (pre-August-2026 US address) |

---

## 📡 Data Source

- **Live XLSX**: [Latest WARN Report](https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx)
- **Parent page**: [CA EDD WARN](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
- Updated by CA EDD multiple times per week

---

## 📄 License

MIT — data is public government information from CA EDD.
