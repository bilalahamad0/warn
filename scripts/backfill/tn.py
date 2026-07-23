"""One-shot backfill: Tennessee historical WARN notices (2012-2023).

Downloads Big Local News' consolidated Tennessee historical CSV from their
public GCS bucket and merges every record dated strictly before the live
store's earliest record (``warn_sources.backfill.live_floor``) into
``data/states/tn/warn_cumulative.json``. Re-running is idempotent —
``merge_records`` dedupes on ``warn_monitor._record_key``.

Source: https://storage.googleapis.com/bln-data-public/warn-layoffs/
tn_historical.csv (Big Local News warn-layoffs project, Apache-2.0).

Field crosswalk and the date/jobs cleaning are ported from Big Local News'
Apache-2.0 warn-transformer (warn_transformer/transformers/tn.py):
company="Company", notice_date="Notice Date", effective_date="Effective
Date", jobs="No. Of Employees" — via the helpers already vendored in
``warn_sources.tn`` (DATE_FORMATS, DATE_CORRECTIONS, JOBS_CORRECTIONS and
the range-splitting fallback that keeps a range's first date). The
historical CSV additionally carries City and Layoff/Closure columns, which
map to the unified ``city`` and ``layoff_type`` fields; "Received Date" and
"Notice ID" have no unified-schema counterpart and are dropped. A trailing
" County" suffix on the county cell (2017-era rows) is stripped so values
match the live feed's bare county names. Dates parse to ISO or become None
— an effective date is never copied from a notice date, or vice versa.

Usage:
    .venv/bin/python scripts/backfill/tn.py
"""

import sys
from pathlib import Path

import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from warn_sources import backfill  # noqa: E402
from warn_sources.tn import _clean_date, _clean_jobs, _normalize_ws  # noqa: E402

CSV_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "tn_historical.csv"
)
RAW_PATH = REPO / "data" / "states" / "tn" / "raw_historical_bln.csv"


def download() -> Path:
    """Fetch the BLN historical CSV into the TN state data dir."""
    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(CSV_URL, timeout=120)
    resp.raise_for_status()
    RAW_PATH.write_bytes(resp.content)
    print(f"Downloaded {len(resp.content)} bytes -> {RAW_PATH}")
    return RAW_PATH


def _opt(value) -> str:
    """Optional text cell -> stripped string ('' when absent)."""
    if value is None or pd.isna(value):
        return ""
    return _normalize_ws(value)


def parse(csv_path: Path) -> tuple[list, int]:
    """CSV -> (unified-schema records, rows dropped for missing company)."""
    df = pd.read_csv(csv_path, dtype=str)
    records, dropped_no_company = [], 0
    for _, row in df.iterrows():
        company = _opt(row["Company"])
        if not company:
            dropped_no_company += 1
            continue
        county = _opt(row["County"])
        if county.endswith(" County"):
            county = county[: -len(" County")].strip()
        records.append(
            {
                "company": company,
                "notice_date": _clean_date(row["Notice Date"]),
                "effective_date": _clean_date(row["Effective Date"]),
                "employees": _clean_jobs(row["No. Of Employees"]),
                "layoff_type": _opt(row["Layoff/Closure"]),
                "county": county,
                "city": _opt(row["City"]),
            }
        )
    return records, dropped_no_company


def main() -> None:
    csv_path = download()
    records, dropped_no_company = parse(csv_path)

    # Drop rows with no parseable event date at all (can't be placed in time),
    # then keep only history strictly before the live store's earliest record
    # — the live feed already covers everything on/after that floor.
    dated = [r for r in records if r["notice_date"] or r["effective_date"]]
    dropped_no_date = len(records) - len(dated)
    floor = backfill.live_floor("tn")
    if floor:
        keep = [
            r for r in dated
            if (r["notice_date"] or r["effective_date"]) < floor
        ]
    else:
        keep = dated
    excluded_overlap = len(dated) - len(keep)

    summary = backfill.merge_records("tn", keep)
    print(
        f"TN backfill: {len(keep)} record(s) merged "
        f"(dropped {dropped_no_company} without company, "
        f"{dropped_no_date} without any date; "
        f"{excluded_overlap} at/after live floor {floor})."
    )
    print(
        f"Cumulative store: {summary['total_records']} records, "
        f"{summary['date_range_start']} .. {summary['date_range_end']}"
    )


if __name__ == "__main__":
    main()
