"""
warn_sources.sc
---------------
South Carolina — WARN notices published by the SC Department of Employment
and Workforce as one PDF per year, linked from the SC Works "Layoff
Notification Reports" page (2013-present).

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/sc.py) — ported, never imported: every ``<a href*=pdf>``
whose link text starts with a 4-digit year is one annual report (first
link per year wins — the newest snapshot of a year is listed first), each
PDF's tables are extracted with pdfplumber, and BLN's per-year cache rule
is kept: only the current and previous year's PDFs are re-fetched once
archived (older reports are static). SSL now verifies cleanly, so BLN's
``verify=False`` is dropped. The live page sits at BLN's historical
"risk-closing" path — the site nav's "at-risk-of-closing" variant 404s.

Two report generations exist and both are honored:

* legacy (2013-2021): a ragged multi-row-header table (Company, Location,
  Projected Closure/Layoff Date, Projected Positions Affected, Closure or
  Layoff, NAICS Code). Parsed with BLN's exact regex cell-pluck: rows
  with <4 non-empty cells are junk; a 5-6-digit cell is the NAICS, a
  ``m/d/yy``-shaped cell the date (BLN's pattern tolerates the feed's
  "12/31//2015" double slash), a 1-4-digit cell the jobs count; <2
  matches is junk; cells 0 and 1 are company and location. BLN's
  ``_clean_cell`` (strip + drop newlines without a space) is kept
  verbatim, so wrapped names come out exactly as in BLN's dataset
  ("AREVA Federal ServicesLLC").
* modern (2022+): a clean 7-column table (Company, County, Notice Date,
  Layoff/Closure Date, Impacted, Layoff/Closure, Address) mapped by its
  own header row. Pages whose header is not the data header (the
  per-county summary table and its continuation fragments) are skipped,
  as is the "Total WARN: ..." footer row.

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/sc.py) for everything it maps:

    company  -> company      (required)
    location -> city         (legacy only; the state's Location column
                              holds municipalities)
    date     -> notice_date  (legacy; the state's only date column.
                              BLN's crosswalk is honored verbatim even
                              though the column header reads "Projected
                              Closure/Layoff Date" — the intel sheet's
                              "notice_date only" for SC. Nothing is ever
                              copied into effective_date.)
    jobs     -> employees    (0 when the state leaves it blank or
                              non-numeric: "TBD", "130+28")

The modern columns BLN's 2021-era scraper predates are mapped by the
state's own labels: Notice Date -> notice_date, Layoff/Closure Date ->
effective_date, Impacted -> employees, Layoff/Closure -> layoff_type,
County -> county, Address -> address. The legacy NAICS code -> industry
(BLN scrapes it but its 10-field schema has nowhere to put it) and the
legacy Closure or Layoff cell -> layoff_type (kept as published: Layoff /
Closure / Closing / Intent to Sell).

Date rules, per BLN's ``date_corrections`` dict: a "start - end" range
collapses to its start date (every range entry in BLN's dict does exactly
this; the generic rule reproduces them all and covers future ranges
without hand-maintenance), and the feed's two literal typos are vendored
("4/8/20/20", "12/31//2015"). Anything else unparseable — the legacy
month-only "2/2014" / "June 2018" cells, the modern bare-year "2025"
effective date — becomes None, never a fabricated date.

Known feed quirk: long County values ("Statewide - Multiple Counties")
wrap in the narrow column and pdfplumber interleaves the overflow with
the Notice Date cell character-by-character ("ltiple 1C/8o/u2n0t2ie6s" =
"ltiple Counties" + "1/8/2026"). Deterministic repair: the date is the
cell's digit/slash subsequence; its letters belong to the county, which
is then canonicalised by subsequence-matching against "Statewide -
Multiple Counties" and the 46 real county names (fixes the two 2023 rows
where the Company column overflowed into County as well; those companies
stay truncated exactly as extracted rather than guessed at).

The one notice listed in two annual reports (Bank of America, 1/31/2015,
in both the 2014 and 2015 PDFs) is deduplicated on full record identity.
"""

import csv
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

PAGE_URL = (
    "https://scworks.org/employer/employer-programs/"
    "risk-closing/layoff-notification-reports"
)

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

