#!/usr/bin/env python3
"""Backfill historical Kentucky WARN notices (1998-2016).

Downloads Big Local News' historical normalized CSV for Kentucky from
their public GCS bucket, maps it onto the tracker's unified schema, and
merges only records dated strictly before the live store's earliest
event date (``warn_sources.backfill.live_floor``), so the era the live
feed already covers (2017-present) is never double-counted. Merging goes
through ``warn_sources.backfill.merge_records``, which dedupes on
``warn_monitor._record_key`` — re-running this script is idempotent.

Attribution: the date and employee-count corrections below, and the
field-map quirk, are ported from Big Local News' Apache-2.0 licensed
warn-transformer project (warn_transformer/transformers/ky.py). Kentucky's
date columns are semantically swapped in the unified schema, exactly as in
BLN's transformer and this repo's live source (warn_sources/ky.py):
``notice_date`` maps from "Projected Date" (date_effective) and
``effective_date`` maps from "Date Received" (date_received). Neither
date is ever copied into the other.

Usage:
    .venv/bin/python scripts/backfill/ky.py
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from warn_sources import backfill, get_source  # noqa: E402
from warn_sources.ky import (  # noqa: E402
    _clean_date,
    _clean_employees,
    _clean_naics,
    _clean_str,
)

STATE = "ky"
CSV_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "ky-historical-normalized.csv"
)

# Messy free-text date fixes ported verbatim from BLN warn-transformer
# (transformers/ky.py ``date_corrections``; datetimes rendered as ISO).
# Lookup happens on the stripped raw cell, so BLN's whitespace-padded
# duplicate keys were dropped — including the padded
# "03/19/2012 - 04/01/2012 " -> 2019-03-19 entry, an evident typo whose
# unpadded twin correctly maps to 2012-03-19. ``None`` means the value is
# known junk with no recoverable date.
DATE_CORRECTIONS = {
    "43490.0": None,
    "N/A": None,
    "November": None,
    "43735.0": None,
    "43490": "2019-01-25",
    "43735": "2019-09-27",
    "01/03/2002 - 04/15/2002": "2002-01-03",
    "02/01/2002 - 09/01/2002": "2002-02-01",
    "08/20/2001 - 02/28/2002": "2001-08-20",
    "Unknown": None,
    "12/29/2002 - 01/26/2003": "2002-12-29",
    "11/18/2007 - 12/16/2007": "2007-11-18",
    "12/23/2007 - 02/29/2008": "2007-12-23",
    "11/26/2007 - 12/01/2007": "2007-11-26",
    "Mid-January 2009": None,
    "01/17/2009 - 01/18/2009": "2009-01-17",
    "01/09/2009 - 01/23/2009": "2009-01-09",
    "November and December 2008": None,
    "01/01/2009 - 01/03/2009": "2009-01-01",
    "10/16/2008 - 12/14/2008": "2008-10-16",
    "10/17/2008 - 10/31/2008": "2008-10-17",
    "On or around 11/10/2008": "2008-11-10",
    "On or around 07/27/2009": "2009-07-27",
    "On or around 10/31/2008": "2008-10-31",
    "10/13/2008 - 10/31/2008": "2008-10-13",
    "10/01/2008 - 10/15/2008": "2008-10-01",
    "09/30/2008 - 03/31/2009": "2008-09-30",
    "On or around 07/14/2008 & 07/27/2008": "2008-07-14",
    "10/06/2008, 11/03/2008, 12/03/2008": "2008-10-06",
    "09/10/208": "2008-09-10",
    "?": None,
    "07/28/2008 - 09/30/2008": "2008-07-28",
    "06/14/2008 - 06/28/2008": "2008-06-14",
    "On or around 05/31/2008": "2008-05-31",
    "12/31/2007 - 02/2008": "2008-12-31",
    "01/02/2009 - 05/01/2009": "2009-01-02",
    "01/01/2010 - 09/30/2010": "2010-01-01",
    "12/26/2009 - 01/08/2010": "2009-12-26",
    "06/21/2010 - 07/04/2010": "2010-06-21",
    "11/20/2009 - 11/27/2009": "2009-11-20",
    "11/30/2009 - 12/14/2009": "2009-11-30",
    "07/31/2009 - 09/04/2009": "2009-07-31",
    "08/14/2009 - 10/10/2009": "2009-08-14",
    "09/30/09 +/-": "2009-09-30",
    "07/28/2009 - 08/11/2009": "2009-07-28",
    "07/01/2009 - 09/14/2009": "2009-07-01",
    "05/25/2009 - 06/30/2009": "2009-05-25",
    "04/01/2009 - 09/30/2009": "2009-04-01",
    "05/30/2009 - 06/13/2009": "2009-05-30",
    "05/31/2009 - 10/31/2009": "2009-05-31",
    "04/20/2009 - 07/15/2009": "2009-04-20",
    "01/30/2009 - 02/13/2009": "2009-01-30",
    "01/14/2011 - 04/01/2011": "2011-01-14",
    "11/21/2010 - 03/15/2011": "2010-11-21",
    "11/12/2010 - 05/15/2011": "2010-11-12",
    "10/30/2010 +/-": "2010-10-30",
    "07/02/2010 - 12/17/2010": "2010-07-02",
    "02/03/2010 - 02/17/2010": "2010-02-03",
    "03/27/2010 - 04/16/2010": "2010-03-27",
    "02/05/2010 - 03/12/2010": "2010-02-05",
    "03/12/2010 - 04/15/2010": "2010-03-12",
    "03/06/2010 - 07/01/2010": "2010-03-06",
    "02/03/2012 - 07/13/2012": "2012-02-03",
    "01/20/2012 - 06/30/2012": "2012-01-20",
    "12/23/2011 - 01/31/2012": "2011-12-23",
    "10/01/2011            11/30/2011      12/31/2011": "2011-10-01",
    "05/20/2011 - 07/15/2011": "2011-05-20",
    "05/02/2011 - 12/31/2011": "2011-05-02",
    "06/10/2011 - 09/30/2011": "2011-06-10",
    "See WARN": None,
    "02/04/2013, +14 days after": "2013-02-04",
    "45 days, ending 01/28/2013": "2012-12-14",
    "12/31/2012 - 01/14/2013": "2012-12-31",
    "12/28/2012 +/- to ?": "2012-12-28",
    "12/28/2012, or with 2 weeks after.": "2012-12-28",
    "On or before 10/31/2012": "2012-10-31",
    "Within 14 days of 11/12/2012": "2012-11-12",
    "10/31/2012 - 11/13/2012": "2012-10-31",
    "08/31/2012 - 12/31/2012": "2012-08-31",
    "On or before 10/22/2012": "2012-10-22",
    "08/20/2012 - 03/31/2013": "2012-08-20",
    "08/20/2012 & 08/31/2012": "2012-08-20",
    "Mid August 2012 - 12/2013": None,
    "08/20/2012 - 12/31/2013": "2012-08-20",
    "06/16/2012 - 07/13/2012": "2012-06-16",
    "07/20/2012 - late 2013": "2012-07-20",
    "08/07/2012  or within 14 days": "2012-08-07",
    "08/07/2012 or with 2 weeks": "2012-08-07",
    "06/08/2012 - 06/29/2012": "2012-06-08",
    "08/04/2012 - 08/18/2012": "2012-08-04",
    "05/18/2012 - 12/31/2012": "2012-05-18",
    "06/05/2012 - 07/15/2012": "2012-06-05",
    "07/01/2012 - 08/15/2012": "2012-07-01",
    "06/19/2012 - 07/03/2012": "2012-06-19",
    "03/30/2012 - 04/27/2012": "2012-03-30",
    "03/22/2012 +/-": "2012-03-22",
    "03/12/2012 - 03/19/2012": "2012-03-12",
    "14th - 28th of February 2014": "2014-02-14",
    "03/31/2014, or during the 14-day period (ending 04/14/2014)": "2014-03-31",
    "01/21/2014 - 01/31/2014": "2014-01-21",
    "01/31/2014, or the 14 days period preceeding 01/31/2014": "2014-01-31",
    "12/20/2013, or during 14 day period (ending 01/03/2014)": "2013-12-20",
    "Decemeber of 2013": "2013-12-01",
    "11/29/2013 - 12/12/2013": "2013-11-29",
    "08/31/2013 - 10/31/2013": "2013-08-31",
    "14th - 25th of October 2013": "2013-10-14",
    "09/27/2013 - 12/27/2013": "2013-09-27",
    "August or September of 2013": None,
    "08/08/2013 - 12/31/2013": "2013-08-08",
    "08/23/2013 - 10/25/2013": "2013-08-23",
    "14-day period following 08/06/2013": "2013-08-06",
    "08/05/2013 - 08/13/2013": "2013-08-05",
    "07/03/2013 - 07/02/2014": "2013-07-03",
    "07/05/2013 - 12/31/2013": "2013-07-05",
    "02/04/2013 - 07/05/2013": "2013-02-04",
    "08/06/2013 - 08/20/2013": "2013-08-06",
    "07/01/2013 - 09/01/2013": "2013-07-01",
    "50/01/2013 - 10/25/2013": None,
    "05/03/2013 - 06/15/2013": "2013-05-03",
    "02/04/2013 - 05/05/2013": "2013-02-04",
    "On or around 03/08/2013": "2013-03-08",
    "03/05/2013 or within the 2-week period afterward": "2013-03-05",
    "07/29/2014 thru 09/30/2014": "2014-07-29",
    "Beginning 07/28/2014": "2014-07-28",
    "Beginning 08/22/2014": "2014-08-22",
    "On or shortly after August 18, 2014": "2014-08-18",
    "Between June 30, 2014 and July 11, 2014": "2014-06-30",
    "September 19, 2014, or within the 14-day period after that date": (
        "2014-09-19"
    ),
    "September 29, 2014, through October 12, 2014": "2014-09-29",
    "Q4, 2014 and are expected to end in Q2, 2015": None,
    "21 jobs beginning December 31, 2014,  See WARN": "2014-12-31",
    "September 19, 2014/See WARN": "2014-09-19",
    "On or about January 31, 2015": "2015-01-31",
    "04/03/2015 and  06/30/2015": "2015-04-03",
    "07/31/2015 and 10/30/2015": "2015-07-31",
    "03/19/2012 - 04/01/2012": "2012-03-19",
    "2041-06-04 00:00:00": "2014-06-04",
    "04/05/2015": "2015-04-05",
    "2026-12-31 00:00:00": "2026-12-31",
    "1/8/2026": "2026-01-08",
    "10/20/2025": "2025-10-20",
    "12/31/2026": "2026-12-31",
}

# Employee-count fixes from BLN's ``jobs_corrections`` that the generic
# transform_jobs-style cleanup in warn_sources.ky._clean_employees cannot
# reproduce (BLN sums the two-plant counts). Keys are whitespace-collapsed
# because lookup happens on the _clean_str-normalized cell.
JOBS_CORRECTIONS = {
    "74 fulltime and 184 parttime": 74,
    "Reduction from 13 to 1": 12,
    "47 30": 77,
    "79 10": 89,
}

# Placeholder values KY used in the "Closure or Layoff?" column.
JUNK_LAYOFF_TYPES = {"?", "see warn"}


def download() -> Path:
    """Fetch the BLN CSV (cached in the temp dir; override via env)."""
    dest = Path(
        os.environ.get(
            "KY_BACKFILL_CSV",
            str(Path(tempfile.gettempdir()) / "ky-historical-normalized.csv"),
        )
    )
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def _fix_date(raw):
    """Raw date cell -> ISO YYYY-MM-DD or None (corrections first)."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[s]
    return _clean_date(s)


