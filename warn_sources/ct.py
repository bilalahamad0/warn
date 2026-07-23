"""
warn_sources.ct
---------------
Connecticut — Department of Labor public document library (JSON API).

The CT DOL "Rapid Response / WARN documents" library exposes a paged JSON
endpoint (``GetSpecializedData``) whose ``blobItems`` carry one WARN filing
per uploaded PDF, with the structured fields we need in ``blobProperties``.
The endpoint sits behind an F5-style bot wall: a plain GET of the JSON URL
returns an empty body, so — exactly like Big Local News' scraper — we first
load the human-facing library page to collect the ``TS*`` cookies, then call
the API with XHR-style headers on the same session.

The library is a rolling window of recent notices (roughly the last year);
the shared engine's cumulative store retains older filings once seen.

Field crosswalk (per the BLN transformer, followed exactly):

* company        <- ``affected_company``
* notice_date    <- ``warn_document_date`` (the state's document date)
* effective_date <- ``layoff_dates`` (free text: single dates, ranges,
  multi-date lists, "Not provided", …)
* employees      <- ``number_of_impacted_workers`` (free text; manual
  corrections vendored from BLN, unparseable/absent counts -> 0)
* city           <- ``layoff_locations`` (the only location granularity CT
  publishes; property keys arrive with stray trailing underscores, e.g.
  ``layoff_locations__``, which are stripped)

``warn_document_received_date``, ``closing_dates`` and ``union`` are not part
of the unified schema and are left in the raw download for audit.

Fetch/parse and the corrections tables are ported from Big Local News'
Apache-2.0 warn-scraper (warn/scrapers/ct.py) and warn-transformer
(warn_transformer/transformers/ct.py) — vendored, not imported. One
deliberate divergence: where BLN raises ``KeyError`` on a date it has no
manual correction for, we log and return ``None`` so one new messy value can
never break the pipeline.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://dolpublicdocumentlibrary.ct.gov/CsblrCategory"
    "?prefix=%2Frapid_response%2Fwarn_documents"
)
API_URL = (
    "https://dolpublicdocumentlibrary.ct.gov/CsblrCategory/GetSpecializedData"
    "?pageSize=5000&pageIndex={page}"
    "&prefix=%2Frapid_response%2Fwarn_documents"
    "&sortedCol=warn_document_date&module=WARN"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
API_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "X-Security-Request": "required",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": PAGE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# The four date formats CT data has been seen in (per the BLN transformer).
# The second one really is "+00.00" — a literal suffix, not a UTC offset.
DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S+00.00", "%m/%d/%Y", "%m-%d-%Y"]

# BLN sanity guards: dates far in the future / distant past and implausible
# head-counts are treated as unparseable rather than trusted.
MAX_FUTURE_DAYS = 365
MINIMUM_YEAR = 1988
MAXIMUM_JOBS = 10000

# Manual corrections for malformed source values, vendored verbatim from
# BLN warn-transformer transformers/ct.py (Apache-2.0). Keys are matched
# twice, mirroring BLN: once against the raw value, once against the
# lower-cased first token after cleanup (hence entries like "february").
DATE_CORRECTIONS = {
    "12/31/16-1/13/17": "2016-12-31",
    "12/4/15-tbd": "2015-12-04",
    "2/29/15": "2015-02-28",
    "9/1//15": "2015-09-01",
    "Not Indicated": None,
    "# 12 2/13/17 through 2018": "2017-02-13",
    "Most 10/29/16": "2016-10-29",
    "Several Weeks - Five Months": None,
    "Not Dated Rec'd 6/24/15": "2015-06-24",
    "3rd Quarter 2015-4th Quarter 2016": "2015-06-01",
    "June 2017 - March 2018": "2017-06-01",
    "First quarter 2019 - 2020": "2019-01-01",
    "June 2018 - September 2, 2018": "2018-06-01",
    "Beginning June 2018": "2018-06-01",
    "December 2018 - March 1, 2019": "2018-12-01",
    "Possibly 50+": None,
    "N/A": None,
    "Reduction in Hours Since March 2020": "2020-03-01",
    "April-June 2020": "2020-04-01",
    "": None,
    "7/3/2020- 7/17/2020": "2020-07-03",
    "Not Dated Rec'd 4/22/2020": "2020-04-22",
    "Not Dated Rec'd 4/13/2020": "2020-04-13",
    "3/16 - 12/13/2020": "2020-03-16",
    "february": None,
    "potentially": None,
    "not": None,
    "9/92024": "2024-09-09",
    "4/112025": "2025-04-11",
    "3/31/2025,": "2025-03-31",
    "11/24/2025 - 4/1/2026": "2025-11-24",
    "February 15, 2025, through February 28, 2025": "2025-02-15",
    "April 12, 2025": "2025-04-12",
    "March 21, 2025": "2025-03-21",
    "8/2/2019, 12/13/2029": "2019-08-02",
    "June 29, 2025": "2025-06-29",
    "6/3/2023, 12/31/2023": "2023-06-03",
    "09/15/2020,12/17/2020": "2020-09-15",
    "03/28/2020, 04/01/2020": "2020-03-28",
    "June 10, 2025": "2025-06-10",
    "05/10/2020,05/23/2020": "2020-05-10",
    "August 23, 2024": "2024-08-23",
    (
        "April 30,  2025. Additional layoffs are scheduled for June 20, 2025 "
        "and July 30, 2025"
    ): "2025-04-30",
    "April 25, 2025-July 3 1 , 2025.": "2025-04-25",
    "03/20/2020,04/03/2020,04/07/2020": "2020-03-20",
    "02/26/2022,03/11/2022": "2022-02-26",
    "December 2, 2025": "2025-12-02",
    (
        "8/4/23, 9/1/23, 10/6/23, 11/3/23, 12/1/23, 1/5/24, 2/2/24, 2/16/24, "
        "3/1/24, 3/29/24, 5/3/24, 5/31/24, 6/28/24, 9/15/24, 9/20/24, 9/27/24, "
        "9/30/24, 12/6/24"
    ): "2023-08-04",
    "10/10/2020,10/23/2020": "2020-10-10",
    "June 7, 2025": "2025-06-07",
    "04/01/2020,06/30/2020": "2020-04-01",
    "February 10th, 2025 through February 24, 2025": "2025-02-10",
    "02 /01/2023 ,03/01/2023": "2023-02-01",
    "July 5th, 2024": "2024-07-05",
    "02/01/2023,03/01/2023": "2023-02-01",
    "7/15/2025, 7/29/2025": "2025-07-15",
    "12/8/24 through 12/21/24": "2024-12-08",
    "08/20/2023, 09/02/2023": "2023-08-20",
    "08/17/2020,08/30/2020": "2020-08-17",
    "3/10/2020,4/07/2020": "2020-03-10",
    "March 25, 2025": "2025-03-25",
    "03/25/2020,03/28/2020": "2020-03-25",
    "09/15/2023, 09/23/2023, 11/17/2023, 12/1/2023": "2023-09-15",
    "1/13/25, 2/13/25": "2025-01-13",
    "01/03/2020,05/30/2020": "2020-01-03",
    "05/01/2020,07/06/2020": "2020-05-01",
    "03/26/2020, 04/16/2020,05/10/2020": "2020-03-26",
    "3/13/2020, 3/20/2020, 4/6/2020,4/19/2020": "2020-03-13",
    "September 27, 2024": "2024-09-27",
    "09/08/2020,09/22/2020": "2020-09-08",
    "09/15/2020,03/17/2021": "2020-09-15",
    "03/23/2020, 03/26/2020": "2020-03-23",
    "12/20/2024, 1/31/25, 2/14/25, 2/21/25": "2024-12-20",
    "04/10/2020,04/22/2020": "2020-04-10",
    "10/12/2025 - 10/25/2025": "2025-10-12",
    "March, 2025": "2025-03-01",
    "5/17/24": "2024-05-17",
    "August 24, 2024": "2024-08-24",
    (
        "10/21/2023, 10/28/2023, 11/04/2023, 11/18/2023, 11/25/2023, 12/30/2025"
    ): "2023-10-21",
    (
        "04/14/2023, 05/13/2023 - 05/27/2023, 06/12/2023-08/11/2023, "
        "06/12/2023-06/26/2023"
    ): "2023-04-14",
    "8/2/2019-12/31/2019": "2019-08-02",
    "9/21/2025 - 10/4/2025": "2025-09-21",
    "January 25, 2025 through February 7, 2025": "2025-01-25",
    "March 31, 2025, June 30, 2025, and September 30, 2025": "2025-03-31",
    "4/2/24": "2024-04-02",
    "9/9/24": "2024-09-09",
    "5/15/24": "2024-05-15",
    "3/22/25": "2025-03-22",
    "5/31/24": "2024-05-31",
    "10/2/23": "2023-10-02",
    "0025-05-16": "2025-05-16",
    "0024-05-31": "2024-05-31",
    "0005-02-04": "2025-02-04",
    "12/28/22025": "2025-12-28",
}

JOBS_CORRECTIONS = {
    "up to 703": 703,
    "18; 87": 105,
    "724 across U.S. including 49 from Ridgefield CT location": 49,
    "Not Provided": None,
    "Not Indicated": None,
    "Possibly 50+": 50,
    "Not indicated": None,
    "12; 6; 5": 23,
    "182; additional 21 on reduced hours": 182,
    "78; additional 13 on reduced hours": 78,
    "124; additional 30 on reduced hours": 124,
    "Not reported on WARN notice": None,
    "Not provided": None,
    "489 - total for CT and other locations": 489,
    "158 Stamford 81 Branford": 239,
    "?": None,
    "110 total; 7 CT 103 remote": 7,
    "Not": None,
    "208 (36 of whom work in CT)": 36,
    "No CT details provided": 0,
    "416 total; 323 work remotely": 93,
    "42: 30 Remote workers": 12,
    "164 Remote workers": 164,
    "13 total: 2 CT residents": 2,
    "22 total: 10 CT residents": 10,
    "4 total: 0 CT residents": 0,
    "2 (1 remote CT worker)": 1,
    "55 total: 1 CT resident": 1,
    "131 total; 92 CT residents": 92,
    "80 Total; 4 CT": 4,
    "66; #CT workers not indicated": None,
    "": None,
    "113,": 113,
    "Greenwich": None,
}

# Junk/placeholder company values that mark a non-record row.
_JUNK_COMPANIES = {"company", "affected company", "n/a", "none", "tbd"}

COLUMNS = ["company", "notice_date", "effective_date", "employees", "city"]


def _transform_date(value) -> Optional[str]:
    """CT date text -> ISO ``YYYY-MM-DD`` or ``None`` (BLN ct.py logic)."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    raw = str(value).strip()
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    # BLN cleanup: drop qualifier words, take the first token — which also
    # resolves ranges like "7/20/2026 - 8/31/2026" to their start date.
    v = raw.lower()
    for junk in ("beginning", "after", "estimated"):
        v = v.replace(junk, "")
    v = v.replace(";", "").replace("*", "").strip()
    if not v:
        return None
    v = v.split()[0].strip()
    dt = None
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            break
        except ValueError:
            continue
    if dt is not None:
        plausible = (
            dt.year >= MINIMUM_YEAR
            and dt <= datetime.today() + timedelta(days=MAX_FUTURE_DAYS)
        )
        if plausible:
            return dt.strftime("%Y-%m-%d")
    if v in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[v]
    log.debug(f"[CT] unparseable date {raw!r} — recording as None")
    return None


