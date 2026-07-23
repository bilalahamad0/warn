"""
warn_sources.ms
---------------
Mississippi — Mississippi Department of Employment Security WARN notices.

MDES publishes quarterly PDF tables (program-year quarters, July 2020 to
present) linked from one HTML page. Fetch mirrors Big Local News'
Apache-2.0 warn-scraper (scrapers/ms.py, authors Ash1R/stucka): collect
every ``.pdf`` link inside ``div#page_content`` (skipping ``map.pdf``),
re-download the five first-listed files each run (the current quarter is
updated in place as notices arrive), and cache the rest. Tables are pulled
with pdfplumber (BLN uses camelot; the fragment/junk-row handling of their
``pdfrodent`` helper is ported here): the wide per-page table is the data
table, header rows are recognised and normalised via BLN's header-fix
crosswalk, spill-over columns (merged cells that camelot/pdfplumber split)
fold back into the preceding named column, and mostly-empty wrap rows are
merged into the previous record.

Field crosswalk vendored from BLN warn-transformer (transformers/ms.py):
company="company", county="county" (their ``location``),
notice_date="date_notice", effective_date="date_of_action" header
("date_effective"), employees="affected", plus their date_corrections and
jobs_corrections dicts, date formats %m/%d/%Y and %m/%d/%y. Quirk honored:
pre-2024 quarters publish one merged "Company Name City (County)" column —
BLN maps that cell wholly to ``company``, so those rows carry no separate
city/county (never guessed out of the text). Newer quarters publish real
City and County columns. ``action_type`` ("Closure"/"Layoff") maps to
``layoff_type``; the NAICS code & description column maps to ``industry``.
Mississippi publishes no street address. Dates the state never gave
("Pending", "TBA", "Management Canceled"...) stay None — never copied from
the other date column.
"""

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://mdes.ms.gov/information-center/warn-information/"

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

# Canonical raw columns of the consolidated CSV written by fetch().
RAW_COLUMNS = [
    "date_notice",
    "company",
    "city",
    "county",
    "workforce_area",
    "event_number",
    "naics",
    "action_type",
    "affected",
    "date_effective",
    "reason",
    "source_file",
]

# Header spellings -> canonical names, vendored from BLN warn-scraper
# scrapers/ms.py ``headerfixes`` (whitespace collapsed; summary-table
# headers omitted because those 3-column tables are filtered out by width).
HEADER_FIXES = {
    "# Affected": "affected",
    "City": "city",
    "Company Name": "company",
    "Company Name (City) (County)": "company",
    "Company Name (City) (County) (Zip)": "company",
    "Company Name City (County)": "company",
    "Company Name City, (County)": "company",
    "Company Name, City (County)": "company",
    "Company Name, City, County": "company",
    "County": "county",
    "Date of Action": "date_effective",
    "Date of Notice": "date_notice",
    "Date of WARN Notice": "date_notice",
    "Event Number": "event_number",
    "NAICS CODE & Description": "naics",
    "NAICS CODE – Description": "naics",
    "Number Affected": "affected",
    "Reason / Comments": "reason",
    "Reason – Comments": "reason",
    "Type of Action": "action_type",
    "Type of Action # Affected": "action_type",
    "Workforce Area": "workforce_area",
    "Workforc e Area": "workforce_area",
}

