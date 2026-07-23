"""
warn_sources.ak
---------------
Alaska — WARN notices published by the Department of Labor and Workforce
Development as a single cumulative HTML table (2006-present, ~65 notices) at
https://jobs.alaska.gov/RR/WARN_notices.htm.

Fetch is a plain conditional GET — the page serves ETag/Last-Modified, so the
shared ``warn_monitor.download_xlsx`` cache machinery applies unchanged. The
downloaded page is validated before the engine ever sees it: if the table
collapses below ``MIN_EXPECTED_ROWS`` notices (the cumulative page has carried
60+ since 2020), fetch raises instead of returning — a truncated or redesigned
page must never surface as phantom withdrawals in the diff engine.

Table extraction vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/ak.py) — ported, never imported: first ``<table>`` on the page,
whitespace collapsed per cell, rows with an empty first (Company) cell dropped
(that rule removes the ``<hr>`` separator row and the blank trailing row,
exactly as BLN does). Cells are keyed off the table's own header row rather
than by position. The page's commented-out template row is invisible to the
parser (comments are not elements).

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/ak.py) exactly:

    Company            -> company        (required)
    Location           -> city           (BLN "location"; multi-city notices
                                          keep the joined text, MO/WA-style)
    Notice Date        -> notice_date
    Layoff Date        -> effective_date
    Employees Affected -> employees      (0 when no count published)
    Notes              -> layoff_type    (the column BLN keys is_closure /
                                          is_temporary off; the published
                                          wording is kept untouched)

Alaska publishes no county, street address, or industry — never fabricated.

Date quirks honored (BLN ``date_format`` "%m/%d/%y" + its ``transform_date``
override): ``date_corrections`` vendored verbatim ("9/30/20*",
"August-November 2021", "4/1/20 5/31/20", "Varied"/"various" -> no usable
date, "March to May 2016", "June-August 2023", the staggered Vigor closure
sentence, and the feed's occasional 4-digit-year cells); corrections are
checked before any parsing, then BLN's free-text cleanup — keep the start of
a " to " range, strip a leading "Starting " — is applied and parsing retried,
so "4/7/20 to 5/31/20" -> 2020-04-07 and "Starting 3/16/18" -> 2018-03-16.
"%m/%d/%Y" is tried after "%m/%d/%y" because the feed mixes both (BLN instead
hand-listed every 4-digit-year cell in ``date_corrections`` — identical
outputs, future-proofed). Out-of-window years parse to None — junk is never
emitted. ``jobs_corrections`` vendored verbatim ("Up to 300" -> 300, "TBA" ->
no published count -> 0, "1 Alaska Worker" -> 1).
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

URL = "https://jobs.alaska.gov/RR/WARN_notices.htm"

# BLN warn-transformer transformers/ak.py date_format, plus the 4-digit-year
# variant the live feed also uses; tried in order.
DATE_FORMATS = ["%m/%d/%y", "%m/%d/%Y"]

# Vendored verbatim from BLN warn-transformer transformers/ak.py
# date_corrections (values as ISO strings; None = no usable date published).
DATE_CORRECTIONS = {
    "9/30/20*": "2020-09-30",
    "August-November 2021": "2021-08-01",
    "4/1/20 5/31/20": "2020-04-01",
    "Varied": None,
    "March to May 2016": "2016-03-01",
    "various": None,
    "June-August 2023": "2023-06-01",
    "9/6/2023": "2023-09-06",
    "9/5/2023": "2023-09-05",
    "12/10/2024": "2024-12-10",
    "Begins 7/7/25 and will be staggered until official closure on 11/30/25":
        "2025-07-07",
}

# Vendored verbatim from BLN warn-transformer transformers/ak.py
# jobs_corrections; None = the state published no usable count.
JOBS_CORRECTIONS = {
    "Up to 300": 300,
    "TBA": None,
    "1 Alaska Worker": 1,
}

# Sanity window for parsed years (BLN minimum_year); outside it is a typo.
MIN_YEAR = 1988

# The cumulative page has listed 60+ notices back to 2006 since 2020; fewer
# surviving rows means a redesign or truncation, not mass rescissions.
MIN_EXPECTED_ROWS = 20

_MISSING = object()


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string (BLN's whitespace collapse)."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _extract_rows(html) -> list:
    """The page's table -> list of dicts keyed by its own header row.

    Adapted from BLN warn-scraper ak.py (first table, collapse whitespace,
    drop rows whose first cell is empty — the <hr> separator and the blank
    trailing row) — but keyed on the header names instead of cell positions.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    grid = []
    for tr in table.find_all("tr"):
        cells = [_squish(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        if not cells or cells[0] == "":
            continue  # BLN's rule: separator/blank rows have no first cell
        grid.append(cells)
    if not grid:
        return []
    headers = grid[0]
    return [dict(zip(headers, cells)) for cells in grid[1:]]


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or _MISSING."""
    if text not in DATE_CORRECTIONS:
        return _MISSING
    return DATE_CORRECTIONS[text]


def _try_formats(text):
    """The known formats in order -> ISO date, else None."""
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
        return None
    return None


def _clean_date(val):
    """AK date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's ak.py transform_date: corrections on the full string
    first, then the formats; on failure, BLN's cleanup — keep the start of
    a " to " range, strip a leading "Starting " — then corrections and
    formats again.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed
    iso = _try_formats(text)
    if iso is not None:
        return iso
    if not text:
        return None
    # BLN's cleanup, in its exact order.
    text = text.split(" to ")[0].strip()
    text = text.replace("Starting ", "").strip()
    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed
    return _try_formats(text)


def _clean_employees(val) -> int:
    """Employees Affected cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


class AlaskaDOLWD(Source):
    code = "ak"
    name = "Alaska"
    agency = "Alaska Department of Labor and Workforce Development"
    source_url = URL
    cadence = "daily"

    def fetch(self, force: bool = False) -> tuple:
        """Conditional GET of the single page, then validate the table.

        A page whose table has collapsed (redesign, truncation, error page
        served with HTTP 200) aborts the run here — writing it through
        would surface every notice as a phantom withdrawal in the diff.
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
        good = sum(1 for r in rows if _squish(r.get("Company") or ""))
        if good < MIN_EXPECTED_ROWS:
            raise RuntimeError(
                f"AK feed: only {good} notice rows on the page (expected >= "
                f"{MIN_EXPECTED_ROWS}) — layout may have changed"
            )
        return changed, raw_path

    def parse(self, raw_path) -> pd.DataFrame:
        """Raw HTML -> unified-schema rows (BLN crosswalk)."""
        html = Path(raw_path).read_bytes().decode("utf-8", errors="replace")
        records = []
        for row in _extract_rows(html):
            company = _squish(row.get("Company") or "")
            if not company:
                continue  # company is required
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Notice Date")),
                    "effective_date": _clean_date(row.get("Layoff Date")),
                    "employees": _clean_employees(
                        row.get("Employees Affected") or ""
                    ),
                    "layoff_type": _squish(row.get("Notes") or ""),
                    "city": _squish(row.get("Location") or ""),
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
                "city",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
