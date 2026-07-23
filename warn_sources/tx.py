"""
warn_sources.tx
---------------
Texas — Texas Workforce Commission (TWC) per-year WARN XLSX listings.

TWC publishes one XLSX per year at
``https://www.twc.texas.gov/sites/default/files/oei/docs/
warn-act-listings-<YEAR>-twc.xlsx`` (2020-present), linked from
https://www.twc.texas.gov/data-reports/warn-notice.

STATUS: ``enabled = False`` — the whole twc.texas.gov site (including the
static XLSX files) sits behind an AWS WAF JavaScript challenge. Probed on
2026-07-20: three attempts (full Chrome UA + Accept/Accept-Language/Referer;
sec-ch-ua/Sec-Fetch header set; cookie-jar session seeded from the listing
page) all returned HTTP 202 with a ``gokuProps``/``awsWafCookieDomainList``
challenge body instead of the file. Passing the challenge requires executing
its JavaScript to mint an ``aws-waf-token`` — i.e. defeating bot detection —
which this pipeline will not do. Fetch and parse below are fully implemented
and honest: if TWC drops the wall (or a sanctioned fetch route such as a
proxy API is added), flip ``enabled`` to True and the source goes live
unchanged. Until then ``fetch`` raises a clear RuntimeError, which
``warn_sources.run_all`` isolates per-state.

Field mapping and date corrections are vendored from Big Local News'
Apache-2.0 warn-scraper / warn-transformer projects (scrapers/tx.py and
transformers/tx.py): company=JOB_SITE_NAME, city=CITY_NAME,
notice_date=NOTICE_DATE, effective_date=LayOff_Date,
employees=TOTAL_LAYOFF_NUMBER, plus COUNTY_NAME which TWC also publishes.
TWC does not publish layoff_type, address, or industry in the per-year
files, so those unified fields are omitted (never fabricated).
"""

import io
import logging
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

LISTING_URL = "https://www.twc.texas.gov/data-reports/warn-notice"
YEAR_URL = (
    "https://www.twc.texas.gov/sites/default/files/oei/docs/"
    "warn-act-listings-{year}-twc.xlsx"
)
FIRST_YEAR = 2020  # oldest per-year file TWC keeps online

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": LISTING_URL,
}

# Vendored from BLN warn-transformer transformers/tx.py (Apache-2.0):
# known-bad dates that appeared verbatim in the TWC feed, keyed post-ISO.
DATE_CORRECTIONS = {
    "1930-03-30": "2020-03-30",
    "1930-03-31": "2020-03-31",
    "2027-03-01": "2017-03-01",
    "2027-03-27": "2026-03-27",
}

# Normalized TWC header -> unified field (BLN crosswalk + COUNTY_NAME).
_FIELD_MAP = {
    "jobsitename": "company",
    "cityname": "city",
    "countyname": "county",
    "noticedate": "notice_date",
    "layoffdate": "effective_date",
    "totallayoffnumber": "employees",
}

# Canonical TWC header spellings, for consolidating per-year workbooks
# whose header case/spacing may drift between files.
_CANONICAL = {
    "jobsitename": "JOB_SITE_NAME",
    "cityname": "CITY_NAME",
    "countyname": "COUNTY_NAME",
    "wdaname": "WDA_NAME",
    "noticedate": "NOTICE_DATE",
    "layoffdate": "LayOff_Date",
    "totallayoffnumber": "TOTAL_LAYOFF_NUMBER",
    "wfddreceiveddate": "WFDD_RECEIVED_DATE",
}

# company values that are really header/junk rows, normalized.
_JUNK_COMPANIES = {"jobsitename", "nan", "none", "total"}

_HREF_RE = re.compile(r"^/sites/default/files/oei/docs/warn-act-listings-")
_YEAR_RE = re.compile(r"(\d{4})")