# Known-bad date strings seen verbatim in the Mississippi PDFs, vendored
# from BLN warn-transformer transformers/ms.py ``date_corrections``
# (datetime values rendered as ISO; None = the state published no date).
DATE_CORRECTIONS = {
    "08/31/2023 09/01/2023": "2023-08-31",
    "6/21/20023": "2023-06-21",
    "10/05/202": "2020-10-05",
    "1/22/2025 Diamond Comic": "2025-01-22",
    "6/11/2025 WARN- Due to": "2025-06-11",
    "Management Canceled": None,
    "9/9/2025 WARN – Due to": "2025-09-09",
    "10/3/2024 WARN- Due to the": "2024-10-03",
    "1/27/2025 MW Components": "2025-01-27",
    "4/15/2025 WARN – Due to": "2025-04-15",
    "04/2022": "2022-08-04",
    "RR-pending": None,
    "08/26/2025 WARN – Due to": "2025-08-26",
    "03/23/2023 Sun Air Products": "2023-03-23",
    "6/30/2025 Non-WARN – non-": "2025-06-30",
    "6/23/2025 WARN – non-renewal": "2025-06-23",
    "4/15/2026 WARN -Decline in": "2026-04-15",
    "12/03/2024 Cooper Lighting": "2024-12-03",
    "No RR event - all employees have left this location": None,
    "4/3.2026": "2026-04-03",
    "10/03/2025 WARN- Due to": "2025-10-03",
    "3/23/2023 Alliance Healthcare": "2023-03-23",
    "7/31/2024 Hartson – Kennedy,": "2024-07-31",
    "12/16/2025 Westlake Chemical": "2025-12-16",
    "1/18/224": "2024-01-18",
    "6/30/2025 Non-WARN – Due to": "2025-06-30",
    "03/20/2023 GXO Logistics Supply": "2023-03-20",
    "02/07/2025 WARN – Due to": "2025-02-07",
    "9/25/2025 WARN – Due to": "2025-09-25",
    "TBA": None,
    "4/14/2025 WARN – Due to": "2025-04-14",
    "Declined RR event": None,
    "05/30/2026 WARN –": "2026-05-30",
    "03/18/2025 Mississippi Polymers": "2025-03-18",
    "12-1-2022": "2022-12-01",
    "4/15/2025 Non-WARN- Lack of": "2025-04-15",
    "10/10/2025 WARN - Declining": "2025-10-10",
    "2/2026": "2026-02-01",
    "02/18/2025 WWL Vehicle": "2025-02-18",
    "07/11/2025 Rex Lumber,": "2025-07-11",
    "6/24/2025 Non-WARN- This is a": "2025-06-24",
    "02/06/2025 Enviva Pellets": "2025-02-06",
    "7/31/ 2023": "2023-07-31",
    "Pending": None,
    "4/17/2026 Aramark": "2026-04-17",
    "6/15/2026 WARN – Due": "2026-06-15",
    "05/11/2026 Leggett &": "2026-05-11",
    "6/11/2026 WARN-Plant": "2026-06-11",
}

# Vendored from BLN warn-transformer transformers/ms.py ``jobs_corrections``.
JOBS_CORRECTIONS = {
    "1,000": 1000,
    "TBA": None,
}

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")  # BLN transformer date_format
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# company values that are really repeated-header/junk rows, lowercased.
_JUNK_COMPANIES = {"company", "company name", "nan", "none", "total"}

# PY2025 oct-dec quarter: merged cells put the "# Affected" count inside
# the Type of Action cell ("Closure 79", "Layoff TBA"). BLN handles the
# same quarter via its ``supplement_5`` -> affected carry.
_TYPE_COUNT_RE = re.compile(r"^(Closure|Layoffs?)\s+([\d,]+|TBA)$", re.I)

_MIN_TABLE_COLS = 6   # data tables are wide; summary tables have 3 columns
_MIN_DATA_CELLS = 3   # fewer filled cells = wrap fragment (BLN pdfrodent)


def _collapse(cell) -> str:
    """PDF cell -> single-spaced text ('' for None), as BLN's clean_cell."""
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def _strict_date(value):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _clean_date(value):
    """Raw Mississippi date cell -> ISO YYYY-MM-DD string or None.

    Exact-match corrections first (vendored from BLN), then the two BLN
    date formats; as a last resort the first whitespace token alone (the
    PDFs sometimes bleed the Reason column into a date cell — that is how
    most of the correction entries were born). Anything else is None —
    never raw text kept as a date, never the other date column.
    """
    raw = _collapse(value)
    if not raw:
        return None
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    iso = _strict_date(raw) or _strict_date(raw.split()[0])
    if iso and _ISO_RE.match(iso) and 1988 <= int(iso[:4]) <= 2100:
        return iso
    return None


