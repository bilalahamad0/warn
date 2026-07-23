"""
warn_sources.nm
---------------
New Mexico — WARN notices published by the Department of Workforce
Solutions as per-year PDF tables linked from the Rapid Response page
(one ``<year>_WARN*.pdf`` per year, 2016 through the current year).

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/nm.py) — ported, never imported: fetch the Rapid
Response page, collect every ``<a href>`` containing "WARN" and ending
in ".pdf", download each PDF and pull its table with pdfplumber. The
canonical URL (dws.state.nm.us) 301-redirects to www.dws.nm.gov, so
relative PDF hrefs are resolved against the *final* response URL.
BLN's positional cells are replaced by keying each row off the PDF's
own header row; repeated headers and all-blank padding rows are dropped
structurally. The host's F5 gate rejects terse/robotic User-Agents with
an HTML "Request Rejected" stub (observed live 2026-07-21) but passes a
full browser UA — fetch verifies the ``%PDF`` magic so that stub can
never be parsed as data, and any file failing aborts the whole fetch
(a partial crawl would surface as phantom withdrawals in the diff
engine).

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/nm.py) exactly:

    JOB SITE NAME       -> company        (required)
    NOTICE DATE         -> notice_date
    LAYOFF DATE         -> effective_date
    TOTAL LAYOFF NUMBER -> employees      (0 when none published)
    CITY NAME           -> city           (BLN "location")
    COUNTY NAME         -> county         (published in every live PDF;
                                           postdates BLN's 5-field map)
    WDA NAME            -> dropped        (not a unified field)
    RECEIVED DATE       -> dropped        (distinct from NOTICE DATE;
                                           never copied into it)

NM publishes no layoff type, street address, or industry; none is
fabricated.

Date quirks honored: BLN's ``date_format`` list is tried in its exact
order, with one port fix — an out-of-window year means "wrong format,
try the next" rather than "no date", because ``%d-%b-%Y`` happily
parses the two-digit year in "05-Jan-17" as year 17 (Python's ``%Y``
accepts 1-4 digits) and must fall through to ``%d-%b-%y``. BLN's
``date_corrections`` are vendored verbatim ("1/0/00" -> no usable date
— live in the 2023 PDF; "July-September 2025" -> 2025-07-15), and the
2025 correction's month-range convention (15th of the first month) is
generalised to a rule so the live feed's "July - August 2026" and
future phased-layoff ranges parse the same way. BLN's
``jobs_corrections`` are vendored verbatim ("Not Disclosed", "?",
"N/A" -> no published count -> 0; all three live in the 2016-2023
PDFs).
"""

import io
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://www.dws.state.nm.us/Rapid-Response"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# BLN warn-transformer transformers/nm.py date_format, tried in order.
DATE_FORMATS = ["%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%m/%d/%y"]

# Vendored from BLN warn-transformer transformers/nm.py date_corrections
# (as (y, m, d) tuples; None = no usable date). "July-September 2025"
# is also covered by the generic month-range rule below; kept verbatim
# for fidelity.
DATE_CORRECTIONS = {
    "1/0/00": None,
    "July-September 2025": (2025, 7, 15),
}

# Phased layoffs render as "MonthA-MonthB YYYY" (spacing varies).
# BLN's correction pins such ranges to the 15th of the first month;
# this rule generalises that convention.
MONTH_RANGE = re.compile(r"^([A-Za-z]+)\s*[-–]\s*[A-Za-z]+\s+(\d{4})$")

# Vendored from BLN warn-transformer transformers/nm.py jobs_corrections;
# None = the state published no usable count.
JOBS_CORRECTIONS = {
    "Not Disclosed": None,
    "?": None,
    "N/A": None,
}

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

# Column headers of every live PDF (2016-2026); fetch keys rows off the
# header row it actually finds, this is the required subset.
REQUIRED_HEADERS = {"JOB SITE NAME", "NOTICE DATE"}

_MISSING = object()


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string (BLN nm.py _clean_text)."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _is_rejected(text: str) -> bool:
    """True when the response is the F5 'Request Rejected' HTML stub."""
    return "Request Rejected" in text or "requested URL was rejected" in text


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or _MISSING."""
    if text not in DATE_CORRECTIONS:
        return _MISSING
    ymd = DATE_CORRECTIONS[text]
    return None if ymd is None else "%04d-%02d-%02d" % ymd


def _try_formats(text):
    """BLN's four formats in order -> ISO date, else None.

    An out-of-window year means the *format* mis-matched (e.g. %d-%b-%Y
    reading "05-Jan-17" as year 17), so the loop continues instead of
    giving up — see the module docstring.
    """
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
    return None


def _clean_date(val):
    """NM date cell -> strict ISO YYYY-MM-DD or None (never junk)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = _squish(val)
    if not text:
        return None
    fixed = _correction(text)
    if fixed is not _MISSING:
        return fixed
    iso = _try_formats(text)
    if iso is not None:
        return iso
    m = MONTH_RANGE.match(text)
    if m:
        try:
            parsed = datetime.strptime(f"{m.group(1)} 15 {m.group(2)}", "%B %d %Y")
        except ValueError:
            return None
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
    return None


