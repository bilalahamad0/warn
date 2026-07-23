"""
warn_sources.ut
---------------
Utah — WARN notices published by the Department of Workforce Services as
one static HTML page holding a table per year (2009-present, ~280 notices)
at https://jobs.utah.gov/employer/business/warnnotices.html.

Fetch is a plain conditional GET — the page serves ETag/Last-Modified, so
the shared ``warn_monitor.download_xlsx`` cache machinery applies
unchanged. The downloaded page is validated before the engine ever sees
it: if the per-year tables collapse below ``MIN_EXPECTED_ROWS`` notices
(the cumulative page has carried ~280 since 2024), fetch raises instead of
returning — a truncated or redesigned page must never surface as phantom
withdrawals in the diff engine.

Table extraction vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/ut.py) — ported, never imported: every ``<table>`` on the
page is scraped (one per year), cell text stripped. BLN concatenates the
tables positionally under the first table's header; this port instead keys
each table's rows off its own header row (all year tables publish the same
"Date of Notice / Company Name / Location / Affected Workers" header),
which survives a column reorder in any one year's table.

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/ut.py) exactly:

    Company Name     -> company        (required)
    Location         -> city           (BLN "location"; usually a city,
                                        sometimes "State of Utah" or a
                                        multi-city string — kept verbatim)
    Date of Notice   -> notice_date
    Affected Workers -> employees      (0 when no count published)

Utah publishes no effective date, county, street address, industry, or
layoff type — never fabricated (notice_date only; see
EXPANSION_RESEARCH.md §5).

Date quirks honored: BLN ``date_format`` is ("%m/%d/%Y", "%m/%d/%y") under
a last-match-wins loop, so 4-digit years resolve via %Y and 2-digit years
via %y; this port tries %y first with first-match-wins — identical outputs
(Python's %Y would swallow "12/05/25" as year 25, which BLN's loop
overwrites and the year window here rejects). ``date_corrections``
vendored verbatim ("03/09/2020&", "01/05/18/", "03/05/14 Updated", the
impossible "09/31/10" -> Sep 30, month-only "05/2009" -> first of month,
"01/07//09", "08/31//2022" — all still live on the page). Out-of-window
years parse to None — junk is never emitted. ``jobs_corrections`` vendored
verbatim ("645 Revised" -> 645).
"""

import logging
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

URL = "https://jobs.utah.gov/employer/business/warnnotices.html"

# BLN warn-transformer transformers/ut.py date_format, reordered %y-first
# for first-match-wins semantics (see module docstring); tried in order.
DATE_FORMATS = ["%m/%d/%y", "%m/%d/%Y"]

# Vendored verbatim from BLN warn-transformer transformers/ut.py
# date_corrections (values as ISO strings).
DATE_CORRECTIONS = {
    "03/09/2020&": "2020-03-09",
    "01/05/18/": "2018-01-05",
    "03/05/14 Updated": "2014-03-05",
    "09/31/10": "2010-09-30",
    "05/2009": "2009-05-01",
    "01/07//09": "2009-01-07",
    "08/31//2022": "2022-08-31",
}

# Vendored verbatim from BLN warn-transformer transformers/ut.py
# jobs_corrections.
JOBS_CORRECTIONS = {
    "645 Revised": 645,
}

# Sanity window for parsed years (BLN minimum_year); outside it is a typo.
MIN_YEAR = 1988

# The cumulative page has listed ~280 notices back to 2009 since 2024;
# fewer surviving rows means a redesign or truncation, not mass
# rescissions.
MIN_EXPECTED_ROWS = 100


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string (BLN's cell strip)."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _extract_rows(html) -> list:
    """Every per-year table -> list of dicts keyed by its own header row.

    Adapted from BLN warn-scraper ut.py (scrape every table on the page,
    strip cell text, skip cell-less rows) — but keyed on each table's own
    header names instead of cell positions.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        grid = []
        for tr in table.find_all("tr"):
            cells = [
                _squish(td.get_text(" "))
                for td in tr.find_all(["td", "th"])
            ]
            if not cells:
                continue
            grid.append(cells)
        if len(grid) < 2:
            continue  # no header + data
        headers = grid[0]
        rows.extend(dict(zip(headers, cells)) for cells in grid[1:])
    return rows


def _clean_date(val):
    """UT date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's transform_date: the known formats, then the vendored
    corrections, under a year sanity window.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    if not text:
        return None
    if text in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[text]
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
        return JOBS_CORRECTIONS[text]
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


class UtahDWS(Source):
    code = "ut"
    name = "Utah"
    agency = "Utah Department of Workforce Services"
    source_url = URL
    cadence = "daily"

    def fetch(self, force: bool = False) -> tuple:
        """Conditional GET of the single page, then validate the tables.

        A page whose tables have collapsed (redesign, truncation, error
        page served with HTTP 200) aborts the run here — writing it
        through would surface every notice as a phantom withdrawal in the
        diff.
        """
        self.paths.ensure()
        changed, raw_path = warn_monitor.download_xlsx(
            force=force,
            url=self.source_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )
        html = Path(raw_path).read_bytes().decode("utf-8", errors="replace")
        rows = _extract_rows(html)
        good = sum(
            1 for r in rows if _squish(r.get("Company Name") or "")
        )
        if good < MIN_EXPECTED_ROWS:
            raise RuntimeError(
                f"UT feed: only {good} notice rows on the page (expected "
                f">= {MIN_EXPECTED_ROWS}) — layout may have changed"
            )
        return changed, raw_path

    def parse(self, raw_path) -> pd.DataFrame:
        """Raw HTML -> unified-schema rows (BLN crosswalk)."""
        html = Path(raw_path).read_bytes().decode("utf-8", errors="replace")
        records = []
        for row in _extract_rows(html):
            company = _squish(row.get("Company Name") or "")
            if not company:
                continue  # company is required
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Date of Notice")),
                    "employees": _clean_employees(
                        row.get("Affected Workers") or ""
                    ),
                    "city": _squish(row.get("Location") or ""),
                }
            )
        out = pd.DataFrame(
            records,
            columns=["company", "notice_date", "employees", "city"],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
