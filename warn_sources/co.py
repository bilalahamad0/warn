"""
warn_sources.co
---------------
Colorado — WARN notices maintained by the Colorado Department of Labor
and Employment (CDLE) as one Google Sheet per year, all linked from
https://cdle.colorado.gov/employers/layoff-separations/layoff-warn-list

The landing page carries a "View Real-Time <year> Warns" button for the
current year plus an accordion of past-year links (2015-present). Each
sheet is pulled through Google's HTML export endpoint
(``/gviz/tq?tqx=out:html``) — flow vendored from Big Local News'
Apache-2.0 warn-scraper (warn/scrapers/co.py), including the header
crosswalk that reconciles a decade of column renames, the junk-row
rules, and the Avis-Budget company fix. Field mapping onto the unified
schema follows BLN's warn-transformer (transformers/co.py) exactly:

    company        <- Company / Company Name
    notice_date    <- WARN Date
    effective_date <- Begin Date / Begin date of layoffs / Layoff Date(s)
    employees      <- permanent job losses (# Permanent / Perm Layoffs …)
                      falling back to the year's total-layoffs column;
                      free-text counts fixed via BLN jobs_corrections
    county         <- Workforce Area / Workforce Region (CDLE publishes
                      workforce areas — mostly county names, sometimes
                      regions like "Rural Consortium")
    layoff_type    <- Reason for Layoff(s)
    industry       <- NAICS

CO publishes no per-notice city; street addresses appear only in the
2021-2023 form-era sheets and are ignored by the BLN transformer, so
they stay in the raw CSV for audit but are not promoted to the unified
schema. Dates known to be unparseable free text are fixed via the
transformer's date_corrections table; anything else unparseable becomes
None — never fabricated, never copied from another date column.
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

_PAGE_URL = (
    "https://cdle.colorado.gov/employers/layoff-separations/layoff-warn-list"
)
_GVIZ_EXPORT = "/gviz/tq?tqx=out:html"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Header crosswalk vendored from BLN warn-scraper warn/scrapers/co.py.
_CROSSWALK = {
    "Company Name": "company",
    "Company": "company",
    "Name": "company",
    "WARN Date": "notice_date",
    "Total Layoffs": "jobs",
    "NAICS": "naics",
    "Workforce Area": "workforce_area",
    "# Perm": "permanent_job_losses",
    "#Temp": "temporary_job_losses",
    "Reduced Hours": "reduced_hours",
    "#Furloughs": "furloughs",
    "Begin Date": "begin_date",
    "End Date": "end_date",
    "Reason for Layoffs": "reason",
    "Reason for Layoff": "reason",
    "WARN Letter": "letter",
    "Occupations Impacted": "occupations",
    "Occupations": "occupations",
    "Select the workforce area": "workforce_area",
    "Total CO": "jobs",
    "CO Layoffs": "jobs",
    "Total number of permanent layoffs": "permanent_job_losses",
    "# permanent": "permanent_job_losses",
    "# Permanent": "permanent_job_losses",
    "Total number of temporary layoffs": "temporary_job_losses",
    "Total number of furloughs": "furloughs",
    "Begin date of layoffs": "begin_date",
    "End date of layoffs": "end_date",
    "Layoff Total": "jobs",
    "Local Area": "workforce_area",
    "Layoff Date(s)": "begin_date",
    "Temp Layoffs": "temporary_job_losses",
    "Perm Layoffs": "permanent_job_losses",
    "Furloughs": "furloughs",
    "Workforce Local Area": "workforce_area",
    "Workforce Region": "workforce_region",
    "Contact Name": "contact",
    "Contact Phone": "phone",
    "Contact Email": "email",
    "FEIN": "fein",
    "Location Address": "location",
    "Total number of employees at the location": "at_the_location",
    "Sector 33 (6414) Guided Missle & Space Vehicle": "naics",
    "@dropdown": "dropdown",
    "Received": "received_date",
    "Notes": "notes",
    "12/1/25`": "company",
    "Total Notified": "total_notified",
    (
        "4850 32nd Avenue South, Fargo, North Dakota, United States."
    ): "company",
    "CO Notifications": "total_notified",
}

# Survey-era columns with no analytic value (vendored BLN garbage list);
# the regex sweeps every "… location 2/3" and "… Union 2/3" variant.
_GARBAGE = frozenset(
    {
        "Timestamp",
        "Email Address",
        "Is this a NEW WARN or a REVISION?",
        "Total number of employees with reduced hours",
        (
            "Include the total number of employees on or expected to be "
            "on a Workshare plan."
        ),
        "Expected date of second job losses at location 1",
        "Expected end date of second job losses at location 1",
        "Expected date of third job losses at location 1",
        "Expected end date of third job losses at location 1",
        "Do the employees have bumping rights?",
        "Are the employees represented by a union?",
        (
            "If you selected Rural Consortium for the workforce area, "
            "please choose a subarea using the map."
        ),
        "Name of union(s)",
        "Contact name(s) for union representative(s)",
        "Contact phone number for union representative(s)",
        "Email address for union representative(s)",
        "Address, City, ZIP for Union 1",
        "Has a second location been impacted?",
        "Has a third location been impacted?",
        (
            "If you selected Other/Sub-Area, please choose a location "
            "from the following dropdown menu:"
        ),
        "Include here any comments or additional details",
    }
)
_GARBAGE_RE = re.compile(r"(location|Union) [23]\b", re.IGNORECASE)

# Sheets that published no header row (2019 still doesn't; 2017 grew a
# real one since BLN wrote theirs) — vendored fallbacks, keyed by year.
_FALLBACK_HEADERS = {
    "2017": [
        "Company",
        "Layoff Total",
        "Workforce Region",
        "WARN Date",
        "Reason for Layoff",
    ],
    "2019": [
        "Company Name",
        "Layoff Total",
        "Workforce Local Area",
        "WARN Date",
        "Reason for Layoff",
        "Occupations",
        "Layoff Date(s)",
    ],
}

# Column order of the consolidated raw CSV (all crosswalk targets).
_RAW_COLUMNS = [
    "company",
    "notice_date",
    "received_date",
    "begin_date",
    "end_date",
    "jobs",
    "permanent_job_losses",
    "temporary_job_losses",
    "furloughs",
    "reduced_hours",
    "total_notified",
    "workforce_area",
    "workforce_region",
    "reason",
    "naics",
    "letter",
    "occupations",
    "location",
    "at_the_location",
    "contact",
    "phone",
    "email",
    "fein",
    "dropdown",
    "notes",
]

_OUT_COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "industry",
]

_DATE_FORMATS = ("%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Free-text date fixes vendored from BLN warn-transformer
# (transformers/co.py date_corrections), rendered as ISO strings.
_DATE_CORRECTIONS = {
    "N/A": None,
    "12/31/19 (for 265) Not specified for 191": "2019-12-31",
    "3/24/20- 4/7/20 in phases": "2020-03-24",
    "Downsize 1/26/20": "2020-01-26",
    "3/13/20 through 4/24/20": "2020-03-13",
    "4/30/20, 5/29/20 and 7/31/20": "2020-04-30",
    "3/17/20 - 4/2/20": "2020-03-17",
    "3/19/20 to 4/20/20, closing 3/19/20": "2020-03-19",
    "3/16/20-3/20/20 and 3/21/20-3/31/20": "2020-03-16",
    "3/19/20-4/1/20": "2020-03-19",
    "3/29": "2020-03-29",
    "6/26/20 & 12/29/20": "2020-06-26",
    "3/20/20, 3/24/20, & 3/26/20": "2020-03-20",
    "3/24/20, 3/26/20 & 3/30/20": "2020-03-24",
    "3/23/20, 3/24/20, & 3/26/20": "2020-03-23",
    (
        "4/1/20-4/30/20 for hourly workers and May 2020 for salaried "
        "associates"
    ): "2020-04-01",
    "3/3/19, 3/31/19": "2019-03-03",
    "3/10/19  (184) & 5/31/19  (19)": "2019-03-10",
    "3/25/19, 4/24/19, 5/24/19": "2019-03-25",
    "5/24/19 - 3/20/20": "2019-05-24",
    "4/13/19 - 5/19/19": "2019-04-13",
    "5/18/19 - 6/1/19": "2019-05-18",
    "5/25/19 (thru 18 mo following)": "2019-05-25",
    "7/6/19 - 7/31/19": "2019-07-06",
    "8/2/19 - 3/31/20": "2019-08-02",
    "9/7/19 - 12/31/19": "2019-09-07",
    "10/5/19-12/31/19": "2019-10-05",
    "10/18/19-12/31/19": "2019-10-18",
    "11/25/19 - 9/30/20": "2019-11-25",
    "11/29/19 - 1/31/2020": "2019-11-29",
    "10/30/2019, 11/8/2019, 11/22/2019, 11/29/2019": "2019-10-30",
    "12/31/19 to 3/26/20": "2019-12-31",
    "1/1/20 to 1/14/20": "2020-01-01",
    "Closure 1/10/20- 04/10/20": "2020-01-10",
    "3/1/19 (received on 3/22/19)": "2019-03-01",
    "3/21/19 (received  3/22/19)": "2019-03-21",
    "7/6/19-7/31/19": "2019-07-06",
    "7/15/19 (received 7/16/19)": "2019-07-15",
    (
        "11-1-19 received 11/19/19 via Local Area/Suthers office"
    ): "2019-11-01",
    "05/24/19 - 3/20/2020": "2019-05-24",
    "1/7/19 & 4/6/2020": "2019-01-07",
    "WARN Date": None,
    "TOTAL": None,
    "P1: 6/1/23 P2: 7/14/23": "2023-06-01",
    "4/3020": "2020-04-30",
    "Not available": None,
    "Unknown": None,
    "4/6": "2020-04-06",
    "11/20/20-11/30/20": "2020-11-20",
    "11/20/20-11/30/290": "2020-11-20",
    "6/5/20 to 6/22/20": "2020-06-05",
    "5/22/20, 5/26/20, 5/29/20": "2020-05-22",
    "6/10/20, 6/17/20, 6/22/20, 6/26/20": "2020-06-10",
    "7/18/20-8/11/20": "2020-07-18",
    "4/8/20 - 5/1/20": "2020-04-08",
    "6/17/2020 - 7/1/2020": "2020-06-17",
    "5/26/20-July/22/20": "2020-05-26",
    "4/13/19-5/30/19": "2019-04-13",
    "Multi Phase (See WARN)": None,
    "Multi phase (see WARN)": None,
    "?": None,
    "12012024": "2024-12-01",
    "012/31/24": "2024-12-31",
    "8/25": "2025-08-01",
}

# Free-text headcount fixes vendored from BLN warn-transformer
# (transformers/co.py jobs_corrections).
_JOBS_CORRECTIONS = {
    "-": None,
    "61 total, 4 in CO": 4,
    "61 total 4 in CO": 4,
    "": None,
    "55-59": 59,
    "Unknown": None,
    "Unknown - Previous submission 117": None,
    "Layoff Total": None,
    "N/A": None,
    "40-60": 40,
    "1 (of 72 in CO)": 1,
    "38 (resigned voluntarily)": None,
    "49 (5 in CO)": 5,
    "4 (of 178)": 4,
    "?": None,
    "?*": None,
    "?* Unclear on the Number in Colorado": None,
    "Unspecified": None,
    "22,000 (unspecified in CO)": None,
    "22000": None,
    "22000*": None,
    "*": None,
    "* Unknown total in Colorado": None,
    "2 employees extended until 10/15/23": None,  # layoff date change
    "Not stated (researching)": None,
    "Not Stated": None,
    "125 (4 in CO)": 4,
    "154 (1 in CO)": 1,
    "75 (1 in CO)": 1,
    "Not specified": 0,
    (
        "*Note: Only 49 not 71 were affected as previously reported "
        "which is under the count for WARN."
    ): 49,
    (
        "**Note: Only 49 not 71 were affected as previously reported "
        "which is under the count for WARN."
    ): 49,
    "25 (5 in CO)": 5,
    "139 (3 in CO)": 3,
    "58 (1 in CO)": 1,
    "Not Specified": 0,
    "9/12/25": 193,
}


def _is_garbage(header: str) -> bool:
    return header in _GARBAGE or bool(_GARBAGE_RE.search(header))


def _sheet_export_url(href: str) -> str:
    """Google Sheet share link -> stable HTML-export URL (BLN flow)."""
    if "/edit" in href:
        return href.split("/edit")[0] + _GVIZ_EXPORT
    if "drive.google.com/open?id=" in href:  # 2016-era link schema
        sheet_id = href.split("open?id=")[-1]
        return (
            "https://docs.google.com/spreadsheets/d/"
            + sheet_id
            + _GVIZ_EXPORT
        )
    raise ValueError(f"Cannot adapt {href!r} to an HTML export URL")


def _table_rows(table, fallback_header=None) -> list:
    """One exported sheet <table> -> list of {header: cell} dicts.

    Vendored row-scraping rules from BLN warn-scraper: the first row is
    the header when it contains "WARN Date"; otherwise a per-year
    fallback header list is required and every row is data. Blank rows
    and mid-sheet header repeats are skipped.
    """
    tr_list = table.find_all("tr")
    first = (
        [td.text.strip() for td in tr_list[0].find_all("td")]
        if tr_list
        else []
    )
    if "WARN Date" in first:
        header, data_rows = first, tr_list[1:]
    elif fallback_header:
        header, data_rows = list(fallback_header), tr_list
    else:
        raise ValueError("Sheet has no recognisable header row")
    if header and not header[0]:
        header[0] = "Company Name"  # BLN: blank first header = company

    rows = []
    for tr in data_rows:
        cells = [td.text.strip() for td in tr.find_all("td")]
        row = {}
        for head, value in zip(header, cells):
            if head:  # unnamed (padding) columns carry no data
                row[head] = value
        values = list(row.values())
        if not any(values):
            continue  # blank filler row
        if "WARN Date" in values:
            continue  # repeated header row inside the data
        rows.append(row)
    return rows


def _standardize(rows: list) -> list:
    """Sheet-native rows -> dicts on _RAW_COLUMNS via the crosswalk."""
    out = []
    unknown = set()
    for row in rows:
        std = {col: "" for col in _RAW_COLUMNS}
        for key, value in row.items():
            if _is_garbage(key):
                continue
            target = _CROSSWALK.get(key)
            if target is None:
                if key not in unknown:
                    unknown.add(key)
                    log.warning(f"[CO] unmapped column {key!r} — skipped")
                continue
            std[target] = value
        company = std["company"].strip()
        # BLN quirk: 2020-era Avis rows carry the name in the WARN
        # Letter column and leave Company Name blank.
        if len(company) < 3 and std["letter"].strip() == "Avis Budget Group":
            company = "Avis Budget Group"
        std["company"] = company
        if len(company) < 3:
            continue  # BLN: row of questionable quality
        if std["begin_date"] == "Layoff Date(s)":
            continue  # BLN: stray header fragment
        out.append(std)
    return out


def _write_raw_csv(rows: list, path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_RAW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _clean_date(value):
    """One CO date cell -> ISO YYYY-MM-DD or None — never a guess."""
    value = (value or "").strip()
    if not value:
        return None
    if value in _DATE_CORRECTIONS:
        return _DATE_CORRECTIONS[value]
    parsed = None
    for fmt in _DATE_FORMATS:  # BLN transformer date_format tuple
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        iso = warn_monitor._safe_date(value)
        if not iso or not _ISO_RE.match(iso):
            return None  # unparseable free text -> no date
        parsed = datetime.strptime(iso, "%Y-%m-%d")
    if not 2010 <= parsed.year <= datetime.now().year + 6:
        return None  # obvious typo, e.g. "1/15/2002" on a 2026 filing
    return parsed.strftime("%Y-%m-%d")


def _clean_jobs(value):
    """One CO headcount cell -> int or None (0 = explicit 'not given')."""
    value = (value or "").strip()
    if value in _JOBS_CORRECTIONS:
        return _JOBS_CORRECTIONS[value]
    n = warn_monitor._safe_int(value)
    if n is not None and not 0 <= n <= 10000:
        return None  # BLN maximum_jobs sanity cap
    return n


class ColoradoCDLE(Source):
    code = "co"
    name = "Colorado"
    agency = "Colorado Department of Labor and Employment"
    source_url = _PAGE_URL
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
                log.warning(f"[CO] {method} {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _sheet_links(self, session) -> list:
        """(label, sheet URL) for the current-year button + accordion."""
        resp = self._request(session, "GET", _PAGE_URL)
        soup = BeautifulSoup(resp.text, "html5lib")
        region = soup.find(class_="region-content")
        if region is None:
            raise ValueError("CO WARN page: no region-content block")
        current = region.find("a", class_="btn-dark-blue")
        if current is None or not current.get("href"):
            raise ValueError("CO WARN page: no current-year sheet link")
        accordions = region.find_all("dl")
        if not accordions:
            raise ValueError("CO WARN page: no past-year accordion")
        links = [(current.text.strip(), current["href"])]
        for accordion in accordions:
            for a in accordion.find_all("a"):
                text, href = a.text.strip(), a.get("href", "")
                if not href or "feedback" in text.lower():
                    continue
                if "docs.google.com" not in href and (
                    "drive.google.com" not in href
                ):
                    log.warning(f"[CO] skipping non-sheet link {href!r}")
                    continue
                links.append((text, href))
        return links

    def _sheet_html(self, session, label: str, year: str, url: str) -> str:
        """Fetch one sheet's HTML export, falling back to the cached
        copy in the state directory if Google errors out."""
        cache = self.paths.root / f"sheet_{year}.html"
        time.sleep(1)  # max 1 request/second/host
        try:
            html = self._request(session, "GET", url).text
            if "<table" not in html.lower():
                raise ValueError(f"no <table> in export for {label!r}")
            cache.write_text(html, encoding="utf-8")
            return html
        except Exception as e:
            if cache.exists():
                log.warning(f"[CO] {label!r}: {e} — using cached copy")
                return cache.read_text(encoding="utf-8")
            raise

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        rows, seen = [], set()
        for label, href in self._sheet_links(session):
            match = re.search(r"(19|20)\d{2}", label)
            year = match.group(0) if match else "current"
            html = self._sheet_html(
                session, label, year, _sheet_export_url(href)
            )
            table = BeautifulSoup(html, "html5lib").find("table")
            if table is None:
                raise ValueError(f"CO sheet {label!r}: export has no table")
            sheet_rows = _table_rows(table, _FALLBACK_HEADERS.get(year))
            for std in _standardize(sheet_rows):
                key = tuple(std[col] for col in _RAW_COLUMNS)
                if key not in seen:  # 2016/2017 sheets overlap
                    seen.add(key)
                    rows.append(std)
            log.info(f"[CO] {label}: {len(sheet_rows)} rows")

        _write_raw_csv(rows, self.paths.raw)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _PAGE_URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = re.sub(r"\s+", " ", row.get("company", "")).strip()
            if not company:
                continue  # company is required
            # BLN transformer: permanent job losses, else the year's
            # total-layoffs column.
            jobs_raw = (
                row.get("permanent_job_losses", "").strip()
                or row.get("jobs", "").strip()
            )
            employees = _clean_jobs(jobs_raw)
            area = (
                row.get("workforce_area", "").strip()
                or row.get("workforce_region", "").strip()
            )
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("notice_date")),
                    "effective_date": _clean_date(row.get("begin_date")),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": row.get("reason", "").strip(),
                    "county": area,
                    "industry": row.get("naics", "").strip(),
                }
            )
        out = pd.DataFrame(records, columns=_OUT_COLUMNS)
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
