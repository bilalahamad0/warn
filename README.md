# 📋 California WARN Layoff Monitor

[![WARN Monitor](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml/badge.svg)](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml)
[![CodeQL](https://github.com/bilalahamad0/warn/actions/workflows/codeql.yml/badge.svg)](https://github.com/bilalahamad0/warn/actions/workflows/codeql.yml)
[![Auto-Update](https://img.shields.io/badge/updates-twice_daily-brightgreen)](https://github.com/bilalahamad0/warn/actions/workflows/monitor.yml)
[![Last Commit](https://img.shields.io/github/last-commit/bilalahamad0/warn)](https://github.com/bilalahamad0/warn/commits/main)
[![Live Dashboard](https://img.shields.io/badge/dashboard-live-orange)](https://bilalahamad0.github.io/warn/)
[![Data Source](https://img.shields.io/badge/source-CA_EDD-blue)](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#-license)

An automated end-to-end pipeline that monitors California layoff notices (WARN Act filings) from the CA Employment Development Department, parses historical records (2014-present), detects changes, generates rich interactive charts, and publishes a live dashboard with email alerts.

---

## 🌐 Live Dashboard

**[→ View Dashboard](https://bilalahamad0.github.io/warn/)**

Or embed on any website:
```html
<iframe
  src="https://bilalahamad0.github.io/warn/"
  width="100%" height="800"
  style="border:none;border-radius:12px;"
  title="California WARN Layoff Monitor"
></iframe>
```

---

## 📊 Charts Generated (11)

| # | Chart | Description |
|---|-------|-------------|
| 1 | **Timeline Scatter** | Employees affected by effective date, sized and coloured by county |
| 2 | **Monthly Totals** | Total employees per month with 3-month moving average |
| 3 | **Rolling Trend** | Daily layoffs, 30-day rolling average, and cumulative total |
| 4 | **Top 25 Companies** | Biggest layoffs by cumulative headcount |
| 5 | **County Heatmap** | County × Month heat-intensity matrix |
| 6 | **Treemap** | Proportional breakdown by company and layoff type |
| 7 | **Year-over-Year** | **Historical** annual employees and notice count (2014-present) |
| 8 | **Multi-Year Trend** | **Historical** seasonal overlay of monthly layoffs across all years |
| 9 | **Industry Breakdown** | Employees affected by industry sector |
| 10 | **Notice Lead Time** | Days from notice filing to effective date vs the 60-day WARN requirement |
| 11 | **Top Counties** | Top 10 counties by total employees affected |

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
# GITHUB_TOKEN=your_personal_access_token (repo write scope)
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
then commits any data/chart changes as `"auto: WARN data update [skip ci]"`. Two
companion workflows keep the repo healthy:
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

The dashboard is served from GitHub Pages — **Settings ▸ Pages**, Branch: `main`,
Folder: `/docs`. Every pipeline run rewrites `docs/index.html` and `docs/data.json`,
so the live site updates automatically once Pages is enabled.

---

## 📬 Email Signups (optional)

The dashboard has a **name + email signup form**. Visitors who subscribe get the
same alert email the pipeline sends whenever new WARN notices appear, and the
signup count doubles as a simple user-base metric.

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
                  (PDF 2014-2024)        (11 Plotly charts)
                          │                      │
                          ▼                      ▼
                  warn_diff.py           docs/charts/*.html
                  (change detect)                │
                          │                      │
                  data/diff_report.md            │
                          └──────┬───────────────┘
                                 ▼
                          warn_publish.py
                          (builds index.html + git push)
                                 │
                          ┌──────┴──────┐
                          ▼             ▼
                   docs/index.html    warn_notify.py
                   (GitHub Pages)     (Email Alerts)
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
| `docs/index.html` | Published premium interactive dashboard |
| `docs/data.json` | Publicly accessible JSON API of current notices |

---

## 📡 Data Source

- **Live XLSX**: [Latest WARN Report](https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx)
- **Parent page**: [CA EDD WARN](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
- Updated by CA EDD multiple times per week

---

## 📄 License

MIT — data is public government information from CA EDD.
