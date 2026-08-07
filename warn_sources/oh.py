"""
warn_sources.oh
---------------
Ohio — Ohio Department of Job and Family Services (ODJFS) WARN notices.

ODJFS publishes the current calendar year as a CSV hosted on the state's
Cloudinary DAM; its versioned URL is embedded as escaped JSON (``csvUrl``)
inside the Next.js payload of the public-notices page, so fetch scrapes the
page first, then downloads the CSV it points to. For prior years, Big Local
News' pre-scraped historical snapshot (2017-2022) is used for backfill and
cached locally after a single download. 2023-2025 used to exist only as
archive PDFs nobody parsed (the same gap BLN's own scraper has), but JFS's
rebuilt site now republishes each archived year as a CSV in the live feed's
exact shape; ``scripts/backfill/oh_2023_2025_gap.py`` captures those three
years into ``history_file`` below.

Scrape flow, feed-line cleaning, and historical meld are vendored from Big
Local News' Apache-2.0 warn-scraper (scrapers/oh.py); the field crosswalk
plus date/jobs corrections come from warn-transformer (transformers/oh.py):
company="Company", notice_date="Date Received", effective_date="Layoff
Date(s)" (first date of any range), employees="Potential Number Affected".
BLN keeps "City/County" as one location string; here it is split on its
single "/" into city and county. Multi-site rows whose location is a
concatenation of several City/County pairs (or "Statewide") cannot be split
reliably, so the whole string stays in ``city`` and ``county`` is left empty
— never guessed. The current-year feed adds a "Layoff/Closure" column
(Layoff | Closure | Temp Layoff) mapped to ``layoff_type``; historical rows
lack it. Ohio publishes no address or industry, so those unified fields are
omitted (never fabricated). Amended filings are re-listed by the state with
an "UPDATE " company prefix, kept verbatim.
"""

import csv
import io
import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests

import warn_monitor
from .base import DATA_DIR, Source

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://jfs.ohio.gov/job-workforce-services/job-programs-and-services/"
    "submit-a-warn-notice/current-public-notices-of-layoffs-and-closures"
)
HISTORICAL_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "oh_historical.csv"
)

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

# The csvUrl sits inside escaped JSON in the page source (vendored regex from
# BLN warn-scraper scrapers/oh.py), with a plain-JSON fallback in case the
# site stops escaping the payload.
_CSV_URL_PATTERNS = [
    re.compile(r"\\\"csvUrl\\\":\\\"(.*?)\\\""),
    re.compile(r"\"csvUrl\"\s*:\s*\"(.*?)\""),
]

# Consolidated raw-file columns (current-year feed superset; historical rows
# leave the extras empty).
RAW_COLUMNS = [
    "Company",
    "Date Received",
    "URL",
    "City/County",
    "Layoff/Closure",
    "Potential Number Affected",
    "Layoff Date(s)",
    "Phone Number",
    "Union",
    "Notice ID",
]

# Historical-snapshot header -> current feed header (vendored from BLN
# warn-scraper scrapers/oh.py "lookup").
_HIST_LOOKUP = {
    "Company": "Company",
    "DateReceived": "Date Received",
    "City/County": "City/County",
    "Potential NumberAffected": "Potential Number Affected",
    "LayoffDate(s)": "Layoff Date(s)",
    "PhoneNumber": "Phone Number",
    "Union": "Union",
    "Notice ID": "Notice ID",
}

