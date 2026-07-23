#!/usr/bin/env python3
"""Backfill Iowa (IA) historical WARN notices (2011 - May 2018).

Downloads Big Local News' public GCS mirror of Iowa Workforce
Development's pre-2018 WARN log and merges the era the live store does
not already cover into ``data/states/ia/warn_cumulative.json``.

Source (no auth required):
    https://storage.googleapis.com/bln-data-public/warn-layoffs/
    ia_historical_2018.xlsx

Column crosswalk and hand-typed-date corrections are ported from Big
Local News' Apache-2.0 warn-transformer
(warn_transformer/transformers/ia.py) via the copy already vendored in
``warn_sources/ia.py``:

    company        <- "Company"
    address        <- "Address Line 1"
    city           <- "City"
    county         <- "County"
    layoff_type    <- "Notice Type"
    employees      <- "Emp #"          (int, 0 when unpublished)
    notice_date    <- "Notice Date"    (ISO YYYY-MM-DD or None)
    effective_date <- "Layoff Date"    (ISO YYYY-MM-DD or None)

Neither date is ever copied into the other. The historic workbook has
no Industry column, so ``industry`` stays empty; unmapped columns
(State, ZIP) are dropped.

Dedup: only records whose event date (notice_date, else
effective_date) falls strictly before
``warn_sources.backfill.live_floor("ia")`` are merged — the live IA
source (``warn_sources/ia.py``) re-fetches this same BLN archive on
every run, so the cumulative store already covers everything at or
after that boundary. Rows with no parseable date at all are dropped
and reported, never guessed. Merging is idempotent via
``warn_monitor._record_key`` dedup.

Usage:
    .venv/bin/python scripts/backfill/ia.py [--xlsx PATH]
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from warn_sources import backfill  # noqa: E402
from warn_sources.ia import (  # noqa: E402
    HISTORIC_URL,
    USER_AGENT,
    _clean_date,
    _clean_employees,
    _clean_str,
    _extract_workbook_rows,
)

STATE = "ia"


def download(dest_dir: Path) -> Path:
    """Fetch the BLN archive workbook into dest_dir and return its path."""
    dest = dest_dir / "ia_historical_2018.xlsx"
    resp = requests.get(
        HISTORIC_URL, headers={"User-Agent": USER_AGENT}, timeout=60
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    print(f"downloaded {len(resp.content)} bytes -> {dest}")
    return dest


def parse(xlsx_path: Path) -> tuple[list[dict], int]:
    """Workbook -> (unified-schema records, rows dropped for no date)."""
    records, no_date = [], 0
    for raw in _extract_workbook_rows(str(xlsx_path)):
        company = _clean_str(raw.get("company"))
        if not company:
            continue
        rec = {
            "company": company,
            "notice_date": _clean_date(raw.get("notice_date")),
            "effective_date": _clean_date(raw.get("effective_date")),
            "employees": _clean_employees(raw.get("employees")),
            "layoff_type": _clean_str(raw.get("layoff_type")),
            "county": _clean_str(raw.get("county")),
            "city": _clean_str(raw.get("city")),
            "address": _clean_str(raw.get("address")),
        }
        if not (rec["notice_date"] or rec["effective_date"]):
            no_date += 1
            continue
        records.append(rec)
    return records, no_date


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--xlsx",
        type=Path,
        default=None,
        help="use an already-downloaded copy instead of fetching",
    )
    args = ap.parse_args()

    if args.xlsx:
        xlsx = args.xlsx
    else:
        xlsx = download(Path(tempfile.mkdtemp(prefix="warn_ia_backfill_")))

    records, no_date = parse(xlsx)
    print(f"parsed {len(records)} dated record(s); dropped {no_date} undated")

    floor = backfill.live_floor(STATE)
    if floor:
        eligible = [
            r
            for r in records
            if (r["notice_date"] or r["effective_date"]) < floor
        ]
        print(
            f"live floor {floor}: {len(eligible)} record(s) strictly "
            f"before it ({len(records) - len(eligible)} already covered "
            "by the live store)"
        )
    else:
        eligible = records
        print("no live floor: merging everything")

    if eligible:
        summary = backfill.merge_records(STATE, eligible)
    else:
        # Nothing to merge: leave the store untouched, just report it.
        store = (
            REPO_ROOT / "data" / "states" / STATE / "warn_cumulative.json"
        )
        summary = json.loads(store.read_text())
        summary = {k: v for k, v in summary.items() if k != "records"}
        print("nothing to merge; cumulative store left untouched")

    print(
        json.dumps(
            {
                "state": STATE.upper(),
                "merged_candidates": len(eligible),
                "dropped_no_date": no_date,
                "live_floor": floor,
                "total_records": summary["total_records"],
                "date_range_start": summary["date_range_start"],
                "date_range_end": summary["date_range_end"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
