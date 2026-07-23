"""
warn_sources.tn
---------------
Tennessee — Department of Labor and Workforce Development WARN notices.

The state publishes its current- and prior-year WARN notices as HTML tables
(``class="tn-datatable"``) on the Workforce Reports page; each data row has
six cells: Date of Posting, Company (a link to the notice PDF), County,
Affected Workers, Closure/Layoff Date, Notice ID. Scrape flow (the
tn-datatable tables, 6-cell row filter, cell order) is vendored from Big
Local News' Apache-2.0 warn-scraper (scrapers/tn.py); the field crosswalk
and the date/jobs corrections are vendored from warn-transformer
(transformers/tn.py): company="Company", notice_date="Notice Date" (the
posting date), effective_date="Effective Date" (Closure/Layoff Date),
jobs="No. Of Employees", with BLN's date-format list and its range-splitting
fallback (a multi-date range collapses to its FIRST listed date, matching
BLN's date_corrections). The live page publishes a county but no city — a
city exists only in BLN's separate historical CSV — so ``county`` is kept
as published and a city is never fabricated from it (BLN's transformer
displays "<County> County" when the city is absent; we keep the fields
separate). Tennessee publishes no layoff-type, address, or industry, so
those unified fields are omitted. The Notice ID column is dropped (not part
of the unified schema). The effective-date cell is free text on the state's
side ("4-1-2026 through October 2026", "8-28-2026/ 10-30-2026/12/31-2026");
anything with no parseable date becomes None — never raw text kept as a
date, never copied from the posting date.
"""

import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://www.tn.gov/workforce/general-resources/major-publications0/"
    "major-publications-redirect/reports.html"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Accepted date formats, vendored from BLN warn-transformer transformers/tn.py
# (date_format tuple).
DATE_FORMATS = (
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%B %d, %Y",
    "%B %Y",
    "%B%d, %Y",
    "%B %d. %Y",
    "%B %d,%Y",
    "%m-%d-%Y",
)

# Range separators BLN strips to keep the first listed date (transform_date
# fallback in transformers/tn.py), plus the plain hyphen variant the live
# page uses ("4/3/2026 - 5/31/2026").
_RANGE_SEPARATORS = (" and ", " to ", " through ", " & ", " – ", " - ")

# Known-bad employee-count strings, vendored from BLN warn-transformer
# (jobs_corrections).
JOBS_CORRECTIONS = {
    "147 (69 Tennessee residents)": 69,
    "135 (7 in Tennessee)": 7,
}

# Known-bad date strings that appear verbatim in the Tennessee feed, vendored
# from BLN warn-transformer (date_corrections; datetimes rendered as ISO).
# Keys are whitespace-normalized (\xa0 and newlines collapse to one space)
# because the live cells contain <br/> line breaks and non-breaking spaces.
DATE_CORRECTIONS = {
    "2018/4/ 27": "2018-04-27",
    "start of layoff - March 13, 2020": "2020-03-13",
    "December 15-30, 2020": "2020-12-15",
    "June 17 - June 30, 2020": "2020-06-17",
    "124": None,
    "March 16, 2020 - June 30, 2020": "2020-03-16",
    "March 20-24, 2020": "2020-03-20",
    "March 4, 2020 - June 4, 2020": "2020-03-04",
    "Apri 1, 2020": "2020-04-01",
    "Beginning April 14 and ending June 30, 2020": "2020-04-14",
    "Beginning February 16 and ending February 29, 2020": "2020-02-16",
    "Beginning March 17 and ending March 30, 2020": "2020-03-17",
    "Beginning in February 2020": "2020-02-01",
    "beginning February 2020": "2020-02-01",
    "January 3, 2020 through January 31, 2020": "2020-01-03",
    "December 13, 2019 and continue until February 28, 2020": "2019-12-13",
    "December 1, 2019 until December 31, 2019": "2019-12-01",
    "Will begin on November 30, 2019 and will continue to December 31, 2020":
        "2019-11-30",
    "Will begin on October 7, 2019 and will continue to November 30, 2019":
        "2019-10-07",
    "Initial layoff September 4, 2019 with additional layoffs planned "
    "September 13 and September 20": "2019-09-04",
    "November 9 through November 23, 2019": "2019-11-23",
    "September 30, 2019 for 4 employees and October 31, 2019 for 174 "
    "employees": "2019-09-30",
    "Late September 2019": "2019-09-15",
    "September 20, 2019 and continuing through December 2019": "2019-09-20",
    "August 30, 2019 through December 31, 2019": "2019-08-30",
    "April 22, 2019, May 4, 2019, and August 7, 2019": "2019-04-22",
    "March 3, 2019, March 11, 2019,": "2019-03-03",
    "June 15, 2018, July 6, 2018, August 3, 2018": "2018-06-15",
    "July 31, 2023; September 30, 2023; December 31, 2023": "2023-07-31",
    "June 12, 2023 – August 11, 2023": "2023-06-12",
    "1021/2024": "2024-10-21",
    "2-28-2026": "2026-02-28",
    "3-20-2026 - 7-24-2026/7-31-2026": "2026-03-20",
    "3-24-2026": "2026-03-24",
    "4/3/2026 - 5/31/2026": "2026-04-03",
    "3-20-2026 / 7-24-2026": "2026-03-20",
    "5-9-2026 / 8-8-2026": "2026-05-09",
    "7-4-2026 / 9-30-2026": "2026-07-04",
    "5-12-2026 to 6-5-2026": "2026-05-12",
    "8-28-2026/ 10-30-2026/ 12/31-2026": "2026-08-28",
}

