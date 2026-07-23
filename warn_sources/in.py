"""
warn_sources.in
---------------
Indiana — Department of Workforce Development WARN notices.

A single page at ``https://www.in.gov/dwd/warn-notices/current-warn-notices/``
carries one HTML table with the full notice history back to 2008 (~1,200
rows), so every fetch is also the whole backfill. Each run downloads the
page politely (browser UA, 3 retries, 60 s timeout), extracts the table
cells, and writes a consolidated JSON of raw rows to ``paths.raw``.

Scrape flow and field crosswalk vendored from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/in.py) and warn-transformer
(warn_transformer/transformers/in.py) — ported, never imported. Quirks
honored from BLN:

* stray ``/td>`` artifacts leak into cell text (e.g. ``All/td>``) — stripped
  before use (BLN scraper ``_parse_table``);
* rows whose "Affected Workers" cell is ``N/A`` are data-free revision
  markers ("<Company>-Revised (9/15/21)" with every field N/A) — dropped
  (BLN ``prep_row_list``);
* head counts include prose ("62 MAY be affected", "4 Hoosiers", ranges,
  "Entire Plant") — resolved via BLN's ``jobs_corrections`` table; a
  correction of None (and any unparseable text) means the state published
  no usable count -> 0;
* "LO/CL Date" (the effective date) comes in at least six formats
  (``%m/%d/%Y``, ``%m/%d/%y``, "March 2024", bare "2024", "3/2024" …) plus
  free-text ranges ("5/29/2009 to 12/1/2009", "1/23/2009-2010") whose start
  is the effective date, and one-off oddities fixed by BLN's
  ``date_corrections`` table (tried before *and* after the range cleanup,
  exactly like BLN's ``transform_date``). Unparseable -> None, never junk.

Field crosswalk (BLN ``fields`` dict, followed exactly): company <-
"Company", city <- "City", employees <- "Affected Workers", notice_date <-
"Notice Date", effective_date <- "LO/CL Date". IN additionally publishes an
industry description and a notice type, which the unified schema keeps:
industry <- "Description of Work/Industry" (the numeric NAICS column is
redundant with it and skipped), layoff_type <- "Notice Type" decoded via
the page's own legend (CL = Closure, LO = Layoff, TR = Transfer,
RH = Reduction in Hours); unlisted wordings ("Potential Closure",
"PENDING CL") pass through as published. IN publishes no county and no
street address, so those unified fields are omitted — never fabricated.
"""

import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://www.in.gov/dwd/warn-notices/current-warn-notices/"

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

# Raw table columns, in page order (the table has a 9th, always-empty cell
# that zip() drops). The first five are BLN's crosswalk fields.
FIELDS = [
    "Company",
    "City",
    "Affected Workers",
    "Notice Date",
    "LO/CL Date",
    "NAICS",
    "Description of Work/Industry",
    "Notice Type",
]

# BLN warn-transformer transformers/in.py date_format, tried in order.
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%B %Y", "%Y", "%b %Y", "%m/%Y"]

# Sanity window: anything outside is a typo (cf. "01/30/1202"), not data.
MIN_YEAR = 1988


def _max_year():
    return date.today().year + 3


_MISSING = object()

# The page's own legend (W = WARN Notice omitted — it never appears in the
# Notice Type column). "L/O" is a live one-off spelling of LO.
NOTICE_TYPE_LEGEND = {
    "CL": "Closure",
    "LO": "Layoff",
    "L/O": "Layoff",
    "TR": "Transfer",
    "RH": "Reduction in Hours",
}

# Vendored from BLN warn-transformer transformers/in.py jobs_corrections;
# None = the state published no usable count. The last entry is a live
# whitespace variant of BLN's "97 (in MI)0 (in IN)" key.
JOBS_CORRECTIONS = {
    "97 (in MI)0 (in IN)": 0,
    "100+": 100,
    "62 MAY be affected": 62,
    "5 in Indiana": 5,
    "Unknown": None,
    "75 in Indiana": 75,
    "40-50": 40,
    "100-130": 100,
    "4 Hoosiers": 4,
    "Undisclosed at this time": None,
    "500 Nationwide": None,
    "NA": None,
    "103 (REVISED) 10/22/2020 108": 103,
    "Entire Plant": None,
    "All": None,
    "97 (in MI) 0 (in IN)": 0,
}