def _strip_doubled(text: str) -> str:
    """Collapse 'X X' -> 'X' when a value is its own exact repetition.

    pdfplumber sometimes reads a merged cell twice (once whole, once as
    wrap fragments), reconstructing the same phrase back to back.
    """
    n = (len(text) - 1) // 2
    if n > 0 and len(text) == 2 * n + 1 and text[:n] == text[n + 1:]:
        return text[:n]
    return text


def _is_header_row(row) -> bool:
    """True when the row is the (possibly repeated) column-header row."""
    fixed = {HEADER_FIXES.get(_collapse(c)) for c in row}
    return "date_notice" in fixed and "company" in fixed


def _build_colmap(row) -> list:
    """Header row -> per-index canonical column names.

    Indices whose header cell is blank (pdfplumber splitting a merged
    cell) inherit the preceding named column so spill-over data folds
    back where it belongs.
    """
    colmap, last = [], None
    for cell in row:
        text = _collapse(cell)
        if text:
            last = HEADER_FIXES.get(text, text)
        colmap.append(last)
    return colmap


def _rows_from_table(table, carry_colmap=None):
    """One extracted PDF table -> (row dicts, colmap for carry-over).

    Ported from BLN warn-scraper's pdfrodent.parse_pdf: recognise header
    rows (initial and repeated), drop blank rows, and merge mostly-empty
    wrap fragments into the previous record.
    """
    rows, colmap = [], None
    for raw in table:
        if _is_header_row(raw):
            colmap = _build_colmap(raw)
            continue
        if colmap is None:
            if carry_colmap is not None and len(carry_colmap) == len(raw):
                colmap = carry_colmap
            else:
                return [], carry_colmap  # unmappable table (summary etc.)
        cells = [_collapse(c) for c in raw]
        if not any(cells):
            continue
        line: dict = {}
        for idx, val in enumerate(cells):
            if not val or idx >= len(colmap) or colmap[idx] is None:
                continue
            name = colmap[idx]
            line[name] = f"{line[name]} {val}".strip() if name in line else val
        filled = sum(1 for v in cells if v)
        if line.get("company") and filled >= _MIN_DATA_CELLS:
            rows.append(line)
        elif rows:  # wrap fragment — fold into the previous record
            prev = rows[-1]
            for key, val in line.items():
                prev[key] = f"{prev.get(key, '')} {val}".strip()
        # fragments before any data row (header debris) are dropped
    return rows, colmap or carry_colmap


def _parse_pdf(pdf_path) -> list:
    """Extract every data row from one quarterly MDES PDF."""
    out, carry = [], None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table[0]) < _MIN_TABLE_COLS:
                    continue  # cell-artifact / summary tables
                rows, carry = _rows_from_table(table, carry)
                out.extend(rows)
    for line in out:
        for key, val in line.items():
            line[key] = _strip_doubled(val)
        line["source_file"] = Path(pdf_path).name
    return out


