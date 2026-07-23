"""
warn_sources.la
---------------
Louisiana — WARN notices published by the Louisiana Workforce Commission
as per-year PDFs (``WarnNotices<YYYY>.pdf``) linked from the Workforce
Development downloads page at
``https://www.laworks.net/Downloads/Downloads_WFD.asp``.

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/la.py) — ported, never imported: find every anchor whose
text contains "WARN Notices" on the downloads page, download each linked
PDF, and parse its tables. BLN used camelot; this port uses pdfplumber
(already a project dependency) whose lattice extraction reads the same
ruled tables cleanly — none of camelot's column-shift artifacts occur, so
BLN's artifact corrections ("601"/"330"/"560" as dates, "3/16/26" etc. as
job counts — the entries BLN itself flags "HEY! Stucka really needs to
fix this") are deliberately NOT vendored: with a clean extraction they
could only corrupt a legitimate value.

**Backfill depth:** the state itself prunes old year PDFs from the page —
probed 2026-07-21, only 2025 and 2026 are linked, and the unlinked
``WarnNotices2024.pdf`` URL now returns 404 (BLN's scraper carries the
same "Historical PDFs are not available" lament). The initial backfill is
therefore whatever years the page links (currently 2025-present); the
cumulative ledger preserves rows after the state drops a year.

The PDFs have used two layouts (both live today, both handled per BLN's
la.py):

* 2026+: six columns with a separate Address column.
* 2025:  five columns; the street address rides inside the Company Name
  cell. BLN's split rule, ported exactly: the address starts at the first
  line that begins with a digit; without such a line the first line is
  the company and any remaining lines are the address (facility
  designations like "(Premier Health Urgent Care)" land in address —
  a BLN quirk kept as-is). One deviation: multi-line company names are
  joined with a space ("General Dynamics Information Technology"), not
  BLN's ", " (which would yield "…Information, Technology").

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/la.py) exactly:

    Company Name        -> company        (required)
    Notice Date         -> notice_date
    Layoff Date         -> effective_date
    Employees Affected  -> employees      (0 when none published)
    Address             -> address        (BLN "location")
    Industry            -> industry       (published but outside BLN's
                                           10-field schema)

LA publishes no county, city, or layoff-type columns; none are
fabricated.

Date quirks honored (BLN ``date_format`` list + ``transform_date``
cleanup chain, vendored): corrections first, then %m/%d/%Y and %m/%d/%y,
then BLN's split cascade (" and ", " to ", " - ", " & ", " – ", "-",
first whitespace token) and a retry — so phased dates
("7/31/25 to 12/31/25") keep their start date and "Not specified"
becomes None. BLN's genuine ``date_corrections`` are vendored verbatim;
its "Starting" -> 2023-08-21 row-specific hack is replaced by stripping
the word case-insensitively (BLN already strips lowercase "starting"),
which parses the same historical cell correctly without poisoning any
future "Starting <date>" cell. Out-of-window years parse to None — junk
is never emitted. BLN's ``jobs_corrections`` are vendored verbatim
(ranges keep their lower bound; "TBD"/"NA" -> no published count -> 0).

Footnote fragments — rows with at most two populated cells, e.g. the
2025 "(*)UPS … Rescinded on 9/5/2025" note — are BLN's "supplement"
rows (mapped to notes, not a unified field) and are dropped; the
rescinded notice itself stays exactly as the state publishes it.
"""

import io
import json
import logging
import os
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

BASE_URL = "https://www.laworks.net/"
DOWNLOADS_URL = f"{BASE_URL}Downloads/Downloads_WFD.asp"

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

# Vendored from BLN warn-scraper la.py ``headerfixes`` (+ Industry, which
# BLN's 10-field schema drops but this project's unified schema keeps).
HEADER_MAP = {
    "Company Name": "company_original",
    "Address": "address_original",
    "Notice Date": "date_notice",
    "Layoff Date": "date_action",
    "Employees Affected": "affected",
    "Industry": "industry",
}

# BLN warn-transformer transformers/la.py date_format, tried in order.
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]

# Vendored from BLN warn-transformer transformers/la.py date_corrections
# (as (y, m, d) tuples; None = no usable date). The camelot-artifact
# entries and the "Starting" hack are intentionally absent — see the
# module docstring.
DATE_CORRECTIONS = {
    "6/31/09": (2009, 6, 30),
    "N/A": None,
    "5/1820": (2020, 5, 18),
    "4/10/23 (Updated 7/12/23)": (2023, 4, 10),
    "2/26/25": (2025, 2, 26),
    "12/31/25": (2025, 12, 31),
    "Various": None,
    "10/3124": (2024, 10, 31),
    "Not": None,
}

