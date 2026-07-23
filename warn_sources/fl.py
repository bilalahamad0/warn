"""
warn_sources.fl
---------------
Florida — FloridaCommerce (formerly DEO) REACT WARN notice listings.

The interactive app at
``https://reactwarn.floridajobs.org/WarnList/Records?year=<YYYY>`` serves
paginated HTML tables per listing year (100 rows/page, next-page links in
the table footer). Each run fetches the current and previous listing years
with full pagination (politely: browser UA, 1 request/second, 3 retries)
and consolidates the raw cell text into a JSON file at ``paths.raw``.
Older years are only published as per-year PDFs
(``viewPreviousYearsPDF?year=...``) and are not fetched, so backfill depth
is roughly 18 months; the cumulative file preserves history from there.
The old floridajobs.org listing URLs are dead.

Scrape flow and field crosswalk vendored from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/fl.py) and warn-transformer
(warn_transformer/transformers/fl.py). Quirks honored from BLN:

* the page's line breaks are malformed ``</br>`` tags — replaced with
  newlines before parsing (BLN scraper);
* the "Company Name" cell is a multi-line block — company on the first
  line, street-address lines in the middle, ``CITY, FL, ZIP`` last.
  company = first line (BLN ``transform_company``); city = last line's
  first comma segment (BLN ``transform_location``); the middle lines are
  the site address, which FL genuinely publishes;
* the "Layoff Date" cell is a ``start thru end`` range — the effective
  date is the range start (BLN ``transform_date`` splits on "thru");
* dates are ``%m-%d-%y`` (older feed years also used ``%m/%d/%Y``) —
  BLN's ``date_format`` tuple, tried in order, unparseable -> None.

FL publishes: company, notice date ("State Notification Date"), effective
date ("Layoff Date"), employees affected, industry, and the site
address/city embedded in the company cell. No county and no layoff-type
column exist, so those unified fields are omitted — never fabricated.
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

BASE_URL = "https://reactwarn.floridajobs.org"
RECORDS_URL = BASE_URL + "/WarnList/Records?year={year}"

# Raw table columns, in page order. Vendored from BLN warn-scraper
# (warn/scrapers/fl.py FIELDS); Attachment is a download button, dropped.
FIELDS = [
    "Company Name",
    "State Notification Date",
    "Layoff Date",
    "Employees Affected",
    "Industry",
    "Attachment",
]

# BLN warn-transformer transformers/fl.py date_format, tried in order.
DATE_FORMATS = ("%m-%d-%y", "%m/%d/%Y")

MAX_PAGES = 50  # hard safety cap per year; real years run 2-6 pages

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

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_date(val):
    """FL date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's transform_date: flatten whitespace, take the part before
    "thru" (the "Layoff Date" cell is a "start thru end" range whose start
    is the effective date), then try the known FL formats in order.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    if not text:
        return None
    text = text.split(" thru")[0].strip()
    text = text.split("thru")[0].strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort for format drift; gated on ISO shape so a bad cell
    # becomes None, never an echoed junk string.
    iso = warn_monitor._safe_date(text)
    if iso is not None and _ISO_RE.match(iso):
        return iso
    return None


def _html_to_rows(page_html):
    """One Records page -> list of raw-cell dicts keyed by FIELDS.

    Adapted from BLN warn-scraper _html_to_rows: fix the site's malformed
    ``</br>`` line breaks first so multi-line cells keep their structure.
    """
    soup = BeautifulSoup(page_html.replace("</br>", "\n"), "html5lib")
    table = soup.find("table")
    if table is None:
        return []
    tbody = table.find("tbody") or table
    rows = []
    for tr in tbody.find_all("tr"):
        cells = [td.get_text("\n").strip() for td in tr.find_all("td")]
        if not cells or not any(cells):
            continue
        rows.append(dict(zip(FIELDS, cells)))
    return rows


