"""
warn_sources.mo
---------------
Missouri — WARN notices published by the Office of Workforce Development
as per-year Drupal table pages at ``https://jobs.mo.gov/warn/<year>``.

**Why ``enabled = False``:** jobs.mo.gov sits behind an Imperva/Incapsula
bot wall. Probed 2026-07-21 with honest browser headers (Chrome UA, full
Accept/Accept-Language set, cookie handshake, retries): every plain HTTP
client — curl, requests — receives the JavaScript-challenge interstitial
instead of the page; only a real browser passes. Big Local News hit the
same wall (warn-scraper issue #597, opened 2024-01) and reports the wall
intermittently *allows their GitHub Actions runners* while blocking
residential/local IPs, so flipping ``enabled`` on in CI may simply work.
``fetch`` below is fully implemented for that day: it detects the
challenge page and raises rather than ever writing a truncated crawl
(which would surface as phantom withdrawals in the diff engine). Parse
logic and the test fixtures were verified against complete real captures
of all live year pages (2019-2026, 368 notices) made through a browser
session on 2026-07-21.

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/mo.py) — ported, never imported: loop the per-year pages
(2019 through the current year; older years exist but carry no table),
one HTML table per page, whose last row is the page's own totals row
(dropped structurally: its Title cell is blank). BLN's positional-cell
hacks (the 2021 "extra column" insert) are replaced by keying each
table's cells off its own header row, because the live layouts genuinely
differ: 2019-2020 have 8 columns, 2022 has 9, 2021 and 2023+ have 10
(Industry and Notes added over time).

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/mo.py) exactly:

    Title                     -> company        (required)
    Received Sort descending  -> notice_date    (the sort-widget caption
                                                 rides along in the header
                                                 text; canonicalised to
                                                 "Received")
    Layoff date(s)            -> effective_date
    # affected                -> employees      (0 when none published)
    Location(s)               -> city           (BLN "location"; multi-city
                                                 notices keep the joined
                                                 text, WA-style)
    County                    -> county         (blank for multi-location
                                                 and out-of-state HQ rows)
    Type                      -> layoff_type    (Closing / Layoff /
                                                 Furlough / Loss of
                                                 Contract / Company
                                                 Restructure, as published)
    Industry                  -> industry       (2021+ layouts only)
    Region, Notes             -> dropped        (not unified fields)

MO publishes no street address; ``address`` is never fabricated.

Date quirks honored (BLN ``date_format`` list + ``transform_date``
fallback): amended notices render both dates plus a marker in Received
("10/02/2019 10/30/2020 rev") and phased layoffs render several Layoff
dates — the FIRST date wins (BLN's first-token cleanup), so notice_date
is the original filing date, never the revision date. BLN's
``date_corrections`` are vendored verbatim; "03/20/0202" -> 2020-03-20 is
added from the live feed (same digit-mangled-year class as BLN's
"11/08/2109"; the row's own <time datetime> attribute carries the same
typo, and the notice was received 03/31/2020). Any other out-of-window
year parses to None — junk is never emitted. BLN's ``jobs_corrections``
are vendored verbatim ("330 remote workers (18 located in Missouri)" ->
18; "Unknown" -> no published count -> 0).
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

BASE_URL = "https://jobs.mo.gov/warn"

# First year the state publishes as a table page (BLN warn-scraper mo.py;
# older year URLs render the page chrome with no table).
START_YEAR = 2019

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

# BLN warn-transformer transformers/mo.py date_format, tried in order.
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%B %Y", "%B %d, %Y"]

# Vendored from BLN warn-transformer transformers/mo.py date_corrections
# (as (y, m, d) tuples; None = no usable date). "03/20/0202" is a
# live-feed addition — see the module docstring.
DATE_CORRECTIONS = {
    "04/-9/2020": (2020, 4, 9),
    "March 2020": (2020, 3, 1),
    "": None,
    "11/08/2109": (2019, 11, 8),
    "03/20/0202": (2020, 3, 20),
}

# Vendored from BLN warn-transformer transformers/mo.py jobs_corrections;
# None = the state published no usable count.
JOBS_CORRECTIONS = {
    "330 remote workers (18 located in Missouri)": 18,
    "Unknown": None,
}

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

_MISSING = object()


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _is_challenge(text) -> bool:
    """True when the response is the Incapsula interstitial, not a page.

    Every genuine jobs.mo.gov response (even an empty year's) titles
    itself "WARN Notices"; the challenge stubs never do but always
    reference the /_Incapsula_Resource script.
    """
    return "WARN Notices" not in text and "_Incapsula_Resource" in text


def _extract_rows(html) -> list:
    """One year page's table -> list of dicts keyed by its own headers.

    Adapted from BLN warn-scraper mo.py (html5lib, first table, header
    row first) — but keyed on header names instead of cell positions so
    the 8/9/10-column year layouts all map correctly. The sort widget's
    caption ("Received Sort descending") is stripped from the header.
    Returns [] when the page carries no table (a year with no notices).
    """
    soup = BeautifulSoup(html, "html5lib")
    table = soup.find("table")
    if table is None:
        return []
    trs = table.find_all("tr")
    if not trs:
        return []
    headers = [
        _squish(cell.get_text(" ")).replace("Sort descending", "").strip()
        for cell in trs[0].find_all(["th", "td"])
    ]
    rows = []
    for tr in trs[1:]:
        cells = [_squish(cell.get_text(" ")) for cell in tr.find_all(["td", "th"])]
        if not cells:
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or _MISSING."""
    if text not in DATE_CORRECTIONS:
        return _MISSING
    ymd = DATE_CORRECTIONS[text]
    return None if ymd is None else "%04d-%02d-%02d" % ymd


def _try_formats(text):
    """The four known formats in order -> ISO date, else None."""
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
    """MO date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's mo.py transform_date: corrections + formats on the full
    string first; on failure, BLN's cleanup — first whitespace token,
    dash-range start, en-dash and comma stripping — then corrections +
    formats again. The first date therefore wins in amended Received
    cells ("10/02/2019 10/30/2020 rev") and phased Layoff cells.
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
    text = text.split()[0].strip()
    text = text.split("-")[0].strip()
    text = text.replace("–", "")
    text = text.replace(",", "")
    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed
    return _try_formats(text)


def _clean_employees(val) -> int:
    """# affected cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


class MissouriOWD(Source):
    code = "mo"
    name = "Missouri"
    agency = "Missouri Office of Workforce Development"
    source_url = f"{BASE_URL}/"
    cadence = "daily"
    # Incapsula bot wall blocks non-browser clients — see module docstring.
    enabled = False

    # -- fetch --------------------------------------------------------------

    def _get_year(self, session, year: int, first: bool) -> str:
        """One year page politely: 1 req/s, 60 s timeout, 3 attempts."""
        url = f"{BASE_URL}/{year}"
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[MO] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[MO] {url} -> HTTP {resp.status_code}")
                continue
            if _is_challenge(resp.text):
                last_err = RuntimeError("Incapsula bot challenge served")
                log.warning(f"[MO] {url} -> Incapsula challenge")
                continue
            return resp.text
        raise RuntimeError(f"MO feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape every year page into one consolidated JSON file.

        Any year failing (bot wall, HTTP error) aborts the whole fetch —
        a partial crawl must never be written, or the diff engine would
        report the missing years as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        rows = []
        years = range(date.today().year, START_YEAR - 1, -1)
        for i, year in enumerate(years):
            html = self._get_year(session, year, first=(i == 0))
            year_rows = _extract_rows(html)
            log.info(f"[MO] {year}: {len(year_rows)} table rows")
            for row in year_rows:
                rows.append({"Year": year, **row})

        # The live feed has carried 350+ notices since 2019; a collapse
        # below 100 means the page layout changed, not mass rescissions.
        if len(rows) < 100:
            raise RuntimeError(
                f"MO feed: only {len(rows)} table rows across all years — "
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
            company = _squish(row.get("Title") or "")
            if not company:
                continue  # the per-page totals row, or padding
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Received")),
                    "effective_date": _clean_date(row.get("Layoff date(s)")),
                    "employees": _clean_employees(row.get("# affected") or ""),
                    "layoff_type": _squish(row.get("Type") or ""),
                    "county": _squish(row.get("County") or ""),
                    "city": _squish(row.get("Location(s)") or ""),
                    "industry": _squish(row.get("Industry") or ""),
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
                "county",
                "city",
                "industry",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
