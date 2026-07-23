"""
warn_sources.nd
---------------
North Dakota — Job Service North Dakota.

The state publishes one cumulative PDF ("WARN Notices 2015 to present",
~55 rows over 2 pages) at a stable URL that supports ETag/Last-Modified,
so ``fetch`` delegates the conditional download to
``warn_monitor.download_xlsx`` (the helper is format-agnostic).

Table layout: ``Company Name | Location | WARN Dated | Date of
Layoff/Closure | Number Laid Off/Affected | Notes``. The header repeats at
the top of each page and is skipped. Field crosswalk vendored from Big
Local News' warn-scraper / warn-transformer (Apache-2.0) ``nd.py`` modules:

    Company Name             -> company
    Location                 -> city    (BLN "location"; free text such as
                                         "Fargo, ND" or "Nationwide - Remote")
    WARN Dated               -> notice_date
    Date of Layoff/Closure   -> effective_date
    Number Laid Off/Affected -> employees

Quirks honored per the BLN transformer: recent filings jam two dates into
the ``WARN Dated`` cell (e.g. "3/3/2025 5/2/2025") while leaving the
layoff column empty — the corrections table keeps the FIRST date as the
notice date and the effective date stays None (never synthesized from the
second date). Free-text counts ("25+", "approx. 2200 nationwide (14
reported in ND)") map through the jobs corrections. ``Notes`` is free
prose and is dropped.
"""

import dataclasses
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

PDF_URL = (
    "https://www.jobsnd.com/sites/www/files/documents/jsnd-documents/"
    "WARN%20Notices%202015%20to%20present.pdf"
)

# BLN transformer date formats for ND (transformers/nd.py, Apache-2.0).
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]

# Manual corrections for multi-valued / free-text date cells, vendored from
# BLN warn-transformer (transformers/nd.py, Apache-2.0). Keys are the exact
# raw cell strings (whitespace-collapsed) as published.
DATE_CORRECTIONS = {
    "4/25/2025 6/28/2025": datetime(2025, 4, 25),
    "1/15/2026 1/28/2026": datetime(2026, 1, 15),
    "10/21/2025 12/21/2025": datetime(2025, 10, 21),
    "Not stated": None,
    "3/1/2032 - 4/30/2023": datetime(2023, 3, 1),
    "3/3/2025 5/2/2025": datetime(2025, 3, 3),
    "9/23/2024 11/22/2024": datetime(2024, 9, 23),
    "7/21/2025beginning 9/26/2025": datetime(2025, 7, 21),
    "Starts 3/25/2020": datetime(2020, 3, 25),
    "Began 1/10/17": datetime(2017, 1, 10),
    "12/20/2024 2/19/2025": datetime(2024, 12, 20),
    "starts 10/29/2017": datetime(2017, 10, 29),
    "5/28/2024 7/27/2024": datetime(2024, 5, 28),
    "starts 10/11/2017": datetime(2017, 10, 11),
    "starts 8/30/2022": datetime(2022, 8, 30),
}

# Free-text employee counts, vendored from BLN warn-transformer
# (transformers/nd.py, Apache-2.0). Both spellings of the Yellow Corp cell
# have appeared in the wild; keep them all.
JOBS_CORRECTIONS = {
    "approx. 2200 nationwide (14 reported in ND)": 14,
    "approx. 22,00 nationwide (14 reported in ND)": 14,
    "25+": 25,
}

# First m/d/y token in a cell — fallback for future multi-date cells that
# have not made it into the corrections table yet.
_LEADING_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")

# (header text, unified column) — matched case-insensitively by substring.
_HEADER_MAP = [
    ("company name", "company"),
    ("location", "city"),
    ("warn dated", "notice_date"),
    ("layoff/closure", "effective_date"),
    ("laid off/affected", "employees"),
]


def _clean_text(text) -> str:
    """Collapse newlines/whitespace in a PDF cell (BLN scrapers/nd.py)."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


class NorthDakotaJSND(Source):
    code = "nd"
    name = "North Dakota"
    agency = "Job Service North Dakota"
    source_url = PDF_URL
    cadence = "as-filed"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        # Keep a .pdf suffix on the raw download for clarity/tooling.
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.pdf")

    # -- fetch --------------------------------------------------------------

    def fetch(self, force: bool = False) -> tuple:
        if not self.paths.raw.exists():
            force = True  # cached ETag but no local file: a 304 would strand us
        return warn_monitor.download_xlsx(
            force=force,
            url=self.source_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(val):
        """Cell -> ISO YYYY-MM-DD or None, honoring the corrections table."""
        text = _clean_text(val)
        if not text:
            return None
        if text in DATE_CORRECTIONS:
            fixed = DATE_CORRECTIONS[text]
            return fixed.strftime("%Y-%m-%d") if fixed else None
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Future multi-date / prefixed cells: keep the first date token.
        m = _LEADING_DATE_RE.search(text)
        if m:
            for fmt in DATE_FORMATS:
                try:
                    parsed = datetime.strptime(m.group(1), fmt)
                    log.warning(f"[ND] multi-value date {text!r} -> first date")
                    return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    continue
        log.warning(f"[ND] unparseable date {text!r} -> None")
        return None

    @staticmethod
    def _clean_jobs(val) -> int:
        """Cell -> int employee count; 0 when the state published none."""
        text = _clean_text(val)
        if text in JOBS_CORRECTIONS:
            return JOBS_CORRECTIONS[text]
        n = warn_monitor._safe_int(text)
        return n if n is not None else 0

    @staticmethod
    def _header_columns(row) -> Optional[dict]:
        """If ``row`` is the table header, return {field: column_index}."""
        texts = [_clean_text(c).lower() for c in row]
        if not any("company name" in t for t in texts):
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
            raise ValueError("ND PDF: header row not found")
        log.info(f"[ND] parsed {len(records)} records")

        df = pd.DataFrame(
            records,
            columns=[
                "company", "notice_date", "effective_date",
                "employees", "city",
            ],
        )
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
            return None  # continuation/junk row without a company
        return {
            "company": company,
            "notice_date": self._clean_date(cell("notice_date")),
            "effective_date": self._clean_date(cell("effective_date")),
            "employees": self._clean_jobs(cell("employees")),
            "city": _clean_text(cell("city")),
        }