def _norm(text) -> str:
    """Lowercase and strip non-alphanumerics for forgiving header matching."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _is_challenge(resp) -> bool:
    """True when the response is the AWS WAF bot-challenge page."""
    if resp.status_code in (202, 403):
        return True
    body = resp.content[:8192]
    return b"awsWafCookieDomainList" in body or b"gokuProps" in body


class TexasTWC(Source):
    code = "tx"
    name = "Texas"
    agency = "Texas Workforce Commission"
    source_url = LISTING_URL
    cadence = "twice-daily"
    # AWS WAF bot wall on all of twc.texas.gov — see module docstring.
    enabled = False

    # -- fetch ---------------------------------------------------------------

    def _request(self, session, url, tries=3):
        """Polite GET with retries; returns a Response or None."""
        resp = None
        for attempt in range(tries):
            if attempt:
                time.sleep(2 * attempt)  # backoff between retries
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                log.warning(f"[TX] request error for {url}: {e}")
                continue
            if resp.status_code == 404:
                return resp  # year file simply absent — no point retrying
            if resp.status_code == 200 and not _is_challenge(resp):
                return resp
            log.warning(
                f"[TX] blocked response ({resp.status_code}) for {url}"
            )
        return resp

    def _discover_year_urls(self, session) -> list:
        """Scrape XLSX links off the listing page; fall back to the known
        per-year URL pattern when the page is unreachable/blocked."""
        resp = self._request(session, LISTING_URL)
        urls = []
        if resp is not None and resp.status_code == 200 and not _is_challenge(
            resp
        ):
            soup = BeautifulSoup(resp.text, "lxml")
            for link in soup.find_all("a", href=_HREF_RE):
                href = link.get("href", "")
                years = _YEAR_RE.findall(href)
                if years and int(years[-1]) >= FIRST_YEAR:
                    urls.append(f"https://www.twc.texas.gov{href}")
        if not urls:
            log.warning(
                "[TX] listing page unusable — probing per-year URL pattern"
            )
            urls = [
                YEAR_URL.format(year=y)
                for y in range(FIRST_YEAR, date.today().year + 1)
            ]
        return urls

    def fetch(self, force: bool = False) -> tuple:
        """Download every per-year XLSX, consolidate to one CSV at
        ``self.paths.raw``. Raises RuntimeError while the bot wall is up."""
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        frames = []
        for url in self._discover_year_urls(session):
            time.sleep(1)  # politeness: max 1 request/second/host
            resp = self._request(session, url)
            if resp is None or resp.status_code != 200 or _is_challenge(resp):
                continue
            if not resp.content.startswith(b"PK"):  # not a real XLSX
                log.warning(f"[TX] non-XLSX payload for {url} — skipped")
                continue
            frame = pd.read_excel(io.BytesIO(resp.content), dtype=object)
            frame = frame.rename(
                columns={c: _CANONICAL.get(_norm(c), c) for c in frame.columns}
            )
            frames.append(frame)

        if not frames:
            raise RuntimeError(
                "TWC feed unreachable: every request returned the AWS WAF "
                "bot challenge (HTTP 202) or an error — see warn_sources/"
                "tx.py docstring"
            )

        df = pd.concat(frames, ignore_index=True)
        self.paths.ensure()
        df.to_csv(self.paths.raw, index=False)
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV (or a single TWC XLSX) -> unified-schema rows."""
        path = Path(raw_path)
        if path.suffix.lower() in (".xlsx", ".xls"):
            raw = pd.read_excel(path, dtype=object)
        else:
            raw = pd.read_csv(path, dtype=str)

        cols = {}
        for c in raw.columns:
            field = _FIELD_MAP.get(_norm(c))
            if field and field not in cols:
                cols[field] = c
        if "company" not in cols:
            raise ValueError(
                f"TX feed: JOB_SITE_NAME column not found in {list(raw.columns)}"
            )

        rows = []
        for _, r in raw.iterrows():
            val = r.get(cols["company"])
            company = "" if pd.isna(val) else str(val).strip()
            if not company or _norm(company) in _JUNK_COMPANIES:
                continue  # blank / repeated-header / junk row
            rec = {"company": company}
            for field in ("notice_date", "effective_date"):
                iso = (
                    warn_monitor._safe_date(r.get(cols[field]))
                    if field in cols
                    else None
                )
                rec[field] = DATE_CORRECTIONS.get(iso, iso)
            emp = (
                warn_monitor._safe_int(r.get(cols["employees"]))
                if "employees" in cols
                else None
            )
            rec["employees"] = emp if emp is not None else 0
            for field in ("county", "city"):
                if field in cols:
                    val = r.get(cols[field])
                    rec[field] = "" if pd.isna(val) else str(val).strip()
            rows.append(rec)

        df = pd.DataFrame(rows)
        order = [
            c
            for c in (
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "county",
                "city",
            )
            if c in df.columns
        ]
        return df[order] if order else df