# Known-bad date strings that appeared verbatim in the Ohio feed, keyed on
# the raw value (vendored from BLN warn-transformer transformers/oh.py
# date_corrections; datetimes rendered as ISO).
DATE_CORRECTIONS = {
    "08/14/02018": "2018-08-14",
    "01/30/201 7": "2017-01-30",
    "10/30/20015": "2015-10-30",
    "None": None,
    "Unknown": None,
    "10/2015": "2015-10-01",
    "Various": None,
    "Mar‐16": None,
    "12/23/2015â": "2015-12-23",
    "01/152024": "2024-01-15",
    "3/5/202403/19/2024": "2024-03-05",
    "3/5/2024-03/19/2024": "2024-03-05",
    "10/31/2024;": "2024-10-31",
    "07/1309/12/2024": "2024-07-13",
    "07/13-09/12/2024": "2024-07-13",
    "10/1/2024;": "2024-10-01",
    "10/1/2024; 10/31/2024; 12/31/2024": "2024-10-01",
    "(9/17/202": "2024-09-17",
    "(9/17/2024-9/30/2024": "2024-09-17",
    "(9/17/2024-9/30/2024)": "2024-09-17",
    "11/4/202404/1/2025": "2024-11-04",
    "8/9/202412/31/2024": "2024-08-09",
    "08/17/2024;": "2024-08-17",
    "01/11/2025; 04/04/2025; 05/01/2025; 07/04/2025": "2025-01-11",
    "03/15/202503/29/2025": "2025-03-15",
    "03/28/2025; 05/30/2025; 08/01/2025; 12/31/2025": "2025-03-28",
    "03/1529/2025": "2025-03-15",
    "03/1529/2025;": "2025-03-15",
    "4/29/2025;": "2025-04-29",
    "4/15/2025;": "2025-04-15",
    "7/18/2025;": "2025-07-18",
    "02/27/202": "2026-02-27",
    "2/27/20265/31/2026": "2026-02-27",
    "2/27/2026; 5/31/2026": "2026-02-27",
    "6/19/20267/02/2026": "2026-06-19",
    "6/19/2026; 7/02/2026": "2026-06-19",
    "5/2/20268/1/2026": "2026-05-02",
    "5/2/2026; 8/1/2026": "2026-05-02",
    "4/22/20266/20/2026": "2026-04-22",
    "4/22/2026; 6/20/2026": "2026-04-22",
    "4/15/20267/01/2026": "2026-04-15",
    "4/15/2026; 7/01/2026": "2026-04-15",
    "5/15/20269/30/2026": "2026-05-15",
    "5/15/2026; 9/30/2026": "2026-05-15",
    "5/11/20265/29/2026": "2026-05-11",
    "5/11/2026; 5/29/2026": "2026-05-11",
    "5/6/20265/31/2026": "2026-05-06",
    "5/6/2026; 5/31/2026": "2026-05-06",
    "2/24/20264/24/2026": "2026-02-24",
    "2/24/2026; 4/24/2026": "2026-02-24",
    "4/19/20265/01/2026": "2026-04-19",
    "4/19/2026; 5/01/2026": "2026-04-19",
    "4/11/20264/30/2026": "2026-04-11",
    "4/11/2026; 4/30/2026": "2026-04-11",
    "3/19/20269/15/2026": "2026-03-19",
    "3/19/2026; 9/15/2026": "2026-03-19",
    "01/30/201": "2017-01-30",
    "6/16/2026;": "2026-06-16",
}

