"""
warn_sources.ma
---------------
Massachusetts — WARN notices published by the Executive Office of Labor
and Workforce Development on mass.gov, updated weekly (Fridays).

Feed layout (probed 2026-07-22): the info-details landing page carries a
"WARN Tracker" current-week table whose data lives behind a "Download
table data as CSV" link (``/files/csv/.../WARN Report FY 20NN week
ending MM-DD-YYYY.csv``, covering the fiscal year to date), plus
"Previous WARN reports" fiscal-year XLSX archives
(``/doc/fyNN-warn-report*/download``, FY22-present). Massachusetts
fiscal years start July 1, so the backfill reaches July 2021.

Akamai note: mass.gov refuses ``python-requests`` outright — the edge
wants an HTTP/2 handshake plus full Chrome-fidelity headers (HTTP/1.1
is 403'd even with the full header set, and HTTP/2 with a bare browser
UA is 403'd too). ``requests`` cannot speak HTTP/2, so ``fetch`` shells
out to the system ``curl --http2`` with the full header set; politeness
is 1 request/second/host, 60 s timeout, 3 attempts with backoff.

No BLN warn-scraper/-transformer exists for MA (one of the five gap
states), so the crosswalk below is this module's own, built from the
real FY22-FY27 files:

    RECEIVED / Date Received     -> notice_date
    EMPLOYER / Company Name      -> company (required; kept exactly as
                                    published, incl. "*Updated*" markers)
    CITY/TOWN / City / City/State-> city (a trailing ", MA" is stripped)
    REGION, or the per-region
      sheet name in FY22/FY23    -> county (MassHire workforce region —
                                    the only sub-state geography MA
                                    publishes; the same promotion the CO
                                    module applies to workforce areas)
    DATE(S) OF LAYOFFS /
      Layoff Date                -> effective_date (the FIRST published
                                    date — the start of the layoff
                                    range/list; month-only prose like
                                    "Early July 2024 to March 2025"
                                    contains no full date and maps to
                                    None, never a synthesized day)
    # EMPLOYEES IMPACTED /
      # Affected                 -> employees

MA publishes no layoff type, street address, or industry — those
unified columns are never fabricated.

Two workbook generations are honored. FY22/FY23: one sheet per MassHire
region (title row, then ``Date Received | Company Name | City | Layoff
Date | # Affected``; the FY22 "Central" sheet omits its "Date Received"
header cell, so an unmapped received column falls back to column 0, and
the "Remote-National" sheet name is normalized to the state's own
"Remote/National" title wording). FY24+: a single sheet with ``RECEIVED
| EMPLOYER | CITY/TOWN | REGION | DATE(S) OF LAYOFFS | # EMPLOYEES
IMPACTED`` (the FY25 workbook also ships an empty header-only FY26
sheet, which yields zero rows naturally). The weekly tracker CSV uses
the FY24+ header and is cp1252-encoded, not UTF-8.

Quirks honored (all observed in real cells):

- "*Updated*" amendment rows may carry a blank RECEIVED cell ->
  notice_date None.
- Received cells can hold a range ("07/07/2021 - (08/30/2021)") — the
  first date wins.
- Layoff-date cells hold ranges/lists ("10/3/2022 - 2/4/2023",
  "1/28/24; 2/29/24; 3/29/24; 4/27/24", "8/15/26 & 11/30/26"), prose
  ("Beginning 2/5/24 and ending 4/12/2024", "On or about 5/28/2024"),
  and typo years ("3/31/12025", "5/31/1204", "11/31/2021",
  "10/21-2024") — first parseable date wins, known typos are fixed via
  DATE_CORRECTIONS, no-full-date prose ("Spring 2025 - Spring 2026",
  "T/B/D") maps to None, and one cell whose only full date is its
  completion date ("To begin early 7/2023 ... no later than 3/31/2024")
  is pinned to None so first-date-wins cannot grab the end date.
- Employee cells mix ints with free text ("181 (total) 91 in MA",
  "120 (25 reside in MA)", "207 total locations", "70 - 80", "Up to
  138", "t/b/d") — the in-MA figure is preferred over a nationwide
  total, ranges keep the lower bound, and no-count cells map to 0.
- Per-location rows may repeat a shared total ("175 Total /all
  locations" on both Back2BU rows) — kept exactly as published.
- Exact-duplicate raw rows across the weekly tracker CSV and a FY
  workbook (possible at fiscal-year rollover) are dropped once;
  distinct filings always differ in at least one field.
"""

