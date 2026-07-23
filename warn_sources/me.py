"""
warn_sources.me
---------------
Maine — WARN notices published by the Department of Labor through the
America's JobLink platform at
https://joblink.maine.gov/search/warn_lookups.

The lookup is a Rails/Ransack search app: a GET with ``q[notice_on_gteq]``
/ ``q[notice_on_lteq]`` date bounds returns an HTML table (Employer, City,
ZIP, LWIB Area, Notice Date, WARN Type); each employer cell links to a
detail page (``/search/warn_lookups/<id>``) whose definition list carries
the street address and the affected-employee count — neither appears in
the search grid, so every record costs one extra polite request. The walk
is vendored from Big Local News' Apache-2.0 warn-scraper (job_center
platform: warn/platforms/job_center/site.py + utils.py, driven by
warn/scrapers/me.py): search one calendar year at a time in reverse
chronological order, follow ``a.next_page`` pagination within a year, and
dedupe by record number since JobLink paging can repeat rows.

Field crosswalk (BLN warn_transformer/transformers/me.py, date_format
``%b %d, %Y``):

    employer                      -> company        (required; search grid)
    city                          -> city
    notice_date                   -> notice_date    ("Jan 13, 2026")
    number_of_employees_affected  -> employees      (detail page; 0 when
                                                     not published)
    warn_type                     -> layoff_type    (the state's own notice
                                                     taxonomy, e.g. "WARN")
    address                       -> address        (detail page; newlines
                                                     collapsed BLN-style)

BLN's ME transformer collapses location to ``city or address or
lwib_area``; the unified schema keeps city and address as separate
columns, so both are carried as-is and LWIB Area (not a schema field) is
dropped. BLN's default 10,000 sanity cap on employee counts is honored
(ME defines no jobs_corrections). JobLink feeds publish no effective date
(EXPANSION_RESEARCH.md §5) and no county or industry — those fields stay
absent, never synthesized from the notice date. Backfill: 2019-01-01
(task intel; BLN's own stop year is 2012 but this tracker's ME horizon
is 2019).
"""

import csv
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

_URL = "https://joblink.maine.gov/search/warn_lookups"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# First year this tracker backfills from (task intel / manual research).
_STOP_YEAR = 2019

_MAX_PAGES = 50  # per-year pagination hard cap

_MAX_JOBS = 10000  # BLN BaseTransformer maximum_jobs sanity cap

# Search-grid headers as rendered by the live site (2026-07 probe).
_GRID_COLUMNS = ["Employer", "City", "ZIP", "LWIB Area", "Notice Date", "WARN Type"]

# Consolidated raw CSV columns (BLN job_center header order).
_CSV_COLUMNS = [
    "employer",
    "notice_date",
    "number_of_employees_affected",
    "warn_type",
    "city",
    "zip",
    "lwib_area",
    "address",
    "record_number",
    "detail_page_url",
]


def _clean_text(text) -> str:
    """Collapse a cell to one clean line."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _search_params(start_date: str, end_date: str) -> dict:
    """Ransack query kwargs (vendored from BLN job_center/site.py)."""
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


def _parse_search_page(html) -> list:
    """Rows (employer/city/zip/lwib_area/notice_date/warn_type + detail
    path) from one search-results page; [] when the app reports no matches.

    A data row is identified structurally: one <td> per grid column, the
    first cell linking to a ``/search/warn_lookups/<id>`` detail page —
    header sort links live in <th> cells and never match.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        if "no matches for your search results" in soup.get_text():
            return []
        raise ValueError("ME search page: no results table and no "
                         "'no matches' message — layout changed?")
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(_GRID_COLUMNS):
            continue  # header row / layout chrome
        link = cells[0].find("a", href=re.compile(r"/search/warn_lookups/\d+"))
        if link is None:
            continue
        path = link["href"].strip()
        rows.append(
            {
                "employer": _clean_text(cells[0].get_text()),
                "city": _clean_text(cells[1].get_text()),
                "zip": _clean_text(cells[2].get_text()),
                "lwib_area": _clean_text(cells[3].get_text()),
                "notice_date": _clean_text(cells[4].get_text()),
                "warn_type": _clean_text(cells[5].get_text()),
                "detail_page_url": urllib.parse.urljoin(_URL, path),
                "record_number": path.rstrip("/").rsplit("/", 1)[-1],
            }
        )
    return rows


