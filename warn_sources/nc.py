"""
warn_sources.nc
---------------
North Carolina — WARN notices published by the NC Department of Commerce
on per-year summary pages under
https://www.commerce.nc.gov/data-tools-reports/labor-market-data-tools/workforce-warn-reports

Custom scraper: NC is a gap state with no Big Local News prior art (absent
from both warn-scraper and warn-transformer), so the crosswalk below was
built from the live feed (probed 2026-07-22). Commerce publishes:

* the current year as an HTML table on
  ``.../workforce-warn-reports/report-workforce-warn-summary-list-<year>``
  (deep links churn — the page is re-discovered from the hub every run);
* past years on the "WARN Summary Report Archives" page. 2022-2025 are
  "Workforce WARN Listings" PDFs sharing the current table schema
  (``WARN Summary by County/Parish``) and are backfilled here via
  pdfplumber. 2014-2021 use several older, incompatible layouts
  (no City column, merged "Layoff/ Closure" values, one row per
  occupation group) and are left out of the backfill.

Both eras publish the same columns, mapped onto the unified schema as:

    company        <- "WARN Notice: WARN Notice Name"
    notice_date    <- "Date Received by NC"  (the received-by-state date,
                      per EXPANSION_RESEARCH §5; the employer-stamped
                      "Date of Notice" column is kept in the raw CSV for
                      audit but never promoted, and no date is ever
                      copied into another)
    effective_date <- "Effective Date"
    employees      <- "Number affected at this location" (NC lists
                      multi-site notices one row per location, sharing a
                      Warn Number — each row stays a separate record)
    layoff_type    <- "WARN notice type" + "Type of layoff or closure"
                      joined, e.g. "Closure Permanent" (the 2022 PDF
                      swaps the two columns' order; mapping is by header
                      text so it comes out identical)
    county         <- "County" minus the " County" suffix, so values
                      aggregate like other states ("Wake" not "Wake
                      County"); "N/A" (out-of-state HQ filings, whose
                      City reads e.g. "New York NY") becomes ""
    city / address <- "City" / "Address 1"

NC publishes no industry column. The state-native "Warn Number" stays in
the raw CSV only. Unparseable dates become None — never fabricated.
"""

import csv
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_HUB_URL = (
    "https://www.commerce.nc.gov/data-tools-reports/"
    "labor-market-data-tools/workforce-warn-reports"
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Current-year HTML report and modern-era (2022+) archive PDF links; the
# 2014-2021 archives use different URL slugs and are excluded by design.
_SUMMARY_RE = re.compile(r"report-workforce-warn-summary-list-(20\d{2})")
_LISTING_RE = re.compile(r"report-workforce-warn-listings-(20\d{2})")
_ARCHIVES_SLUG = "warn-summary-report-archives"

# Header-text crosswalk (whitespace-collapsed, lowercased). Identical for
# the HTML table and the 2022-2025 listing PDFs; PDF headers merely wrap
# ("Warn\nNumber"), which the collapse absorbs.
_HEADER_MAP = {
    "county": "county",
    "warn number": "warn_number",
    "date of notice": "date_of_notice",
    "date received by nc": "received_date",
    "effective date": "effective_date",
    "warn notice: warn notice name": "company",
    "warn notice type": "notice_type",
    "type of layoff or closure": "layoff_kind",
    "number affected at this location": "employees",
    "address 1": "address",
    "city": "city",
}

# Column order of the consolidated raw CSV.
_RAW_COLUMNS = [
    "year",
    "county",
    "warn_number",
    "date_of_notice",
    "received_date",
    "effective_date",
    "company",
    "notice_type",
    "layoff_kind",
    "employees",
    "address",
    "city",
]

_OUT_COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
]

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Modern-era Warn Numbers are the year + a 5-digit serial (e.g. 202500001).
_WARN_NO_RE = re.compile(r"^20\d{7}$")


