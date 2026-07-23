"""
Backfill Georgia historical WARN notices (1989-2023).

Downloads Big Local News' public Georgia historical CSV, maps it onto
the unified schema, and merges only records dated strictly before the
live store's earliest record (``warn_sources.backfill.live_floor``) into
``data/states/ga/warn_cumulative.json``.

Column crosswalk ported from Big Local News' warn-transformer
(warn_transformer/transformers/ga.py, Apache-2.0) and this repo's
``warn_sources/ga.py``:

    Company Name    -> company
    Separation Date -> effective_date  (%m/%d/%Y -> ISO)
    Est. Impact     -> employees       (int, 0 if unknown)
    County          -> county          (" County" suffix stripped)
    City            -> city

Georgia publishes NO notice date, so ``notice_date`` is always None —
never synthesized from the separation date. ID, ZIP and LWDA have no
place in the unified schema and are dropped. The historical CSV carries
no layoff type, address or industry.

Note: ``warn_sources/ga.py`` already folds this same BLN CSV into the
live GA feed, so under normal circumstances the live floor equals the
CSV's earliest date and this backfill is a no-op. It remains useful as a
reproducible record and as a repair path should the live store ever be
rebuilt without the historical era.

Usage:
    .venv/bin/python scripts/backfill/ga.py [--cache-dir DIR]
"""

import argparse
import csv
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from warn_sources import backfill  # noqa: E402

HISTORICAL_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "ga_historical.csv"
)


def _squish(val) -> str:
    """Whitespace-normalized string ('' for None)."""
    return re.sub(r"\s+", " ", str(val or "")).strip()


def _clean_date(val) -> Optional[str]:
    """'%m/%d/%Y' (BLN's GA date_format) -> ISO; unparseable -> None."""
    text = _squish(val)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _clean_county(val) -> str:
    """Strip any ' County' suffix so both eras aggregate together."""
    return re.sub(r"\s+county$", "", _squish(val), flags=re.I)


def download(cache_dir: Path) -> Path:
    """Fetch the BLN historical CSV (cached; re-run friendly)."""
    import requests

    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "ga_historical.csv"
    if not dest.exists():
        resp = requests.get(HISTORICAL_URL, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def parse(csv_path: Path) -> tuple:
    """CSV -> (unified-schema records, rows dropped for no date)."""
    records, dropped_no_date = [], 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            company = _squish(row.get("Company Name"))
            if not company:
                continue
            effective = _clean_date(row.get("Separation Date"))
            if effective is None:
                dropped_no_date += 1  # no parseable event date at all
                continue
            records.append(
                {
                    "company": company,
                    # GA publishes no notice date; never synthesized.
                    "notice_date": None,
                    "effective_date": effective,
                    "employees": row.get("Est. Impact"),
                    "county": _clean_county(row.get("County")),
                    "city": _squish(row.get("City")),
                }
            )
    return records, dropped_no_date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "warn_backfill_ga",
        help="where the downloaded CSV is cached",
    )
    args = parser.parse_args()

    csv_path = download(args.cache_dir)
    records, dropped_no_date = parse(csv_path)
    print(f"parsed {len(records)} dated record(s); "
          f"dropped {dropped_no_date} with no parseable date")

    floor = backfill.live_floor("ga")
    if floor is not None:
        eligible = [r for r in records if r["effective_date"] < floor]
        print(f"live floor {floor}: {len(eligible)} record(s) strictly "
              f"before it ({len(records) - len(eligible)} excluded as "
              f"already covered by the live store)")
    else:
        eligible = records
        print("no live floor: merging everything")

    summary = backfill.merge_records("ga", eligible)
    print(f"cumulative now {summary['total_records']} record(s), "
          f"{summary['date_range_start']} .. {summary['date_range_end']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