_WS_RE = re.compile(r"\s+")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# First numeric date token in a free-text range, e.g. the "8-28-2026" in
# "8-28-2026/ 10-30-2026/12/31-2026" or "5-12-2026to 6-5-2026".
_DATE_TOKEN_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}")
# company values that are really repeated-header/junk rows, lowercased.
_JUNK_COMPANIES = {"company", "nan", "none"}


def _normalize_ws(value) -> str:
    """Collapse all whitespace runs (incl. \\xa0, <br/> newlines) to one
    space and strip."""
    return _WS_RE.sub(" ", str(value)).strip()


def _parse_one(token):
    """One date token -> ISO string or None (BLN format list first, then the
    shared warn_monitor._safe_date guarded to real, in-range ISO dates)."""
    token = token.strip().strip(",;")
    if not token:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback only when the token carries an explicit 4-digit year —
    # otherwise pandas would silently inject the current year.
    if re.search(r"\d{4}", token):
        iso = warn_monitor._safe_date(token)
        if iso and _ISO_RE.match(iso) and 1988 <= int(iso[:4]) <= 2100:
            return iso
    return None


def _clean_date(value):
    """Raw Tennessee date cell -> ISO YYYY-MM-DD string or None.

    Mirrors BLN's transform_date: corrections dict, then the format list,
    then split a range on its separators and keep the first date. A final
    first-date-token scan covers the state's newer unspaced ranges
    ("5-12-2026to 6-5-2026"). Unparseable text becomes None.
    """
    raw = _normalize_ws("" if value is None else value)
    if not raw:
        return None
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    iso = _parse_one(raw)
    if iso:
        return iso
    for sep in _RANGE_SEPARATORS:
        head = raw.split(sep)[0].strip()
        if head and head != raw:
            iso = _parse_one(head)
            if iso:
                return iso
    m = _DATE_TOKEN_RE.search(raw)
    if m:
        return _parse_one(m.group(0))
    return None


def _clean_jobs(value) -> int:
    """Raw Affected Workers cell -> int (0 when the state publishes none)."""
    raw = _normalize_ws("" if value is None else value)
    if raw in JOBS_CORRECTIONS:
        return JOBS_CORRECTIONS[raw]
    n = warn_monitor._safe_int(raw)
    return n if n is not None else 0


def extract_rows(html: str) -> list:
    """Reports-page HTML -> list of 6-cell row dicts (BLN cell order).

    Vendored BLN flow: every <tr> inside a class="tn-datatable" container;
    rows with exactly six <td> cells are data rows (header rows use <th>).
    """
    soup = BeautifulSoup(html, "html5lib")
    rows = []
    for table in soup.find_all(attrs={"class": "tn-datatable"}):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) != 6:
                continue
            text = [c.get_text() for c in cells]
            rows.append(
                {
                    "notice_date": text[0],
                    "company": text[1],
                    "county": text[2],
                    "employees": text[3],
                    "effective_date": text[4],
                    "notice_id": text[5],
                }
            )
    return rows


class TennesseeDLWD(Source):
    code = "tn"
    name = "Tennessee"
    agency = "Tennessee Department of Labor and Workforce Development"
    source_url = PAGE_URL
    cadence = "weekly"

    # -- fetch ---------------------------------------------------------------

    def fetch(self, force: bool = False) -> tuple:
        """Download the Reports page HTML to ``self.paths.raw``.

        One polite GET (browser headers, three tries with backoff); the raw
        artifact is the page itself so parse() works offline from it.
        """
        self.paths.ensure()
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        resp = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)  # politeness: >=1s between retries
            try:
                resp = session.get(PAGE_URL, timeout=60)
            except requests.RequestException as e:
                log.warning(f"[TN] request error: {e}")
                continue
            if resp.status_code == 200:
                break
            log.warning(f"[TN] HTTP {resp.status_code} for {PAGE_URL}")
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"TN feed: reports page {status} ({PAGE_URL})")
        if "tn-datatable" not in resp.text:
            raise RuntimeError(
                "TN feed: reports page has no tn-datatable — layout changed?"
            )

        Path(self.paths.raw).write_text(resp.text, encoding="utf-8")
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Reports-page HTML -> unified-schema rows (BLN field crosswalk)."""
        html = Path(raw_path).read_text(encoding="utf-8")
        raw_rows = extract_rows(html)
        if not raw_rows:
            raise ValueError("TN feed: no data rows found in tn-datatable")

        rows = []
        for r in raw_rows:
            company = _normalize_ws(r["company"])
            if not company or company.lower() in _JUNK_COMPANIES:
                continue  # blank / repeated-header / junk row
            rows.append(
                {
                    "company": company,
                    "notice_date": _clean_date(r["notice_date"]),
                    "effective_date": _clean_date(r["effective_date"]),
                    "employees": _clean_jobs(r["employees"]),
                    "county": _normalize_ws(r["county"]),
                }
            )

        out = pd.DataFrame(
            rows,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "county",
            ],
        )
        # pandas coerces None -> NaN on construction; restore real None so
        # missing dates serialize as JSON null, not the string "nan".
        return out.astype(object).where(pd.notna(out), None)
