#!/usr/bin/env python
"""Backfill historical Ohio WARN notices from Big Local News snapshots.

Downloads BLN's public Ohio snapshots — ``oh_historical.csv`` (2017-2022)
and ``oh_2015-2022.zip``, whose bundled ``oh.csv`` extends coverage back to
2015 — maps them onto the unified schema, and merges only records whose
event date (notice date, else effective date) falls strictly before the
live store's floor (``warn_sources.backfill.live_floor``), so the era the
live feed already covers is never double-counted. The CSV is preferred;
the zip contributes only rows the CSV lacks (dedup on
``warn_monitor._record_key``).

Field crosswalk and date/jobs cleaning are ported from Big Local News'
Apache-2.0 warn-scraper / warn-transformer (transformers/oh.py), as
vendored in ``warn_sources.oh``: company="Company", notice_date="Date
Received" (historical header "DateReceived"), effective_date=first date of
"Layoff Date(s)", employees="Potential Number Affected", and the single
"City/County" string split into city and county (unsplittable multi-site
or "Statewide" values stay whole in ``city``; county is never guessed).
Ohio publishes no address or industry, and these historical rows lack the
current feed's "Layoff/Closure" column, so those unified fields are
omitted — never fabricated.

Usage:
    .venv/bin/python scripts/backfill/oh.py [--cache-dir DIR]

Re-running is idempotent: merge_records dedups on warn_monitor._record_key.
"""

import argparse
import csv
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import warn_monitor  # noqa: E402
from warn_sources import get_source  # noqa: E402
from warn_sources.backfill import live_floor, merge_records  # noqa: E402
from warn_sources.oh import (  # noqa: E402
    _HIST_LOOKUP,
    _JUNK_COMPANIES,
    _clean_date,
    _clean_jobs,
    _split_location,
)

BLN_BASE = "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
CSV_URL = BLN_BASE + "oh_historical.csv"
ZIP_URL = BLN_BASE + "oh_2015-2022.zip"
ZIP_MEMBER = "oh.csv"  # 2015-2023 superset bundled inside the zip


def _download(url: str, dest: Path) -> Path:
    """Fetch ``url`` to ``dest`` once; reuse the cached copy on re-runs."""
    if not (dest.exists() and dest.stat().st_size):
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def _to_record(row: dict) -> Optional[dict]:
    """One raw historical row -> unified-schema record (None if junk)."""
    row = {new: row.get(old, "") for old, new in _HIST_LOOKUP.items()}
    company = str(row.get("Company") or "").strip()
    if not company or company.lower() in _JUNK_COMPANIES:
        return None
    city, county = _split_location(row.get("City/County"))
    emp = _clean_jobs(row.get("Potential Number Affected"))
    return {
        "company": company,
        "notice_date": _clean_date(row.get("Date Received")),
        "effective_date": _clean_date(row.get("Layoff Date(s)")),
        "employees": emp if emp is not None else 0,
        "county": county,
        "city": city,
    }


def _records_from_csv_text(text: str) -> list:
    """CSV text (historical header set) -> list of unified records."""
    rows = csv.DictReader(io.StringIO(text))
    return [r for r in map(_to_record, rows) if r is not None]


def _cumulative_count() -> int:
    path = get_source("oh").paths.cumulative
    if not path.exists():
        return 0
    return len(json.loads(path.read_text()).get("records", []))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--cache-dir",
        default=None,
        help="where source downloads are cached "
        "(default: <tmp>/warn_backfill_oh)",
    )
    args = ap.parse_args()
    cache = (
        Path(args.cache_dir)
        if args.cache_dir
        else Path(tempfile.gettempdir()) / "warn_backfill_oh"
    )
    cache.mkdir(parents=True, exist_ok=True)

    csv_path = _download(CSV_URL, cache / "oh_historical.csv")
    zip_path = _download(ZIP_URL, cache / "oh_2015-2022.zip")

    primary = _records_from_csv_text(csv_path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(zip_path) as zf:
        zip_text = zf.read(ZIP_MEMBER).decode("utf-8")
    secondary = _records_from_csv_text(zip_text)

    # Prefer the CSV; take zip rows only when the CSV lacks them.
    seen = {warn_monitor._record_key(r) for r in primary}
    combined = list(primary)
    zip_only = 0
    for r in secondary:
        key = warn_monitor._record_key(r)
        if key not in seen:
            seen.add(key)
            combined.append(r)
            zip_only += 1

    floor = live_floor("oh")
    kept, no_date, overlap = [], 0, 0
    for r in combined:
        event = r["notice_date"] or r["effective_date"]
        if not event:
            no_date += 1  # no parseable date at all: drop, never guess
        elif floor is None or event < floor:
            kept.append(r)
        else:
            overlap += 1  # live store already covers this era

    before = _cumulative_count()
    summary = merge_records("oh", kept)
    after = summary["total_records"]

    print(f"live floor:            {floor}")
    print(f"csv rows parsed:       {len(primary)}")
    print(f"zip-only rows added:   {zip_only} (of {len(secondary)} in zip)")
    print(f"dropped (no date):     {no_date}")
    print(f"excluded (>= floor):   {overlap}")
    print(f"merged candidates:     {len(kept)}")
    print(f"records added:         {after - before}")
    print(f"cumulative total:      {after}")
    print(f"date range:            {summary['date_range_start']}"
          f" .. {summary['date_range_end']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