def _clean_employees(val) -> int:
    """TOTAL LAYOFF NUMBER cell -> int; 0 when no usable count published."""
    text = _squish(val)
    if text in JOBS_CORRECTIONS:
        fixed = JOBS_CORRECTIONS[text]
        return 0 if fixed is None else fixed
    count = warn_monitor._safe_int(text)
    return count if count is not None and count >= 0 else 0


def _extract_pdf_rows(content: bytes, file_name: str) -> list:
    """One WARN PDF -> list of dicts keyed by its own header row.

    Adapted from BLN warn-scraper nm.py (pdfplumber extract_table per
    page) — keyed on the header instead of cell positions. Repeated
    header rows and all-blank padding rows are dropped; the multi-page
    2020 PDF carries its header only on page one, so the keys persist
    across pages.
    """
    rows = []
    headers = None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for raw in page.extract_table() or []:
                cells = [_squish(cell) for cell in raw]
                if not any(cells):
                    continue
                if headers is None:
                    headers = cells
                    if not REQUIRED_HEADERS.issubset(headers):
                        raise RuntimeError(
                            f"NM feed: unrecognized table header in "
                            f"{file_name}: {headers}"
                        )
                    continue
                if cells == headers or cells[0] == "NOTICE DATE":
                    continue  # repeated header
                rows.append({"file": file_name, **dict(zip(headers, cells))})
    if headers is None:
        raise RuntimeError(f"NM feed: no table found in {file_name}")
    return rows


class NewMexicoDWS(Source):
    code = "nm"
    name = "New Mexico"
    agency = "New Mexico Department of Workforce Solutions"
    source_url = PAGE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url: str, first: bool = False):
        """One URL politely: 1 req/s, 60 s timeout, 3 attempts."""
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[NM] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[NM] {url} -> HTTP {resp.status_code}")
                continue
            return resp
        raise RuntimeError(f"NM feed: fetch failed for {url} ({last_err})")

    def _pdf_urls(self, session) -> list:
        """Rapid Response page -> ordered, deduped absolute PDF URLs.

        BLN warn-scraper nm.py link filter: "WARN" in the href and the
        href ends in ".pdf". Relative hrefs resolve against the final
        (post-redirect) page URL.
        """
        resp = self._get(session, PAGE_URL, first=True)
        if _is_rejected(resp.text):
            raise RuntimeError("NM feed: Rapid Response page request rejected")
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = []
        for link in soup.find_all("a"):
            href = link.get("href", "")
            if "WARN" in href and href.endswith(".pdf"):
                absolute = urljoin(resp.url, href)
                if absolute not in urls:
                    urls.append(absolute)
        if not urls:
            raise RuntimeError(
                "NM feed: no WARN PDF links on the Rapid Response page — "
                "page layout may have changed"
            )
        return urls

    def fetch(self, force: bool = False) -> tuple:
        """Scrape every yearly PDF into one consolidated JSON file.

        Any file failing (bot wall, HTTP error, non-PDF body) aborts the
        whole fetch — a partial crawl must never be written, or the diff
        engine would report the missing years as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        rows = []
        for pdf_url in self._pdf_urls(session):
            file_name = pdf_url.rsplit("/", 1)[-1]
            resp = self._get(session, pdf_url)
            if not resp.content.startswith(b"%PDF"):
                raise RuntimeError(
                    f"NM feed: {file_name} is not a PDF "
                    "(bot gate stub or moved file)"
                )
            pdf_rows = _extract_pdf_rows(resp.content, file_name)
            log.info(f"[NM] {file_name}: {len(pdf_rows)} table rows")
            rows.extend(pdf_rows)

        # The live feed carries ~120 notices across 2016-2026; a collapse
        # below half means lost PDFs or a layout change, not withdrawals.
        if len(rows) < 60:
            raise RuntimeError(
                f"NM feed: only {len(rows)} table rows across all PDFs — "
                "feed may have moved or changed layout"
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
            company = _squish(row.get("JOB SITE NAME") or "")
            if not company:
                continue  # padding row
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("NOTICE DATE")),
                    "effective_date": _clean_date(row.get("LAYOFF DATE")),
                    "employees": _clean_employees(
                        row.get("TOTAL LAYOFF NUMBER") or ""
                    ),
                    "county": _squish(row.get("COUNTY NAME") or ""),
                    "city": _squish(row.get("CITY NAME") or ""),
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "county",
                "city",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