# Consolidated raw-CSV columns: the union of both report generations'
# cells, kept as extracted (all cleaning happens in parse()).
RAW_HEADERS = [
    "era", "company", "county", "location", "notice_date",
    "layoff_closure_date", "date", "jobs", "layoff_closure", "naics",
    "address", "source",
]

# Vendored verbatim from BLN warn-scraper sc.py: the legacy-table pluck
# patterns (the date pattern's [/]{1,2} tolerates "12/31//2015").
NAICS_RE = re.compile(r"^[0-9]{5,6}$")
DATE_RE = re.compile(r"^[0-9]{1,2}/[0-9]{1,2}[/]{1,2}[0-9]{2}")
JOBS_RE = re.compile(r"^[0-9]{1,4}$")

# m/d/Y (4-digit year first — "8/19/24 - 9/20/24" style 2-digit falls
# through to %m/%d/%y).
DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")

# A "start - end" range collapses to its start (BLN date_corrections
# convention: every range entry in their dict maps to the range start).
RANGE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/+\d{2,4})\s*-\s*\d")

# Vendored from BLN warn-transformer transformers/sc.py date_corrections:
# the two literal feed typos (the dict's many range entries are covered
# by RANGE_RE above).
DATE_CORRECTIONS = {
    "4/8/20/20": (2020, 4, 8),
    "12/31//2015": (2015, 12, 31),
}

# Modern data-table header -> raw CSV column.
MODERN_COLS = {
    "company": "company",
    "county": "county",
    "notice date": "notice_date",
    "layoff/closure date": "layoff_closure_date",
    "impacted": "jobs",
    "layoff/closure": "layoff_closure",
    "address": "address",
}

# Legacy "Closure or Layoff" wordings, kept as published.
LEGACY_TYPES = {"layoff", "closure", "closing", "intent to sell"}

STATEWIDE = "Statewide - Multiple Counties"

# South Carolina's 46 counties, for de-interleaving mangled County cells.
COUNTIES = (
    "Abbeville", "Aiken", "Allendale", "Anderson", "Bamberg", "Barnwell",
    "Beaufort", "Berkeley", "Calhoun", "Charleston", "Cherokee",
    "Chester", "Chesterfield", "Clarendon", "Colleton", "Darlington",
    "Dillon", "Dorchester", "Edgefield", "Fairfield", "Florence",
    "Georgetown", "Greenville", "Greenwood", "Hampton", "Horry",
    "Jasper", "Kershaw", "Lancaster", "Laurens", "Lee", "Lexington",
    "Marion", "Marlboro", "McCormick", "Newberry", "Oconee",
    "Orangeburg", "Pickens", "Richland", "Saluda", "Spartanburg",
    "Sumter", "Union", "Williamsburg", "York",
)

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

# BLN warn-transformer guard-rail (maximum_jobs).
MAX_JOBS = 10000

# The 2013-2026 crawl yields ~600 rows; a collapse below this means the
# page or PDF layout changed, not mass rescissions.
MIN_TOTAL_ROWS = 300


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val or "").replace("\xa0", " ")).strip()


def _clean_cell(cell):
    """Vendored from BLN warn-scraper sc.py: strip + drop newlines."""
    if cell is None:
        return None
    return cell.strip().replace("\n", "")


def _is_subseq(needle: str, hay: str) -> bool:
    """True when ``needle``'s characters appear in ``hay`` in order."""
    it = iter(hay)
    return all(ch in it for ch in needle)


def _year_pdf_links(html: str) -> dict:
    """Hub page -> {year: pdf_href}, vendored from BLN warn-scraper sc.py.

    Every ``<a>`` with "pdf" in its href whose text starts with a 4-digit
    year is an annual report; the first link per year (the newest
    snapshot) wins. Years are sanity-bounded so a stray numeric link can
    never register as a report.
    """
    soup = BeautifulSoup(html, "html.parser")
    pdf_dict: dict = {}
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href or "pdf" not in href:
            continue
        try:
            a_year = int(a.text.strip()[:4].strip())
        except ValueError:
            continue
        if not (1988 <= a_year <= date.today().year + 1):
            continue
        if a_year not in pdf_dict:
            pdf_dict[a_year] = href
    return pdf_dict


