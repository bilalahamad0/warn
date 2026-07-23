"""
warn_sources.az
---------------
Arizona — WARN notices via the Arizona Job Connection JobLink search app
(https://www.azjobconnection.gov/search/warn_lookups), an America's Job
Center platform site run for the Arizona Department of Economic Security.

Scrape strategy vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/az.py + warn/platforms/job_center/site.py, utils.py):
date-range searches one calendar year at a time in reverse chronological
order, following the ``a.next_page`` pagination links, then visiting each
notice's detail page for the two fields the results table omits —
"Number of Employees Affected" and "Address" (newlines collapsed to
"; ", as BLN does). Paged results can repeat a record across pages, so
rows are deduplicated by record number. Backfill starts at 2019 (BLN
scrapes back to 2010; capped here to keep runtime sane — every detail
page is a separate request throttled to 1/second).

Field mapping vendored exactly from BLN's Apache-2.0 warn-transformer
(warn_transformer/transformers/az.py): company <- "employer",
notice_date <- "notice_date" parsed with BLN's date_format "%b %d, %Y",
employees <- "number_of_employees_affected" (AZ has no jobs_corrections;
BLN's 10,000 BaseTransformer sanity cap is honored). Arizona publishes
NO effective date — JobLink feeds lack one and it is never synthesized
from the notice date — and no county or industry, so those columns are
not emitted. The results table's "WARN Type" column (observed value:
"WARN") is carried as layoff_type; city and detail-page address are
carried as-is (some rows list an out-of-state corporate HQ city — that
is what the state publishes). ZIP and LWIB Area are dropped (not
unified-schema fields).
"""

import html as html_mod
import json
import logging
import re
import time
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

SEARCH_URL = "https://www.azjobconnection.gov/search/warn_lookups"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# BLN scrapes AZ back to 2010; capped at 2019 to keep runtime sane (one
# throttled request per notice detail page).
BACKFILL_YEAR = 2019

# BLN warn-transformer az.py: date_format = "%b %d, %Y" (e.g. "Feb 26, 2026").
_DATE_FORMAT = "%b %d, %Y"

_MAX_JOBS = 10000  # BLN BaseTransformer maximum_jobs sanity cap
_MIN_YEAR = 2000   # anything earlier in a date cell is a typo

_DELAY = 1.0  # politeness: max 1 request/second/host

# Arizona publishes no effective date, county, or industry.
_OUTPUT_COLUMNS = [
    "company",
    "notice_date",
    "employees",
    "layoff_type",
    "city",
    "address",
]


def _clean_str(val) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", html_mod.unescape(str(val))).strip()


def _clean_date(val):
    """Raw feed date text -> ISO YYYY-MM-DD string or None (never junk)."""
    s = _clean_str(val)
    if not s:
        return None
    iso = None
    try:
        iso = datetime.strptime(s, _DATE_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        iso = warn_monitor._safe_date(s)
    if not iso or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(iso)):
        return None
    if not _MIN_YEAR <= int(str(iso)[:4]) <= date.today().year + 10:
        return None
    return str(iso)


def _clean_employees(val) -> int:
    """Raw employee-count text -> int (0 when no usable count published)."""
    n = warn_monitor._safe_int(val)
    if n is None or n < 0 or n > _MAX_JOBS:
        return 0
    return n


def _search_params(start_date: str, end_date: str) -> dict:
    """Query params for a date-range search (BLN Site._search_kwargs)."""
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


def _build_page_url(url_path: str) -> str:
    """Absolute URL for a site-relative path (BLN Site._build_page_url)."""
    bits = urllib.parse.urlsplit(SEARCH_URL)
    return urllib.parse.urljoin(f"{bits.scheme}://{bits.netloc}", url_path.strip())


