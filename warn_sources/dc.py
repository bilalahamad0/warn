"""
warn_sources.dc
---------------
District of Columbia — WARN notices published by the Department of
Employment Services as per-year Drupal pages at
``https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications-<year>``.

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/dc.py) — ported, never imported: the current year's page is
the root (falling back to the prior year's page early in January, before
the new page exists); its first ``div.field-items`` block links one page
per prior year (2017-2025 as of 2026-07); every page carries exactly one
5-column HTML table. BLN's "weird table inside a table cell" regex patch
(added for a June 2025 entry) is vendored verbatim and applied to every
page before parsing. Two BLN behaviors are deliberately replaced:

- rows are keyed off each table's own header row instead of positionally
  under the root page's header, because the employee-count header really
  varies by year ("Number toEmployees Affected" (sic) 2021+, "Number to
  Employees Affected" 2020, "Number of Employees Affected" 2017-2019) —
  all canonicalised to BLN's crosswalk key, which is exactly what BLN's
  positional CSV achieves implicitly;
- BLN's missing-2014 archived-copy workaround (warn-scraper #238) is
  dropped as dead code: today's hub carries no 2014 link at all. Pages
  for 2012-2016 exist only behind the stale year-less legacy URL that
  BLN's flow (and therefore this port) never reaches, so backfill depth
  is 2017-present.

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/dc.py) exactly:

    Organization Name             -> company        (required)
    Notice Date                   -> notice_date
    Effective Layoff Date         -> effective_date
    Number toEmployees Affected   -> employees      (0 when TBD/All/blank)
    location                      -> city           (BLN hardcodes
                                                     "Washington D.C." —
                                                     the jurisdiction is a
                                                     single city)
    Code Type                     -> dropped        (published with no
                                                     legend anywhere on the
                                                     site; BLN does not map
                                                     it, so it is never
                                                     guessed into
                                                     layoff_type)

DC publishes no county, address, or industry; none is fabricated.

Date quirks honored (BLN ``date_format`` list + ``date_corrections``
vendored verbatim): effective dates are often free text ("Various Dates
through September 30, 2025", "May 19 - June 2, 2026", "TBD") — every such
live string maps through BLN's corrections, including the two BLN oddities
kept verbatim: "February 28, 2022 March 31, 2022" -> 2020-02-28 (sic,
BLN's year) and "September 30 through September 28, 2025" -> 2025-09-30
(BLN's own "# What?" row). Un-correctable, un-parseable text yields None —
junk is never emitted, and a notice date is never copied into the
effective date or vice versa. The 2025 page's National Democratic
Institute row carries notice date "March 24, 2024" (probable state typo);
it parses cleanly, BLN emits it unchanged, and so does this port.

Jobs quirks honored: BLN's ``jobs_corrections`` vendored verbatim ("All"
and "TBD" mean no published count -> 0; the four "N (amended)" rows from
the 2025 Institute of International Education saga -> N), generalized with
a "^N (amended)$" fallback so the next amended row cannot regress to 0.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

BASE_URL = "https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications"

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

# BLN warn-scraper dc.py "weird table inside a table cell" patch (June 2025
# entry), vendored verbatim: unwrap the nested table, keeping its cell text.
NESTED_TABLE_RE = re.compile(
    r"(\s+<table>\s+<tbody>\s+<tr>\s+<td>)(.*)(</td>\s+</tr>\s+</tbody>\s+</table>\s+)"
)

# The state's employee-count header drifts by year; BLN's positional CSV
# collapses every variant under the current-year header, which is the
# transformer's crosswalk key. Canonicalising by name reproduces that.
EMPLOYEES_KEY = "Number toEmployees Affected"  # (sic) — 2021+ pages
HEADER_CANON = {
    "Number to Employees Affected": EMPLOYEES_KEY,  # 2020 page
    "Number of Employees Affected": EMPLOYEES_KEY,  # 2017-2019 pages
    "CodeType": "Code Type",                        # 2017 page
}

# BLN warn-transformer transformers/dc.py date_format, tried in order.
DATE_FORMATS = ["%B %d, %Y", "%m/%d/%y", "%B, %d, %Y", "%B %d,%Y"]

# Vendored verbatim from BLN warn-transformer transformers/dc.py
# date_corrections (as (y, m, d) tuples; None = no usable date). The
# 2020-for-2022 year on the ABM row and the "September 30 through
# September 28" row (BLN's own "# What?") are kept exactly as BLN ships
# them — see the module docstring.
DATE_CORRECTIONS = {
    "May 2 and 5, 2020": (2020, 5, 2),
    "March, 20, 2020": (2020, 3, 20),
    "31, 2019": (2019, 12, 31),
    "August 2013": (2013, 8, 1),
    "October 2013": (2013, 10, 1),
    "May 7,14 & 31, 2012": (2012, 5, 7),
    "February 28, 2022 March 31, 2022": (2020, 2, 28),  # sic — BLN's year
    "December 25, and Feb - Jun 2021": (2020, 12, 25),
    "TBD": None,
    "September 15, 2020 and March 18, 2020": (2020, 9, 15),
    "May 31, 2012 June 15, 2012": (2012, 5, 31),
    "June 29, 2012 & August 3, 2012": (2012, 6, 29),
    "November 15 - December 16, 2022": (2022, 11, 15),
    "December 3 - December 17, 2022": (2022, 12, 3),
    "Februart 14, 2025": (2025, 2, 14),
    "December 20 & 31, 2024": (2024, 12, 20),
    "Februart 20, 2025": (2025, 2, 20),
    "Various Dates through September 30, 2025": (2025, 9, 30),
    "February 28 and March 7 & 14, 2025": (2025, 2, 28),
    "Various Dates through May 30, 2025": (2025, 5, 30),
    "March 28 through May 31, 2025": (2025, 3, 28),
    "March 14, 2025 through April30, 2025": (2025, 3, 14),
    "Various Dates through May 16, 2026": (2025, 5, 16),
    "Various Dates through May 16, 2025": (2025, 5, 16),
    "May 2, 2025 through May 16, 2026": (2025, 5, 2),
    "May 9 through September 30, 2025": (2025, 5, 9),
    "Various dates through June 30, 2025": (2025, 6, 30),
    "July 31 through August 28, 2025": (2025, 7, 31),
    "September 30 through December 30, 2025": (2025, 9, 30),
    "Various Dates through June 29, 2025": (2025, 6, 29),
    "Various Dates through September 28, 2025": (2025, 9, 28),
    "Various Dates through November2, 2025": (2025, 11, 2),
    "Various Dates through November 2, 2025": (2025, 11, 2),
    "September 30 through September 28, 2025": (2025, 9, 30),  # BLN: What?
    "March 14, 2025 through April 30, 2025": (2025, 3, 14),
    "May 19 - June 2, 2026": (2026, 5, 19),
    "September 11 - 23, 2026": (2026, 9, 11),
}

# Vendored verbatim from BLN warn-transformer transformers/dc.py
# jobs_corrections; None = the state published no usable count.
JOBS_CORRECTIONS = {
    "All": None,
    "TBD": None,
    "45 (amended)": 45,
    "63 (amended)": 63,
    "54 (amended)": 54,
    "46 (amended)": 46,
}

# Generalization of the four vendored "(amended)" corrections, so the
# next amended count the state publishes cannot regress to 0.
AMENDED_RE = re.compile(r"^(\d[\d,]*)\s*\(amended\)$", re.IGNORECASE)

YEAR_RE = re.compile(r"(19|20)\d{2}")

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

CITY = "Washington D.C."  # BLN transformer hardcode — DC is one city


def _max_year():
    return date.today().year + 3


def _year_url(year: int) -> str:
    return f"{BASE_URL}-{year}"


def _squish(val) -> str:
    """Cell text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _patch_nested_tables(html: str) -> str:
    """Vendored BLN patch: unwrap a table nested inside a table cell."""
    patched, count = NESTED_TABLE_RE.subn(r"\2", html)
    if count:
        log.debug(f"[DC] unwrapped {count} nested table(s)")
    return patched