# Known-odd employee counts (vendored from BLN warn-transformer
# transformers/oh.py jobs_corrections).
JOBS_CORRECTIONS = {
    "13 FT": 13,
    "58 94 97 35": 58,
    "Unknown": None,
    "unknown": None,
    "242 80": 242,
    "323‐500": 323,
    "98 Part-time Workers": 98,
    "56 part time": 56,
    "39 part time": 39,
    "484 Perm Layoffs/850 Temp Layoffs": 1334,
    "1 remote": 1,
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# company values that are really repeated-header/junk rows, lowercased.
_JUNK_COMPANIES = {"company", "nan", "none"}


def _extract_csv_url(html: str) -> str:
    """Pull the current-year CSV URL out of the notices page source."""
    for pattern in _CSV_URL_PATTERNS:
        found = pattern.findall(html)
        if found:
            return found[0]
    raise RuntimeError(
        "OH feed: csvUrl not found in the ODJFS notices page — the site "
        "layout may have changed (see warn_sources/oh.py)"
    )


def _read_feed_csv(text: str) -> list:
    """Feed CSV text -> list of row dicts.

    The Ohio CSV arrives with prefacing junk lines ("s,s,h,s,...", a row of
    bare commas) that make it not-quite-CSV; drop any line without useful
    data before parsing (vendored cleanup from BLN warn-scraper).
    """
    clean = [line for line in text.splitlines() if len(line) > 20]
    return list(csv.DictReader(io.StringIO("\n".join(clean))))


def _clean_date(value):
    """Raw Ohio date cell -> ISO YYYY-MM-DD string or None.

    Cleaning steps vendored from BLN warn-transformer transformers/oh.py
    transform_date: strip Updated/Revised prefixes, split ranges (hyphen,
    " to ", run-on double dates) and keep the first date.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    v = raw.replace("Updated", "").replace("Revised", "")
    v = v.replace("-", " ").strip()
    if " " in v:
        v = v.split()[0]
    if len(v) == 20:  # run-on double date, 4-digit years
        v = v[:10]
    elif len(v) == 19:
        v = v[:9]
    v = re.split(r"\s{2,}", v)[0].strip()
    v = v.split("Originated")[0].strip()
    v = v.split(" to ")[0].strip()
    v = v.replace("‐", "")  # U+2010 hyphen
    v = v.strip("();, ")
    if v in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[v]
    iso = warn_monitor._safe_date(v)
    if iso and _ISO_RE.match(iso) and 1990 <= int(iso[:4]) <= 2100:
        return iso
    return None  # unparseable junk — never keep raw text as a date


def _clean_jobs(value):
    """Raw employee-count cell -> int or None (count not published)."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    if raw in JOBS_CORRECTIONS:
        return JOBS_CORRECTIONS[raw]
    n = warn_monitor._safe_int(raw)
    if n is None:  # e.g. "20 FT" variants: first integer wins
        m = re.match(r"(\d+)", raw.replace(",", ""))
        n = int(m.group(1)) if m else None
    return n


def _split_location(value) -> tuple:
    """"City/County" -> (city, county); unsplittable values stay in city."""
    loc = "" if value is None else str(value).strip()
    if loc.count("/") == 1:
        city, county = loc.split("/")
        return city.strip(), county.strip()
    return loc, ""  # "Statewide" or concatenated multi-site rows


class OhioJFS(Source):
    code = "oh"
    name = "Ohio"
    agency = "Ohio Department of Job and Family Services"
    source_url = PAGE_URL
    cadence = "twice-daily"
    # 2023-2025 fell between the BLN snapshot (ends 2022) and the live
    # current-year feed; scripts/backfill/oh_2023_2025_gap.py recovered them
    # from JFS's per-year archive CSVs. Like CA and NY, the file surfaces in
    # the national dataset only — the OH live pipeline stays exactly as it is.
    history_file = DATA_DIR / "historical" / "oh_history.json"

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
                log.warning(f"[OH] request error for {url}: {e}")
                continue
            if resp.status_code == 200:
                return resp
            log.warning(f"[OH] HTTP {resp.status_code} for {url}")
            if resp.status_code == 404:
                return resp  # hard miss — no point retrying
        return resp

    def _historical_rows(self, session) -> list:
        """BLN's pre-scraped 2017-2022 snapshot, cached after one download.

        The snapshot is static, so it is fetched at most once; on a cold
        cache with the bucket unreachable the run continues current-year
        only (logged) rather than failing the state.
        """
        cache_file = self.paths.root / "oh_historical.csv"
        if not cache_file.exists():
            time.sleep(1)  # politeness: max 1 request/second
            resp = self._request(session, HISTORICAL_URL)
            if resp is None or resp.status_code != 200:
                log.warning(
                    "[OH] historical snapshot unavailable — continuing with "
                    "current-year data only"
                )
                return []
            cache_file.write_text(resp.text)
        rows = []
        with open(cache_file, newline="") as fh:
            for row in csv.DictReader(fh):
                rows.append(
                    {new: row.get(old, "") for old, new in _HIST_LOOKUP.items()}
                )
        return rows

    def fetch(self, force: bool = False) -> tuple:
        """Scrape page -> current-year CSV, meld cached historical snapshot,
        write one consolidated CSV at ``self.paths.raw``."""
        self.paths.ensure()
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        resp = self._request(session, PAGE_URL)
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"OH feed: notices page {status}")
        csv_url = _extract_csv_url(resp.text)

        time.sleep(1)  # politeness: max 1 request/second/host
        resp = self._request(session, csv_url)
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"OH feed: current-year CSV {status} ({csv_url})")

        rows = _read_feed_csv(resp.text)
        if not rows:
            raise RuntimeError("OH feed: current-year CSV parsed to zero rows")
        rows.extend(self._historical_rows(session))

        with open(self.paths.raw, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=RAW_COLUMNS, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV -> unified-schema rows."""
        raw = pd.read_csv(Path(raw_path), dtype=str, keep_default_na=False)
        missing = {"Company", "Date Received"} - set(raw.columns)
        if missing:
            raise ValueError(f"OH feed: expected columns missing: {missing}")

        rows = []
        for _, r in raw.iterrows():
            company = str(r.get("Company", "")).strip()
            if not company or company.lower() in _JUNK_COMPANIES:
                continue  # blank / repeated-header / junk row
            city, county = _split_location(r.get("City/County", ""))
            emp = _clean_jobs(r.get("Potential Number Affected", ""))
            rows.append(
                {
                    "company": company,
                    "notice_date": _clean_date(r.get("Date Received", "")),
                    "effective_date": _clean_date(r.get("Layoff Date(s)", "")),
                    "employees": emp if emp is not None else 0,
                    "layoff_type": str(r.get("Layoff/Closure", "")).strip(),
                    "county": county,
                    "city": city,
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
            ],
        )
        # pandas coerces None -> NaN on construction; restore real None so
        # missing dates serialize as JSON null, not the string "nan".
        return out.astype(object).where(pd.notna(out), None)
