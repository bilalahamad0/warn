"""
scripts/backfill/mi.py
----------------------
One-shot historical backfill for Michigan.

Downloads Big Local News' public archive of the pre-2025-11-25 Michigan LEO
WARN site (``mi-before-20251125.zip`` in the ``bln-data-public`` GCS bucket)
and merges its records into ``data/states/mi/warn_cumulative.json`` via
``warn_sources.backfill.merge_records``.

Source of record inside the zip: the ``mi-old`` sheet of
``mi-reconcile1.xlsx`` — the old site's tabular archive (2016-2025) with
columns ``Company Name, City, Date Received, Incident Type, Number of
Layoffs``. That sheet is byte-for-byte the same data as the bundled
``consolidated-michigan.csv`` (732 of 734 rows match; the extras are exact
duplicates / an encoding artifact) but keeps the state's own "Incident Type"
wording for ``layoff_type``.

Deliberately NOT merged, to avoid double-counting the same filings:

* ``mi-new`` / ``newclean`` sheets — the new-site scrape keyed by *effective*
  date. The same layoffs appear in ``mi-old`` keyed by *notice* date, and
  there is no reliable join key, so importing both would duplicate events.
* ``integrated-michigan.csv`` — BLN's cross-feed reconciliation; it retains
  the same filing under both feeds' company spellings (e.g. "Webasto Roof
  Systems" and "WEBASTO CONVERTIBLES USA INC" for one event).

Field crosswalk (per BLN warn-transformer ``transformers/mi.py``):

* company     <- ``Company Name``
* city        <- ``City`` (BLN ``location``)
* notice_date <- ``Date Received`` (the old feed's only date; the *new* feed's
  ``date_start`` is an effective date and is a different concept — neither is
  ever copied into the other)
* employees   <- ``Number of Layoffs`` (BLN ``jobs``; 0 when unreported)
* layoff_type <- ``Incident Type``

``effective_date``, ``county``, ``address`` and ``industry`` are not present
in the old feed and are left unset — never synthesized.

The jobs corrections table and the count/date parsing rules are ported from
Big Local News' Apache-2.0 warn-transformer
(warn_transformer/transformers/mi.py); the mojibake fix map follows the
TEXTFIXES table in BLN's warn-scraper (warn/scrapers/mi.py).

Dedup guard: only records whose event date (notice_date, else
effective_date) is strictly before ``warn_sources.backfill.live_floor("mi")``
are merged — everything on/after that boundary is already covered by the
live store. Re-running is idempotent (``warn_monitor._record_key`` dedup).

Usage:
    .venv/bin/python scripts/backfill/mi.py [--workdir DIR]
"""

import argparse
import json
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from warn_sources import backfill, get_source  # noqa: E402

ZIP_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "mi-before-20251125.zip"
)
ZIP_NAME = "mi-before-20251125.zip"
XLSX_NAME = "mi-reconcile1.xlsx"
SHEET = "mi-old"

# Mojibake / unicode cleanup, after BLN warn-scraper TEXTFIXES ("â€™" is the
# cp1252 mis-decode of a right single quote as stored in the sheet itself).
TEXTFIXES = {
    "â€™": "'",
    "’": "'",
    "–": "--",
    "—": "--",
}

# Vendored from BLN warn-transformer transformers/mi.py jobs_corrections
# (only the keys that occur in the mi-old sheet).
JOBS_CORRECTIONS = {
    "80*": 80,
    "Unreported": None,
    "Unknown": None,
}

_INT_RE = re.compile(r"\d[\d,]*")
MAXIMUM_JOBS = 10000  # BLN sanity guard


def _clean_text(value) -> str:
    s = str(value)
    for bad, good in TEXTFIXES.items():
        s = s.replace(bad, good)
    return " ".join(s.split())


def _transform_jobs(value) -> int:
    """Worker-count cell -> int; 0 when the state published no count."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    raw = _clean_text(value)
    if raw in JOBS_CORRECTIONS:
        corrected = JOBS_CORRECTIONS[raw]
        return int(corrected) if corrected is not None else 0
    m = _INT_RE.search(raw)
    if not m:
        return 0
    n = int(m.group(0).replace(",", ""))
    if n < 0 or n > MAXIMUM_JOBS:
        return 0
    return n


def download(workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / ZIP_NAME
    if not dest.exists() or dest.stat().st_size == 0:
        print(f"downloading {ZIP_URL} -> {dest}")
        with urllib.request.urlopen(ZIP_URL, timeout=120) as resp:
            dest.write_bytes(resp.read())
    return dest


def parse(zip_path: Path) -> list:
    """Zip archive -> list of unified-schema record dicts (may be undated)."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(XLSX_NAME) as fh:
            df = pd.read_excel(fh, sheet_name=SHEET)
    records = []
    for row in df.to_dict("records"):
        company = _clean_text(row.get("Company Name", ""))
        if not company or company.lower() == "nan":
            continue
        received = row.get("Date Received")
        notice = None
        if received is not None and not pd.isna(received):
            notice = pd.Timestamp(received).strftime("%Y-%m-%d")
        city = _clean_text(row.get("City", ""))
        records.append(
            {
                "company": company,
                "notice_date": notice,
                "effective_date": None,  # not published by the old feed
                "employees": _transform_jobs(row.get("Number of Layoffs")),
                "layoff_type": _clean_text(row.get("Incident Type", "")),
                "city": "" if city.lower() == "nan" else city,
            }
        )
    return records


def main() -> int:
    default_dir = Path(tempfile.gettempdir()) / "warn-backfill-mi"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workdir",
        type=Path,
        default=default_dir,
        help="download/cache directory (default: %(default)s)",
    )
    args = ap.parse_args()

    records = parse(download(args.workdir))
    total = len(records)

    undated = [r for r in records if not (r["notice_date"] or r["effective_date"])]
    records = [r for r in records if r["notice_date"] or r["effective_date"]]

    floor = backfill.live_floor("mi")
    if floor:
        kept = [
            r
            for r in records
            if (r["notice_date"] or r["effective_date"]) < floor
        ]
    else:
        kept = records
    excluded = len(records) - len(kept)

    store = get_source("mi").paths.cumulative
    before = (
        len(json.loads(store.read_text()).get("records", []))
        if store.exists()
        else 0
    )
    summary = backfill.merge_records("mi", kept)
    after = summary["total_records"]

    print(
        json.dumps(
            {
                "rows_parsed": total,
                "dropped_undated": len(undated),
                "excluded_on_or_after_live_floor": excluded,
                "live_floor": floor,
                "merged_candidates": len(kept),
                "records_added": after - before,
                "cumulative_total": after,
                "date_range_start": summary.get("date_range_start"),
                "date_range_end": summary.get("date_range_end"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