def _next_page_url(page_html, next_page):
    """Absolute URL of the footer link to ``next_page``, or None on the
    last page. (BLN follows the same tfoot links; ``(?!\\d)`` keeps
    page=2 from matching page=20.)"""
    soup = BeautifulSoup(page_html, "html5lib")
    footer = soup.find("tfoot")
    if footer is None:
        return None
    link = footer.find("a", href=re.compile(rf"page={next_page}(?!\d)"))
    if link is None:
        return None
    return BASE_URL + link.get("href")


class FloridaCommerce(Source):
    code = "fl"
    name = "Florida"
    agency = "Florida Department of Commerce (FloridaCommerce)"
    source_url = "https://reactwarn.floridajobs.org/WarnList/Records"
    cadence = "twice-daily"

    # -- fetch ---------------------------------------------------------------

    def _request(self, session, url, tries=3):
        """Polite GET with retries; returns a Response or None."""
        resp = None
        for attempt in range(tries):
            if attempt:
                time.sleep(2 * attempt)  # backoff between retries
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                log.warning(f"[FL] request error for {url}: {e}")
                continue
            if resp.status_code in (200, 404):
                return resp  # 404 = year not published; no point retrying
            log.warning(
                f"[FL] bad response ({resp.status_code}) for {url}"
            )
        return resp

    def _year_rows(self, session, year):
        """All raw rows for one listing year, following pagination.

        Any mid-year failure raises: a partially fetched year would look
        like mass withdrawals to the diff engine. A 404 on the year's
        first page means the year isn't published yet (early January) and
        yields no rows.
        """
        url = RECORDS_URL.format(year=year)
        rows = []
        page = 1
        while url:
            if page > MAX_PAGES:
                raise RuntimeError(
                    f"FL feed: year {year} exceeded {MAX_PAGES} pages — "
                    "pagination loop?"
                )
            resp = self._request(session, url)
            if resp is not None and resp.status_code == 404 and page == 1:
                log.warning(f"[FL] year {year} not published yet (404)")
                return []
            if resp is None or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                raise RuntimeError(
                    f"FL feed: page {page} of year {year} failed ({status})"
                )
            rows.extend(_html_to_rows(resp.text))
            page += 1
            url = _next_page_url(resp.text, page)
            if url:
                time.sleep(1)  # politeness: max 1 request/second/host
        return rows

    def fetch(self, force: bool = False) -> tuple:
        """Scrape current + previous listing years into one JSON file."""
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        this_year = date.today().year
        years = (this_year - 1, this_year)
        rows = []
        for i, year in enumerate(years):
            if i:
                time.sleep(1)
            rows.extend(self._year_rows(session, year))
        if not rows:
            raise RuntimeError(
                "FL feed: no rows fetched for any year — site layout may "
                "have changed"
            )

        self.paths.ensure()
        payload = {
            "source": self.source_url,
            "years": list(years),
            "rows": rows,
        }
        self.paths.raw.write_text(json.dumps(payload, indent=1))
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated JSON -> unified-schema rows (BLN crosswalk)."""
        payload = json.loads(Path(raw_path).read_text())
        raw_rows = payload["rows"] if isinstance(payload, dict) else payload

        records = []
        for row in raw_rows:
            cell = row.get("Company Name") or ""
            lines = [ln.strip() for ln in str(cell).split("\n") if ln.strip()]
            if not lines:
                continue  # blank row
            company = lines[0]
            if company.upper() == "COMPANY NAME":
                continue  # stray repeated header row (BLN guard)
            city = ""
            address = ""
            if len(lines) > 1:
                # BLN transform_location: last line is "CITY, FL, ZIP".
                city = lines[-1].split(",")[0].strip()
                address = ", ".join(lines[1:-1])
            employees = warn_monitor._safe_int(row.get("Employees Affected"))
            industry = re.sub(
                r"\s+", " ", str(row.get("Industry") or "")
            ).strip()
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(
                        row.get("State Notification Date")
                    ),
                    "effective_date": _clean_date(row.get("Layoff Date")),
                    "employees": employees if employees is not None else 0,
                    "city": city,
                    "address": address,
                    "industry": industry,
                }
            )
        return pd.DataFrame(records)