# Vendored from BLN warn-transformer transformers/la.py jobs_corrections
# (minus its camelot-artifact date-string keys); None = the state
# published no usable count.
JOBS_CORRECTIONS = {
    "700 *exact number pending relocation to other departments": 700,
    "50-297": 50,
    "426 426": 426,
    "1 Multi-state notification- Louisiana total =1": 1,
    "48 by 8/28/2015 closure by 12/2015": 48,
    "60-70": 60,
    "8 +1": 9,
    "385 465": 385,
    "114 +112": 226,
    "227 -8": 227,
    "32 +1": 32,
    "167 100": 167,
    "4 +55": 59,
    "150 +50": 200,
    "NA": None,
    "161 +1": 162,
    "23 15 41 4": 23,
    "70 +2": 72,
    "30 +5 +4 +1 +3": 43,
    "420 405 369": 420,
    "n/a": None,
    "74 +1 +21": 96,
    "84 -4 +10": 98,
    "1*": 1,
    "100 98": 98,
    "100 98 98": 98,
    "100 98 90": 90,
    "1 (Louisiana)": 1,
    "100 98 9087": 87,
    "100 98 90 87": 87,
    "TBD": None,
    "144 56": 56,
    "125* *Only one employee affected in Louisiana.": 1,
    "8*": 8,
    "51*": 51,
    "83*": 83,
    "38*": 38,
    "*292 *Only forty (40) employees affected in Louisiana.": 40,
    "*508 *Only sixty- three (63) employees affected in Louisiana.": 63,
    "*179 *Some or all of the workers may be retained by the new vendor.": 0,
    "*434 *Some or all may be picked up by new vendor": 0,
    "3* *located in Louisiana": 3,
    "*508 *Only sixty-three (63) employees affected in Louisiana.": 63,
    "139* *Only 3 employees are affected in Louisiana.": 3,
    "58* *Only 1 employee is affected in Louisiana.": 1,
    "65* 4*": 4,
}

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

_MISSING = object()


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Cell text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _join_lines(val) -> str:
    """Multi-line cell text -> one comma-joined line (address style).

    Trailing commas the PDF already carries ("601 Poydras Street,") are
    stripped per line so the join never doubles them up.
    """
    lines = (_squish(line).rstrip(",") for line in str(val).splitlines())
    return ", ".join(line for line in lines if line)


