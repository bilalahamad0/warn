"""
warn_sources.vt
---------------
Vermont — WARN notices from the Department of Labor's JobLink portal at
https://www.vermontjoblink.com/search/warn_lookups.

The portal is an America's Job Center (JobLink) app: a date-filtered
search form returns a results table (Employer, City, ZIP, LWIB Area,
Notice Date, WARN Type) whose employer links lead to detail pages that
publish the street address and the affected-employee count as a
definition list. The walk is vendored from Big Local News' Apache-2.0
warn-scraper (warn/platforms/job_center/site.py + warn/scrapers/vt.py):
search one calendar year at a time, follow ``a.next_page`` pagination,
fetch every detail page, and dedupe on the record number (date searches
can repeat rows across paged results). A mid-walk HTTP failure aborts
the fetch — a truncated crawl must never be written, or the diff engine
would report phantom withdrawals. Years with no notices render a
"no matches for your search results" message (2023 really is empty).

Field crosswalk — BLN's transformer (warn_transformer/transformers/vt.py)
maps ``employer``->company, ``notice_date`` (format ``%b %d, %Y``, e.g.
"Jan 22, 2026")->date, ``number_of_employees_affected``->jobs with the
correction ``9999999 -> unknown`` (the portal's sentinel for "count not
provided"), and a single ``location`` falling back city -> address ->
lwib_area. This schema keeps city and address as separate columns, so:

    employer                      -> company        (required,
                                                     HTML-unescaped)
    notice_date                   -> notice_date    (%b %d, %Y -> ISO)
    number_of_employees_affected  -> employees      (int; 0 when blank
                                                     or the 9999999
                                                     unknown sentinel)
    city                          -> city           (search results col)
    address (detail page)         -> address        (newlines -> "; ",
                                                     BLN _prepare_row)

JobLink feeds publish no effective date — never synthesized from the
notice date. No county (LWIB Area is a workforce-board area, not a
county), no industry. The "WARN Type" column is a WARN/non-WARN flag,
not a closure/layoff taxonomy, so it is kept in the raw CSV for audit
but not mapped to layoff_type (BLN's transformer drops it too).
Backfill: 2019-01-01 onward, refetched in full each run (~40 notices —
Vermont is tiny).
"""

import csv
import html as html_mod
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_URL = "https://www.vermontjoblink.com/search/warn_lookups"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_START_YEAR = 2019  # backfill depth chosen for this tracker

# Search results table header as rendered by the live site.
_HEADERS = ["Employer", "City", "ZIP", "LWIB Area", "Notice Date", "WARN Type"]

# Consolidated CSV layout (superset of BLN's job_center headers we use).
_CSV_FIELDS = [
    "employer",
    "notice_date",
    "number_of_employees_affected",
    "warn_type",
    "city",
    "zip",
    "lwib_area",
    "address",
    "record_number",
]

_NO_RESULTS_MSG = "no matches for your search results"

# The portal's sentinel for "employee count not provided"
# (BLN jobs_corrections: {9999999: None}).
_UNKNOWN_JOBS_SENTINEL = 9999999

_MAX_JOBS = 10000  # BLN BaseTransformer maximum_jobs sanity cap

_MAX_PAGES_PER_YEAR = 50  # hard cap; VT has never needed a second page


def _clean_field(text):
    """Unescape HTML entities and collapse whitespace (BLN site.py)."""
    if text is None:
        return ""
    text = html_mod.unescape(str(text))
    return re.sub(r"\s+", " ", text).strip()


def _search_params(start_date, end_date):
    """Date-range search query (vendored from BLN site._search_kwargs)."""
    return {
        "utf8": "✓",
        "q[employer_name_cont]": "",
        "q[main_contact_contact_info_addresses_full_location_city_matches]": "",
        "q[zipcode_code_start]": "",
        "q[service_delivery_area_id_eq]": "",
        "q[notice_on_gteq]": start_date,
        "q[notice_on_lteq]": end_date,
        "q[notice_eq]": "",
        "commit": "Search",
    }


