"""
warn_sources.id
---------------
Idaho — Idaho Department of Labor.

The state publishes one cumulative PDF (2009-present, ~200 rows over 5
pages) linked from the layoff-assistance page. The URL path really is
"businesss" (triple s) — the typo is the state's, not ours. The PDF's
filename changes with each update (e.g. ``Idaho-WARN-Notices-7.9.26.pdf``),
so ``fetch`` first scrapes the landing page for the current link, then
delegates the conditional download to ``warn_monitor.download_xlsx``
(format-agnostic; the endpoint honors ETag/Last-Modified).

Link discovery is vendored from Big Local News' warn-scraper (Apache-2.0)
``scrapers/id.py``: the notices link is the last anchor before the
"Contact" heading; we additionally prefer an anchor whose PDF href/text
mentions WARN, so nav-link reshuffles can't hijack the pick.

Table layout: ``Date of Letter | Updates | Company | Address | City |
State | Zip | No. of Employees Affected | Effective or Commencing Date``.
The header repeats on every page and is skipped. Field crosswalk vendored
from BLN warn-transformer (Apache-2.0) ``transformers/id.py``:

    Date of Letter               -> notice_date
    Company                      -> company
    Address                      -> address
    City                         -> city
    No. of Employees Affected    -> employees   (custom transform, below)
    Effective or Commencing Date -> effective_date

``Updates`` (free-prose "received/revised" annotations), ``State`` and
``Zip`` are dropped. Rows for out-of-state headquarters (e.g. a NJ-based
employer cutting Idaho jobs) are kept — they are Idaho filings, and the
jobs corrections map their counts to the in-Idaho figure.

Quirks honored per the BLN transformer: date cells may jam several dates
together ("4/10/2026 5/1/2026 5/31/2026" for phased layoffs) — the FIRST
date wins, "starting" prefixes are stripped, and known-mangled strings go
through the corrections table. The jobs column mixes plain ints with
"120 (2 in ID)", "(1 in ID)", "80-100", "TBD" — corrections plus generic
"(N in ID)" / range fallbacks keep the in-Idaho count (never the
nationwide total). BLN's scraper forward-filled empty cells from the row
above for page-spanning merged cells; the current PDF keeps multi-line
content inside single cells, and blind forward-fill risks bleeding one
record's dates into another, so companyless fragments are dropped and
logged instead.
"""

import dataclasses
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

BASE_URL = "https://www.labor.idaho.gov"
# Triple-s "businesss" is the state's real URL, not a typo of ours.
PAGE_URL = "https://www.labor.idaho.gov/businesss/layoff-assistance/"

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

# BLN transformer date formats for ID (transformers/id.py, Apache-2.0).
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]

# Known-mangled date strings, vendored from BLN warn-transformer
# (transformers/id.py, Apache-2.0). Keys are raw cell strings after
# whitespace collapse; values are the intended dates.
DATE_CORRECTIONS = {
    "2/19/219": datetime(2019, 2, 19),
    "3/7/2010-3/20/2010": datetime(2010, 3, 7),
    "rting": datetime(2015, 2, 16),
    "3/7/2010-3/20/2": datetime(2010, 3, 7),
    "2/19/219 (rec'd 2/26/19)": datetime(2019, 2, 19),
}

# Free-text employee counts, vendored from BLN warn-transformer
# (transformers/id.py, Apache-2.0). None = state published no usable
# count (parse() maps that to 0 per the unified schema). The bare
# "22000" was a nationwide total with no Idaho figure.
JOBS_CORRECTIONS = {
    "8 in ID": 8,
    "17 in ID": 17,
    "80-100": 80,
    "2 5s1ta": 251,
    "120 (2 in ID)": 2,
    "106 (17 in ID)": 17,
    "22000": None,
    "22000 (102 in ID)": 102,
    "TBD": None,
    "135 (1 in ID)": 1,
    "324 (32 in ID)": 32,
    "(1 in ID)": 1,
}