import csv
import dataclasses
import io
import logging
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin

import pandas as pd
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

BASE_URL = "https://www.mass.gov"
# Canonical URL (301s to the "...-warn-layoff-and-closure-updates" slug;
# curl -L follows it).
PAGE_URL = (
    "https://www.mass.gov/info-details/"
    "worker-adjustment-and-retraining-notification-act-warn"
)

# Full Chrome-fidelity header set. Akamai on mass.gov requires HTTP/2
# plus (approximately) this set — a bare browser UA is refused.
CURL_HEADERS = [
    (
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36",
    ),
    (
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7",
    ),
    ("Accept-Language", "en-US,en;q=0.9"),
    (
        "sec-ch-ua",
        '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    ),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", '"macOS"'),
    ("Sec-Fetch-Dest", "document"),
    ("Sec-Fetch-Mode", "navigate"),
    ("Sec-Fetch-Site", "none"),
    ("Sec-Fetch-User", "?1"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Cache-Control", "max-age=0"),
]

# Consolidated raw-CSV column names (native MA fields, normalized).
RAW_FIELDS = ["received", "employer", "city", "region", "layoff_dates",
              "employees"]

# FY archive links look like /doc/fy26-warn-report-0/download.
_FY_DOC_RE = re.compile(r"/doc/fy(\d+)-warn-report[^/]*/download$")
_FY_NUM_RE = re.compile(r"/doc/fy(\d+)-")

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TOKEN_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]
MIN_YEAR = 1988

# The in-MA figure inside mixed counts: "91 in MA", "25 reside in MA",
# "1 resides in MA" — preferred over the nationwide total.
_IN_MA_RE = re.compile(r"(\d[\d,]*)\s*(?:resides?\s+)?in\s+MA\b", re.I)
_LEADING_INT_RE = re.compile(r"^(\d[\d,]*)\b")
_CITY_MA_RE = re.compile(r"\s*,\s*MA$")

# Known-mangled date cells (squished, lowercased) -> intended ISO date
# or None where the state published no usable date. All are real cells
# from the FY22-FY27 files.
DATE_CORRECTIONS = {
    "3/31/12025": "2025-03-31",           # extra digit; FY25 row
    "5/31/1204": "2024-05-31",            # mangled year; FY24 row
    "11/31/2021": "2021-11-30",           # November has 30 days
    "10/21-2024 - 12/31/2024": "2024-10-21",  # dash for slash
    (
        "postpone the layoff of remaining 11 employees until march 1, "
        "2024 (or within a 14-day period thereafter)"
    ): "2024-03-01",
    # The only full date here is the COMPLETION date; the start is
    # month-only prose ("early 7/2023"), so first-date-wins must not
    # grab the end date -> None.
    (
        "to begin early 7/2023 and will be completed no later than "
        "3/31/2024"
    ): None,
    "t/b/d": None,
}

# Free-text employee cells (squished, lowercased) the generic rules
# would misread or miss -> intended count; None = no count published
# (parse maps that to 0). All are real cells from the FY22-FY27 files.
JOBS_CORRECTIONS = {
    (
        "the current company headcount minus employees who have less "
        "than 6 months working at the company = 72"
    ): 72,
    "min. 1 - max. 10": 1,
    "up to 138": 138,
    "t/b/c": None,
    "t/b/d": None,
    "work in progress": None,
}

# FY22/FY23 sheet names double as the region; normalize the one name
# that differs from the state's own in-sheet title wording.
SHEET_REGIONS = {"Remote-National": "Remote/National"}

# (header substring, raw field) — matched case-insensitively against
# the header row of every sheet/CSV generation.
_HEADER_NEEDLES = [
    ("received", "received"),
    ("employer", "employer"),
    ("company", "employer"),
    ("city", "city"),
    ("region", "region"),
    ("layoff", "layoff_dates"),
    ("affected", "employees"),
    ("impacted", "employees"),
]

_OUT_COLUMNS = ["company", "notice_date", "effective_date", "employees",
                "county", "city"]


def _max_year():
    return date.today().year + 5


def _squish(val) -> str:
    """Value -> clean single-spaced string."""
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _cell_str(val) -> str:
    """One sheet/CSV cell -> consolidated-CSV string.

    Datetime cells become ISO dates at consolidation time so the raw
    CSV stays readable; everything else is squished text.
    """
    if val is None:
        return ""
    if not isinstance(val, str) and pd.isna(val):
        return ""  # NaN / NaT
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return _squish(val)


def _header_columns(row) -> Optional[dict]:
    """If ``row`` is a header row, return {raw_field: column_index}."""
    texts = [_squish(c).lower() for c in row]
    if not any("employer" in t or "company name" in t for t in texts):
        return None
    cols = {}
    for j, text in enumerate(texts):
        for needle, field in _HEADER_NEEDLES:
            if needle in text and field not in cols:
                cols[field] = j
                break
    # The FY22 "Central" sheet omits its "Date Received" header cell;
    # the received dates still live in the first column.
    cols.setdefault("received", 0)
    return cols


def _grid_rows(grid, default_region: str = "") -> list:
    """One cell grid -> raw row dicts keyed by RAW_FIELDS.

    Shared by both workbook generations and the weekly tracker CSV:
    rows before the header are title junk, repeated headers are
    re-mapped, and employer-less rows (spacers) are dropped.
    ``default_region`` (the FY22/FY23 sheet name) applies only when the
    header has no region column.
    """
    cols = None
    out = []
    for row in grid:
        header = _header_columns(row)
        if header is not None:
            cols = header
            continue
        if cols is None:
            continue  # pre-header title rows

        def cell(field):
            j = cols.get(field)
            return _cell_str(row[j]) if j is not None and j < len(row) else ""

        employer = cell("employer")
        if not employer:
            continue  # blank spacer rows
        region = cell("region") if "region" in cols else default_region
        out.append(
            {
                "received": cell("received"),
                "employer": employer,
                "city": cell("city"),
                "region": _squish(region),
                "layoff_dates": cell("layoff_dates"),
                "employees": cell("employees"),
            }
        )
    return out


def _workbook_rows(content: bytes) -> list:
    """One FY XLSX (bytes) -> raw rows from every sheet."""
    rows = []
    xl = pd.ExcelFile(io.BytesIO(content))
    for sheet in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=sheet, header=None)
        region = SHEET_REGIONS.get(sheet, sheet)
        rows.extend(_grid_rows(df.values.tolist(), default_region=region))
    return rows


