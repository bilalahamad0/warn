"""
scripts/backfill/or.py
----------------------
One-shot, re-runnable backfill of HISTORICAL Oregon WARN notices into
``data/states/or/warn_cumulative.json``.

Source: Big Local News' public warn-layoffs archive (no auth required):
    https://storage.googleapis.com/bln-data-public/warn-layoffs/or_historical.xlsx
a static 1988-2021 export of the Oregon CCWD WARN log that predates the
live feed's rolling ten-year window.

Column crosswalk is ported from Big Local News' Apache-2.0
warn-transformer project (warn_transformer/transformers/or.py):
company=Company Name, city=Location, notice_date=Received Date,
effective_date=Layoff Date, employees=Laid Off — plus Layoff Type,
which the workbook also carries. The "1899-12-29" Excel-epoch sentinel
in date columns maps to None (BLN date_corrections). Oregon publishes
no county, address, or industry, so those fields are omitted — never
fabricated. Workbook layout knowledge (two title rows, headers on row
three) is reused from ``warn_sources/or.py``, itself vendored from
BLN's Apache-2.0 warn-scraper.

NOTE: the live source ``warn_sources/or.py`` already fetches and merges
this very workbook into the state's store, so ``live_floor`` normally
equals the workbook's earliest date (1988-11-28) and every row lands in
the excluded overlap window — this script then merges nothing, by
design. It only adds records if the live store is ever reset or starts
from the bare rolling window.

Dedup rule: only records whose event date (notice_date, else
effective_date) falls strictly BEFORE ``warn_sources.backfill.live_floor``
are merged — the live store owns everything at/after its floor. If the
floor is None every dated record merges. Re-running is idempotent via
``warn_monitor._record_key`` dedup.

Usage:
    .venv/bin/python scripts/backfill/or.py [--download-dir DIR]
"""

import argparse
import importlib
import json
import logging
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import warn_monitor  # noqa: E402
from warn_sources import backfill  # noqa: E402

# "or" is a Python keyword, so the source module needs importlib.
_or_mod = importlib.import_module("warn_sources.or")

log = logging.getLogger("backfill.or")

STATE = "or"
SOURCE_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "or_historical.xlsx"
)


def download(dest_dir: Path) -> Path:
    """Fetch the BLN historical XLSX (reused if already downloaded)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "or_historical.xlsx"
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"Reusing existing download at {dest}")
        return dest
    log.info(f"Downloading {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"PK"):
        raise RuntimeError("Downloaded payload is not an XLSX (no PK magic)")
    dest.write_bytes(data)
    log.info(f"Saved {len(data)} bytes to {dest}")
    return dest


def parse(xlsx_path: Path) -> list:
    """XLSX -> records in the unified schema.

    Only fields the data really has are emitted; dates go out as ISO or
    None (one date is NEVER copied into the other), employees as int
    with 0 meaning unknown.
    """
    records = []
    for row in _or_mod._extract_rows(xlsx_path):
        company = re.sub(r"\s+", " ", row.get("Company Name", "")).strip()
        if not company:
            continue  # company is required
        employees = warn_monitor._safe_int(row.get("Laid Off", ""))
        records.append(
            {
                "company": company,
                "notice_date": _or_mod.OregonCCWD._clean_date(
                    row.get("Received Date")
                ),
                "effective_date": _or_mod.OregonCCWD._clean_date(
                    row.get("Layoff Date")
                ),
                "employees": employees if employees is not None else 0,
                "layoff_type": row.get("Layoff Type", "").strip(),
                # "Location" is a city; OR publishes no county/address/
                # industry.
                "city": row.get("Location", "").strip().strip(","),
            }
        )
    return records


def _event_date(rec: dict):
    """The date that situates a record in time: notice, else effective."""
    return rec.get("notice_date") or rec.get("effective_date")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    ap.add_argument(
        "--download-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "warn_backfill",
        help="where to cache the downloaded XLSX",
    )
    args = ap.parse_args()

    records = parse(download(args.download_dir))

    undated = [r for r in records if not _event_date(r)]
    records = [r for r in records if _event_date(r)]

    floor = backfill.live_floor(STATE)
    if floor is None:
        log.info("No live store for OR — merging every dated record.")
        eligible, overlap = records, []
    else:
        eligible = [r for r in records if _event_date(r) < floor]
        overlap = [r for r in records if _event_date(r) >= floor]
        log.info(
            f"Live floor {floor}: keeping {len(eligible)} record(s) before "
            f"it, excluding {len(overlap)} at/after it."
        )

    if undated:
        log.info(f"Dropped {len(undated)} record(s) with no parseable date.")

    summary = backfill.merge_records(STATE, eligible)
    result = {
        "state": STATE.upper(),
        "eligible": len(eligible),
        "excluded_overlap": len(overlap),
        "dropped_undated": len(undated),
        "live_floor": floor,
        "cumulative_total": summary["total_records"],
        "date_range_start": summary["date_range_start"],
        "date_range_end": summary["date_range_end"],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