# Vendored from BLN warn-transformer transformers/in.py date_corrections
# (as (y, m, d) tuples; None = no usable date). Keys are looked up after
# whitespace normalisation, so BLN's "\xa0" variants collapse into one.
# Entries like "No closure date announced. Layoffs" match the *cleaned*
# remainder after range-splitting, exactly as in BLN's transform_date.
DATE_CORRECTIONS = {
    "01/30/1202": (2012, 1, 30),
    "April/June 2020": (2020, 4, 1),
    "Unknown": None,
    "Q1 2019": (2019, 1, 1),
    "Q1 2018": (2018, 1, 1),
    "Sept. 2016": (2016, 9, 1),
    "No closure date announced. Layoffs to commence 05/27/2015": (2015, 5, 27),
    "No closure date announced. Layoffs to commence 5/27/2015": (2015, 5, 27),
    "TBD": None,
    "09/22/2014-12/07/2014": (2014, 9, 22),
    "08/18/2014-12/31/2014": (2014, 8, 18),
    "End of 2013": (2013, 12, 31),
    "Mid-Year 2014": (2014, 6, 15),
    "02/29/2013": (2013, 2, 28),
    "2/29/2013": (2013, 2, 28),
    "year end 2014": (2014, 12, 31),
    "4th Qtr 2012": (2012, 9, 1),
    "Mid February 2012": (2012, 2, 14),
    "3rd Qtr 2012": (2012, 6, 1),
    "LO-01/14/2011 CL-End of 2012": (2011, 1, 14),
    "LO-1/14/2011 CL-End of 2012": (2011, 1, 14),
    "Prior to the end of 2009 (as stated in the WARN notice)": (2009, 12, 31),
    "No closure date announced. Layoffs": None,
    "1st Quarter 2009": (2009, 1, 1),
    "02/02/2009 to 12/30/2009": (2009, 2, 2),
    "3rd Quarter of 2009": (2009, 6, 1),
    "3rd quarter of 2009": (2009, 6, 1),
    "August to December 2008": (2008, 8, 1),
    "10/37/2008": (2008, 10, 27),
    "N/A": None,
}


def _squish(val) -> str:
    """Cell text -> clean single-spaced string; strips '/td>' artifacts."""
    text = re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()
    if text.endswith("/td>"):  # BLN scraper's markup-leak cleanup
        text = text[: -len("/td>")].strip()
    return text


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or _MISSING."""
    if text not in DATE_CORRECTIONS:
        return _MISSING
    ymd = DATE_CORRECTIONS[text]
    return None if ymd is None else "%04d-%02d-%02d" % ymd


def _clean_date(val):
    """IN date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's transform_date: corrections first, then strip range
    phrasing (the start of a range is the effective date), then
    corrections again, then the six known formats in order.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    if not text:
        return None

    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed

    # BLN's range/prose cleanup, in its exact order.
    text = text.replace("starting", "")
    for sep in (" and ", " to ", " through ", " - ", " & ", " – ", "-"):
        text = text.strip().split(sep)[0].strip()

    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed

    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
        return None
    return None


def _clean_employees(val) -> int:
    """Affected Workers cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    n = warn_monitor._safe_int(text)
    return n if n is not None and 0 <= n else 0


def _clean_notice_type(val) -> str:
    """Notice Type cell -> legend wording; unlisted text passes through."""
    text = _squish(val)
    if not text or text.upper() == "N/A":
        return ""
    return NOTICE_TYPE_LEGEND.get(text, text)


def _parse_table(html):
    """Page HTML -> list of raw-cell dicts keyed by FIELDS.

    Adapted from BLN warn-scraper _parse_table: every <tr>'s td/th text,
    in order. The header row rides along and is dropped in parse().
    """
    soup = BeautifulSoup(html, "html5lib")
    table = soup.find("table")
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells or not any(cells):
            continue
        rows.append(dict(zip(FIELDS, cells)))
    return rows


class IndianaDWD(Source):
    code = "in"
    name = "Indiana"
    agency = "Indiana Department of Workforce Development"
    source_url = PAGE_URL
    cadence = "daily"

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the single all-years page into one JSON file."""
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        last_err = None
        resp = None
        for attempt in range(3):
            if attempt:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(self.source_url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[IN] request error: {e}")
                continue
            if resp.status_code == 200:
                break
            log.warning(f"[IN] bad response ({resp.status_code})")
        if resp is None or resp.status_code != 200:
            status = resp.status_code if resp is not None else last_err
            raise RuntimeError(f"IN feed: fetch failed ({status})")

        rows = _parse_table(resp.text)
        # Header + at least a few hundred history rows on every good fetch.
        if len(rows) < 100:
            raise RuntimeError(
                f"IN feed: only {len(rows)} table rows — page layout may "
                "have changed"
            )

        self.paths.ensure()
        payload = {"source": self.source_url, "rows": rows}
        self.paths.raw.write_text(json.dumps(payload, indent=1))
        return True, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated JSON -> unified-schema rows (BLN crosswalk)."""
        payload = json.loads(Path(raw_path).read_text())
        raw_rows = payload["rows"] if isinstance(payload, dict) else payload

        records = []
        for row in raw_rows:
            company = _squish(row.get("Company") or "")
            if not company or company.lower() == "company":
                continue  # blank or header row
            if _squish(row.get("Affected Workers") or "") == "N/A":
                continue  # data-free revision marker (BLN prep_row_list)
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Notice Date")),
                    "effective_date": _clean_date(row.get("LO/CL Date")),
                    "employees": _clean_employees(row.get("Affected Workers")),
                    "layoff_type": _clean_notice_type(row.get("Notice Type")),
                    "city": _squish(row.get("City") or ""),
                    "industry": _squish(
                        row.get("Description of Work/Industry") or ""
                    ),
                }
            )
        return pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "city",
                "industry",
            ],
        )
