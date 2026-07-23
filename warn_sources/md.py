"""
warn_sources.md
---------------
Maryland — WARN notices published by the Department of Labor (formerly
DLLR) as one HTML table per year: the current-year log lives on
https://www.dllr.state.md.us/employment/warn.shtml and each prior year on
warn<YYYY>.shtml archive pages linked from it (2010-present).

Fetch walks the archive exactly like Big Local News' Apache-2.0
warn-scraper (warn/scrapers/md.py, vendored): GET the main page, collect
the ``a.sub`` year links, download each politely (1 request/second, 3
retries, browser UA). BLN hit connection resets in late 2024 that
``verify=False`` worked around; TLS verification is attempted first here
and only dropped, with a warning, if the handshake itself fails. Only
``warn<YYYY>.shtml`` links are followed — the main page also links a
"Federal RIF Log" (rif2025.shtml), which is a separate non-WARN
dislocation log that BLN's link-walk predates; it is deliberately
excluded. All pages land in one consolidated JSON at ``paths.raw``.

Parse follows BLN's warn-transformer crosswalk
(warn_transformer/transformers/md.py): Company -> company, Notice Date ->
notice_date, Effective Date -> effective_date, Total Employees ->
employees, Location -> the single free-text place column (a street
address on modern pages, sometimes just a city in 2010-era rows) ->
``address``, never split into a fabricated city. Columns MD publishes
beyond BLN's five-field crosswalk are kept: NAICS Code -> industry,
Local Area -> county (Maryland's workforce areas: county names plus
regions like "Upper Shore"), Type -> layoff_type. Pre-2021 pages encode
the last two as numeric "WIA Code" / "Type Code" columns whose legends
are printed on every page (1. A.A. CO. ... 13. STATEWIDE; 1. PLANT
CLOSURE, 2. MASS LAYOFF); those printed legends are applied, and any
value outside them passes through verbatim.

Free-text dates (ranges, "(REVISED)" annotations, phased schedules)
resolve to the FIRST m/d/y token in the string — the rule behind almost
all of BLN's hand-written ``date_corrections``. Replayed against all 52
BLN corrections: 36 reproduce exactly via the first-date rule; the 13
that cannot be derived (glued digits like "5/62011", month-only spans,
year typos, an invalid 2/29 leap date) are vendored verbatim below; the
3 "N/A"-style entries fall out to None naturally. BLN's
``jobs_corrections`` are vendored verbatim (ranges keep the low bound,
"Unknown"/"TBD" become no-count rows).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_BASE = "https://www.dllr.state.md.us/employment/"
_URL = _BASE + "warn.shtml"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_YEAR_PAGE = re.compile(r"^warn\d{4}\.shtml$")
_WS = re.compile(r"\s+")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# First m/d/yyyy (or m/d/yy not glued to more digits) token in free text.
_DATE_TOKEN = re.compile(r"\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}(?!\d))")

# Header cells (whitespace-normalized) -> unified schema fields. Both the
# modern (Local Area / Type) and pre-2021 (WIA Code / Type Code) header
# eras are covered; per-page mapping is built from each table's own
# header row, so column order never matters.
_HEADER_MAP = {
    "Company": "company",
    "Notice Date": "notice_date",
    "Effective Date": "effective_date",
    "Total Employees": "employees",
    "Location": "address",
    "NAICS Code": "industry",
    "Local Area": "county",
    "WIA Code": "county",
    "Type": "layoff_type",
    "Type Code": "layoff_type",
}

# Printed on every year page: "Local Area Codes" legend (pre-2021 pages
# publish the numeric code; the name spellings follow the modern pages).
_AREA_CODES = {
    "1": "Anne Arundel",
    "2": "Baltimore County",
    "3": "Baltimore City",
    "4": "Frederick",
    "5": "Lower Shore",
    "6": "Mid-Maryland",
    "7": "Montgomery",
    "8": "Prince George's",
    "9": "Southern Maryland",
    "10": "Susquehanna",
    "11": "Upper Shore",
    "12": "Western Maryland",
    "13": "Statewide",
}

# Printed on every year page: "Type Codes" legend.
_TYPE_CODES = {"1": "Plant Closure", "2": "Mass Layoff"}

# Vendored from BLN warn-transformer transformers/md.py date_corrections —
# only the entries the first-date rule cannot derive (keys are
# whitespace-normalized; values are the ISO date of BLN's datetime).
_DATE_CORRECTIONS = {
    "3/3020/17": "2017-03-20",  # glued-digit typo for 3/20/2017
    "5/62011": "2011-05-06",
    "4/82011": "2011-04-08",
    "7/62011": "2011-07-06",
    "8/2017-12/2018": "2017-08-01",  # month-only span: first month
    "12/2017-8/2018": "2017-12-01",
    "3/2018-8/2018": "2018-03-01",
    "2/29/2014": "2014-02-28",  # 2014 is not a leap year
    "4th quarter of this year": "2012-09-01",
    "10/230/2023": "2023-10-23",
    "6/30/204": "2024-06-30",  # dropped-digit year typo
    "7/24/1969": "2024-07-24",  # year typo on the source page
    "01/301/2026": "2026-01-31",
}

# Vendored verbatim from BLN warn-transformer transformers/md.py
# jobs_corrections (keys whitespace-normalized). None = the state
# published no usable count (becomes employees=0 in the unified schema).
_JOBS_CORRECTIONS = {
    "103 (REVISED) 10/22/2020 108": 103,
    "1100-1200 (MDDCVA)": 1100,
    "TBD": None,
    "approx. 150": 150,
    "Initially 110 (Possibly as high as 160)": 110,
    "Unknown": None,
    "Starting with 35 leading up to several hundred by December 2013": 35,
    "Total 106 (at this time number impacted at this location is unknown)":
        106,
    "Not sure of the number of impacted workers in MD at this time": None,
    "Unknown at this time": None,
    "60-70 in Maryland": 60,
    "unknown at this time": None,
    "8 additional": 8,
    "Total 35 (At this time number impacted at this location is unknown.)":
        35,
    "Not known": None,
    "N/A": None,
    "9 50": 59,
    "3 (remote workers from MD)": 3,
    "50 - 60": 50,
    "5 (Remote workers in MD)": 5,
    "3 (Remote workers in MD)": 3,
}


def _clean_text(text):
    """Collapse an HTML cell to one clean line (vendored from BLN md.py)."""
    if text is None:
        return ""
    return _WS.sub(" ", str(text)).strip()


def _clean_date(value):
    """First date in the string as a guaranteed-ISO value, or None."""
    text = _clean_text(value)
    if not text:
        return None
    if text in _DATE_CORRECTIONS:
        return _DATE_CORRECTIONS[text]
    match = _DATE_TOKEN.search(text)
    if not match:
        return None
    iso = warn_monitor._safe_date(match.group(0))
    # _safe_date echoes unparseable input back; never let non-ISO through.
    return iso if iso and _ISO.match(iso) else None


def _clean_jobs(value):
    """Affected-worker count as int, or None when the state published none."""
    text = _clean_text(value)
    if not text:
        return None
    for candidate in (text, text.replace(",", "")):
        if candidate in _JOBS_CORRECTIONS:
            return _JOBS_CORRECTIONS[candidate]
    count = warn_monitor._safe_int(text)
    if count is None:
        # Annotated counts ("150 statewide"-style) keep the leading figure.
        match = re.match(r"\d[\d,]*", text)
        if match:
            count = warn_monitor._safe_int(match.group(0))
    return count


def _extract_rows(html: str) -> list:
    """Record dicts (unified field names) from one year page's table.

    Table walk vendored from BLN warn-scraper warn/scrapers/md.py: the
    page's first table is the log; the header row maps columns by name
    (handling both the WIA Code/Type Code and Local Area/Type eras).
    """
    soup = BeautifulSoup(html, "html5lib")
    tables = soup.find_all("table")
    if not tables:
        return []
    columns = None
    rows = []
    for tr in tables[0].find_all("tr"):
        cells = [_clean_text(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if "Company" in cells and "Notice Date" in cells:
            columns = [_HEADER_MAP.get(c) for c in cells]
            continue
        if columns is None:
            continue  # preamble rows before the header
        rows.append(
            {col: val for col, val in zip(columns, cells) if col is not None}
        )
    return rows


class MarylandDOL(Source):
    code = "md"
    name = "Maryland"
    agency = "Maryland Department of Labor"
    source_url = _URL
    cadence = "daily"

    def _get(self, url: str, session: requests.Session) -> str:
        """Polite GET with retries and BLN's TLS-verification fallback."""
        last_error = None
        verify = True
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = session.get(
                    url, headers={"User-Agent": _UA}, timeout=60, verify=verify
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp.text
            except requests.exceptions.SSLError as err:
                # Vendored workaround from BLN md.py: the site's TLS setup
                # intermittently breaks handshakes (seen November 2024).
                log.warning(f"[MD] TLS failure on {url}; retrying unverified")
                verify = False
                last_error = err
            except requests.RequestException as err:
                last_error = err
        raise RuntimeError(f"MD fetch failed for {url}: {last_error}")

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        main_html = self._get(self.source_url, session)
        soup = BeautifulSoup(main_html, "html.parser")
        hrefs = []
        for a in soup.find_all("a", {"class": "sub"}):
            href = (a.get("href") or "").strip()
            if _YEAR_PAGE.match(href) and href not in hrefs:
                hrefs.append(href)
        pages = [{"name": "warn.shtml", "html": main_html}]
        for href in hrefs:
            time.sleep(1.1)  # max 1 request/second, per BLN's backoff
            pages.append({"name": href, "html": self._get(_BASE + href, session)})
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source_url": self.source_url,
            "pages": pages,
        }
        raw = Path(self.paths.raw)
        raw.write_text(json.dumps(payload), encoding="utf-8")
        log.info(f"[MD] fetched {len(pages)} pages")
        return True, raw

    def parse(self, raw_path) -> pd.DataFrame:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        records = []
        for page in payload["pages"]:
            for row in _extract_rows(page["html"]):
                company = row.get("company", "")
                if not company:
                    continue  # company is required
                employees = _clean_jobs(row.get("employees"))
                records.append(
                    {
                        "company": company,
                        "notice_date": _clean_date(row.get("notice_date")),
                        "effective_date": _clean_date(row.get("effective_date")),
                        "employees": employees if employees is not None else 0,
                        "layoff_type": _TYPE_CODES.get(
                            row.get("layoff_type", ""),
                            row.get("layoff_type", ""),
                        ),
                        "county": _AREA_CODES.get(
                            row.get("county", ""), row.get("county", "")
                        ),
                        "address": row.get("address", ""),
                        "industry": row.get("industry", ""),
                    }
                )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "county",
                "address",
                "industry",
            ],
        )
        # The current-year log can transiently overlap the newest archive
        # page around New Year; identical rows collapse to one.
        out = out.drop_duplicates()
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