def _split_company_address(cell) -> tuple:
    """Address-less layout's Company Name cell -> (company, address).

    BLN warn-scraper la.py rule, ported: the street address starts at the
    first line beginning with a digit; everything before it is the
    company name (line wraps joined with a space). Without such a line,
    the first line is the company and any remaining lines the address.
    """
    text = str(cell).replace("\xa0", " ")
    match = re.search(r"\n\d", text)
    if match:
        return _squish(text[: match.start()]), _join_lines(text[match.start():])
    lines = [_squish(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""
    return lines[0], ", ".join(line.rstrip(",") for line in lines[1:])


def _rows_from_tables(tables, context="") -> list:
    """pdfplumber tables -> raw row dicts keyed by canonical field names.

    Header rows (first cell "Company Name") set the column mapping — they
    repeat on later pages and differ between the five- and six-column
    layouts. Rows with at most two populated cells are BLN's footnote /
    supplement fragments and are dropped. Raw cell text (newlines and
    all) is preserved: the company/address split needs it.
    """
    rows: list = []
    headers = None
    for table in tables or []:
        for cells in table or []:
            filled = [c for c in cells if _squish(c or "")]
            if not filled:
                continue
            if _squish(cells[0] or "") == "Company Name":
                headers = [
                    HEADER_MAP.get(_squish(c or ""), _squish(c or ""))
                    for c in cells
                ]
                continue
            if headers is None:
                raise RuntimeError(
                    "LA feed: table lacks the known 'Company Name' header"
                    f" row — layout may have changed ({context})"
                )
            if len(filled) <= 2:
                continue  # footnote fragment (e.g. a rescission note)
            rows.append({h: (c or "") for h, c in zip(headers, cells)})
    return rows


def _extract_pdf_rows(pdf_source, year=None, filename="") -> list:
    """One WARN Notices PDF -> raw row dicts (all pages, headers dropped)."""
    tables = []
    with pdfplumber.open(pdf_source) as pdf:
        for page in pdf.pages:
            tables.extend(page.extract_tables())
    rows = _rows_from_tables(tables, context=filename or str(pdf_source))
    for row in rows:
        if year is not None:
            row["year"] = year
        if filename:
            row["file"] = filename
    return rows


def _correction(text):
    """date_corrections lookup -> ISO date, None (no date), or _MISSING."""
    if text not in DATE_CORRECTIONS:
        return _MISSING
    ymd = DATE_CORRECTIONS[text]
    return None if ymd is None else "%04d-%02d-%02d" % ymd


def _try_formats(text):
    """The two known formats in order -> ISO date, else None."""
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
    """LA date cell -> strict ISO YYYY-MM-DD or None (never junk).

    Mirrors BLN's la.py transform_date: corrections + formats on the full
    string first; on failure, BLN's cleanup — strip "starting", keep the
    first chunk of " and "/" to "/" - "/" & "/" – " and "-" ranges, then
    the first whitespace token — and corrections + formats again. Phased
    dates therefore keep their start date and "Not specified" -> None.
    """
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
    # BLN's cleanup chain, in its exact order ("starting" stripped
    # case-insensitively — see the module docstring).
    text = re.sub(r"(?i)starting", "", text).strip()
    for sep in (" and ", " to ", " - ", " & ", " – "):
        text = text.split(sep)[0].strip()
    text = text.split("-")[0].strip()
    if not text:
        return None
    text = text.split()[0].strip()
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


class LouisianaLWC(Source):
    code = "la"
    name = "Louisiana"
    agency = "Louisiana Workforce Commission"
    source_url = DOWNLOADS_URL
    cadence = "weekly"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url, first=False):
        """One URL politely: 1 req/s, 60 s timeout, 3 attempts."""
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[LA] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[LA] {url} -> HTTP {resp.status_code}")
                continue
            return resp
        raise RuntimeError(f"LA feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the downloads page, pull every linked WARN PDF, and
        write one consolidated JSON of raw table rows.

        Any listed PDF failing (HTTP error, non-PDF body) aborts the
        whole fetch — a partial crawl must never be written, or the diff
        engine would report the missing year as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        page = self._get(session, DOWNLOADS_URL, first=True)
        soup = BeautifulSoup(page.text, "html.parser")
        links = sorted(
            {
                urljoin(BASE_URL, a["href"])
                for a in soup.find_all("a", href=True)
                if "WARN Notices" in a.get_text()
            }
        )
        if not links:
            raise RuntimeError(
                "LA feed: no 'WARN Notices' links on the downloads page — "
                "page layout may have changed"
            )

        rows = []
        for url in links:
            fname = os.path.basename(url)
            year_match = re.search(r"(\d{4})", fname)
            year = int(year_match.group(1)) if year_match else None
            resp = self._get(session, url)
            if not resp.content.startswith(b"%PDF"):
                raise RuntimeError(f"LA feed: {url} did not return a PDF")
            pdf_rows = _extract_pdf_rows(
                io.BytesIO(resp.content), year=year, filename=fname
            )
            log.info(f"[LA] {fname}: {len(pdf_rows)} data rows")
            rows.extend(pdf_rows)

        # The page always carries at least the current + prior year;
        # a near-empty crawl means the layout changed, not zero layoffs.
        if len(rows) < 5:
            raise RuntimeError(
                f"LA feed: only {len(rows)} data rows across "
                f"{len(links)} PDFs — layout may have changed"
            )

        self.paths.ensure()
        payload = {"source": self.source_url, "files": len(links), "rows": rows}
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
            if "address_original" in row:
                # Six-column layout: a dedicated Address column.
                company = _squish(row.get("company_original") or "")
                address = _join_lines(row.get("address_original") or "")
            else:
                # Five-column layout: address embedded in the company cell.
                company, address = _split_company_address(
                    row.get("company_original") or ""
                )
            if not company:
                continue  # header/junk residue — company is required
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("date_notice")),
                    "effective_date": _clean_date(row.get("date_action")),
                    "employees": _clean_employees(row.get("affected") or ""),
                    "address": address,
                    "industry": _squish(row.get("industry") or ""),
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "address",
                "industry",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