def _extract_modern(table: list, idx: dict, source: str) -> list:
    """Modern-format data rows -> raw dicts (cells kept as extracted)."""
    rows = []
    for row in table[1:]:
        rec = {
            key: _squish(row[i]) if i < len(row) else ""
            for key, i in idx.items()
        }
        company = rec.get("company", "")
        if not company or company.lower().startswith("total warn"):
            continue
        rec["era"] = "modern"
        rec["source"] = source
        rows.append(rec)
    return rows


def _extract_legacy(table: list, source: str) -> list:
    """Legacy-format rows via BLN warn-scraper sc.py's exact cell-pluck."""
    rows = []
    for row in table:
        values = [v for v in row if v]
        if len(values) < 4:
            continue
        cell_list = [_clean_cell(c) for c in row if _clean_cell(c)]
        d: dict = {}
        for cell in cell_list:
            if NAICS_RE.search(cell):
                d["naics"] = cell
            elif DATE_RE.search(cell):
                d["date"] = cell
            elif JOBS_RE.search(cell):
                d["jobs"] = cell
            elif cell.lower() in LEGACY_TYPES:
                d["layoff_closure"] = cell
        # Fewer than two pattern matches = junk (headers, titles).
        if len([k for k in d if k != "layoff_closure"]) < 2:
            continue
        d["company"] = cell_list[0]
        d["location"] = cell_list[1]
        d["era"] = "legacy"
        d["source"] = source
        rows.append(d)
    return rows


def _parse_pdf(pdf_path, year: int) -> list:
    """One annual report PDF -> raw row dicts (both generations)."""
    source = f"sc/{year}.pdf"
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            header = [_squish(c).lower() for c in table[0]]
            if header and header[0] == "company" and "notice date" in header:
                idx = {
                    MODERN_COLS[h]: i
                    for i, h in enumerate(header)
                    if h in MODERN_COLS
                }
                rows.extend(_extract_modern(table, idx, source))
            elif year < 2022:
                rows.extend(_extract_legacy(table, source))
            # else: modern-era summary page / footer fragment — skip.
    return rows


