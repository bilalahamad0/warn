# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run full pipeline (download → diff → charts → publish → git push)
python3 warn_publish.py

# Run pipeline without pushing to git
python3 warn_publish.py --no-push

# Force re-download even if ETag unchanged
python3 warn_publish.py --force

# Run individual pipeline stages
python3 warn_monitor.py          # Download + parse XLSX only
python3 warn_diff.py             # Detect changes between runs
python3 warn_charts.py           # Regenerate 8 Plotly charts
python3 warn_history.py          # Re-parse historical PDFs (2014-2024)
python3 warn_notify.py --test    # Send a test email

# Run all tests
pytest -v --cov=.

# Run a single test file
pytest tests/test_monitor.py -v

# Run a single test
pytest tests/test_monitor.py::test_fix_company_name -v

# Lint
flake8 .
```

## Architecture

**Pipeline flow** (orchestrated by `warn_publish.py`):

```
EDD XLSX (online)
  → warn_monitor.py   → data/warn_latest.json, data/meta.json (ETag cache)
  → warn_diff.py      → data/diff_report.md, data/changelog.jsonl
  → warn_history.py   → data/warn_all_years.json (merged 2014-present)
  → warn_charts.py    → docs/charts/*.html (8 self-contained Plotly divs)
  → warn_publish.py   → docs/index.html (GitHub Pages), docs/data.json (public API)
                      → warn_notify.py (Gmail alert if changes detected)
                      → git commit + push
```

**Key data files** (under `data/`):
- `warn_latest.json` — current WARN records from the live XLSX
- `warn_all_years.json` — unified 2014-present dataset (live + historical PDFs)
- `warn_snapshot.json` — previous run state used by `warn_diff.py` for comparison
- `meta.json` — ETag + file hash + timestamps for cache invalidation
- `changelog.jsonl` — append-only log of every detected change

**GitHub Actions** (`.github/workflows/monitor.yml`) runs the full pipeline twice daily (00:00 and 12:00 UTC). Automated commits use `"auto: WARN data update [skip ci]"` to prevent loops.

**Environment** (copy `.env.example` → `.env`):
- `GITHUB_TOKEN` — for git push in local runs
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL` — for email alerts

## Testing

Tests use pytest with fixtures in `tests/conftest.py` (`mock_env`, `mock_data_dir`, `sample_warn_data`). The CI workflow also installs `pytest-mock` and runs `pytest -v --cov=.` before the pipeline step.