def _fix_employees(raw) -> int:
    """Raw employees cell -> int, 0 when the state published no count."""
    key = _clean_str(raw)
    if key in JOBS_CORRECTIONS:
        return JOBS_CORRECTIONS[key]
    return _clean_employees(raw)


def parse(csv_path: Path) -> list:
    """BLN historical CSV -> unified-schema records."""
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    records = []
    for row in df.to_dict("records"):
        company = _clean_str(row.get("Company Name"))
        if not company:
            continue
        layoff_type = _clean_str(row.get("Closure or Layoff?"))
        if layoff_type.lower() in JUNK_LAYOFF_TYPES:
            layoff_type = ""
        # Prefer the NAICS code (matching the live KY feed's industry
        # field); fall back to the sparse free-text industry column.
        industry = _clean_naics(row.get("NAICS Code"))
        if not industry:
            industry = _clean_str(row.get("industry"))
        records.append(
            {
                "company": company,
                # KY quirk (see module docstring): dates are swapped.
                "notice_date": _fix_date(row.get("Projected Date")),
                "effective_date": _fix_date(row.get("Date Received")),
                "employees": _fix_employees(row.get("Employees")),
                "layoff_type": layoff_type,
                "county": _clean_str(row.get("County")),
                "address": _clean_str(row.get("address")),
                "industry": industry,
            }
        )
    return records


