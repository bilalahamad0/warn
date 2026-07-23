"""
warn_sources.ne
---------------
Nebraska — WARN notices published by the Department of Labor as plain
HTML tables: one "active" page with the current-era notices plus two
per-year archive endpoints.

    active   https://dol.nebraska.gov/ReemploymentServices/LayoffServices/
             LayoffsAndDownsizingWARN            (2023-03 -> present, live)
    warn     /LayoffServices/WARNReportData/?year=<YYYY>
             ("WARN Report", data 2010-2020; 2021+ render
             "No events to display" — the era moved to the active page)
    layoff   /LayoffServices/LayoffAndClosureReportData/?year=<YYYY>
             ("Layoff and Closures Report", data 2009-2020; the state's
             broader layoff/closure record, WARN and sub-WARN alike)

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/ne.py) — ported, never imported: crawl the active page
plus both archive endpoints per year and consolidate. BLN's hardcoded
positional headers are replaced by keying each table's cells off its own
header row (the three layouts genuinely differ: active 4 columns, WARN
archive 5, layoff archive 6). BLN's fixed range(2010, 2020) becomes
2009 -> current year so the crawl picks archives back up the day the
state resumes populating them (verified live 2026-07-21: 2009 and 2020
carry data BLN's range missed; empty years render a real "No events"
table and parse to zero rows, never an error).

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/ne.py) exactly:

    Date           -> notice_date   (BLN notice_date="Date"; for layoff-
                                     report rows the state records the
                                     event under its occurrence date, but
                                     the crosswalk maps Date uniformly)
    Company        -> company       (required; link text on the active
                                     page, where each notice anchors its
                                     PDF)
    Jobs Affected  -> employees     (0 when no usable count published)
    City           -> city          (BLN location="City"; the archive
                                     tables' column. The active table
                                     publishes no City — its "Location"
                                     column carries the city text
                                     ("Omaha", "Remote", multi-city
                                     lists), so it feeds city there; BLN
                                     dropped it only as an artifact of
                                     writing one CSV with the archive's
                                     header set)
    Type           -> layoff_type   (layoff report only: Closure /
                                     Layoff, as published; BLN's
                                     check_if_closure reads the same
                                     column)
    Location       -> dropped       (archive tables only: a facility
                                     descriptor, e.g. "Distribution
                                     Center - Sidney" — not a street
                                     address; unused by BLN's transformer
                                     too)

NE publishes no effective date (JobLink-style feed, see
EXPANSION_RESEARCH.md §5), county, street address, or industry — those
fields are never fabricated.

Known quirk, inherited from the state and BLN alike: the layoff report
re-lists WARN events under their occurrence date (e.g. Bimbo Bakeries,
noticed 8/2/2019, appears again as a 10/4/2019 Closure), so one event
can carry two rows — exactly as in BLN's consolidated output.

Date quirks honored: BLN's ``date_corrections`` are vendored verbatim
(keyed post-whitespace-squish, since the raw cells embed NBSP/newline
runs). The amended two-date cell "12/19/2022 11/2/2022" resolves to the
*second listed* date per BLN, and the generic fallback follows that
precedent: unparseable multi-date cells keep the last m/d/Y token.
A ``%m/%d/%y`` fallback covers future "04/25/25"-class typos beyond the
vendored correction. Out-of-window years parse to None — junk is never
emitted. BLN's ``jobs_corrections`` are vendored verbatim ("100+" ->
100, "5-9" -> 5, "3-5" -> 3, "a few" -> 1).
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

BASE = "https://dol.nebraska.gov"
ACTIVE_URL = (
    f"{BASE}/ReemploymentServices/LayoffServices/LayoffsAndDownsizingWARN"
)
WARN_URL = f"{BASE}/LayoffServices/WARNReportData/?year={{year}}"
LAYOFF_URL = f"{BASE}/LayoffServices/LayoffAndClosureReportData/?year={{year}}"

# First year either archive endpoint serves data (layoff report, live
# probe 2026-07-21; the WARN report starts 2010).
START_YEAR = 2009

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

# BLN warn-transformer transformers/ne.py date_format (+ two-digit-year
# fallback for the "04/25/25" typo class BLN corrects manually).
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]

# Vendored from BLN warn-transformer transformers/ne.py date_corrections,
# keyed by their whitespace-squished form (the raw cells carry
# NBSP/newline runs); values as (y, m, d).
DATE_CORRECTIONS = {
    "12/19/2022 11/2/2022": (2022, 11, 2),
    "12/19/2022 11/02/2022": (2022, 11, 2),
    "04/25/25": (2025, 4, 25),
}

# Vendored from BLN warn-transformer transformers/ne.py jobs_corrections.
JOBS_CORRECTIONS = {
    "100+": 100,
    "5-9": 5,
    "3-5": 3,
    "a few": 1,
}

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

# The static archives alone carry ~1,000 rows (live count 2026-07-21:
# 107 WARN + 892 layoff + 46 active). A crawl below this floor means the
# site changed, not that Nebraska rescinded a decade of notices.
MIN_TOTAL_ROWS = 800

_DATE_TOKEN = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _extract_rows(html) -> list:
    """One page's table -> list of dicts keyed by its own header row.

    Adapted from BLN warn-scraper ne.py (first table, td rows) — but
    keyed on the table's own header th row instead of hardcoded
    positional headers so the 4/5/6-column layouts all map correctly.
    The pages' decoration rows ("WARN Report" title, "<year> Events as
    of ..." + print link) are all-th rows of fewer than 4 cells and are
    skipped; "No events to display." pages yield zero rows. Returns
    None when the response carries no table at all (layout change).
    """
    soup = BeautifulSoup(html, "html5lib")
    table = soup.find("table")
    if table is None:
        return None
    headers = None
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            ths = [_squish(th.get_text(" ")) for th in tr.find_all("th")]
            if len(ths) >= 4:  # the real column-header row
                headers = ths
            continue
        if headers is None:
            continue
        values = [_squish(td.get_text(" ")) for td in cells]
        rows.append(dict(zip(headers, values)))
    return rows


def _clean_date(val):
    """NE date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Corrections first (BLN's, squish-keyed), then the format list. The
    fallback keeps the LAST m/d/Y token of a multi-date cell — the
    precedent BLN's own correction sets for the amended
    "12/19/2022 11/2/2022" cell, which resolves to the second date.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    if not text:
        return None
    if text in DATE_CORRECTIONS:
        return "%04d-%02d-%02d" % DATE_CORRECTIONS[text]
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
        return None
    tokens = _DATE_TOKEN.findall(text)
    if tokens:
        try:
            parsed = datetime.strptime(tokens[-1], "%m/%d/%Y")
        except ValueError:
            return None
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
    return None


def _clean_employees(val) -> int:
    """Jobs Affected cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        return JOBS_CORRECTIONS[text]
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


class NebraskaDOL(Source):
    code = "ne"
    name = "Nebraska"
    agency = "Nebraska Department of Labor"
    source_url = ACTIVE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url: str, first: bool) -> str:
        """One page politely: 1 req/s, 60 s timeout, 3 attempts."""
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[NE] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[NE] {url} -> HTTP {resp.status_code}")
                continue
            return resp.text
        raise RuntimeError(f"NE feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Crawl the active page + both per-year archives into one JSON.

        Any page failing (HTTP error, table gone) aborts the whole
        fetch — a partial crawl must never be written, or the diff
        engine would report the missing pages as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        rows = []

        def crawl(report, url, first=False):
            page_rows = _extract_rows(self._get(session, url, first))
            if page_rows is None:
                raise RuntimeError(
                    f"NE feed: no table at {url} — page layout may have changed"
                )
            log.info(f"[NE] {report} {url}: {len(page_rows)} rows")
            for row in page_rows:
                rows.append({"report": report, **row})

        crawl("active", ACTIVE_URL, first=True)
        for year in range(date.today().year, START_YEAR - 1, -1):
            crawl("warn", WARN_URL.format(year=year))
            crawl("layoff", LAYOFF_URL.format(year=year))

        if len(rows) < MIN_TOTAL_ROWS:
            raise RuntimeError(
                f"NE feed: only {len(rows)} rows across all pages — "
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
            company = _squish(row.get("Company") or "")
            if not company:
                continue  # decoration or padding, never a notice
            # Archive tables publish a City column; the active table's
            # city text rides in its Location column (see docstring).
            city = row["City"] if "City" in row else row.get("Location")
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Date")),
                    "employees": _clean_employees(row.get("Jobs Affected") or ""),
                    "layoff_type": _squish(row.get("Type") or ""),
                    "city": _squish(city or ""),
                }
            )
        out = pd.DataFrame(
            records,
            columns=["company", "notice_date", "employees", "layoff_type", "city"],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