class MississippiMDES(Source):
    code = "ms"
    name = "Mississippi"
    agency = "Mississippi Department of Employment Security"
    source_url = PAGE_URL
    cadence = "weekly"

    # -- fetch ---------------------------------------------------------------

    def _request(self, session, url, tries=3):
        """Polite GET with retries/backoff; returns a Response or None."""
        resp = None
        for attempt in range(tries):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                log.warning(f"[MS] request error for {url}: {e}")
                continue
            if resp.status_code == 200:
                return resp
            log.warning(f"[MS] HTTP {resp.status_code} for {url}")
            if resp.status_code == 404:
                return resp  # hard miss — no point retrying
        return resp

    def _pdf_urls(self, html: str) -> list:
        """WARN page HTML -> absolute quarterly-PDF URLs, listing order.

        Vendored BLN flow: anchors inside div#page_content ending .pdf,
        skipping the county ``map.pdf``; duplicates (the page repeats
        some links) collapse to the first occurrence.
        """
        prefix = PAGE_URL.split(".gov")[0] + ".gov"
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find(id="page_content") or soup
        urls: list = []
        for anchor in content.find_all("a"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            url = href if "http" in href else prefix + href
            if url.lower().endswith(".pdf") and not url.endswith("map.pdf"):
                if url not in urls:
                    urls.append(url)
        return urls

    def fetch(self, force: bool = False) -> tuple:
        """Download the quarterly PDFs (first five fresh, rest cached) and
        consolidate every parsed row into one CSV at ``self.paths.raw``."""
        self.paths.ensure()
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        resp = self._request(session, PAGE_URL)
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"MS feed: WARN page {status} ({PAGE_URL})")

        urls = self._pdf_urls(resp.text)
        if not urls:
            raise RuntimeError("MS feed: no quarterly PDF links found")

        pdf_dir = self.paths.root / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(urls):
            local = pdf_dir / url.split("/")[-1]
            # BLN cadence: the five first-listed files fresh every run
            # (the current quarter is revised in place), the rest cached.
            if i > 4 and local.exists() and not force:
                continue
            time.sleep(1)  # politeness: max 1 request/second/host
            resp = self._request(session, url)
            if resp is None or resp.status_code != 200:
                if local.exists():
                    log.warning(f"[MS] keeping cached copy of {local.name}")
                    continue
                status = (
                    "unreachable" if resp is None else f"HTTP {resp.status_code}"
                )
                raise RuntimeError(f"MS feed: {status} for {url}")
            if not resp.content.startswith(b"%PDF"):
                log.warning(f"[MS] non-PDF response for {url} — skipped")
                continue
            local.write_bytes(resp.content)

        rows: list = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf")):
            rows.extend(_parse_pdf(pdf_path))
        if not rows:
            raise RuntimeError("MS feed: PDFs parsed to zero rows")

        # Overlapping quarters can repeat a notice verbatim; keep one copy.
        seen, unique = set(), []
        for row in rows:
            key = tuple(_collapse(row.get(c)) for c in RAW_COLUMNS[:-1])
            if key not in seen:
                seen.add(key)
                unique.append(row)

        with open(self.paths.raw, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=RAW_COLUMNS, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(unique)
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV -> unified-schema rows (BLN field crosswalk)."""
        raw = pd.read_csv(Path(raw_path), dtype=str, keep_default_na=False)
        missing = {"company", "date_notice", "date_effective"} - set(raw.columns)
        if missing:
            raise ValueError(f"MS feed: expected columns missing: {missing}")

        rows = []
        for _, r in raw.iterrows():
            company = _collapse(r.get("company", ""))
            if not company or company.lower() in _JUNK_COMPANIES:
                continue  # blank / repeated-header / junk row
            layoff_type = _collapse(r.get("action_type", ""))
            affected = _collapse(r.get("affected", ""))
            if not affected:
                m = _TYPE_COUNT_RE.match(layoff_type)
                if m:  # count merged into the Type of Action cell
                    layoff_type, affected = m.group(1), m.group(2)
            if affected in JOBS_CORRECTIONS:
                emp = JOBS_CORRECTIONS[affected]
            else:
                emp = warn_monitor._safe_int(affected)
            rows.append(
                {
                    "company": company,
                    "notice_date": _clean_date(r.get("date_notice", "")),
                    "effective_date": _clean_date(r.get("date_effective", "")),
                    "employees": emp if emp is not None else 0,
                    "layoff_type": layoff_type,
                    "county": _collapse(r.get("county", "")),
                    "city": _collapse(r.get("city", "")),
                    "industry": _collapse(r.get("naics", "")),
                }
            )

        out = pd.DataFrame(
            rows,
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
        # pandas coerces None -> NaN on construction; restore real None so
        # missing dates serialize as JSON null, not the string "nan".
        return out.astype(object).where(pd.notna(out), None)
