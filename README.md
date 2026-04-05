# 📋 California WARN Layoff Monitor

[![Auto-Update](https://img.shields.io/badge/updates-twice_daily-brightgreen)](https://github.com/bilalahamad0/warn)
[![Data Source](https://img.shields.io/badge/source-CA_EDD-blue)](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
[![Live Dashboard](https://img.shields.io/badge/dashboard-live-orange)](https://bilalahamad0.github.io/warn/)

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

## 📊 Charts Generated

| # | Chart | Description |
|---|-------|-------------|
| 1 | **Timeline Scatter** | Employees affected by effective date, coloured by county |
| 2 | **Monthly Bar + MA** | Total employees per month with 3-month moving average |
| 3 | **Rolling Trend** | Daily, 30-day rolling average, and cumulative total |
| 4 | **Top 25 Companies** | Biggest layoffs by total headcount |
| 5 | **County Heatmap** | County × Month heat intensity matrix |
| 6 | **Treemap** | Proportional breakdown by company and layoff type |
| 7 | **Year-over-Year** | **Historical** annual employees and notice count (2014-present) |
| 8 | **Multi-Year Trend** | **Historical** seasonal overlay of monthly layoffs across all years |

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

### 4. Enable automated runs (macOS)

```bash
# Copy the launchd plist
cp automation/com.bilalahamad.warn.plist ~/Library/LaunchAgents/

# Load it (runs at 6 AM + 6 PM daily)
launchctl load ~/Library/LaunchAgents/com.bilalahamad.warn.plist

# To stop:
launchctl unload ~/Library/LaunchAgents/com.bilalahamad.warn.plist

# View logs
tail -f data/warn_cron.log
```

---

## 🏗 Architecture

```
EDD WARN XLSX  ───► warn_monitor.py ──► data/warn_latest.json
    (ETag cache)          │                      │
                          ▼                      ▼
                  warn_history.py        warn_charts.py
                  (PDF 2014-2024)        (8 Plotly charts)
                          │                      │
                          ▼                      ▼
                  warn_diff.py           output/charts/*.html
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
                  output/index.html   warn_notify.py
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
| `output/index.html` | Published premium interactive dashboard |
| `output/data.json` | Publicly accessible JSON API of current notices |

---

## 📡 Data Source

- **Live XLSX**: [Latest WARN Report](https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx)
- **Parent page**: [CA EDD WARN](https://edd.ca.gov/en/jobs_and_training/layoff_services_warn)
- Updated by CA EDD multiple times per week

---

## 📄 License

MIT — data is public government information from CA EDD.