def _decode_csv(content: bytes) -> str:
    """Weekly tracker CSV bytes -> text (the state ships cp1252)."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _csv_rows(content: bytes) -> list:
    """The weekly tracker CSV (bytes) -> raw rows."""
    grid = list(csv.reader(io.StringIO(_decode_csv(content))))
    return _grid_rows(grid)


def _find_data_links(html: str) -> tuple:
    """Landing page -> (FY XLSX urls oldest-first, tracker CSV urls)."""
    soup = BeautifulSoup(html, "html.parser")
    xlsx, csvs = [], []
    for atag in soup.find_all("a"):
        href = (atag.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        path = unquote(url.split("?")[0]).lower()
        if _FY_DOC_RE.search(path):
            if url not in xlsx:
                xlsx.append(url)
        elif "/files/csv/" in path and "warn" in path:
            if url not in csvs:
                csvs.append(url)

    def fy_num(url):
        m = _FY_NUM_RE.search(unquote(url).lower())
        return int(m.group(1)) if m else 0

    return sorted(xlsx, key=fy_num), csvs


def _clean_date(val) -> Optional[str]:
    """Cell -> ISO YYYY-MM-DD or None; the FIRST published date wins."""
    text = _squish(val)
    if not text:
        return None
    if text.lower() in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[text.lower()]
    if ISO_RE.match(text):  # consolidation-time datetime stamp
        return text if MIN_YEAR <= int(text[:4]) <= _max_year() else None
    match = _DATE_TOKEN_RE.search(text)
    if match:
        for fmt in DATE_FORMATS:
            try:
                parsed = datetime.strptime(match.group(1), fmt)
            except ValueError:
                continue
            if MIN_YEAR <= parsed.year <= _max_year():
                return parsed.strftime("%Y-%m-%d")
    log.warning(f"[MA] no usable date in {text!r} -> None")
    return None


def _clean_jobs(val) -> int:
    """Cell -> int employee count; 0 when the state published none.

    Corrections first, then the in-MA figure ("181 (total) 91 in MA" ->
    91), then the leading integer (covers "207 total locations", and
    ranges like "70 - 80" keep the lower bound).
    """
    text = _squish(val)
    if not text:
        return 0
    if text.lower() in JOBS_CORRECTIONS:
        n = JOBS_CORRECTIONS[text.lower()]
        return n if n is not None else 0
    match = _IN_MA_RE.search(text)
    if match:
        return int(match.group(1).replace(",", ""))
    match = _LEADING_INT_RE.match(text)
    if match:
        return int(match.group(1).replace(",", ""))
    n = warn_monitor._safe_int(text)
    if n is not None:
        return n
    log.warning(f"[MA] no usable employee count in {text!r} -> 0")
    return 0


def _clean_city(val) -> str:
    """City cell -> squished text with a trailing ", MA" stripped."""
    return _CITY_MA_RE.sub("", _squish(val))


class MassachusettsEOLWD(Source):
    code = "ma"
    name = "Massachusetts"
    agency = "Massachusetts Executive Office of Labor and Workforce Development"
    source_url = PAGE_URL
    cadence = "weekly"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        # Keep a .csv suffix on the consolidated raw file for clarity.
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.csv")

    # -- fetch --------------------------------------------------------------

    def _get(self, url: str, first: bool = False) -> bytes:
        """One URL politely via curl: HTTP/2 + Chrome-fidelity headers,
        1 req/s, 60 s timeout, 3 attempts with backoff."""
        if shutil.which("curl") is None:
            raise RuntimeError(
                "MA feed: system curl not found (required — mass.gov's "
                "Akamai edge needs HTTP/2, which requests cannot speak)"
            )
        cmd = ["curl", "-sS", "--fail", "-L", "--http2", "--compressed",
               "--max-time", "60"]
        for key, value in CURL_HEADERS:
            cmd += ["-H", f"{key}: {value}"]
        cmd.append(url)

        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
            last_err = proc.stderr.decode("utf-8", "replace").strip()
            log.warning(f"[MA] curl attempt {attempt + 1} {url}: {last_err}")
        raise RuntimeError(f"MA feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Landing page -> every FY workbook + the weekly tracker CSV,
        consolidated into one raw CSV. Returns (changed, raw_path).

        Any piece failing aborts the whole fetch — a partial crawl must
        never be written, or the diff engine would report the missing
        files' notices as phantom withdrawals.
        """
        html = self._get(PAGE_URL, first=True).decode("utf-8", "replace")
        xlsx_urls, csv_urls = _find_data_links(html)
        if not xlsx_urls:
            raise RuntimeError(
                "MA feed: no fiscal-year XLSX links on the landing page — "
                "layout may have changed"
            )
        if not csv_urls:
            raise RuntimeError(
                "MA feed: no weekly tracker CSV link on the landing page — "
                "layout may have changed"
            )

        rows = []
        for url in xlsx_urls:  # oldest fiscal year first
            fy_rows = _workbook_rows(self._get(url))
            log.info(f"[MA] {url.rsplit('/', 2)[-2]}: {len(fy_rows)} rows")
            rows.extend(fy_rows)
        for url in csv_urls:  # the current-week tracker last (newest)
            week_rows = _csv_rows(self._get(url))
            log.info(f"[MA] weekly tracker: {len(week_rows)} rows")
            rows.extend(week_rows)

        # Exact-duplicate raw rows (weekly tracker vs FY workbook at
        # fiscal-year rollover) are dropped once — distinct filings
        # always differ in at least one field.
        seen, unique = set(), []
        for row in rows:
            key = tuple(row[f] for f in RAW_FIELDS)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        if len(unique) < len(rows):
            log.info(f"[MA] dropped {len(rows) - len(unique)} duplicate rows")
        rows = unique

        # FY22-FY27 alone hold ~400 rows; a collapse below 150 means a
        # layout change (e.g. archives dropped), not mass rescissions.
        if len(rows) < 150:
            raise RuntimeError(
                f"MA feed: only {len(rows)} rows across all files — "
                "page layout may have changed"
            )

        self.paths.ensure()
        with open(self.paths.raw, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=RAW_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

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
        """Consolidated raw CSV -> unified-schema rows (crosswalk above)."""
        with open(raw_path, newline="", encoding="utf-8") as fh:
            raw_rows = list(csv.DictReader(fh))

        records = []
        for row in raw_rows:
            company = _squish(row.get("employer"))
            if not company:
                continue  # company is required
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("received")),
                    "effective_date": _clean_date(row.get("layoff_dates")),
                    "employees": _clean_jobs(row.get("employees")),
                    "county": _squish(row.get("region")),
                    "city": _clean_city(row.get("city")),
                }
            )
        out = pd.DataFrame(records, columns=_OUT_COLUMNS)
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
