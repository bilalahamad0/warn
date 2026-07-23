"""
warn_sources.wa
---------------
Washington — WARN notices published by the Employment Security
Department's ASP.NET search app at
https://fortress.wa.gov/esd/file/warn/Public/SearchWARN.aspx.

The grid loads pre-populated and paginates through classic WebForms
postbacks (``__EVENTTARGET=ucPSW$gvMain``, ``__EVENTARGUMENT=Page$N``),
15 rows per page. The walk is vendored from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/wa.py): GET page 1, then re-post the hidden
VIEWSTATE/EVENTVALIDATION fields for each next page. Instead of BLN's
"loop until an exception" stop, we stop cleanly when the pager renders
no link to a higher page number — jumping past the end (or ``Page$Last``)
makes the server 500, so a mid-walk HTTP error is treated as fatal
rather than as end-of-data; a truncated crawl must never be written, or
the diff engine would report phantom withdrawals.

Field crosswalk: BLN's transformer (warn_transformer/transformers/wa.py,
date_format %m/%d/%Y) predates the site's current layout — back then only
"Layoff Start Date" existed and BLN filled both of its date fields from
it (the copy this repo forbids). The live grid now publishes:

    Company           -> company              (required)
    Location          -> city                 (city, or "Various locations
                                               in Washington"-style text)
    Layoff Start Date -> effective_date
    # of Workers      -> employees            (0 when not published)
    Closure Layoff    -> layoff_type          ("Closure"/"Layoff" …
    Type of Layoff    -> layoff_type           … + "Permanent"/"Temporary",
                                               joined CA-style)
    Received Date     -> notice_date          (genuine received-by-state
                                               date the site added after
                                               BLN's crosswalk was written;
                                               populated across the full
                                               history, kept None if ever
                                               blank — never copied from
                                               the start date)
    Notice            -> (dropped: PDF link cell, no text)

WA publishes no county, street address, or industry. Backfill: the app
serves its full history (2004 onward, ~1500 notices as of 2026).
"""

import csv
import logging
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_URL = "https://fortress.wa.gov/esd/file/warn/Public/SearchWARN.aspx"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Grid columns as rendered by the live site (header row of the GridView).
_COLUMNS = [
    "Company",
    "Location",
    "Layoff Start Date",
    "# of Workers",
    "Closure Layoff",
    "Type of Layoff",
    "Received Date",
    "Notice",
]

# Hidden WebForms fields echoed back on every pagination postback.
_HIDDEN_FIELDS = [
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION",
]

_MAX_PAGES = 600  # hard cap: ~9000 rows, far beyond the feed's history


def _clean_text(text):
    """Collapse an HTML cell to one clean line (vendored from BLN wa.py)."""
    if text is None:
        return ""
    text = re.sub(r"\s+", " ", str(text))
    return text.strip()


def _parse_page(html) -> list:
    """Data rows (as dicts on _COLUMNS) from one grid page.

    The GridView is the first table; its pager renders as nested tables
    whose <tr>s also surface under a recursive find_all, so a data row is
    identified structurally: exactly one <td> per column and no ``Page$``
    postback link (the Notice column's PDF links are not postbacks).
    """
    soup = BeautifulSoup(html, "html5lib")
    tables = soup.find_all("table")
    if not tables:
        raise ValueError("WA search page: no results table found")
    grid = tables[0]

    headers = None
    for tr in grid.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            headers = [_clean_text(th.get_text()) for th in ths]
            break
    if headers is None or headers[:3] != _COLUMNS[:3]:
        raise ValueError(f"WA search page: unexpected headers {headers!r}")

    rows = []
    for tr in grid.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(headers):
            continue  # pager chrome, header row, nested-table fragments
        if tr.find("a", href=re.compile(r"Page\$")):
            continue  # pager row that happens to match the column count
        values = [_clean_text(td.get_text()) for td in cells]
        rows.append(dict(zip(headers, values)))
    return rows


def _has_next_page(html, current_page: int) -> bool:
    """True when the pager links to any page beyond the current one."""
    pages = re.findall(r"Page\$(\d+)", html)
    return any(int(p) > current_page for p in pages)


def _hidden_value(soup, name) -> str:
    tag = soup.find("input", attrs={"name": name})
    return tag.get("value", "") if tag is not None else ""


class WashingtonESD(Source):
    code = "wa"
    name = "Washington"
    agency = "Washington Employment Security Department"
    source_url = _URL
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
                log.warning(f"[WA] {method} {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _walk_pages(self, session) -> list:
        """All data rows via the VIEWSTATE pagination dance (BLN wa.py)."""
        resp = self._request(session, "GET", _URL)
        html = resp.text
        rows = _parse_page(html)
        log.info(f"[WA] page 1: {len(rows)} rows")

        page = 1
        prev_rows = rows
        while _has_next_page(html, page) and page < _MAX_PAGES:
            page += 1
            soup = BeautifulSoup(html, "html5lib")
            formdata = {
                "__EVENTTARGET": "ucPSW$gvMain",
                "__EVENTARGUMENT": f"Page${page}",
                "__LASTFOCUS": "",
                "ucPSW$txtSearch": "",
            }
            for name in _HIDDEN_FIELDS:
                formdata[name] = _hidden_value(soup, name)

            time.sleep(1)  # max 1 request/second/host
            resp = self._request(
                session,
                "POST",
                _URL,
                data=formdata,
                headers={"Referer": _URL},
            )
            html = resp.text
            page_rows = _parse_page(html)
            if page_rows == prev_rows:  # server re-served the same page
                log.warning(f"[WA] page {page} repeated; stopping walk")
                break
            rows.extend(page_rows)
            prev_rows = page_rows
        log.info(f"[WA] walked {page} pages, {len(rows)} rows total")
        return rows

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        rows = self._walk_pages(session)
        if not rows:
            raise ValueError("WA search app returned no data rows")

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in _COLUMNS})

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(value):
        """ISO date or None; the grid renders %m/%d/%Y (BLN date_format)."""
        text = _clean_text(value)
        if not text:
            return None
        try:
            return datetime.strptime(text, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = warn_monitor._safe_date(text)
            if iso and re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
                return iso
            return None

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _clean_text(row.get("Company", ""))
            if not company or company == "Company":
                continue  # company is required; drop stray header rows
            employees = warn_monitor._safe_int(row.get("# of Workers", ""))
            # "Closure"/"Layoff" + "Permanent"/"Temporary", CA-style.
            layoff_type = " ".join(
                part
                for part in (
                    _clean_text(row.get("Closure Layoff", "")),
                    _clean_text(row.get("Type of Layoff", "")),
                )
                if part
            )
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("Received Date")),
                    "effective_date": self._clean_date(
                        row.get("Layoff Start Date")
                    ),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": layoff_type,
                    # WA publishes no county, address, or industry.
                    "city": _clean_text(row.get("Location", "")),
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