def _extract_rows(html) -> list:
    """One page's table -> list of dicts keyed by its own headers.

    Adapted from BLN warn-scraper dc.py (html5lib, first table) — but
    keyed on each page's own header row, with the known header variants
    canonicalised (see HEADER_CANON), instead of positionally under the
    root page's header. BLN's any()-filter for blank padding rows (the
    2018 page has one) is kept; a repeated header row is skipped.
    Returns [] when the page carries no table yet (a brand-new year).
    """
    soup = BeautifulSoup(_patch_nested_tables(html), "html5lib")
    table = soup.find("table")
    if table is None:
        return []
    trs = table.find_all("tr")
    if not trs:
        return []
    headers = [
        HEADER_CANON.get(_squish(cell.get_text(" ")), _squish(cell.get_text(" ")))
        for cell in trs[0].find_all(["th", "td"])
    ]
    rows = []
    for tr in trs[1:]:
        cells = [_squish(cell.get_text(" ")) for cell in tr.find_all(["td", "th"])]
        if not any(cells):
            continue  # BLN's blank-row filter (2018 page padding)
        if [HEADER_CANON.get(c, c) for c in cells] == headers:
            continue  # repeated header row
        rows.append(dict(zip(headers, cells)))
    return rows


def _year_links(html) -> list:
    """Root page -> [(year, href)] from the first ``div.field-items``.

    BLN's dc.py takes every link in that block; hardened to links whose
    text carries a 4-digit year (the block's other entries are Rapid
    Response boilerplate on some page generations), deduped by href.
    """
    soup = BeautifulSoup(html, "html5lib")
    blocks = soup.find_all("div", {"class": "field-items"})
    if not blocks:
        return []
    out, seen = [], set()
    for atag in blocks[0].find_all("a"):
        href = (atag.get("href") or "").strip()
        match = YEAR_RE.search(_squish(atag.get_text(" ")))
        if not href or href in seen or not match:
            continue
        seen.add(href)
        out.append((int(match.group(0)), href))
    return out


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or KeyError."""
    ymd = DATE_CORRECTIONS[text]
    return None if ymd is None else "%04d-%02d-%02d" % ymd


def _clean_date(val):
    """DC date cell -> strict ISO YYYY-MM-DD or None (never junk).

    BLN's corrections verbatim first, then the four known formats in
    BLN's order, inside a year sanity window so a digit-mangled year can
    never be emitted as data.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    if not text:
        return None
    if text in DATE_CORRECTIONS:
        return _correction(text)
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
    """Employee-count cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    match = AMENDED_RE.match(text)
    if match:
        return int(match.group(1).replace(",", ""))
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


class DistrictOfColumbiaDOES(Source):
    code = "dc"
    name = "District of Columbia"
    agency = "District of Columbia Department of Employment Services"
    source_url = _year_url(date.today().year)
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url: str, first: bool) -> str:
        """One page politely: 1 req/s, 60 s timeout, 3 attempts.

        A 404 is a definitive answer (the new year's page does not exist
        yet), so it raises immediately instead of retrying.
        """
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[DC] {url} request error: {e}")
                continue
            if resp.status_code == 404:
                last_err = RuntimeError("HTTP 404")
                log.warning(f"[DC] {url} -> HTTP 404")
                break
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[DC] {url} -> HTTP {resp.status_code}")
                continue
            resp.encoding = "utf-8"  # BLN dc.py forces utf-8
            return resp.text
        raise RuntimeError(f"DC feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the root + every linked year page into one JSON file.

        Any linked year failing aborts the whole fetch — a partial crawl
        must never be written, or the diff engine would report the
        missing years as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        # BLN dc.py root fallback: early January, before the new year's
        # page exists, last year's page is still the root.
        root_html, root_year = None, None
        current_year = date.today().year
        for year in (current_year, current_year - 1):
            try:
                root_html = self._get(
                    session, _year_url(year), first=(year == current_year)
                )
                root_year = year
                break
            except RuntimeError as e:
                log.warning(f"[DC] no page for {year} ({e})")
        if root_html is None:
            raise RuntimeError("DC feed: no root page for this or last year")

        links = _year_links(root_html)
        if not links:
            raise RuntimeError(
                "DC feed: no year links in div.field-items — "
                "root layout may have changed"
            )

        rows = []
        root_rows = _extract_rows(root_html)
        log.info(f"[DC] {root_year} (root): {len(root_rows)} table rows")
        rows.extend({"Year": root_year, **row} for row in root_rows)
        for year, href in links:
            html = self._get(session, href, first=False)
            year_rows = _extract_rows(html)
            log.info(f"[DC] {year}: {len(year_rows)} table rows")
            rows.extend({"Year": year, **row} for row in year_rows)

        # The live feed has carried 140+ notices across 2017-2026; a
        # collapse below 60 means the layout changed, not mass removals.
        if len(rows) < 60:
            raise RuntimeError(
                f"DC feed: only {len(rows)} table rows across all years — "
                "page layout may have changed"
            )

        self.paths.ensure()
        payload = {"source": self.source_url, "rows": rows}
        self.paths.raw.write_text(json.dumps(payload, indent=1))

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": self.source_url,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated JSON -> unified-schema rows (BLN crosswalk)."""
        payload = json.loads(Path(raw_path).read_text())
        raw_rows = payload["rows"] if isinstance(payload, dict) else payload

        records = []
        for row in raw_rows:
            company = _squish(row.get("Organization Name") or "")
            if not company:
                continue  # blank padding row
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Notice Date")),
                    "effective_date": _clean_date(
                        row.get("Effective Layoff Date")
                    ),
                    "employees": _clean_employees(row.get(EMPLOYEES_KEY) or ""),
                    "city": CITY,
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "city",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