def _store_stats():
    """(record count, earliest event date) of the KY cumulative store."""
    paths = get_source(STATE).paths
    if not paths.cumulative.exists():
        return 0, None
    recs = json.loads(paths.cumulative.read_text()).get("records", [])
    dates = [
        str(r.get("notice_date") or r.get("effective_date") or "")[:10]
        for r in recs
    ]
    dates = [d for d in dates if len(d) == 10]
    return len(recs), (min(dates) if dates else None)


def main() -> dict:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    csv_path = download()
    records = parse(csv_path)
    floor = backfill.live_floor(STATE)
    kept, dropped_no_date, excluded_post_floor = [], 0, 0
    for r in records:
        event = r["notice_date"] or r["effective_date"]
        if not event:
            dropped_no_date += 1
            continue
        if floor and event >= floor:
            excluded_post_floor += 1
            continue
        kept.append(r)
    total_before, _ = _store_stats()
    backfill.merge_records(STATE, kept)
    total_after, date_min = _store_stats()
    report = {
        "state": STATE.upper(),
        "csv_rows": len(records),
        "live_floor": floor,
        "dropped_no_date": dropped_no_date,
        "excluded_post_floor": excluded_post_floor,
        "merge_input": len(kept),
        "added": total_after - total_before,
        "cumulative_total": total_after,
        "date_min": date_min,
    }
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
