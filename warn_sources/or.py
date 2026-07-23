"""
warn_sources.or
---------------
Oregon — WARN list published by the Higher Education Coordinating
Commission's Office of Community Colleges and Workforce Development
(CCWD) at https://ccwd.hecc.oregon.gov/Layoff/WARN.

The bare download endpoint returns an HTML shell, not the file. The
working flow (vendored from Big Local News' Apache-2.0 warn-scraper,
warn/scrapers/or.py) is a three-step dance:

    1. GET  /Layoff/WARN/Download  -> anti-forgery token + session cookie
    2. POST same URL (token, WARNFormat=xlsx, WARNSort=LOT4)
            -> page whose "btn-primary" link points at a freshly
               generated /Layoff/Reports/WARNList_<n>.xlsx
    3. GET  that link -> the actual workbook

The live export is a rolling ten-year window, so — again following the
BLN scraper — a static historical workbook (1988–2021, BLN's public
mirror) is fetched once, cached beside the state's data files, and
merged in for backfill. ``fetch`` consolidates both workbooks into one
CSV at ``self.paths.raw``; ``parse`` maps it onto the unified schema
using BLN's field crosswalk (warn_transformer/transformers/or.py):
Company Name -> company, Received Date -> notice_date,
Layoff Date -> effective_date, Laid Off -> employees, and the
"1899-12-29" Excel-epoch sentinel in date columns -> None.
"""

import csv
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_BASE_URL = "https://ccwd.hecc.oregon.gov"
_DOWNLOAD_URL = _BASE_URL + "/Layoff/WARN/Download"
# Static 1988-2021 backfill hosted by Big Local News (Apache-2.0 project).
_HISTORICAL_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "or_historical.xlsx"
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Raw workbook columns (header row is the third row of each sheet).
_COLUMNS = [
    "WARN#",
    "Company Name",
    "Location",
    "Layoff Date",
    "Laid Off",
    "Layoff Type",
    "Received Date",
]


def _cell_text(value) -> str:
    """Serialise one worksheet cell for the consolidated CSV."""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return str(value)  # "%Y-%m-%d %H:%M:%S" — BLN's date_format
    return str(value)


def _extract_rows(xlsx_path) -> list:
    """Rows (as dicts on _COLUMNS) from one CCWD workbook.

    Vendored layout knowledge from BLN warn-scraper: two title rows,
    headers on the third row, data after; trailing blank rows dropped
    when both WARN# and Company Name are empty.
    """
    workbook = load_workbook(filename=xlsx_path, read_only=True)
    sheetrows = list(workbook.worksheets[0].rows)
    workbook.close()
    if len(sheetrows) < 3:
        raise ValueError(f"Unexpected workbook shape in {xlsx_path}")
    headers = [c.value for c in sheetrows[2]]
    if headers[:2] != _COLUMNS[:2]:
        raise ValueError(f"Unexpected header row {headers!r} in {xlsx_path}")
    rows = []
    for row in sheetrows[3:]:
        values = [_cell_text(cell.value) for cell in row]
        line = dict(zip(headers, values))
        warn_no = (line.get("WARN#") or "").strip()
        company = (line.get("Company Name") or "").strip()
        if not warn_no and not company:
            continue  # blank filler row
        rows.append({col: line.get(col, "") for col in _COLUMNS})
    return rows


def _dedup_key(row: dict) -> tuple:
    """Whitespace-insensitive identity of a raw row.

    The historical workbook pads old company names with trailing spaces;
    stripped comparison keeps the live/historical overlap (2016-2021)
    from double-counting.
    """
    return tuple(str(row.get(col, "")).strip() for col in _COLUMNS)


class OregonCCWD(Source):
    code = "or"
    name = "Oregon"
    agency = (
        "Oregon Higher Education Coordinating Commission, Office of "
        "Community Colleges and Workforce Development"
    )
    source_url = _BASE_URL + "/Layoff/WARN"
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _request(self, session, method, url, **kwargs):
        """One polite request: 60s timeout, 3 attempts, backoff."""
        kwargs.setdefault("timeout", 60)
        last_error = None
        for attempt in range(1, 4):
            try:
                resp = session.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[OR] {method} {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _download_live_xlsx(self, session, dest: Path) -> None:
        """Token dance vendored from BLN warn-scraper warn/scrapers/or.py."""
        resp = self._request(session, "GET", _DOWNLOAD_URL)
        soup = BeautifulSoup(resp.content, "html5lib")
        token = soup.find("input", {"name": "__RequestVerificationToken"})
        if token is None:
            raise ValueError("OR download page: no anti-forgery token found")

        time.sleep(1)  # max 1 request/second/host
        payload = {
            "__RequestVerificationToken": token["value"],
            "WARNFormat": "xlsx",
            "WARNSort": "LOT4",
        }
        resp = self._request(
            session,
            "POST",
            _DOWNLOAD_URL,
            data=payload,
            headers={
                "Origin": _BASE_URL,
                "Referer": _DOWNLOAD_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        link = BeautifulSoup(resp.content, "html5lib").find(
            "a", {"class": "btn-primary"}
        )
        if link is None or not link.get("href"):
            raise ValueError("OR download page: no generated Excel link found")

        time.sleep(1)
        resp = self._request(session, "GET", _BASE_URL + link["href"])
        dest.write_bytes(resp.content)

    def _ensure_historical_xlsx(self, session, dest: Path) -> bool:
        """Cache the static 1988-2021 backfill workbook; soft-fail."""
        if dest.exists():
            return True
        try:
            time.sleep(1)
            resp = self._request(session, "GET", _HISTORICAL_URL)
            dest.write_bytes(resp.content)
            return True
        except Exception as e:  # backfill is optional; live feed still runs
            log.warning(f"[OR] historical backfill unavailable: {e}")
            return False

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        live_path = self.paths.root / "latest.xlsx"
        historical_path = self.paths.root / "historical.xlsx"
        self._download_live_xlsx(session, live_path)
        have_historical = self._ensure_historical_xlsx(
            session, historical_path
        )

        rows = _extract_rows(live_path)
        seen = {_dedup_key(r) for r in rows}
        if have_historical:
            for row in _extract_rows(historical_path):
                key = _dedup_key(row)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _DOWNLOAD_URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(value):
        """ISO date or None; BLN date_corrections null the 1899 sentinel."""
        iso = warn_monitor._safe_date(value)
        if iso and iso < "1900-01-01":  # Excel-epoch garbage, e.g. 1899-12-29
            return None
        return iso

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = re.sub(r"\s+", " ", row.get("Company Name", "")).strip()
            if not company:
                continue  # company is required
            employees = warn_monitor._safe_int(row.get("Laid Off", ""))
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("Received Date")),
                    "effective_date": self._clean_date(row.get("Layoff Date")),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": row.get("Layoff Type", "").strip(),
                    # "Location" is a city (occasionally "City, ST" for
                    # out-of-state HQ filings); OR publishes no county,
                    # address, or industry.
                    "city": row.get("Location", "").strip().strip(","),
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
                "city",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
