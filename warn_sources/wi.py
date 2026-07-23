"""
warn_sources.wi
---------------
Wisconsin — Department of Workforce Development layoff (WARN) notices.

The DWD layoff-notice pages (``default.htm?year=<YYYY>``) are a JavaScript
shell: the browser fetches a Google Sheets API feed (spreadsheet
``1cyZiHZ…``, sheet ``Originals``) and renders it client-side. The sheet
holds every original notice from 2020 to the present with named columns
(``PK, FK, PDF, Company, City, AffectedWorkers, NoticeRcvd, NoticeType,
LayoffBeginDate, NAICSDescription, County, WDA, HasUpdates``), so one API
call is the whole backfill. Pre-2020 notices are no longer online — the site
says they "may be requested" from DWD by email, and every old per-year URL
now serves the same empty JS shell.

The Sheets API key is a public *browser* key embedded in the site's own
``Keys.js`` (``dwdAPIKey``) and is referrer-locked to ``dwd.wisconsin.gov``,
so requests send the site's own ``Referer`` — exactly what the page itself
does. The key is re-read from ``Keys.js`` on every fetch (it has rotated
before; Big Local News' vendored copy is already stale) with the last-known
value as fallback.

Field crosswalk (per the BLN warn-transformer, followed exactly):

* company        <- ``Company``   ("- Revision" suffix cut, per BLN; some
  cells carry an embedded HTML footnote — text is taken up to the first tag)
* city           <- ``City``
* notice_date    <- ``NoticeRcvd``       (stored as ``%Y%m%d`` in the sheet)
* effective_date <- ``LayoffBeginDate``  (``%m/%d/%Y`` or ``%m/%d/%y``)
* employees      <- ``AffectedWorkers``  ("Unknown"/"TBD" -> no count -> 0)

WI additionally publishes county, NAICS industry description and a notice
type, which the unified schema keeps: county <- ``County``, industry <-
``NAICSDescription``, layoff_type <- ``NoticeType`` decoded via the page's
own legend (CL = Facility Closure, WR = Workforce Reduction, …).

Fetch/parse logic and the corrections tables are ported from Big Local
News' Apache-2.0 warn-scraper (warn/scrapers/wi.py) and warn-transformer
(warn_transformer/transformers/wi.py) — vendored, not imported.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://dwd.wisconsin.gov/dislocatedworker/warn/"
KEYS_JS_URL = "https://dwd.wisconsin.gov/include/internet/det/js/modules/Keys.js"
SHEET_ID = "1cyZiHZcepBI7ShB3dMcRprUFRG24lbwEnEDRBMhAqsA"
SHEET_URL = (
    "https://sheets.googleapis.com/v4/spreadsheets/"
    f"{SHEET_ID}/values/Originals?key={{key}}"
)
# Last-known dwdAPIKey (public browser key, referrer-locked to the DWD site);
# refreshed from KEYS_JS_URL on every fetch in case it rotates again.
FALLBACK_API_KEY = "AIzaSyB__fZmuycL7IedOivEHYtBobCo-ehze4k"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# NoticeRcvd is %Y%m%d; LayoffBeginDate is %m/%d/%Y with stray %m/%d/%y rows
# (per the BLN transformer's date_format tuple).
DATE_FORMATS = ["%Y%m%d", "%m/%d/%Y", "%m/%d/%y"]

# BLN sanity guards: implausible dates/head-counts are unparseable, not data.
MAX_FUTURE_DAYS = 365
MINIMUM_YEAR = 1988
MAXIMUM_JOBS = 10000

# Manual corrections vendored from BLN warn-transformer transformers/wi.py.
DATE_CORRECTIONS = {
    "11/03": None,
    "Unknown": None,
}
JOBS_CORRECTIONS = {
    "Unknown": None,
    "TBD": None,
}

# The page's own legend ("Legend — Original Notice Types / Update Types").
NOTICE_TYPE_LEGEND = {
    "CL": "Facility Closure",
    "WR": "Workforce Reduction",
    "AW": "Change to Number of Affected Workers",
    "LS": "Change to Layoff Schedule",
    "OC": "Other Change",
    "RN": "Rescission of Notice",
}

# Junk/placeholder company values that mark a non-record row.
_JUNK_COMPANIES = {"company", "n/a", "none", "tbd"}

COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "industry",
]


def _clean_text(value) -> str:
    """Sheet cell -> plain text: cut embedded HTML, unescape, tidy spaces.

    A handful of Company cells carry a footnote as raw HTML after the name
    (``…LLC<br/></a><a><em>* The company …</em>``); the name is everything
    before the first tag.
    """
    if value is None:
        return ""
    text = str(value).split("<")[0]
    return " ".join(unescape(text).split())


def _transform_company(value) -> str:
    """Company cell -> clean name (BLN wi.py: cut '- Revision' suffixes)."""
    name = _clean_text(value).split("- Revision")[0]
    return name.rstrip("*").strip()


def _transform_date(value) -> Optional[str]:
    """WI date text -> ISO ``YYYY-MM-DD`` or ``None`` (BLN wi.py logic)."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    raw = _clean_text(value)
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    if not raw:
        return None
    # BLN _clean_text: drop trailing junk after a m/d/Y-shaped date.
    if re.match(r"^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", raw):
        raw = re.sub(r"(?<=[0-9]{4}).*", "", raw)
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        plausible = (
            dt.year >= MINIMUM_YEAR
            and dt <= datetime.today() + timedelta(days=MAX_FUTURE_DAYS)
        )
        if plausible:
            return dt.strftime("%Y-%m-%d")
    log.debug(f"[WI] unparseable date {value!r} — recording as None")
    return None


