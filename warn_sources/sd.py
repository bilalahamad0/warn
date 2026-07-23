"""
warn_sources.sd
---------------
South Dakota — WARN notices published by the Department of Labor and
Regulation as one cumulative HTML table (2007-present) at
https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/sd.py) — ported, never imported: one GET, first <table>
on the page, whitespace-squished cell text, first row is the header.
Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/sd.py) exactly:

    Company            -> company      (required)
    Location           -> city         (BLN "location"; free text — one
                                        or more city names, sometimes
                                        "Nationwide" or a bare "South
                                        Dakota"; the literal placeholder
                                        "n/a" becomes "")
    Date Received      -> notice_date  (BLN date_format %m/%d/%Y, which
                                        also parses the feed's one
                                        single-digit day "01/5/2012")
    Employees Affected -> employees    (0 when no usable count is
                                        published)

SD publishes no effective date, county, address, industry, or layoff
type — those fields are never fabricated (EXPANSION_RESEARCH.md §5:
never synthesize one date from another).

Count quirks honored (BLN ``jobs_corrections``, vendored verbatim, all
four keys still live or in the archive rows): "1-5" -> 1,
"324 (11 reside in South Dakota)" -> 11 (only the in-state figure),
"n/a" and "173 (nationwide)" -> no usable count -> 0. An empty cell
(the KBR/EROS row publishes a bare &nbsp;) also means no count -> 0.
BLN's ``maximum_jobs`` 10 000 sanity cap is adopted.

Backfill depth: the single live table reaches back to 2007, so one
fetch captures the state's entire published history (~79 notices).
"""

import csv
import logging
import re
import time
from datetime import date, datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx"

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

# The raw consolidated CSV keeps the state's own column names, which are
# also the BLN transformer's crosswalk keys.
RAW_COLUMNS = ["Company", "Location", "Date Received", "Employees Affected"]

# BLN warn-transformer transformers/sd.py date_format.
DATE_FORMAT = "%m/%d/%Y"

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

# Vendored verbatim from BLN warn-transformer transformers/sd.py
# jobs_corrections; None = the state published no usable count.
JOBS_CORRECTIONS = {
    "1-5": 1,
    "324 (11 reside in South Dakota)": 11,
    "n/a": None,
    "173 (nationwide)": None,
}

# The cumulative table has carried ~79 notices since 2007; far fewer
# means a broken/partial page, which must never reach the diff engine
# (it would surface as phantom withdrawals).
MIN_EXPECTED_ROWS = 40


def _squish(val) -> str:
    """Cell text -> clean single-spaced string (BLN warn-scraper rule)."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _parse_table(html) -> list:
    """Page HTML -> list of row dicts keyed by the header row.

    Vendored from BLN warn-scraper sd.py: first <table> on the page,
    every <tr>'s th/td text squished; the first row is the header
    (its "Employees<br/>Affected" cell squishes to the crosswalk key).
    Blank rows and mid-table header repeats are skipped.
    """
    soup = BeautifulSoup(html, "html5lib")
    table = soup.find("table")
    if table is None:
        raise ValueError("SD WARN page: no <table> found")
    trs = table.find_all("tr")
    if not trs:
        raise ValueError("SD WARN page: table has no rows")
    header = [_squish(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
    if "Company" not in header or "Date Received" not in header:
        raise ValueError(f"SD WARN page: unexpected header {header!r}")
    rows = []
    for tr in trs[1:]:
        cells = [_squish(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if not any(cells):
            continue
        if "Date Received" in cells:
            continue  # repeated header row inside the data
        rows.append(dict(zip(header, cells)))
    return rows


def _clean_date(val):
    """SD date cell -> ISO YYYY-MM-DD or None — never junk.

    BLN's single %m/%d/%Y format (strptime also accepts the feed's one
    unpadded day, "01/5/2012"); out-of-window years are typos, not data.
    """
    text = _squish(val or "")
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, DATE_FORMAT)
    except ValueError:
        return None
    if not MIN_YEAR <= parsed.year <= date.today().year + 6:
        return None
    return parsed.strftime("%Y-%m-%d")


def _clean_employees(val) -> int:
    """Employees Affected cell -> int; 0 when no usable count."""
    text = _squish(val or "")
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    count = warn_monitor._safe_int(text)
    if count is None or not 0 <= count <= 10000:  # BLN maximum_jobs cap
        return 0
    return count


class SouthDakotaDLR(Source):
    code = "sd"
    name = "South Dakota"
    agency = "South Dakota Department of Labor and Regulation"
    source_url = PAGE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get_page(self) -> str:
        """The notices page politely: 60 s timeout, 3 attempts, backoff."""
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(PAGE_URL, timeout=60)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[SD] {PAGE_URL} attempt {attempt + 1}: {e}")
        raise RuntimeError(f"SD feed: fetch failed ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the cumulative table into one consolidated CSV."""
        rows = _parse_table(self._get_page())
        if len(rows) < MIN_EXPECTED_ROWS:
            raise RuntimeError(
                f"SD feed: only {len(rows)} table rows — the page layout "
                "may have changed"
            )
        log.info(f"[SD] scraped {len(rows)} notices")

        self.paths.ensure()
        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=RAW_COLUMNS, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": PAGE_URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV -> unified-schema rows (BLN crosswalk)."""
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _squish(row.get("Company", ""))
            if not company:
                continue  # company is required
            city = _squish(row.get("Location", ""))
            if city.lower() == "n/a":
                city = ""  # the state's no-location placeholder
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Date Received")),
                    "employees": _clean_employees(
                        row.get("Employees Affected")
                    ),
                    "city": city,
                }
            )
        out = pd.DataFrame(
            records, columns=["company", "notice_date", "employees", "city"]
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