def _parse_search_rows(html) -> list:
    """Rows from one search results page (vendored from BLN site.py).

    Returns [] for a genuine empty-result page; raises ValueError when
    the page matches neither the results-table nor the no-results layout
    so silent site redesigns are caught instead of parsed as zero rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        if _NO_RESULTS_MSG in soup.text:
            return []
        raise ValueError("VT search page: no results table and no "
                         "'no matches' message — layout changed?")
    headers = [_clean_field(th.get_text()) for th in table.find_all("th")]
    if headers != _HEADERS:
        raise ValueError(f"VT search page: unexpected headers {headers!r}")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(_HEADERS):
            continue  # header row / layout chrome
        link = cells[0].find("a")
        if link is None or "/warn_lookups/" not in (link.get("href") or ""):
            continue
        url_path = link["href"].strip()
        rows.append(
            {
                "employer": _clean_field(cells[0].get_text()),
                "city": _clean_field(cells[1].get_text()),
                "zip": _clean_field(cells[2].get_text()),
                "lwib_area": _clean_field(cells[3].get_text()),
                "notice_date": _clean_field(cells[4].get_text()),
                "warn_type": _clean_field(cells[5].get_text()),
                "detail_path": url_path,
                "record_number": url_path.rstrip("/").rsplit("/", 1)[-1],
            }
        )
    return rows


def _parse_detail(html) -> dict:
    """Definition-list fields of a detail page (vendored from BLN site.py)."""
    payload = {"address": "", "number_of_employees_affected": ""}
    soup = BeautifulSoup(html, "html.parser")
    titles = [
        t.get_text().strip().lower().replace(" ", "_")
        for t in soup.select(".definition-list__title")
    ]
    values = [
        d.get_text().strip()
        for d in soup.select(".definition-list__definition")
    ]
    payload.update(dict(zip(titles, values)))
    # One-line the multi-line street address (BLN utils._prepare_row).
    payload["address"] = re.sub(r"\n+\s*", "; ", payload["address"].strip())
    return payload


def _next_page_link(html):
    """Absolute URL of the next results page, or None (BLN site.py)."""
    soup = BeautifulSoup(html, "html.parser")
    next_page = soup.find("a", class_="next_page")
    if next_page is None or not next_page.get("href"):
        return None
    return urllib.parse.urljoin(_URL, next_page["href"].strip())


class VermontJobLink(Source):
    code = "vt"
    name = "Vermont"
    agency = "Vermont Department of Labor"
    source_url = _URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _request(self, session, url, params=None):
        """One polite GET: 60s timeout, 3 attempts, backoff, 1 req/s."""
        last_error = None
        for attempt in range(1, 4):
            try:
                time.sleep(1)  # max 1 request/second/host
                resp = session.get(url, params=params, timeout=60)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[VT] GET {url} attempt {attempt}: {e}")
        raise last_error

    def _walk_year(self, session, year) -> list:
        """All search rows for one calendar year, following pagination."""
        params = _search_params(f"{year}-01-01", f"{year}-12-31")
        resp = self._request(session, _URL, params=params)
        rows = _parse_search_rows(resp.text)
        next_url = _next_page_link(resp.text)
        pages = 1
        while next_url and pages < _MAX_PAGES_PER_YEAR:
            resp = self._request(session, next_url)
            rows.extend(_parse_search_rows(resp.text))
            next_url = _next_page_link(resp.text)
            pages += 1
        log.info(f"[VT] {year}: {len(rows)} rows across {pages} page(s)")
        return rows

    def _walk(self, session) -> list:
        """Full 2019-present crawl, detail pages included, deduped."""
        rows = []
        current_year = datetime.now(timezone.utc).year
        for year in range(current_year, _START_YEAR - 1, -1):
            rows.extend(self._walk_year(session, year))

        seen = set()
        deduped = []
        for row in rows:
            if row["record_number"] in seen:
                continue  # date searches can repeat rows across pages
            seen.add(row["record_number"])
            deduped.append(row)

        for row in deduped:
            detail_url = urllib.parse.urljoin(_URL, row.pop("detail_path"))
            detail = _parse_detail(self._request(session, detail_url).text)
            row["address"] = detail["address"]
            row["number_of_employees_affected"] = detail[
                "number_of_employees_affected"
            ]
        return deduped

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        rows = self._walk(session)
        if not rows:
            raise ValueError("VT JobLink crawl returned no rows")

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in _CSV_FIELDS})

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = force or new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(value):
        """ISO date or None; the portal renders '%b %d, %Y' (BLN format)."""
        text = _clean_field(value)
        if not text:
            return None
        try:
            return datetime.strptime(text, "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = warn_monitor._safe_date(text)
            if iso and re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
                return iso
            return None

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _clean_field(row.get("employer", ""))
            if not company or company == "Employer":
                continue  # company is required; drop stray header rows
            employees = warn_monitor._safe_int(
                row.get("number_of_employees_affected", "")
            )
            if employees == _UNKNOWN_JOBS_SENTINEL:
                employees = None  # BLN jobs_corrections {9999999: None}
            if employees is None or employees < 0 or employees > _MAX_JOBS:
                employees = 0  # count not published / implausible
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("notice_date")),
                    # VT publishes no effective date, county, or industry.
                    "employees": employees,
                    "city": _clean_field(row.get("city", "")),
                    "address": _clean_field(row.get("address", "")),
                }
            )
        out = pd.DataFrame(
            records,
            columns=["company", "notice_date", "employees", "city", "address"],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