def _transform_jobs(value) -> int:
    """Affected-workers text -> int; 0 when the state publishes no count."""
    if value is None:
        return 0
    raw = _clean_text(value)
    if raw in JOBS_CORRECTIONS:
        corrected = JOBS_CORRECTIONS[raw]
        return int(corrected) if corrected is not None else 0
    n = warn_monitor._safe_int(raw)
    if n is None or n < 0 or n > MAXIMUM_JOBS:
        if raw:
            log.debug(f"[WI] unparseable worker count {value!r} — recording 0")
        return 0
    return n


def _transform_notice_type(value) -> str:
    """Decode notice-type codes via the page's legend ('CL, WR' -> both)."""
    raw = _clean_text(value)
    if not raw or raw.lower() == "unknown":
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(NOTICE_TYPE_LEGEND.get(p, p) for p in parts)


def _polite_get(session, url, headers=None, attempts=3, timeout=90):
    """GET with 1 req/s politeness and up to ``attempts`` tries."""
    last_err = None
    for attempt in range(1, attempts + 1):
        time.sleep(1)  # max 1 request/second/host
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            log.warning(f"[WI] request failed (attempt {attempt}/{attempts}): {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"WI DWD fetch failed after {attempts} attempts: {last_err}")


class WisconsinDWD(Source):
    code = "wi"
    name = "Wisconsin"
    agency = "Wisconsin Department of Workforce Development"
    source_url = PAGE_URL
    cadence = "daily"  # the DWD page says "updated daily at 4:30 pm"

    def _api_key(self, session) -> str:
        """Read the current public dwdAPIKey from the site's own Keys.js."""
        try:
            resp = _polite_get(session, KEYS_JS_URL, attempts=2, timeout=60)
            m = re.search(r'dwdAPIKey\s*=\s*"([^"]+)"', resp.text)
            if m:
                return m.group(1)
            log.warning("[WI] dwdAPIKey not found in Keys.js; using fallback")
        except RuntimeError as e:
            log.warning(f"[WI] Keys.js unavailable ({e}); using fallback key")
        return FALLBACK_API_KEY

    def fetch(self, force: bool = False) -> tuple:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        key = self._api_key(session)
        # The key is referrer-locked to dwd.wisconsin.gov, so send the site's
        # own Referer — the same request the DWD page makes from the browser.
        resp = _polite_get(
            session,
            SHEET_URL.format(key=key),
            headers={"Referer": PAGE_URL},
        )
        values = resp.json().get("values") or []
        if len(values) < 2:
            raise RuntimeError("WI DWD sheet returned no WARN records")

        self.paths.ensure()
        self.paths.raw.write_text(
            json.dumps({"values": values}, indent=2, sort_keys=True)
        )

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": pd.Timestamp.utcnow().isoformat(),
                "url": PAGE_URL,
                "records_seen": len(values) - 1,
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        data = json.loads(Path(raw_path).read_text())
        values = data.get("values") or []
        if not values:
            return pd.DataFrame(columns=COLUMNS)
        header = [str(h).strip() for h in values[0]]
        rows = []
        for cells in values[1:]:
            rec = dict(zip(header, cells))
            company = _transform_company(rec.get("Company"))
            if not company or company.lower() in _JUNK_COMPANIES:
                continue
            if str(rec.get("AffectedWorkers", "")).strip() == "AffectedWorkers":
                continue  # stray header row (BLN wi.py guard)
            rows.append(
                {
                    "company": company,
                    "notice_date": _transform_date(rec.get("NoticeRcvd")),
                    "effective_date": _transform_date(
                        rec.get("LayoffBeginDate")
                    ),
                    "employees": _transform_jobs(rec.get("AffectedWorkers")),
                    "layoff_type": _transform_notice_type(
                        rec.get("NoticeType")
                    ),
                    "county": _clean_text(rec.get("County")),
                    "city": _clean_text(rec.get("City")),
                    "industry": _clean_text(rec.get("NAICSDescription")),
                }
            )
        return pd.DataFrame(rows, columns=COLUMNS)