def _parse_search_results(html: str) -> list:
    """Results-page HTML -> row dicts (BLN Site._parse_search_results).

    Returns [] when the app reports no matches for the date range; raises
    if neither results nor the no-matches message are present (layout
    drift guard). Header rows use <th> cells so they never leak through.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 6 or cells[0].a is None:
            continue
        url_path = cells[0].a["href"].strip()
        rows.append(
            {
                "employer": _clean_str(cells[0].text),
                "city": cells[1].text.strip(),
                "zip": cells[2].text.strip(),
                "lwib_area": cells[3].text.strip(),
                "notice_date": cells[4].text.strip(),
                "warn_type": cells[5].text.strip(),
                "record_number": url_path.rsplit("/", 1)[-1],
                "detail_page_url": _build_page_url(url_path),
            }
        )
    if not rows and "no matches for your search results" not in soup.text:
        raise RuntimeError(
            "AZ search page had neither results nor the no-matches message "
            "(layout changed?)"
        )
    return rows


def _next_page_link(html: str):
    """URL of the next results page, or None (BLN Site._next_page_link)."""
    soup = BeautifulSoup(html, "html.parser")
    next_page = soup.find("a", class_="next_page")
    if next_page is None or not next_page.get("href"):
        return None
    return _build_page_url(next_page["href"])


def _parse_detail_page(html: str) -> dict:
    """Detail-page HTML -> field dict (BLN Site._parse_detail_page)."""
    payload = {"number_of_employees_affected": "", "address": ""}
    soup = BeautifulSoup(html, "html.parser")
    headers = [
        h.text.strip().lower().replace(" ", "_")
        for h in soup.select(".definition-list__title")
    ]
    values = [v.text.strip() for v in soup.select(".definition-list__definition")]
    payload.update(dict(zip(headers, values)))
    # BLN job_center utils._prepare_row: collapse multi-line addresses.
    address = re.sub(r"\n+", "; ", payload["address"].strip())
    payload["address"] = re.sub(r"[ \t]+", " ", address)
    return payload


class ArizonaJobConnection(Source):
    code = "az"
    name = "Arizona"
    agency = "Arizona Department of Economic Security"
    source_url = SEARCH_URL
    cadence = "daily"

    def _get(self, url, params=None, tries=3):
        """Polite GET: browser UA, 60s timeout, throttled, 3 backed-off tries."""
        last_err = None
        for attempt in range(tries):
            time.sleep(_DELAY + 2 * attempt)
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=60,
                )
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[AZ] GET {url} failed (try {attempt + 1}): {e}")
        raise last_err

    def _scrape_year(self, year: int) -> list:
        """All rows for one calendar year, following pagination."""
        rows = []
        url = SEARCH_URL
        params = _search_params(f"{year}-01-01", f"{year}-12-31")
        while url:
            html = self._get(url, params=params)
            rows.extend(_parse_search_results(html))
            url, params = _next_page_link(html), None
        return rows

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()

        rows = []
        for year in range(date.today().year, BACKFILL_YEAR - 1, -1):
            rows.extend(self._scrape_year(year))

        # Paged results can repeat a record across pages (BLN dedupes too).
        seen, deduped = set(), []
        for row in rows:
            if row["record_number"] in seen:
                continue
            seen.add(row["record_number"])
            deduped.append(row)

        # Detail pages carry the employee count and street address. One
        # notice's page failing shouldn't nuke the whole state, but if every
        # single one fails something systemic is wrong — bail out.
        failures = 0
        for row in deduped:
            try:
                detail = _parse_detail_page(self._get(row["detail_page_url"]))
            except requests.RequestException as e:
                failures += 1
                log.warning(f"[AZ] detail page {row['detail_page_url']}: {e}")
                detail = {"number_of_employees_affected": "", "address": ""}
            row["number_of_employees_affected"] = detail[
                "number_of_employees_affected"
            ]
            row["address"] = detail["address"]
        if deduped and failures == len(deduped):
            raise RuntimeError("AZ: every detail-page fetch failed")

        # Deterministic raw content so the change hash only moves on real
        # feed changes, not response ordering.
        deduped.sort(key=lambda r: int(r["record_number"]), reverse=True)
        self.paths.raw.write_text(json.dumps(deduped, indent=1))

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = force or new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": SEARCH_URL,
                "backfill_year": BACKFILL_YEAR,
                "row_count": len(deduped),
                "detail_failures": failures,
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        rows = json.loads(Path(raw_path).read_text())
        out = []
        for r in rows:
            company = _clean_str(r.get("employer"))
            if not company or company == "Employer":  # junk/header echo
                continue
            out.append(
                {
                    "company": company,
                    "notice_date": _clean_date(r.get("notice_date")),
                    "employees": _clean_employees(
                        r.get("number_of_employees_affected")
                    ),
                    "layoff_type": _clean_str(r.get("warn_type")),
                    "city": _clean_str(r.get("city")),
                    "address": _clean_str(r.get("address")),
                }
            )
        df = pd.DataFrame(out, columns=_OUTPUT_COLUMNS)
        # pandas coerces None -> NaN when building from dicts; the unified
        # schema wants missing dates as None.
        col = df["notice_date"].astype(object)
        df["notice_date"] = col.where(col.notna(), None)
        return df