def _write_raw_csv(rows: list, path: Path) -> None:
    """Consolidated raw rows -> CSV at ``path``."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in RAW_HEADERS})


def _fix_county(county_cell: str, notice_cell: str) -> tuple:
    """(County cell, Notice Date cell) -> (county, date text), repaired.

    When the county overflows into the Notice Date column (see module
    docstring), the date is the cell's digit/slash subsequence and the
    letters are county overflow; the combined county text is then
    canonicalised by subsequence match against the statewide label and
    the real county names.
    """
    county = _squish(county_cell)
    date_text = notice_cell
    if not notice_cell or not re.search(r"[A-Za-z]", notice_cell):
        return county, date_text
    date_text = re.sub(r"[^0-9/]", "", notice_cell)
    combined = county_cell + re.sub(r"[0-9/]", "", notice_cell)
    alpha = re.sub(r"[^a-z]", "", combined.lower())
    if _is_subseq("statewidemultiplecounties", alpha):
        return STATEWIDE, date_text
    matches = [c for c in COUNTIES if _is_subseq(c, combined)]
    if len(matches) == 1:
        return matches[0], date_text
    return _squish(combined), date_text


def _clean_date(val):
    """SC date cell -> strict ISO YYYY-MM-DD or None (never junk).

    BLN's two literal typo corrections first, then range-start collapse,
    then m/d/Y | m/d/y, inside a year sanity window. Month-only cells
    ("2/2014", "June 2018") and bare years ("2025") stay None — a day is
    never fabricated.
    """
    text = _squish(val)
    if not text:
        return None
    if text in DATE_CORRECTIONS:
        return "%04d-%02d-%02d" % DATE_CORRECTIONS[text]
    m = RANGE_RE.match(text)
    if m:
        text = m.group(1)
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year < 100:
            continue  # "8/19/24": %Y read a literal year 24 — retry %y
        if MIN_YEAR <= parsed.year <= _max_year():
            return parsed.strftime("%Y-%m-%d")
        return None
    return None


def _clean_jobs(val) -> int:
    """Jobs cell -> int; 0 when blank/non-numeric/implausible."""
    n = warn_monitor._safe_int(val)
    if n is None or n < 0 or n > MAX_JOBS:
        return 0
    return n


class SouthCarolinaDEW(Source):
    code = "sc"
    name = "South Carolina"
    agency = "South Carolina Department of Employment and Workforce"
    source_url = PAGE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url: str, first: bool):
        """One request politely: 1 req/s, 60 s timeout, 3 attempts."""
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[SC] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[SC] {url} -> HTTP {resp.status_code}")
                continue
            return resp
        raise RuntimeError(f"SC feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Crawl hub + annual PDFs into one consolidated CSV.

        PDFs are archived under ``<state>/archive/<year>.pdf``; per BLN's
        cache rule only the current and previous year are re-downloaded
        once archived (``--force`` refreshes everything). Archived years
        that fall off the page keep contributing rows, so a hub-page
        prune can never look like mass withdrawals. Any failing download
        aborts the whole fetch — a partial crawl must never be written.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        hub = self._get(session, PAGE_URL, first=True)
        pdf_dict = _year_pdf_links(hub.text)
        if not pdf_dict:
            raise RuntimeError(
                "SC feed: no annual-report PDF links found — "
                "hub layout may have changed"
            )

        self.paths.ensure()
        archive = self.paths.root / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        archived_years = {
            int(p.stem) for p in archive.glob("[0-9][0-9][0-9][0-9].pdf")
        }

        current_year = date.today().year
        rows = []
        for year in sorted(set(pdf_dict) | archived_years):
            pdf_path = archive / f"{year}.pdf"
            href = pdf_dict.get(year)
            if href and (
                force or year >= current_year - 1 or not pdf_path.exists()
            ):
                resp = self._get(
                    session, urljoin("https://scworks.org/", href), first=False
                )
                pdf_path.write_bytes(resp.content)
            year_rows = _parse_pdf(pdf_path, year)
            log.info(f"[SC] {year}: {len(year_rows)} rows")
            rows.extend(year_rows)

        if len(rows) < MIN_TOTAL_ROWS:
            raise RuntimeError(
                f"SC feed: only {len(rows)} rows across all years — "
                "PDF layout may have changed"
            )

        _write_raw_csv(rows, self.paths.raw)

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
        """Consolidated CSV -> unified-schema rows (BLN crosswalk)."""
        with open(raw_path, newline="", encoding="utf-8") as fh:
            raw_rows = list(csv.DictReader(fh))

        records, seen = [], set()
        for row in raw_rows:
            company = _squish(row.get("company"))
            if not company:
                continue
            if row.get("era") == "modern":
                county, notice_text = _fix_county(
                    row.get("county", ""), row.get("notice_date", "")
                )
                rec = {
                    "company": company,
                    "notice_date": _clean_date(notice_text),
                    "effective_date": _clean_date(
                        row.get("layoff_closure_date")
                    ),
                    "employees": _clean_jobs(row.get("jobs")),
                    "layoff_type": _squish(row.get("layoff_closure")),
                    "county": county,
                    "city": "",
                    "address": _squish(row.get("address")),
                    "industry": "",
                }
            else:
                # Legacy: BLN warn-transformer crosswalk — date is the
                # notice_date, location the city; effective_date is not
                # published (never synthesized from the notice date).
                rec = {
                    "company": company,
                    "notice_date": _clean_date(row.get("date")),
                    "effective_date": None,
                    "employees": _clean_jobs(row.get("jobs")),
                    "layoff_type": _squish(row.get("layoff_closure")),
                    "county": "",
                    "city": _squish(row.get("location")),
                    "address": "",
                    "industry": _squish(row.get("naics")),
                }
            key = tuple(rec.values())
            if key in seen:
                continue  # e.g. Bank of America 1/31/2015 in 2014+2015
            seen.add(key)
            records.append(rec)

        out = pd.DataFrame(
            records,
            columns=[
                "company", "notice_date", "effective_date", "employees",
                "layoff_type", "county", "city", "address", "industry",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
