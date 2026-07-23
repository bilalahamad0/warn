"""
scripts/backfill/tx.py
----------------------
One-shot, re-runnable backfill of HISTORICAL Texas WARN notices into
``data/states/tx/warn_cumulative.json``.

Source: Big Local News' public warn-layoffs archive (no auth required):
    https://storage.googleapis.com/bln-data-public/warn-layoffs/tx_historical.xlsx
a single-sheet export of the Texas Workforce Commission's WARN log covering
1999-2019 — the era predating the per-year XLSX files that the (currently
WAF-blocked) live source ``warn_sources/tx.py`` would fetch.

Column crosswalk and date corrections are ported from Big Local News'
Apache-2.0 warn-transformer project (warn_transformer/transformers/tx.py):
company=JOB_SITE_NAME, city=CITY_NAME, notice_date=NOTICE_DATE,
effective_date=LayOff_Date, employees=TOTAL_LAYOFF_NUMBER — plus
COUNTY_NAME, which TWC also publishes. The historical file's remaining
columns (LAYOFF_REASON_DESCRIPTION, WDA/SSA identifiers, visit tracking,
Temporary_Layoff_Flag, …) carry agency-internal or reason/flag data that
does not match the unified layoff_type/address/industry semantics, so they
are omitted — never fabricated (same policy as the live TX source).

Dedup rule: only records whose event date (notice_date, else
effective_date) falls strictly BEFORE ``warn_sources.backfill.live_floor``
are merged — the live store owns everything at/after its floor. While TX
has no live store the floor is None and every dated record merges.
Re-running is idempotent via ``warn_monitor._record_key`` dedup.

Usage:
    .venv/bin/python scripts/backfill/tx.py [--download-dir DIR]
"""

import argparse
import json
import logging
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

import warn_monitor  # noqa: E402
from warn_sources import backfill  # noqa: E402
from warn_sources.tx import DATE_CORRECTIONS, _FIELD_MAP, _norm  # noqa: E402

log = logging.getLogger("backfill.tx")

STATE = "tx"
SOURCE_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "tx_historical.xlsx"
)

# TWC uses these strings for company cells that are really junk/header rows.
_JUNK_COMPANIES = {"jobsitename", "nan", "none", "total"}


def download(dest_dir: Path) -> Path:
    """Fetch the BLN historical XLSX (reused if already downloaded)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "tx_historical.xlsx"
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


def parse(xlsx_path: Path) -> tuple:
    """XLSX -> (records in the unified schema, junk-row count).

    Only fields the data really has are emitted; dates go out as ISO or
    None (one date is NEVER copied into the other), employees as int with
    0 meaning unknown.
    """
    raw = pd.read_excel(xlsx_path, dtype=object)

    cols = {}
    for c in raw.columns:
        field = _FIELD_MAP.get(_norm(c))
        if field and field not in cols:
            cols[field] = c
    missing = {"company", "notice_date", "effective_date"} - set(cols)
    if missing:
        raise ValueError(f"TX historical file lacks columns for: {missing}")

    records, junk = [], 0
    for _, r in raw.iterrows():
        val = r.get(cols["company"])
        company = "" if pd.isna(val) else str(val).strip()
        if not company or _norm(company) in _JUNK_COMPANIES:
            junk += 1
            continue
        rec = {"company": company}
        for field in ("notice_date", "effective_date"):
            iso = warn_monitor._safe_date(r.get(cols[field]))
            # BLN-documented feed typos (e.g. 2027-03-01 -> 2017-03-01).
            rec[field] = DATE_CORRECTIONS.get(iso, iso)
        emp = warn_monitor._safe_int(r.get(cols["employees"]))
        rec["employees"] = emp if emp is not None else 0
        for field in ("county", "city"):
            if field in cols:
                val = r.get(cols[field])
                rec[field] = "" if pd.isna(val) else str(val).strip()
        records.append(rec)
    return records, junk


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

    records, junk = parse(download(args.download_dir))

    undated = [r for r in records if not _event_date(r)]
    records = [r for r in records if _event_date(r)]

    floor = backfill.live_floor(STATE)
    if floor is None:
        log.info("No live store for TX — merging every dated record.")
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
    if junk:
        log.info(f"Skipped {junk} junk/blank company row(s).")

    summary = backfill.merge_records(STATE, eligible)
    result = {
        "state": STATE.upper(),
        "eligible": len(eligible),
        "excluded_overlap": len(overlap),
        "dropped_undated": len(undated),
        "junk_rows": junk,
        "live_floor": floor,
        "cumulative_total": summary["total_records"],
        "date_range_start": summary["date_range_start"],
        "date_range_end": summary["date_range_end"],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