def _transform_jobs(value) -> int:
    """CT worker-count text -> int; 0 when the state publishes no count."""
    if value is None:
        return 0
    raw = str(value).strip()
    if raw in JOBS_CORRECTIONS:
        corrected = JOBS_CORRECTIONS[raw]
        return int(corrected) if corrected is not None else 0
    n = warn_monitor._safe_int(raw)
    if n is None or n < 0 or n > MAXIMUM_JOBS:
        if raw:
            log.debug(f"[CT] unparseable worker count {raw!r} — recording 0")
        return 0
    return n


def _polite_get(session, url, headers=None, attempts=3, timeout=90):
    """GET with 1 req/s politeness and up to ``attempts`` tries."""
    last_err = None
    for attempt in range(1, attempts + 1):
        time.sleep(1)  # max 1 request/second/host
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            log.warning(f"[CT] request failed (attempt {attempt}/{attempts}): {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"CT DOL fetch failed after {attempts} attempts: {last_err}")


class ConnecticutDOL(Source):
    code = "ct"
    name = "Connecticut"
    agency = "Connecticut Department of Labor"
    source_url = PAGE_URL
    cadence = "twice-daily"

    def fetch(self, force: bool = False) -> tuple:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        # Step 1: human-facing library page — collects the bot-wall cookies
        # without which the JSON endpoint answers with an empty body.
        _polite_get(session, self.source_url)
        # Step 2: the JSON API, paged (one page in practice at pageSize=5000).
        resp = _polite_get(session, API_URL.format(page=1), headers=API_HEADERS)
        payload = resp.json()
        items = list(payload.get("blobItems") or [])
        total_pages = int(payload.get("totalPages") or 1)
        for page in range(2, min(total_pages, 50) + 1):
            resp = _polite_get(
                session, API_URL.format(page=page), headers=API_HEADERS
            )
            items.extend(resp.json().get("blobItems") or [])
        if not items:
            raise RuntimeError("CT DOL API returned no WARN records")
        # blobToken is a per-session download token that changes on every
        # request; drop it so the raw file (and its change hash) are stable.
        for item in items:
            item.pop("blobToken", None)

        self.paths.ensure()
        self.paths.raw.write_text(
            json.dumps({"blobItems": items}, indent=2, sort_keys=True)
        )

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": pd.Timestamp.utcnow().isoformat(),
                "url": API_URL.format(page=1),
                "records_seen": len(items),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        data = json.loads(Path(raw_path).read_text())
        rows = []
        for entry in data.get("blobItems", []):
            # Flatten blobProperties; CT's keys carry stray trailing
            # underscores (layoff_locations__, closing_dates_) — strip them.
            # (Ported from BLN warn-scraper warn/scrapers/ct.py.)
            props = {
                k.strip("_"): v
                for k, v in (entry.get("blobProperties") or {}).items()
            }
            company = " ".join(str(props.get("affected_company") or "").split())
            if not company or company.lower() in _JUNK_COMPANIES:
                continue
            rows.append(
                {
                    "company": company,
                    "notice_date": _transform_date(
                        props.get("warn_document_date")
                    ),
                    "effective_date": _transform_date(props.get("layoff_dates")),
                    "employees": _transform_jobs(
                        props.get("number_of_impacted_workers")
                    ),
                    "city": str(props.get("layoff_locations") or "").strip(),
                }
            )
        return pd.DataFrame(rows, columns=COLUMNS)