# Generic fallbacks for cells the corrections table hasn't seen yet.
_IN_ID_RE = re.compile(r"\((\d[\d,]*)\s+in\s+ID\)", re.I)
_BARE_IN_ID_RE = re.compile(r"^(\d[\d,]*)\s+in\s+ID$", re.I)
_RANGE_RE = re.compile(r"^(\d[\d,]*)\s*-\s*\d[\d,]*$")
_DATE_TOKEN_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")

# (header substring, unified column) — matched case-insensitively. The
# employees needle is just "employees" because the state has published
# both "No. of EmployeesAffected" and "No. of Employees Affected" (the
# BLN transform_jobs quirk).
_HEADER_MAP = [
    ("date of letter", "notice_date"),
    ("company", "company"),
    ("address", "address"),
    ("city", "city"),
    ("employees", "employees"),
    ("effective", "effective_date"),
]

_OUT_COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "city",
    "address",
]


def _clean_text(text) -> str:
    """Collapse newlines/whitespace in a PDF cell (BLN scrapers/id.py)."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


class IdahoDOL(Source):
    code = "id"
    name = "Idaho"
    agency = "Idaho Department of Labor"
    source_url = PAGE_URL
    cadence = "as-filed"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        # Keep a .pdf suffix on the raw download for clarity/tooling.
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.pdf")

    # -- fetch --------------------------------------------------------------

    @staticmethod
    def _find_pdf_url(html: str) -> str:
        """Locate the current WARN-notices PDF link on the landing page.

        Vendored from BLN warn-scraper scrapers/id.py (Apache-2.0): the
        link is the last anchor before the "Contact" heading. Hardened:
        prefer a .pdf anchor mentioning WARN so nav reshuffles can't
        hijack the last-anchor heuristic.
        """
        localized = html.split("<h2>Contact")[0]
        soup = BeautifulSoup(localized, "html.parser")
        anchors = [a for a in soup.find_all("a") if (a.get("href") or "").strip()]
        if not anchors:
            raise RuntimeError("ID feed: no anchors found on landing page")

        def blob(a):
            return f"{a.get('href', '')} {a.get_text()}".lower()

        pdfish = [
            a for a in anchors
            if ".pdf" in a["href"].lower() and "warn" in blob(a)
        ]
        chosen = (pdfish or anchors)[-1]["href"].strip()
        if "https" in chosen:
            return chosen
        return f"{BASE_URL}{chosen}"

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the landing page for the current PDF link, then download
        it with ETag/Last-Modified caching. Returns (changed, raw_path)."""
        self.paths.ensure()
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        # BLN cache-buster: the site occasionally pins a ?v= param on this
        # page; a random value defeats stale CDN copies of the link list.
        page_url = f"{PAGE_URL}?v={random.randrange(0, 10_000_000_000)}"
        resp = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)  # politeness + backoff
            try:
                resp = session.get(page_url, timeout=60)
                if resp.status_code == 200:
                    break
            except requests.RequestException as exc:
                log.warning(f"[ID] landing page attempt {attempt + 1}: {exc}")
                resp = None
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"ID feed: landing page {status} ({PAGE_URL})")

        pdf_url = self._find_pdf_url(resp.text)
        log.info(f"[ID] current notices PDF: {pdf_url}")

        time.sleep(1)  # politeness: max 1 request/second/host
        if not self.paths.raw.exists():
            force = True  # cached ETag but no local file: a 304 would strand us
        return warn_monitor.download_xlsx(
            force=force,
            url=pdf_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(val):
        """Cell -> ISO YYYY-MM-DD or None, per BLN transform_date."""
        text = _clean_text(val)
        if not text:
            return None
        if text in DATE_CORRECTIONS:
            fixed = DATE_CORRECTIONS[text]
            return fixed.strftime("%Y-%m-%d") if fixed else None
        # BLN transform_date: drop "starting", keep the first token
        # (multi-date cells list phased layoffs; the first date wins),
        # strip commas.
        text = text.replace("starting", "").strip()
        if text in DATE_CORRECTIONS:  # e.g. "rting" arose from this split
            fixed = DATE_CORRECTIONS[text]
            return fixed.strftime("%Y-%m-%d") if fixed else None
        first = text.split()[0].replace(",", "").strip() if text else ""
        candidates = [first] if first else []
        m = _DATE_TOKEN_RE.search(text)
        if m and m.group(1) not in candidates:
            candidates.append(m.group(1))  # fallback: first m/d/y anywhere
        for cand in candidates:
            if cand in DATE_CORRECTIONS:
                fixed = DATE_CORRECTIONS[cand]
                return fixed.strftime("%Y-%m-%d") if fixed else None
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(cand, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
        log.warning(f"[ID] unparseable date {_clean_text(val)!r} -> None")
        return None

    @staticmethod
    def _clean_jobs(val) -> int:
        """Cell -> int employee count; 0 when the state published none.

        Corrections first (BLN transform_jobs quirks), then generic
        "(N in ID)" / "N in ID" / range fallbacks — always preferring the
        in-Idaho figure over a nationwide total.
        """
        text = _clean_text(val)
        if text in JOBS_CORRECTIONS:
            n = JOBS_CORRECTIONS[text]
            return n if n is not None else 0
        m = _IN_ID_RE.search(text) or _BARE_IN_ID_RE.match(text)
        if m:
            return int(m.group(1).replace(",", ""))
        m = _RANGE_RE.match(text)
        if m:
            return int(m.group(1).replace(",", ""))
        n = warn_monitor._safe_int(text)
        return n if n is not None else 0

    @staticmethod
    def _header_columns(row) -> Optional[dict]:
        """If ``row`` is the table header, return {field: column_index}."""
        texts = [_clean_text(c).lower() for c in row]
        if not any("date of letter" in t for t in texts):
            return None
        col_idx = {}
        for j, text in enumerate(texts):
            for needle, field in _HEADER_MAP:
                if needle in text and field not in col_idx:
                    col_idx[field] = j
                    break
        return col_idx

    def parse(self, raw_path) -> pd.DataFrame:
        records = []
        col_idx = None
        with pdfplumber.open(str(raw_path)) as pdf:
            for page in pdf.pages:
                for row in page.extract_table() or []:
                    if not any(_clean_text(c) for c in row):
                        continue  # fully empty row
                    header = self._header_columns(row)
                    if header is not None:
                        col_idx = header  # first or repeated page header
                        continue
                    if col_idx is None:
                        continue  # pre-header junk (title lines etc.)
                    rec = self._parse_row(row, col_idx)
                    if rec is not None:
                        records.append(rec)
        if col_idx is None:
            raise ValueError("ID PDF: header row not found")
        log.info(f"[ID] parsed {len(records)} records")

        df = pd.DataFrame(records, columns=_OUT_COLUMNS)
        # The DataFrame constructor coerces None -> NaN in mixed columns;
        # restore true None so dates are strictly ISO-string-or-None.
        for col in ("notice_date", "effective_date"):
            df[col] = df[col].astype(object).where(df[col].notna(), None)
        return df

    def _parse_row(self, row, col_idx):
        def cell(field):
            j = col_idx.get(field)
            return row[j] if j is not None and j < len(row) else None

        company = _clean_text(cell("company"))
        if not company:
            log.warning(f"[ID] dropping companyless fragment row: {row!r}")
            return None
        return {
            "company": company,
            "notice_date": self._clean_date(cell("notice_date")),
            "effective_date": self._clean_date(cell("effective_date")),
            "employees": self._clean_jobs(cell("employees")),
            "city": _clean_text(cell("city")),
            "address": _clean_text(cell("address")),
        }