def _squish(value) -> str:
    """Collapse all whitespace (incl. PDF cell wraps) to single spaces."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _header_mapping(cells):
    """Column index -> raw field if this row is the notice-table header."""
    mapping = {}
    for i, cell in enumerate(cells):
        target = _HEADER_MAP.get(_squish(cell).lower())
        if target:
            mapping[i] = target
    got = set(mapping.values())
    if {"warn_number", "company"} <= got:
        return mapping
    return None  # title banner, breakout table, or data row


def _rows_from_grid(grid, carry=None):
    """One table (list of cell-lists) -> (row dicts, mapping-for-carry).

    The header row is located by its text (column order differs between
    the 2022 PDF and later years). Multi-page PDF tables repeat rows
    without a header; ``carry`` re-uses the previous table's mapping, but
    only for rows of the same width carrying a real Warn Number — so the
    aggregate "Break Outs" tables in some archive PDFs can never be
    misread as notices. Title banners, repeated headers, the "Total Sum
    Count" footer and blank filler rows all fail the company/junk checks.
    """
    mapping, width, strict = None, None, False
    if carry:
        mapping, width = carry
        strict = True  # headerless continuation: validate warn numbers
    rows = []
    for raw_cells in grid:
        cells = [_squish(c) for c in raw_cells]
        header = _header_mapping(cells)
        if header:
            mapping, width, strict = header, len(cells), False
            continue
        if mapping is None or len(cells) != width:
            continue
        row = {
            field: cells[i]
            for i, field in mapping.items()
            if i < len(cells)
        }
        if not row.get("company"):
            continue  # banner / footer / filler
        if strict and not _WARN_NO_RE.match(row.get("warn_number", "")):
            continue  # headerless table that is not a continuation
        rows.append(row)
    return rows, (mapping, width) if mapping is not None else None


def _clean_date(value):
    """One NC date cell -> ISO YYYY-MM-DD or None — never a guess."""
    value = _squish(value)
    if not value or value.upper() == "N/A":
        return None
    parsed = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        iso = warn_monitor._safe_date(value)
        if not iso or not _ISO_RE.match(iso):
            return None  # unparseable free text -> no date
        parsed = datetime.strptime(iso, "%Y-%m-%d")
    if not 2010 <= parsed.year <= datetime.now().year + 6:
        return None  # obvious typo
    return parsed.strftime("%Y-%m-%d")


def _clean_employees(value):
    """One headcount cell -> int or None (BLN 10k sanity cap adopted)."""
    n = warn_monitor._safe_int(_squish(value))
    if n is not None and not 0 <= n <= 10000:
        return None
    return n


def _clean_county(value) -> str:
    """'Wake County' -> 'Wake'; the out-of-state 'N/A' marker -> ''."""
    county = _squish(value)
    if county.upper() == "N/A":
        return ""
    if county.endswith(" County"):
        county = county[: -len(" County")].strip()
    return county


def _write_raw_csv(rows, path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_RAW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


class NorthCarolinaCommerce(Source):
    code = "nc"
    name = "North Carolina"
    agency = "North Carolina Department of Commerce"
    source_url = _HUB_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _request(self, session, method, url, **kwargs):
        """One polite request: 60s timeout, 3 attempts, backoff."""
        kwargs.setdefault("timeout", 60)
        last_error = None
        for attempt in range(1, 4):
            try:
                resp = session.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[NC] {method} {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _discover(self, session):
        """Hub page -> ({current-year page URL: year}, archives URL)."""
        resp = self._request(session, "GET", _HUB_URL)
        soup = BeautifulSoup(resp.text, "html5lib")
        current, archives_url = {}, None
        for a in soup.find_all("a", href=True):
            href = urljoin(_HUB_URL, a["href"])
            match = _SUMMARY_RE.search(href)
            if match:
                current[href.split("#")[0]] = match.group(1)
            if _ARCHIVES_SLUG in href:
                archives_url = href.split("#")[0]
        if not current:
            raise ValueError("NC hub page: no current-year summary link")
        return current, archives_url

    def _archive_listings(self, session, archives_url):
        """Archives page -> {year: PDF URL} for the modern (2022+) era."""
        time.sleep(1)  # max 1 request/second/host
        resp = self._request(session, "GET", archives_url)
        soup = BeautifulSoup(resp.text, "html5lib")
        listings = {}
        for a in soup.find_all("a", href=True):
            href = urljoin(archives_url, a["href"])
            match = _LISTING_RE.search(href)
            if match:
                listings[match.group(1)] = href
        return listings

    def _listing_pdf(self, session, year, url):
        """Fetch one archive-year PDF, falling back to the cached copy
        in the state directory if Commerce errors out."""
        cache = self.paths.root / f"listing_{year}.pdf"
        time.sleep(1)
        try:
            content = self._request(session, "GET", url).content
            if not content.startswith(b"%PDF"):
                raise ValueError(f"listing {year}: response is not a PDF")
            cache.write_bytes(content)
        except Exception as e:
            if not cache.exists():
                raise
            log.warning(f"[NC] listing {year}: {e} — using cached copy")
        return cache

    def _listing_rows(self, session, year, url):
        """One archive PDF -> notice row dicts (header carried across
        pages; aggregate breakout pages yield nothing)."""
        pdf_path = self._listing_pdf(session, year, url)
        rows, carry = [], None
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    got, carry = _rows_from_grid(table, carry)
                    rows.extend(got)
        if carry is None:
            raise ValueError(f"NC listing {year}: no notice table in PDF")
        log.info(f"[NC] listing {year}: {len(rows)} rows")
        return rows

    def _summary_rows(self, session, year, url):
        """One current-year HTML report page -> notice row dicts."""
        time.sleep(1)
        resp = self._request(session, "GET", url)
        table = BeautifulSoup(resp.text, "html5lib").find("table")
        if table is None:
            raise ValueError(f"NC summary list {year}: page has no table")
        grid = [
            [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        rows, mapping = _rows_from_grid(grid)
        if mapping is None:
            raise ValueError(
                f"NC summary list {year}: table header not recognised"
            )
        log.info(f"[NC] summary list {year}: {len(rows)} rows")
        return rows

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        current, archives_url = self._discover(session)

        rows, seen = [], set()

        def add(row, year):
            row = {col: row.get(col, "") for col in _RAW_COLUMNS}
            row["year"] = year
            key = tuple(row[c] for c in _RAW_COLUMNS if c != "year")
            if key not in seen:  # year-rollover overlap guard
                seen.add(key)
                rows.append(row)

        if archives_url:
            listings = self._archive_listings(session, archives_url)
            for year in sorted(listings):
                for row in self._listing_rows(session, year, listings[year]):
                    add(row, year)
        else:
            log.warning("[NC] no archives link on hub — skipping backfill")

        for url, year in sorted(current.items(), key=lambda kv: kv[1]):
            for row in self._summary_rows(session, year, url):
                add(row, year)

        _write_raw_csv(rows, self.paths.raw)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _HUB_URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _squish(row.get("company", ""))
            if not company:
                continue  # company is required
            employees = _clean_employees(row.get("employees", ""))
            layoff_type = _squish(
                f"{row.get('notice_type', '')} {row.get('layoff_kind', '')}"
            )
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("received_date")),
                    "effective_date": _clean_date(row.get("effective_date")),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": layoff_type,
                    "county": _clean_county(row.get("county", "")),
                    "city": _squish(row.get("city", "")),
                    "address": _squish(row.get("address", "")),
                }
            )
        out = pd.DataFrame(records, columns=_OUT_COLUMNS)
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