def _next_page_url(html):
    """URL of the pager's next page, or None (BLN job_center/site.py)."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", class_="next_page")
    if link is None or not link.get("href"):
        return None
    return urllib.parse.urljoin(_URL, link["href"].strip())


def _parse_detail_page(html) -> dict:
    """Address + employee count from a detail page's definition list."""
    soup = BeautifulSoup(html, "html.parser")
    titles = [
        _clean_text(t.get_text()).lower().replace(" ", "_")
        for t in soup.select(".definition-list__title")
    ]
    values = [
        d.get_text("\n", strip=True)
        for d in soup.select(".definition-list__definition")
    ]
    data = dict(zip(titles, values))
    # Newlines -> "; " exactly as BLN's job_center _prepare_row does.
    address = re.sub(r"\n+", "; ", data.get("address", "").strip())
    return {
        "address": _clean_text(address),
        "number_of_employees_affected": _clean_text(
            data.get("number_of_employees_affected", "")
        ),
    }


class MaineJobLink(Source):
    code = "me"
    name = "Maine"
    agency = "Maine Department of Labor"
    source_url = _URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url, params=None):
        """One polite GET: 60s timeout, 3 attempts, backoff, 1 req/s."""
        time.sleep(1)  # max 1 request/second/host
        last_error = None
        for attempt in range(1, 4):
            try:
                resp = session.get(url, params=params, timeout=60)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[ME] GET {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _walk_year(self, session, year: int) -> list:
        """All search rows for one calendar year, following pagination."""
        params = _search_params(f"{year}-01-01", f"{year}-12-31")
        resp = self._get(session, _URL, params=params)
        rows = _parse_search_page(resp.text)
        next_url = _next_page_url(resp.text)
        pages = 1
        while next_url and pages < _MAX_PAGES:
            pages += 1
            resp = self._get(session, next_url)
            rows.extend(_parse_search_page(resp.text))
            next_url = _next_page_url(resp.text)
        log.info(f"[ME] {year}: {len(rows)} rows across {pages} page(s)")
        return rows

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        # Reverse chronological year walk (BLN job_center/utils.py), then
        # dedupe by record number — JobLink paging can repeat rows.
        rows: dict = {}
        current_year = datetime.now(timezone.utc).year
        for year in range(current_year, _STOP_YEAR - 1, -1):
            for row in self._walk_year(session, year):
                rows.setdefault(row["record_number"], row)
        if not rows:
            raise ValueError("ME JobLink search returned no rows at all")

        # The employee count and address only exist on detail pages.
        for row in rows.values():
            resp = self._get(session, row["detail_page_url"])
            row.update(_parse_detail_page(resp.text))

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            for row in rows.values():
                writer.writerow({col: row.get(col, "") for col in _CSV_COLUMNS})

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
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
        """ISO date or None; JobLink renders %b %d, %Y (BLN date_format)."""
        text = _clean_text(value)
        if not text:
            return None
        try:
            return datetime.strptime(text, "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = warn_monitor._safe_date(text)
            if iso and re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
                return iso
            return None

    @staticmethod
    def _clean_employees(value) -> int:
        """Int count; 0 when unpublished or beyond BLN's 10,000 cap."""
        n = warn_monitor._safe_int(value)
        if n is None or n < 0 or n > _MAX_JOBS:
            return 0
        return n

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _clean_text(row.get("employer", ""))
            if not company or company.lower() == "employer":
                continue  # company is required; drop stray header rows
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("notice_date")),
                    # ME/JobLink publishes no effective date — never
                    # synthesized from the notice date.
                    "employees": self._clean_employees(
                        row.get("number_of_employees_affected", "")
                    ),
                    "layoff_type": _clean_text(row.get("warn_type", "")),
                    "city": _clean_text(row.get("city", "")),
                    "address": _clean_text(row.get("address", "")),
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "employees",
                "layoff_type",
                "city",
                "address",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
