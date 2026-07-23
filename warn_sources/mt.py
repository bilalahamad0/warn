"""
warn_sources.mt
---------------
Montana — Department of Labor & Industry (Workforce Services Division).

The state publishes one cumulative XLSX (2015-present, ~50 rows) linked from
its WARN notice page. The filename changes on every refresh (e.g.
``warn-notices-updated-march-2026.xlsx``), so ``fetch`` re-discovers the link
each run, then delegates the conditional download (ETag/Last-Modified cache)
to ``warn_monitor.download_xlsx``.

Live sheet layout (2026): ``Year | Date of Notice | Name of Company | County |
Industry | Date of Impact | Number of Employees Affected``. Older snapshots
lacked the Year/Industry columns, so the parser maps columns by header text,
never by position. Rows are grouped by year with blank spacer rows between
groups; the Year column is sparsely populated and dropped.

Field crosswalk and manual data corrections vendored from Big Local News'
warn-scraper / warn-transformer (Apache-2.0) ``mt.py`` modules.
"""

import dataclasses
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

PAGE_URL = "https://wsd.dli.mt.gov/wioa/related-links/warn-notice-page"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Manual corrections for typo'd / multi-valued cells in the state file,
# vendored from BLN warn-transformer (transformers/mt.py, Apache-2.0).
# Keys are the exact raw cell strings as published.
DATE_CORRECTIONS = {
    "3/1620 to 4/30/20": datetime(2020, 3, 16),
    "Sept-April": None,
    "5/22/2025, 5/29/2025, 6/5/2025": datetime(2025, 5, 22),
    "12/31/202": datetime(2025, 12, 31),
    "11/17/225": datetime(2025, 11, 17),
}
JOBS_CORRECTIONS = {
    "Not noted": None,
    "MT # unknown": None,
    "up to 300": 1,
    "Over 100": 100,
}

# (header-substring, unified column) — matched case-insensitively.
_HEADER_MAP = [
    ("name of company", "company"),
    ("date of notice", "notice_date"),
    ("date of impact", "effective_date"),
    ("employees", "employees"),
    ("county", "county"),
    ("industry", "industry"),
]


def _squash(val) -> str:
    """Collapse internal whitespace and strip a cell value."""
    return " ".join(str(val).split()) if val is not None else ""


class MontanaDLI(Source):
    code = "mt"
    name = "Montana"
    agency = "Montana Department of Labor & Industry"
    source_url = PAGE_URL
    cadence = "monthly"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        # openpyxl refuses files without a recognized suffix, so the raw
        # download keeps an .xlsx extension (standard layout otherwise).
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.xlsx")

    # -- fetch --------------------------------------------------------------

    def _discover_xlsx_url(self) -> str:
        """Find the current XLSX href on the WARN notice page."""
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)  # polite backoff between retries
            try:
                resp = requests.get(
                    PAGE_URL, headers={"User-Agent": BROWSER_UA}, timeout=60
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                last_err = e
        else:
            raise RuntimeError(f"MT WARN page unreachable: {last_err}")

        soup = BeautifulSoup(resp.text, "html.parser")
        # BLN scrapers/mt.py scopes the search to the #boardPage container;
        # fall back to the whole page if the CMS renames the container.
        scope = soup.find(id="boardPage") or soup
        for link in scope.find_all("a"):
            href = link.get("href", "")
            if href.lower().endswith(".xlsx"):
                return urljoin(PAGE_URL, href)
        raise RuntimeError("No XLSX link found on the MT WARN notice page")

    def fetch(self, force: bool = False) -> tuple:
        excel_url = self._discover_xlsx_url()
        time.sleep(1)  # max 1 request/second against the same host
        if not self.paths.raw.exists():
            force = True  # cached ETag but no local file: a 304 would strand us
        return warn_monitor.download_xlsx(
            force=force,
            url=excel_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _locate_header(rows) -> tuple:
        """Return (header_row_index, {unified_field: column_index})."""
        for i, row in enumerate(rows):
            texts = [str(c).strip().lower() for c in row if c is not None]
            if not any("name of company" in t for t in texts):
                continue
            col_idx = {}
            for j, cell in enumerate(row):
                if cell is None:
                    continue
                text = str(cell).strip().lower()
                for needle, field in _HEADER_MAP:
                    if needle in text and field not in col_idx:
                        col_idx[field] = j
                        break
            return i, col_idx
        raise ValueError("MT XLSX: header row not found")

    @staticmethod
    def _clean_date(val):
        """Cell -> ISO YYYY-MM-DD or None, honoring the corrections table."""
        if isinstance(val, str):
            key = val.strip()
            if key in DATE_CORRECTIONS:
                fixed = DATE_CORRECTIONS[key]
                return fixed.strftime("%Y-%m-%d") if fixed else None
        iso = warn_monitor._safe_date(val)
        if iso is not None and not _ISO_RE.match(iso):
            log.warning(f"[MT] unparseable date {val!r} -> None")
            return None
        return iso

    @staticmethod
    def _clean_jobs(val) -> int:
        """Cell -> int employee count; 0 when the state published none."""
        if isinstance(val, str) and val.strip() in JOBS_CORRECTIONS:
            val = JOBS_CORRECTIONS[val.strip()]
        n = warn_monitor._safe_int(val)
        return n if n is not None else 0

    def _parse_row(self, row, col_idx):
        def cell(field):
            j = col_idx.get(field)
            return row[j] if j is not None and j < len(row) else None

        company = _squash(cell("company"))
        if not company or company.lower() == "name of company":
            return None  # blank spacer row or repeated header
        rec = {
            "company": company,
            "notice_date": self._clean_date(cell("notice_date")),
            "effective_date": self._clean_date(cell("effective_date")),
            "employees": self._clean_jobs(cell("employees")),
        }
        if "county" in col_idx:
            rec["county"] = _squash(cell("county"))
        if "industry" in col_idx:
            rec["industry"] = _squash(cell("industry"))
        return rec

    def parse(self, raw_path) -> pd.DataFrame:
        wb = load_workbook(
            filename=str(raw_path), read_only=True, data_only=True
        )
        try:
            ws = wb.worksheets[0]
            rows = [[cell.value for cell in row] for row in ws.rows]
        finally:
            wb.close()

        header_idx, col_idx = self._locate_header(rows)
        records = []
        for row in rows[header_idx + 1:]:
            rec = self._parse_row(row, col_idx)
            if rec is not None:
                records.append(rec)
        log.info(f"[MT] parsed {len(records)} records")

        df = pd.DataFrame(records)
        # The DataFrame constructor coerces None -> NaN in mixed columns;
        # restore true None so dates are strictly ISO-string-or-None.
        for col in ("notice_date", "effective_date"):
            if col in df.columns:
                df[col] = df[col].astype(object).where(df[col].notna(), None)
        return df
